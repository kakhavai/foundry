"""The capture pass: scope -> identity -> three upstreams -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

Five things here are correctness rather than style.

--------------------------------------------------------------------------
1. Narrowing happens BEFORE the first upstream request
--------------------------------------------------------------------------

`fetch_scope_or_fail` resolves both narrowing seams — the membership list out
of the lake and the `player-identity` client the forward join runs through —
before `players.csv` is touched. That ordering *is* failing closed. A pass that
cannot narrow costs zero upstream calls rather than 7.3 MB plus a career snap
sweep fetched to publish nothing, and there is deliberately no unnarrowed
fallback: one would spend the whole budget precisely during the incident that
took `roster-scope` out.

--------------------------------------------------------------------------
2. `coverage.expected` is the SCOPE SLOT, never the upstream row
--------------------------------------------------------------------------

Coverage is keyed by scope member, expected up front for every individual
player the watchlist names, and failed for the ones no resolved upstream row
covered. Keying it by upstream row instead would make an out-of-scope player
read as a permanent coverage regression and — far worse — would make a pass
that resolved four rows report `expected: 4, present: 4`, ratio 1.0.

`EXPECTED_FLOOR` encodes the size the universe is KNOWN to have, independently
of any fetch and independently of the scope's own size: `Coverage.ratio` returns
1.0 when `expected` is 0, so a truncated scope naming two players would
otherwise read as a perfect pass.

--------------------------------------------------------------------------
3. What `present` means differs per signal type, and the spec says so
--------------------------------------------------------------------------

> every player in the current `roster-scope` watchlist has a profile record with
> a non-null `player_id`, `position`, and `experience_seasons`; **athletic
> measurements are explicitly optional and their absence does not count as
> missing.**

So `player_biographical` records a slot only when all three of those are
non-null, while `player_athleticism` and `player_career_load` record a slot as
soon as a row is emitted for it — an all-null `athleticism` object is a
complete answer ("we looked; this player never tested"), not a hole. Scoring
athleticism the other way would peg the ratio at ~0.56 forever, since only 749
of 1,326 recent players have a combine row at all, and a permanently-red gauge
is a gauge nobody reads.

--------------------------------------------------------------------------
4. A `static reference` cadence must not append an identical snapshot daily
--------------------------------------------------------------------------

Per signal type, this pass digests everything it would publish and compares it
to what this process last published AND saw land. Identical raises
`UpstreamUnchanged`, which `run_capture_loop` already handles by advancing
`last_capture_at` without touching the stored envelopes. `venue` established
the pattern and its docstring carries the full reasoning.

Per signal type rather than per pass, because the four move at wildly different
speeds: a draft class changes once a year and `player_biographical` changes
**every day**, since `age_years` is taken as of `scope.as_of_date`. That is not
the gate failing — the data really did change — and the gate still saves the
three signal types that did not.

--------------------------------------------------------------------------
5. The library owns `collector_capture_failures_total` for a fatal pass
--------------------------------------------------------------------------

`fail_capture` and `publish_capture` both record it. This module records it
itself in exactly one place — the degraded snap-counts path, where neither
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

from . import derive, staleness
from .adapters.scope import (
    IDENTITY_UPSTREAM_ERROR,
    IdentityFailures,
    build_identity_client,
    fetch_scope,
    individual_players,
    resolve_in_scope,
)
from .adapters.upstream import (
    UPSTREAM_ADAPTER,
    CareerSnaps,
    ProfileRow,
    fetch_career_snaps,
    fetch_combine,
    fetch_profiles,
    source_ref,
)
from .metrics import metrics

logger = logging.getLogger(__name__)

__all__ = [
    "ATHLETICISM",
    "BIOGRAPHICAL",
    "CADENCE_CLASS",
    "CAREER_LOAD",
    "COLLECTOR_NAME",
    "DRAFT_CAPITAL",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "build_athleticism_row",
    "build_biographical_row",
    "build_career_load_row",
    "build_draft_capital_row",
    "capture_player_profile",
    "player_key",
    "reset_published_digests",
]

COLLECTOR_NAME = "player-profile"
CADENCE_CLASS = CadenceClass.STATIC_REFERENCE

BIOGRAPHICAL = "player_biographical"
DRAFT_CAPITAL = "player_draft_capital"
ATHLETICISM = "player_athleticism"
CAREER_LOAD = "player_career_load"
SIGNAL_TYPES = (BIOGRAPHICAL, DRAFT_CAPITAL, ATHLETICISM, CAREER_LOAD)

# ── the declared universe ────────────────────────────────────────────────────
#
# `roster-scope`'s config is 32 teams x (QB 2 + RB 3 + WR 4 + TE 2 + K 1 + DST 1)
# = 416 slots. Twelve of those thirteen per-team slots are individual players;
# the thirteenth is a team defense, which has no birth date, no draft pick and
# no combine result, and is excluded by `individual_players`. So:
#
#     32 * 12 = 384
#
# A fact about the league's config, decided before any fetch and independent of
# how many members the scope actually resolved. It is the only number that
# survives narrowing, which is exactly why it is a constant: an expectation
# taken from the scope would make a truncated scope read as a perfect pass and a
# scope that failed to resolve read as a bye.
LEAGUE_TEAMS = 32
INDIVIDUAL_SCOPE_SLOTS_PER_TEAM = 12
EXPECTED_FLOOR: dict[str, int] = dict.fromkeys(
    SIGNAL_TYPES, LEAGUE_TEAMS * INDIVIDUAL_SCOPE_SLOTS_PER_TEAM
)

# Coverage failure reasons. Named constants rather than literals, because each
# implies a different operator action and a test asserting on a typo'd string
# passes just as happily as one asserting on the real thing.
REASON_NO_PROFILE = "profile_not_in_upstream"
REASON_INCOMPLETE_PROFILE = "profile_incomplete"
REASON_DEADLINE = "deadline_exceeded"
REASON_STALE_POSITION = "position_not_reconfirmed"
# A birth date after the pass's own `as_of_date`. `derive.age_years` refuses to
# turn it into a number; this is the half that makes the refusal visible, since
# a null `age_years` is otherwise indistinguishable from "no adapter supplied a
# birth date" — which the spec explicitly permits.
REASON_BIRTH_DATE_IN_FUTURE = "birth_date_in_future"
REASON_SNAP_SEASONS_MISSING = "career_snap_seasons_missing"
REASON_HISTORY_UNAVAILABLE = "prior_history_unavailable"

# `(season, week, signal_type) -> the digest THIS process last published AND saw
# land in the lake`. In memory rather than read back from the lake, for the
# reason `venue.capture` sets out at length: a restart that found a matching
# digest would raise `UpstreamUnchanged` against an EMPTY `CaptureState` and
# serve nothing until the data next changed.
_PUBLISHED_DIGESTS: dict[tuple[int, int, str], str] = {}


def reset_published_digests() -> None:
    """Forget what this process has published. For tests only."""
    _PUBLISHED_DIGESTS.clear()


def player_key(player_id: str) -> str:
    """The coverage key for one scope slot.

    Prefixed so it can never be mistaken for a bare id, and canonical rather
    than the upstream's GSIS key: `coverage.missing` and `signals[].player_id`
    are the two halves of one answer to "what was owed and what arrived", and a
    consumer can only join them if they name players in the same namespace.
    """
    return f"player:{player_id}"


def _digest(payload: object) -> str:
    """A stable sha256 over anything JSON-serialisable."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_of_date(now: datetime) -> date:
    """The date `age_years` is taken as of, and the scope's `as_of_date`.

    Read off the capture's own `now` rather than `date.today()`, so a test that
    freezes the clock freezes this too and a capture stays reproducible from its
    own envelope.
    """
    return now.astimezone(UTC).date()


