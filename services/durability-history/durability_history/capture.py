"""The capture pass: scope -> identity -> five upstreams -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

Six things here are correctness rather than style.

--------------------------------------------------------------------------
1. Narrowing happens BEFORE the first upstream request
--------------------------------------------------------------------------

`fetch_scope_or_fail` resolves both narrowing seams — the membership list out of
the lake and the `player-identity` client the forward join runs through — before
a single byte of any feed is requested. That ordering *is* failing closed. A
pass that cannot narrow costs zero upstream calls rather than 43.8 MB fetched to
publish nothing, and there is deliberately no unnarrowed fallback: one would
spend the whole budget precisely during the incident that took `roster-scope`
out.

The three per-season feeds are fetched *after* identity resolution as well,
which is the second half of the same idea: each is filtered to the ~380 resolved
scope ids as it streams, so the 8.28 MB weekly-stats file never materialises the
~4,600 players this collector does not want.

--------------------------------------------------------------------------
2. `coverage.expected` is the SCOPE SLOT, never the upstream row
--------------------------------------------------------------------------

Coverage is keyed by scope member, expected up front for every individual player
the watchlist names, and failed for the ones no resolved upstream row covered.
Keying it by upstream row instead would make an out-of-scope player read as a
permanent coverage regression and — far worse — would make a pass that resolved
four rows report `expected: 4, present: 4`, ratio 1.0.

`EXPECTED_FLOOR` encodes the size the universe is KNOWN to have, independently of
any fetch and independently of the scope's own size: `Coverage.ratio` returns 1.0
when `expected` is 0, so a truncated scope naming two players would otherwise
read as a perfect pass.

--------------------------------------------------------------------------
3. A CLEAN HISTORY IS DATA, and getting this backwards is catastrophic
--------------------------------------------------------------------------

The spec's coverage rule, verbatim: "every scoped player with at least one
completed NFL season has a durability record; a record with zero injury events
and `sample_size_events = 0` is present and complete, not missing."

So a player who has never been hurt is `record`ed **present**. Failing him
instead would mark every healthy player in the league as a coverage hole, peg
`collector_coverage_ratio` near zero forever, and make the one gauge that can see
a real narrowing failure permanently uninformative.

What IS a hole, per signal type:

* `player_durability_profile` — a slot whose tenure could not be enumerated at
  all (`career_games_possible == 0`). Without it there is no denominator, so
  `availability_rate` is null and the record answers nothing.
* `player_injury_history` and `player_return_trajectory` — a slot no resolved
  upstream row covered. Zero events is a complete answer for both.

--------------------------------------------------------------------------
4. The named failure mode is guarded, counted, and published
--------------------------------------------------------------------------

Games missed for a suspension, a personal matter, a healthy scratch or a
late-season rest week must never reach `career_games_missed_injury`.
`events.classify_absence` is the guard — the reason is read off the designation,
and a game with no designation is `undesignated`, never injury — and this module
is the half that makes it visible: `games_missed_by_reason` is published on every
profile row, `durability_history_undesignated_absences` is recorded on every pass
including zero, and the spec's assertion (injury-attributed misses never exceed
designated games) is checked per player, with a violation raised as a priority
error rather than trusted silently.

--------------------------------------------------------------------------
5. A `seasonal` cadence must not append an identical snapshot daily
--------------------------------------------------------------------------

Per signal type, this pass digests everything it would publish and compares it to
what this process last published AND SAW LAND. Identical raises
`UpstreamUnchanged`, which `run_capture_loop` already handles by advancing
`last_capture_at` without touching the stored envelopes. `venue` established the
pattern; the "saw land" half is what stops a digest recorded for content the lake
never received suppressing the retry until the data itself changes — which on
this cadence can be a whole season.

--------------------------------------------------------------------------
6. The library owns `collector_capture_failures_total` for a fatal pass
--------------------------------------------------------------------------

`fail_capture` and `publish_capture` both record it. This module records it
itself in exactly one place — `_degraded`, the non-fatal sweeps, where neither
runs — which is the case `docs/collectors.md` names as the collector's own.
"""

import hashlib
import json
import logging
from datetime import UTC, date, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.conditional import UpstreamUnchanged
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture
from collector_core.scope import fetch_scope_or_fail