def build_biographical_row(
    row: ProfileRow,
    *,
    player_id: str,
    age: float | None,
    age_percentile: float | None,
    histories: dict[str, staleness.FieldHistory],
    stale_position: bool,
) -> dict:
    """One resolved profile as a `player_biographical` signal row.

    `player_id` is passed in rather than read off the row: the upstream GSIS key
    is the join's *input*, and letting this function reach for it is how a
    fallback to the un-canonical id gets reintroduced by accident.

    The four `*_last_changed_at` / `*_last_confirmed_at` fields are the spec's
    guard against silent staleness, and `staleness.py` owns what they mean.
    """
    return {
        "player_id": player_id,
        "position": row.position,
        "birth_date": None if row.birth_date is None else row.birth_date.isoformat(),
        "age_years": age,
        "position_age_percentile": age_percentile,
        "position_age_curve_stage": derive.position_age_curve_stage(row.position, age),
        "experience_seasons": row.experience_seasons,
        "height_inches": row.height_inches,
        "weight_lbs": row.weight_lbs,
        "college": row.college,
        "position_last_changed_at": histories["position"].last_changed_at,
        "position_last_confirmed_at": histories["position"].last_confirmed_at,
        "weight_lbs_last_changed_at": histories["weight_lbs"].last_changed_at,
        "weight_lbs_last_confirmed_at": histories["weight_lbs"].last_confirmed_at,
        # The assertion the spec asks for, published on the row rather than left
        # in a log line: a consumer reading a lake object a month later can see
        # which priors were already suspect when it was written.
        "position_stale": stale_position,
    }