from . import derive, events
from .adapters.scope import (
    IDENTITY_UPSTREAM_ERROR,
    IdentityFailures,
    build_identity_client,
    fetch_scope,
    individual_players,
    resolve_in_scope,
)
from .adapters.upstream import (
    RECURRENCE_WINDOW_DAYS,
    UPSTREAM_ADAPTER,
    Designations,
    Participation,
    Production,
    fetch_designations,
    fetch_participation,
    fetch_players,
    fetch_production,
    fetch_schedule,
    history_seasons,
    source_ref,
)
from .metrics import metrics

logger = logging.getLogger(__name__)

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "DURABILITY_PROFILE",
    "EXPECTED_FLOOR",
    "INJURY_HISTORY",
    "RETURN_TRAJECTORY",
    "SIGNAL_TYPES",
    "build_injury_history_row",
    "build_profile_row",
    "build_trajectory_row",
    "capture_durability_history",
    "player_key",
    "reset_published_digests",
]

COLLECTOR_NAME = "durability-history"
CADENCE_CLASS = CadenceClass.SEASONAL

DURABILITY_PROFILE = "player_durability_profile"
INJURY_HISTORY = "player_injury_history"
RETURN_TRAJECTORY = "player_return_trajectory"
SIGNAL_TYPES = (DURABILITY_PROFILE, INJURY_HISTORY, RETURN_TRAJECTORY)

# ── the declared universe ────────────────────────────────────────────────────
#
# `roster-scope`'s config is 32 teams x (QB 2 + RB 3 + WR 4 + TE 2 + K 1 + DST 1)
# = 416 slots. Twelve of those thirteen per-team slots are individual players;
# the thirteenth is a team defense, which has no hamstring to strain and is
# excluded by `individual_players`. So:
#
#     32 * 12 = 384
#
# A fact about the league's config, decided before any fetch and independent of
# how many members the scope actually resolved. An expectation taken from the
# scope would make a truncated scope read as a perfect pass and a scope that
# failed to resolve read as a bye.
LEAGUE_TEAMS = 32
INDIVIDUAL_SCOPE_SLOTS_PER_TEAM = 12
EXPECTED_FLOOR: dict[str, int] = dict.fromkeys(
    SIGNAL_TYPES, LEAGUE_TEAMS * INDIVIDUAL_SCOPE_SLOTS_PER_TEAM
)

# Coverage failure reasons. Named constants rather than literals, because each
# implies a different operator action and a test asserting on a typo'd string
# passes just as happily as one asserting on the real thing.
REASON_NO_UPSTREAM_ROW = "player_not_in_upstream"
REASON_NO_TENURE = "tenure_not_enumerable"
REASON_DEADLINE = "deadline_exceeded"
REASON_SEASONS_MISSING = "history_seasons_missing"
REASON_PRODUCTION_UNAVAILABLE = "production_feed_unavailable"
REASON_PARTICIPATION_UNAVAILABLE = "participation_feed_unavailable"
REASON_NO_DESIGNATION_SEASON = "no_designation_season_readable"
REASON_ATTRIBUTION_VIOLATION = "injury_attribution_exceeds_designations"

UNDESIGNATED = "undesignated"

# `(season, week, signal_type) -> the digest THIS process last published AND saw
# land in the lake`. In memory rather than read back from the lake: a restart
# that found a matching digest would raise `UpstreamUnchanged` against an EMPTY
# `CaptureState` and serve nothing until the data next changed.
_PUBLISHED_DIGESTS: dict[tuple[int, int, str], str] = {}


def reset_published_digests() -> None:
    """Forget what this process has published. For tests only."""
    _PUBLISHED_DIGESTS.clear()


def player_key(player_id: str) -> str:
    """The coverage key for one scope slot.

    Prefixed so it can never be mistaken for a bare id, and canonical rather than
    the upstream's GSIS key: `coverage.missing` and `signals[].player_id` are the
    two halves of one answer to "what was owed and what arrived", and a consumer
    can only join them if they name players in the same namespace.
    """
    return f"player:{player_id}"