def build_draft_capital_row(row: ProfileRow, *, player_id: str) -> dict:
    """One resolved profile as a `player_draft_capital` signal row.

    An undrafted player is a complete record with three nulls and a score of
    0.0, not an absent one. The spec states the 0.0 outright, and the nulls
    matter for the same reason a zero forty time does: draft year 0 or round 0
    would sort ahead of a first-round pick in any naive comparison.
    """
    return {
        "player_id": player_id,
        "draft_year": row.draft_year,
        "draft_round": row.draft_round,
        "draft_overall_pick": row.draft_overall_pick,
        "draft_capital_score": derive.draft_capital_score(row.draft_overall_pick),
        "is_undrafted": row.draft_overall_pick is None,
    }


def build_athleticism_row(row: ProfileRow, *, player_id: str, combine) -> dict:
    """One resolved profile as a `player_athleticism` signal row.

    Every member may be null and the object is always present. "We looked and
    this player never tested" and "we did not look" are different facts, and an
    absent key cannot distinguish them.

    **Never a zero.** The spec: "an untested player has null combine fields, not
    zeros, because a zero forty time is numerically catastrophic downstream."
    """
    measurements = {
        "forty_yard": None if combine is None else combine.forty_yard,
        "vertical_inches": None if combine is None else combine.vertical_inches,
        "broad_jump_inches": None if combine is None else combine.broad_jump_inches,
        "three_cone": None if combine is None else combine.three_cone,
        "shuttle": None if combine is None else combine.shuttle,
        "bench_reps": None if combine is None else combine.bench_reps,
    }
    return {
        "player_id": player_id,
        "athleticism": {
            **measurements,
            "composite_score": derive.athleticism_composite(measurements),
        },
        # Which join key produced the measurements, so a wrong one is traceable.
        # `combine.csv` carries no GSIS id, so the hop is gsis -> pfr -> combine
        # and a missing `pfr_id` is why 122 of 1,326 players have no row.
        "measurement_source_id": row.pfr_id,
    }


def build_career_load_row(
    row: ProfileRow,
    *,
    player_id: str,
    snaps: int | None,
    load_percentile: float | None,
    career: CareerSnaps,
) -> dict:
    """One resolved profile as a `player_career_load` signal row.

    `breakout_age_years` is **null on every row, always**, and that is a
    deliberate refusal rather than an omission. The spec defines it as "age at
    first season crossing the position's breakout threshold", and neither half
    of that is available here: no upstream this collector reads carries
    per-season production, and the spec never defines what the threshold is.
    Emitting a plausible number computed from a threshold this collector
    invented would be exactly the fabrication the adapter notes forbid. The key
    is present and null so a consumer can tell "not supplied" from "absent
    field", and this is recorded as a known gap in the service README.

    `career_snaps_complete` is the honesty flag on the number that IS supplied,
    and it is only ever `True` on evidence. nflverse publishes snap counts from
    `CAREER_SNAP_FIRST_SEASON` only, so a player who was already in the league
    before it has a floor rather than a total; a season this pass could not read
    has the same effect; and **an unknown `rookie_season` is a third case, not a
    pass.** It used to be one: the pre-2012 comparison was skipped when the
    field was null and `complete` stayed `True`, so a twelve-year veteran whose
    rookie season the feed omitted published a 2012-onward floor labelled
    complete — and a consumer weighting `career_snap_load_percentile` would
    treat a truncated total as a full one. That is an assumption standing in for
    a measurement, which is the same thing `is_stale`, `CareerSnaps.total_for`
    and `age_years` each refuse to do.
    """
    complete = (
        snaps is not None
        and not career.seasons_missing
        and row.rookie_season is not None
        and row.rookie_season >= career.first_season
    )
    return {
        "player_id": player_id,
        "career_offensive_snaps": snaps,
        "career_snap_load_percentile": load_percentile,
        "breakout_age_years": None,
        "experience_seasons": row.experience_seasons,
        "career_snaps_complete": bool(complete),
        "career_snaps_first_season": career.first_season,
    }


def _populations(
    resolved: list[tuple[ProfileRow, str]],
    *,
    as_of: date,
    career: CareerSnaps,
) -> tuple[dict[str, list[float]], dict[tuple[str, int], list[float]]]:
    """The reference populations both percentiles rank against.

    **The pass's own captured population**, which `derive`'s module docstring
    argues for at length and which the extra route republishes so a consumer can
    reproduce it. Built here, once, before any row is built, because a
    percentile is a population statistic and cannot be computed row-at-a-time.
    """
    ages_by_position: dict[str, list[float]] = {}
    snaps_by_cohort: dict[tuple[str, int], list[float]] = {}
    for row, _player_id in resolved:
        age = derive.age_years(row.birth_date, as_of)
        if age is not None:
            ages_by_position.setdefault(row.position, []).append(age)
        snaps = career.total_for(row.pfr_id)
        if snaps is not None and row.experience_seasons is not None:
            key = (row.position, row.experience_seasons)
            snaps_by_cohort.setdefault(key, []).append(float(snaps))
    return ages_by_position, snaps_by_cohort


def _new_accumulators() -> dict[str, CoverageAccumulator]:
    return {
        signal_type: CoverageAccumulator(floor=EXPECTED_FLOOR[signal_type])
        for signal_type in SIGNAL_TYPES
    }