def _digest(payload: object) -> str:
    """A stable sha256 over anything JSON-serialisable."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_of_date(now: datetime) -> date:
    """The date ages are taken as of, and the scope's `as_of_date`.

    Read off the capture's own `now` rather than `date.today()`, so a test that
    freezes the clock freezes this too and a capture stays reproducible from its
    own envelope.
    """
    return now.astimezone(UTC).date()


# ── the three signal rows ────────────────────────────────────────────────────


def build_profile_row(
    history: events.PlayerHistory,
    *,
    age: float | None,
    population: list[tuple[str, float, float]],
) -> dict:
    """One reconstructed history as a `player_durability_profile` signal row.

    `games_missed_by_reason` is the spec's guard, published rather than left in a
    log line: a consumer reading a lake object a month later can see that the
    eleven games this player missed were three injuries and eight suspension
    games. `designated_games` is the right-hand side of the spec's assertion, so
    the assertion is checkable from the row alone.

    `observation_window_first_season` and `career_history_complete` are the honesty
    fields on every `career_*` number here. The window is bounded for cost (see
    `adapters/upstream.py`), and a truncated total labelled complete is a
    well-formed number that is silently wrong — the same call
    `player-profile`'s `career_snaps_complete` makes.
    """
    rate = derive.availability_rate(history.games_possible, history.games_missed_injury)
    return {
        "player_id": history.player_id,
        "position": history.position,
        "career_games_possible": history.games_possible,
        "career_games_missed_injury": history.games_missed_injury,
        "availability_rate": rate,
        "age_adjusted_availability_rate": derive.age_adjusted_availability_rate(
            rate, position=history.position, age=age, population=population
        ),
        # The three fields that make the null above readable. Without them a
        # consumer cannot tell "the cohort was too small" from "this player has
        # no birth date" from a bug — `min_sample_events` on the trajectory row
        # already sets the precedent for publishing a floor beside what it
        # suppresses.
        "age_years": age,
        "age_cohort_size": derive.age_cohort_size(
            position=history.position, age=age, population=population
        ),
        "min_age_cohort": derive.MIN_COHORT,
        "body_part_history": derive.body_part_history(history.events),
        "soft_tissue_recurrence_rate": derive.soft_tissue_recurrence_rate(
            history.events
        ),
        "median_days_to_return_by_body_part": (
            derive.median_days_to_return_by_body_part(history.events)
        ),
        "sample_size_events": derive.sample_size_events(history.events),
        # The guard, published.
        "games_missed_by_reason": history.missed_by_reason(),
        "designated_games": history.designated_games,
        # The window, published.
        "observation_window_first_season": history.observation_window_first_season,
        "career_history_complete": history.complete,
    }


def build_injury_history_row(history: events.PlayerHistory) -> dict:
    """One reconstructed history as a `player_injury_history` signal row.

    An empty `injury_events` list is a complete answer, not an absent one — see
    the module docstring, section 3.

    `injury_site` travels on every event beside the spec's coarse `body_part`,
    because it is the key the recurrence rule actually uses (`events.py` explains
    why `body_part` is too coarse to key on) and `is_recurrence_of` has to stay
    reproducible from the published row.
    """
    return {
        "player_id": history.player_id,
        "injury_events": [
            {
                "event_id": event.event_id,
                "body_part": event.body_part,
                "injury_site": event.injury_site,
                "tissue_class": event.tissue_class,
                "onset_date": event.onset_date.isoformat(),
                "games_missed": event.games_missed,
                "days_to_return": event.days_to_return,
                "is_recurrence_of": event.is_recurrence_of,
            }
            for event in history.events
        ],
        "absences": [
            {
                "season": absence.season,
                "week": absence.week,
                "game_id": absence.game_id,
                # Required, never null, and never inferred from the absence
                # itself. This is the spec's guard as a published field.
                "absence_reason": absence.absence_reason,
                "body_part": absence.body_part,
            }
            for absence in history.absences
        ],
        "sample_size_events": derive.sample_size_events(history.events),
        "recurrence_window_days": RECURRENCE_WINDOW_DAYS,
    }


def build_trajectory_row(
    history: events.PlayerHistory,
    *,
    gsis_id: str,
    points: dict[tuple[str, int, int], float],
) -> dict:
    """One reconstructed history as a `player_return_trajectory` signal row.

    Both derived series are null below `derive.MIN_SAMPLE_EVENTS`, per the spec:
    "aggregates with `sample_size_events` below the configured floor are emitted
    with the raw events but with the derived rates null." A four-week snap
    trajectory off one return is a chart of one player's one week.
    """
    return {
        "player_id": history.player_id,
        "post_return_snap_trajectory": derive.post_return_snap_trajectory(history),
        "post_return_production_delta": derive.post_return_production_delta(
            history, gsis_id=gsis_id, points=points
        ),
        "sample_size_events": derive.sample_size_events(history.events),
        "min_sample_events": derive.MIN_SAMPLE_EVENTS,
    }


def _new_accumulators() -> dict[str, CoverageAccumulator]:
    return {
        signal_type: CoverageAccumulator(floor=EXPECTED_FLOOR[signal_type])
        for signal_type in SIGNAL_TYPES
    }


async def _degraded(awaitable, *, fallback, reason: str):
    """Run a non-fatal sweep, absorbing its failure into `(fallback, detail)`.

    Records `collector_capture_failures_total` **here**, because the library
    cannot see this one: neither `fail_capture` nor a failed `publish_capture`
    write runs on this branch. `docs/collectors.md` names "a degraded path that
    builds its own envelopes" as exactly the case a collector counts for itself.
    """
    try:
        return await awaitable, None
    except Exception as exc:  # noqa: BLE001 — degraded, not fatal
        logger.warning("durability-history: %s: %s", reason, exc)
        metrics.capture_failure(exc, reason=reason)
        return fallback, f"{type(exc).__name__}: {exc}"[:200]


async def capture_durability_history(  # noqa: C901 — one linear pass, top to bottom
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one envelope per signal type."""
    as_of = _as_of_date(now)
    seasons = history_seasons(season)
    scope = {
        "season": season,
        "week": week,
        "as_of_date": as_of.isoformat(),
        # The window is part of what every `career_*` number MEANS, so it is in
        # the scope rather than only in `source_ref`: a lake object read a year
        # later must say what "career" covered when it was written.
        "history_seasons": list(seasons),
        "recurrence_window_days": RECURRENCE_WINDOW_DAYS,
    }
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=source_ref(season, week),
    )

    metrics.capture_attempt()
    # Seeded before anything can fail and overwritten with the real numbers
    # below. An absent Prometheus series and a healthy one are indistinguishable,
    # so these gauges must not simply stop on a failure path.
    metrics.rows_captured(0)
    metrics.undesignated_absences(0)
    metrics.attribution_violations(0)
    metrics.unresolved_scope_slots(0)

    failure_context = dict(
        collector=COLLECTOR_NAME,
        signal_types=SIGNAL_TYPES,
        adapter=UPSTREAM_ADAPTER,
        now=now,
        scope=scope,
        lake=lake,
        metrics=metrics,
        expected=EXPECTED_FLOOR,
        source_ref=source_ref(season, week),
    )

    async def _narrowing_seams():
        """Both seams narrowing needs, resolved together.

        `build_identity_client` is synchronous and raises `ScopeUnavailable` when
        `PLAYER_IDENTITY_URL` is empty, which is why `fetch_scope_or_fail` takes
        a callable rather than an awaitable.
        """
        return build_identity_client(client), await fetch_scope(lake, season, week)

    # BEFORE the first upstream request. That ordering IS failing closed — see
    # the module docstring. `fetch_scope_or_fail` owns both refusal arms.
    identity, membership = await fetch_scope_or_fail(
        _narrowing_seams, **failure_context
    )
    owed = individual_players(membership)

    try:
        players = await fetch_players(season, client=client)
        schedule = await fetch_schedule(seasons, client=client)
    except UpstreamUnchanged:
        # Forward cover only: the adapter converts a 304 into a memo read rather
        # than letting it escape. Re-raised ABOVE the generic handler regardless,
        # so a future change there can never route an unchanged upstream into
        # `fail_capture`, which would write `present: 0` over a healthy capture.
        raise
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Writes a `present: 0` envelope per signal type, then re-raises. Never
        # returns — and do NOT call `metrics.capture_failure(exc)` first: the
        # library owns that counter for a failure that ends a pass.
        await fail_capture(exc, **failure_context)

    identity_failures = IdentityFailures()
    resolved = await resolve_in_scope(
        players,
        season=season,
        scope_members=owed,
        identity=identity,
        failures=identity_failures,
    )

    # The three per-season feeds are filtered to exactly these ids AS THEY
    # STREAM — the second half of failing closed, and the reason the 8.28 MB
    # weekly-stats file costs a few hundred KB of memory rather than all of it.
    keep_gsis = frozenset(row.gsis_id for row, _ in resolved)
    keep_pfr = frozenset(row.pfr_id for row, _ in resolved if row.pfr_id)

    designations = Designations(by_player={})
    participation, participation_error = Participation(), None
    production, production_error = Production(), None

    # **Zero resolved rows means zero per-season fetches.** This is the same
    # narrowing decision as the scope check above, one layer down: with nothing
    # to filter FOR, the three sweeps would download ~34 MB and keep none of it.
    # A total `player-identity` outage is precisely when that happens, so
    # omitting this guard would make an identity incident cost the full budget
    # every pass — the cascade `fetch_scope_or_fail` exists to prevent, arriving
    # by a different route.
    if resolved:
        try:
            designations = await fetch_designations(
                seasons, client=client, keep_gsis=keep_gsis, deadline=deadline
            )
        except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
            await fail_capture(exc, **failure_context)

        if not designations.seasons_read:
            # Fatal, and this is the only feed that is: `absence_reason` has no
            # other source, so a pass with no readable designation season cannot
            # tell an injury from a suspension for anybody. Publishing "zero
            # injuries league-wide" would be the most confident possible version
            # of the named failure mode.
            await fail_capture(
                RuntimeError(
                    f"no injury designation season readable of {sorted(seasons)}"
                ),
                reason=REASON_NO_DESIGNATION_SEASON,
                **failure_context,
            )

        # Degraded rather than fatal, deliberately: an absent snap feed costs the
        # trajectory and the participation half of tenure, and an absent
        # production feed costs one field. Neither is a reason to discard a
        # perfectly good injury reconstruction.
        participation, participation_error = await _degraded(
            fetch_participation(
                seasons, client=client, keep_pfr=keep_pfr, deadline=deadline
            ),
            fallback=Participation(),
            reason=REASON_PARTICIPATION_UNAVAILABLE,
        )
        production, production_error = await _degraded(
            fetch_production(
                seasons, client=client, keep_gsis=keep_gsis, deadline=deadline
            ),
            fallback=Production(),
            reason=REASON_PRODUCTION_UNAVAILABLE,
        )

    seasons_missing = sorted(
        set(designations.seasons_missing)
        | set(participation.seasons_missing)
        | set(production.seasons_missing)
    )

    accumulators = _new_accumulators()
    rows: dict[str, list[dict]] = {signal_type: [] for signal_type in SIGNAL_TYPES}

    # Every individual scope member is owed a durability record — declared up
    # front, because the watchlist naming them is what makes them owed. Never
    # because a fetch below happened to return them.
    for player_id in sorted(owed):
        for acc in accumulators.values():
            acc.expect(player_key(player_id))

    # Pass one: reconstruct. The availability population cannot be built
    # row-at-a-time, because an age-adjusted rate is a population statistic.
    histories: list[tuple[str, events.PlayerHistory, float | None]] = []
    covered: set[str] = set()
    undesignated = 0
    violations: list[str] = []
    truncated = 0

    for row, player_id in resolved:
        covered.add(player_id)
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing away
            # what already resolved: a truncated pass that reports itself
            # truncated is useful; one that reports itself complete is not.
            truncated += 1
            for acc in accumulators.values():
                acc.fail(player_key(player_id), REASON_DEADLINE)
            continue

        history = events.reconstruct(
            row,
            player_id,
            seasons=seasons,
            schedule=schedule,
            designations=designations.by_player.get(row.gsis_id, []),
            participation=participation,
            complete=(
                not seasons_missing
                and row.rookie_season is not None
                and row.rookie_season >= min(seasons)
            ),
        )
        undesignated += sum(
            1 for absence in history.absences if absence.absence_reason == UNDESIGNATED
        )
        if not derive.injury_absence_invariant_holds(history):
            # By construction this cannot happen — see
            # `derive.injury_absence_invariant_holds`. It is checked anyway
            # because it is the property that breaks first if anybody ever makes
            # absence itself imply an injury, and a silent break here is a
            # league-wide fabricated durability problem.
            violations.append(player_id)
        histories.append(
            (row.gsis_id, history, derive.age_years(row.birth_date, as_of))
        )

    population: list[tuple[str, float, float]] = []
    for _gsis, history, age in histories:
        rate = derive.availability_rate(
            history.games_possible, history.games_missed_injury
        )
        if age is not None and rate is not None:
            population.append((history.position, age, rate))

    # Pass two: build the rows, and score coverage per signal type.
    for gsis_id, history, age in histories:
        key = player_key(history.player_id)

        rows[DURABILITY_PROFILE].append(
            build_profile_row(history, age=age, population=population)
        )
        rows[INJURY_HISTORY].append(build_injury_history_row(history))
        rows[RETURN_TRAJECTORY].append(
            build_trajectory_row(history, gsis_id=gsis_id, points=production.points)
        )

        # The spec's coverage rule, applied per signal type — see the module
        # docstring, section 3. A record with zero injury events is PRESENT for
        # all three; only an unenumerable tenure is a hole, and only for the
        # profile, whose whole product is a rate over that denominator.
        if history.games_possible > 0:
            accumulators[DURABILITY_PROFILE].record(key)
        else:
            accumulators[DURABILITY_PROFILE].fail(key, REASON_NO_TENURE)
        accumulators[INJURY_HISTORY].record(key)
        accumulators[RETURN_TRAJECTORY].record(key)

    # A scope slot no resolved upstream row covered. This is where narrowing
    # becomes visible: an unresolved or absent player is a HOLE against the
    # watchlist, not a row that quietly disappeared from both numerator and
    # denominator.
    for player_id in sorted(owed - covered):
        for acc in accumulators.values():
            acc.fail(player_key(player_id), REASON_NO_UPSTREAM_ROW)

    if identity_failures.rows:
        # One summarised entry, never one per row: a total `player-identity`
        # outage against a ~1,400-row feed would otherwise fill the 50-entry cap
        # by itself and push every other reason off the list.
        for acc in accumulators.values():
            acc.add_error(IDENTITY_UPSTREAM_ERROR, identity_failures.detail())

    if seasons_missing:
        detail = f"season(s) {', '.join(str(s) for s in seasons_missing)}"
        for acc in accumulators.values():
            acc.add_error(REASON_SEASONS_MISSING, detail)
    if participation_error is not None:
        accumulators[RETURN_TRAJECTORY].add_priority_error(
            REASON_PARTICIPATION_UNAVAILABLE, participation_error
        )
    if production_error is not None:
        accumulators[RETURN_TRAJECTORY].add_priority_error(
            REASON_PRODUCTION_UNAVAILABLE, production_error
        )
    if truncated:
        for acc in accumulators.values():
            acc.add_priority_error(
                REASON_DEADLINE, f"{truncated} scope slot(s) past the deadline"
            )
    if violations:
        # `add_priority_error`, not `add_error`: an entry saying this collector
        # attributed more missed games to injury than there were designations to
        # source them from must survive the cap rather than queueing behind a few
        # hundred routine per-slot failures.
        detail = (
            f"{len(violations)} player(s) with injury-attributed misses exceeding "
            f"designated games: {', '.join(sorted(violations)[:5])}"
        )
        for acc in accumulators.values():
            acc.add_priority_error(REASON_ATTRIBUTION_VIOLATION, detail)

    metrics.rows_captured(len(rows[DURABILITY_PROFILE]))
    metrics.undesignated_absences(undesignated)
    metrics.attribution_violations(len(violations))
    metrics.unresolved_scope_slots(len(owed - covered))

    envelopes = {
        signal_type: Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=signal_type,
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=accumulators[signal_type].result(),
            errors=accumulators[signal_type].errors,
            signals=rows[signal_type],
        )
        for signal_type in SIGNAL_TYPES
    }

    # Per signal type, not per pass — see the module docstring, section 5. The
    # three move at different speeds: an injury history changes the moment a club
    # files a report, while a trajectory built off three-season aggregates can go
    # weeks without moving.
    digests = {signal_type: _digest(rows[signal_type]) for signal_type in SIGNAL_TYPES}
    changed = {
        signal_type: envelope
        for signal_type, envelope in envelopes.items()
        if _PUBLISHED_DIGESTS.get((season, week, signal_type)) != digests[signal_type]
    }
    if not changed:
        raise UpstreamUnchanged(
            source_ref(season, week), source_ref=digests[INJURY_HISTORY]
        )

    # The shared tail: writes each envelope off the event loop, records its
    # coverage gauge, records `collector_capture_failures_total` if a write fails
    # — then returns the envelopes ANYWAY, because the capture succeeded and only
    # its archival copy did not.
    published = await publish_capture(changed, lake=lake, metrics=metrics)

    # Gated on the write LANDING. A digest recorded for content the lake never
    # received permanently suppresses the retry: the next pass digests the same
    # content, matches, raises `UpstreamUnchanged`, and the object is never
    # written again until the data itself changes — a whole season, on a
    # `seasonal` cadence. See `collector_core.publish.PublishResult`.
    for signal_type in published:
        if published.landed(signal_type):
            _PUBLISHED_DIGESTS[(season, week, signal_type)] = digests[signal_type]

    # An unchanged envelope is not written, but its coverage gauge is still
    # recorded — `publish_capture` only records the ones it was handed. A gauge
    # that went quiet whenever the data was stable would read exactly like a
    # collector that had stopped.
    for signal_type, envelope in envelopes.items():
        if signal_type not in changed:
            metrics.coverage(signal_type, envelope.coverage.ratio)

    return envelopes