async def capture_player_profile(  # noqa: C901 — one linear pass, read top to bottom
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
    # `as_of_date` is in the scope because the spec defines `age_years` as "age
    # in decimal years as of `scope.as_of_date`" — the field is meaningless
    # without the date it was taken against, and a lake object must carry it.
    scope = {"season": season, "week": week, "as_of_date": as_of.isoformat()}
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=source_ref(season, week),
    )

    metrics.capture_attempt()
    # Seeded before anything can fail and overwritten with the real numbers
    # below. An absent Prometheus series and a healthy one are
    # indistinguishable, so these gauges must not simply stop on a failure path.
    metrics.rows_captured(0)
    metrics.stale_position_players(0)
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

        `build_identity_client` is synchronous and raises `ScopeUnavailable`
        when `PLAYER_IDENTITY_URL` is empty, which is why `fetch_scope_or_fail`
        takes a callable rather than an awaitable.
        """
        return build_identity_client(client), await fetch_scope(lake, season, week)

    # BEFORE the first upstream request. That ordering IS failing closed — see
    # the module docstring. `fetch_scope_or_fail` owns both refusal arms.
    identity, membership = await fetch_scope_or_fail(
        _narrowing_seams, **failure_context
    )
    owed = individual_players(membership)

    try:
        profiles = await fetch_profiles(season, client=client)
        combine = await fetch_combine(client=client)
    except UpstreamUnchanged:
        # Forward cover only: the adapter converts a 304 into a memo read rather
        # than letting it escape (see `_read_rows`). Re-raised ABOVE the generic
        # handler regardless, so a future change there can never route an
        # unchanged upstream into `fail_capture`, which would write `present: 0`
        # over a healthy capture.
        raise
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Writes a `present: 0` envelope per signal type, then re-raises. Never
        # returns — and do NOT call `metrics.capture_failure(exc)` first: the
        # library owns that counter for a failure that ends a pass.
        await fail_capture(exc, **failure_context)

    # Degraded rather than fatal, and deliberately so: three of the four signal
    # types do not read this feed, so a single season file 404ing must not
    # discard a perfectly good biographical capture. `fetch_career_snaps`
    # already absorbs a per-season failure into `seasons_missing`; this arm is
    # for a failure of the sweep itself.
    snap_error: str | None = None
    try:
        career = await fetch_career_snaps(season, client=client, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 — degraded, not fatal
        logger.warning("player-profile: career snap sweep failed: %s", exc)
        # Recorded HERE because the library cannot see this one: neither
        # `fail_capture` nor a failed `publish_capture` write runs on this
        # branch. `docs/collectors.md` names "a degraded path that builds its
        # own envelopes" as exactly the case a collector counts for itself.
        metrics.capture_failure(exc, reason=REASON_SNAP_SEASONS_MISSING)
        career = CareerSnaps()
        snap_error = f"{type(exc).__name__}: {exc}"[:200]

    identity_failures = IdentityFailures()
    resolved = await resolve_in_scope(
        profiles.rows,
        season=season,
        scope_members=owed,
        identity=identity,
        failures=identity_failures,
    )

    prior = await staleness.load_prior_histories(lake, season, week)

    ages_by_position, snaps_by_cohort = _populations(
        resolved, as_of=as_of, career=career
    )

    accumulators = _new_accumulators()
    rows: dict[str, list[dict]] = {signal_type: [] for signal_type in SIGNAL_TYPES}

    # Every individual scope member is owed a profile — declared up front,
    # because the watchlist naming them is what makes them owed. Never because a
    # fetch below happened to return them.
    for player_id in sorted(owed):
        for acc in accumulators.values():
            acc.expect(player_key(player_id))

    stale_positions = 0
    covered: set[str] = set()
    future_birth_dates: list[str] = []

    for row, player_id in resolved:
        key = player_key(player_id)
        covered.add(player_id)

        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing away
            # what already resolved: a truncated pass that reports itself
            # truncated is useful; one that reports itself complete is not.
            for acc in accumulators.values():
                acc.fail(key, REASON_DEADLINE)
            continue

        age = derive.age_years(row.birth_date, as_of)
        if age is None and row.birth_date is not None:
            # `age_years` returns None for a missing birth date AND for one
            # after `as_of_date`. The spec permits the first outright, so only
            # the second is worth an error — and without this the two are
            # indistinguishable in the published row.
            future_birth_dates.append(player_id)
        age_percentile = derive.position_age_percentile(
            ages_by_position, row.position, age
        )

        histories = {
            field: staleness.advance(
                prior.get(player_id, {}).get(field),
                getattr(row, field),
                now=now,
                republished=profiles.republished,
            )
            for field in staleness.TRACKED_FIELDS
        }
        stale_position = staleness.is_stale(histories["position"], now=now, week=week)
        if stale_position:
            stale_positions += 1

        rows[BIOGRAPHICAL].append(
            build_biographical_row(
                row,
                player_id=player_id,
                age=age,
                age_percentile=age_percentile,
                histories=histories,
                stale_position=stale_position,
            )
        )
        rows[DRAFT_CAPITAL].append(build_draft_capital_row(row, player_id=player_id))
        rows[ATHLETICISM].append(
            build_athleticism_row(
                row,
                player_id=player_id,
                combine=combine.by_pfr_id.get(row.pfr_id) if row.pfr_id else None,
            )
        )
        snaps = career.total_for(row.pfr_id)
        rows[CAREER_LOAD].append(
            build_career_load_row(
                row,
                player_id=player_id,
                snaps=snaps,
                load_percentile=derive.career_snap_load_percentile(
                    snaps_by_cohort, row.position, row.experience_seasons, snaps
                ),
                career=career,
            )
        )

        # The spec's coverage rule, applied per signal type — see the module
        # docstring, section 3. Biographical is the strict one; the other three
        # are satisfied by a row existing, because their contents are explicitly
        # optional.
        #
        # Of the spec's three required fields, `experience_seasons` is the only
        # one that can still be null HERE, and the other two are not checked
        # because a check that cannot fail is a check nobody can test:
        # `_to_profile` drops a row with no `gsis_id` or an unmappable
        # `position` before it is ever returned, and `resolve_in_scope` yields
        # only rows `player-identity` resolved to a scope member, so
        # `player_id` is non-empty by construction. Asserting all three here
        # read as defensive and was in fact two unreachable clauses wrapped
        # around one live one.
        if row.experience_seasons is not None:
            accumulators[BIOGRAPHICAL].record(key)
        else:
            accumulators[BIOGRAPHICAL].fail(key, REASON_INCOMPLETE_PROFILE)
        for signal_type in (DRAFT_CAPITAL, ATHLETICISM, CAREER_LOAD):
            accumulators[signal_type].record(key)

    # A scope slot no resolved upstream row covered. This is where narrowing
    # becomes visible: an unresolved or absent player is a HOLE against the
    # watchlist, not a row that quietly disappeared from both numerator and
    # denominator.
    for player_id in sorted(owed - covered):
        for acc in accumulators.values():
            acc.fail(player_key(player_id), REASON_NO_PROFILE)

    if identity_failures.rows:
        # One summarised entry, never one per row: a total `player-identity`
        # outage against a 1,326-row feed would otherwise fill the 50-entry cap
        # by itself and push every other reason off the list.
        for acc in accumulators.values():
            acc.add_error(IDENTITY_UPSTREAM_ERROR, identity_failures.detail())

    if future_birth_dates:
        # Summarised, never per row: a feed whose date column shifted would file
        # one entry per scoped player and push every other reason off
        # `CoverageAccumulator`'s 50-entry cap. The first few ids are named
        # because chasing a corrected birth date starts with knowing whose.
        accumulators[BIOGRAPHICAL].add_error(
            REASON_BIRTH_DATE_IN_FUTURE,
            f"{len(future_birth_dates)} player(s) born after "
            f"{as_of.isoformat()}: {', '.join(sorted(future_birth_dates)[:5])}",
        )

    if stale_positions:
        # `add_priority_error`, not `add_error`: the entry that explains why a
        # whole pass's priors are suspect must survive the cap rather than
        # queueing behind a few hundred routine per-slot failures.
        accumulators[BIOGRAPHICAL].add_priority_error(
            REASON_STALE_POSITION,
            f"{stale_positions} scoped player(s) whose position has not been "
            f"re-confirmed in {staleness.STALE_AFTER_DAYS} days",
        )

    if career.seasons_missing:
        accumulators[CAREER_LOAD].add_error(
            REASON_SNAP_SEASONS_MISSING,
            f"season(s) {', '.join(str(s) for s in career.seasons_missing)}",
        )
    if snap_error is not None:
        accumulators[CAREER_LOAD].add_priority_error(
            REASON_SNAP_SEASONS_MISSING, snap_error
        )
    if not prior:
        accumulators[BIOGRAPHICAL].add_error(
            REASON_HISTORY_UNAVAILABLE,
            "no prior player_biographical envelope; field ages restart",
        )

    metrics.rows_captured(len(rows[BIOGRAPHICAL]))
    metrics.stale_position_players(stale_positions)
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

    # Per signal type, not per pass — see the module docstring, section 4.
    digests = {signal_type: _digest(rows[signal_type]) for signal_type in SIGNAL_TYPES}
    changed = {
        signal_type: envelope
        for signal_type, envelope in envelopes.items()
        if _PUBLISHED_DIGESTS.get((season, week, signal_type)) != digests[signal_type]
    }
    if not changed:
        raise UpstreamUnchanged(
            source_ref(season, week), source_ref=digests[BIOGRAPHICAL]
        )

    # The shared tail: writes each envelope off the event loop, records its
    # coverage gauge, records `collector_capture_failures_total` if a write
    # fails — then returns the envelopes ANYWAY, because the capture succeeded
    # and only its archival copy did not.
    published = await publish_capture(changed, lake=lake, metrics=metrics)

    # Gated on the write LANDING. A digest recorded for content the lake never
    # received permanently suppresses the retry: the next pass digests the same
    # content, matches, raises `UpstreamUnchanged`, and the object is never
    # written again until the data itself changes — a whole season, for a
    # `static reference` collector. See `collector_core.publish.PublishResult`.
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
