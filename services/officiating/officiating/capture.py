"""The capture pass: three upstreams -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

--------------------------------------------------------------------------
1. `coverage.expected` never derives from what succeeded
--------------------------------------------------------------------------

This collector has a sharp version of that trap, because its crew feed is
**post-game**. `officials.csv` records who worked a game that has been played,
so in week 3 it describes 48 games out of 272. Declaring the expectation from
"games the officials feed knows about" would report `expected: 48, present: 48`
— ratio 1.0, in week 3, with 82% of the season absent.

So `EXPECTED_FLOOR` encodes what the season is KNOWN to hold: 272 regular-season
games and 17 crews, both independent of any fetch. A game the officials feed has
not reached yet is `expect`ed and `fail`ed with `crew_not_published`, never
quietly dropped from the universe.

**This is a deliberate deviation from the spec, and the third one this collector
takes.** The phase doc defines `coverage.expected` as "one
`game_crew_assignment` for every game in scope *whose crew has been published*,
and a `crew_tendency_rates` record for every distinct `crew_id` *appearing in
those assignments*" — which, read literally, is the fetch-derived expectation
described above. Against a post-game feed it produces ratio 1.0 all season.

The platform rule wins: "`expected` never derives from what succeeded" is stated
in `collector_core.coverage`'s module docstring and in CLAUDE.md, enforced
fleet-wide, and aimed at exactly this pathology. A per-collector spec line does
not override it. Recorded here and in the service README's "Spec deviations"
section rather than left for a future reader to rediscover by diffing.

--------------------------------------------------------------------------
2. The two signal types fail independently
--------------------------------------------------------------------------

`game_crew_assignment` needs `games.csv` (2.1 MB) and `officials.csv` (1.3 MB).
`crew_tendency_rates` additionally needs play-by-play (18.2 MiB gzipped) — the
largest upstream, the slowest, and the one that **does not exist at all** until
a season's first games have been played.

Routing a play-by-play failure through `fail_capture` would re-raise and
discard a perfectly good assignment capture already built in memory, over an
upstream that signal type does not use. So it takes the DEGRADED path:
assignments publish normally, `crew_tendency_rates` gets a hand-built
`present: 0` envelope from `failure_envelopes`, and this module records
`collector_capture_failures_total` **itself**. That is the documented exception
to "the library owns the counter" — `docs/collectors.md` names "a degraded path
that builds its own envelopes" as exactly the collector-owned case, with
`venue`'s schedule branch and `roster-scope`'s `LedgerUnavailable` branch as
the fleet's other two.

A failure of `games.csv` or `officials.csv` ends the pass, through
`fail_capture`: without the crosswalk there are no modern game ids, and without
the officials feed there are no crews, so neither signal type has anything to
say.

--------------------------------------------------------------------------
3. The two guards, kept separate
--------------------------------------------------------------------------

**Stability** decides whether a rate may be served at all, and lives in
`officiating.rates`. **Continuity** decides whether a served rate is describing
the right people, and is raised here — because it is a join: a continuity of
0.31 only matters if that crew's rates are actually being served. A crew whose
rates were all refused is making no claim about anyone, and alarming on it
would train an operator to ignore the alarm.

--------------------------------------------------------------------------
4. The digest gate, and why it waits for the write to land
--------------------------------------------------------------------------

A `weekly` cadence polls daily, and a season's crew data changes about once a
week. Publishing a byte-identical envelope on each of the other six days fills
an append-only lake with objects that carry no information.

So the pass digests what it would publish and compares it, per signal type, to
what this process last published **and saw reach the lake**. That last clause
is the whole of `PublishResult.landed`: record a digest for content the lake
never received and the next pass digests the same content, matches, raises
`UpstreamUnchanged`, and the object is never written again until the upstream
data itself changes. Per signal type, not per pass — a partial lake failure is
the only thing that can tell those apart.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.conditional import UpstreamUnchanged
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture, failure_envelopes
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from . import crews as crews_module
from . import rates as rates_module
from .adapters import games as games_adapter
from .adapters import officials as officials_adapter
from .adapters import pbp as pbp_adapter
from .adapters.pbp import GamePenalties
from .crews import CrewAssignment
from .metrics import metrics

logger = logging.getLogger(__name__)

__all__ = [
    "ASSIGNMENT",
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "RATES",
    "SIGNAL_TYPES",
    "build_assignment_row",
    "build_rates_row",
    "capture_officiating",
    "reset_published_digests",
]

COLLECTOR_NAME = "officiating"
CADENCE_CLASS = CadenceClass.WEEKLY

ASSIGNMENT = "game_crew_assignment"
RATES = "crew_tendency_rates"
SIGNAL_TYPES = (ASSIGNMENT, RATES)

UPSTREAM_ADAPTER = "nflverse-officiating"

# The size each universe is KNOWN to have, declared independently of any fetch.
#
#   game_crew_assignment  272 regular-season games. Not "games the officials
#                         feed has published" — see the module docstring; that
#                         is the derivation this floor exists to defeat.
#                         Postseason pushes the real count above it, and a
#                         floor never CAPS a genuine count.
#   crew_tendency_rates   17 crews. The league has fielded 17 white hats every
#                         season since 2015; verified against 2025, where all
#                         272 games resolve to exactly 17 distinct referees.
EXPECTED_FLOOR: dict[str, int] = {
    ASSIGNMENT: 272,
    RATES: 17,
}

# Coverage failure reasons. Named constants rather than literals, because each
# implies a different operator action and because a test asserting on a typo'd
# string passes just as happily as one asserting on the real thing.
REASON_PENALTIES_UNAVAILABLE = "penalties_unavailable"
REASON_NO_GAMES_SAMPLED = "no_games_sampled"
REASON_LOW_CONTINUITY = "low_continuity_with_served_rates"

# Spec fields with no free upstream. `officials.csv` is a post-game record of
# who worked a game, not a forward-looking assignment feed, so neither the
# publication instant nor the provisional flag exists anywhere in it. They are
# emitted as `null` with this reason attached to the row, rather than
# fabricated, defaulted to `false`, or dropped from the schema — an untested
# value is a null with a reason.
NULL_FIELD_REASON = "post_game_feed_has_no_assignment_publication_record"

# `(season, week, signal_type) -> the digest THIS process last published AND
# saw land in the lake`. In memory rather than read back from the lake: a pod
# restart then costs exactly one redundant snapshot, where reading the lake
# would make a restart find a matching digest, raise `UpstreamUnchanged`
# against an EMPTY cache, and serve nothing from `/signals` until the data next
# changed. Same trade `ETagStore` makes for the same reason.
_PUBLISHED_DIGESTS: dict[tuple[int, int, str], str] = {}


def reset_published_digests() -> None:
    """Forget what this process has published. For tests only.

    A module-level dict outlives a test, so a second test asserting a real
    publish would otherwise get `UpstreamUnchanged` from the first one's
    leftovers and fail somewhere unrelated to what it was checking.
    """
    _PUBLISHED_DIGESTS.clear()


def _digest(payload: object) -> str:
    """A stable sha256 over anything JSON-serialisable.

    `sort_keys` so dict insertion order cannot change the digest, and
    `default=str` so a stray non-serialisable value renders rather than raising
    — a digest helper that throws would turn a cosmetic change into a capture
    failure.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_assignment_row(
    assignment: CrewAssignment, *, continuity: float, games_sampled: int
) -> dict:
    """One game's crew as a `game_crew_assignment` signal row.

    Mirrored by `contracts/signal-envelope/collectors/officiating.json`, which
    `tests/test_capture_contract_conformance.py` validates the REAL output of
    this function against.

    Carries no capture timestamp. That is not an omission: the capture instant
    belongs on the envelope's `captured_at`, and a per-row one would make every
    daily digest unique and silently disable the unchanged-snapshot gate.
    """
    return {
        "game_id": assignment.game_id,
        "week": assignment.week,
        "crew_id": assignment.crew_id,
        "referee_name": assignment.referee_name,
        "crew_members": [
            {
                "official_id": member.official_id,
                "name": member.name,
                "position": member.position,
            }
            for member in assignment.members
        ],
        # Null by necessity, not by omission. See NULL_FIELD_REASON.
        "assignment_announced_at": None,
        "is_provisional": None,
        "null_field_reason": NULL_FIELD_REASON,
        "crew_continuity_pct": round(continuity, 4),
        "games_sampled": games_sampled,
        "rate_window": rates_module.RATE_WINDOW_SEASON_TO_DATE,
    }


def build_rates_row(
    sample: rates_module.CrewSample,
    *,
    referee_name: str,
    priors: dict[str, rates_module.RatePriors],
) -> dict:
    """One crew's rate profile as a `crew_tendency_rates` signal row.

    Each of the spec's eight rate fields is an **object** rather than a bare
    float. The spec lists `is_shrunk` (bool) and `rate_stderr` (float) as
    scalar fields of the signal, and they cannot be: there are eight rates with
    independent sample variances and independent stability verdicts, and one
    bool cannot describe eight of them truthfully. Keeping the spec's field
    NAMES at the top level and putting the per-rate metadata inside each one is
    the smallest deviation that stays honest — a consumer still reads
    `row["dpi_per_game"]`, and gets back something that says how much to trust
    it.
    """
    return {
        "crew_id": sample.crew_id,
        "referee_name": referee_name,
        "games_sampled": len(sample.games),
        "rate_window": rates_module.RATE_WINDOW_SEASON_TO_DATE,
        **{
            rate: rates_module.build_rate(sample, rate, priors[rate])
            for rate in rates_module.RATE_NAMES
        },
    }


def _assignment_envelope(
    assignments: list[CrewAssignment],
    misses: dict[str, str],
    *,
    continuity: dict[str, float],
    games_sampled: dict[str, int],
    now: datetime,
    scope: dict,
    deadline: datetime | None,
    extra_errors: tuple[tuple[str, str], ...] = (),
) -> tuple[Envelope, list[dict]]:
    """Build `game_crew_assignment` over every SCHEDULED game.

    `misses` is every scheduled game that produced no assignment, and it is
    passed IN rather than inferred. That is the coverage rule made structural:
    a game whose crew the post-game feed has not published yet is expected and
    missing, not absent from the universe.

    `extra_errors` are pass-level entries — the continuity alarm, and the
    degraded pass's explanation — routed through `add_priority_error` rather
    than inserted into the finished array. That is not a style point: `errors`
    is capped at 50 and the cap is applied when the property is read, so an
    entry inserted afterwards pushes the array PAST its cap. Worse, on a
    post-game feed in week 3 there are 200+ routine `crew_not_published`
    entries, and an appended alarm would be the one silently dropped —
    a guard that fires into a truncated list is a guard that does not fire.
    """
    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[ASSIGNMENT])
    rows: list[dict] = []

    for game_id, reason in misses.items():
        # `fail` declares the key expected itself — a failure is evidence the
        # key was owed, which is the opposite of deriving the expectation from
        # a success.
        acc.fail(game_id, reason)

    undersized = 0
    for assignment in assignments:
        acc.expect(assignment.game_id)
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing away
            # what already resolved: a truncated pass that reports itself
            # truncated is useful; one that reports itself complete is not.
            acc.fail(assignment.game_id, "deadline_exceeded")
            continue
        if crews_module.undersized(assignment):
            # Counted and recorded, never dropped: who worked the game is a
            # fact even when the feed is incomplete about it. The reason
            # matters because an undersized crew silently changes the
            # DENOMINATOR of its own `crew_continuity_pct` — a mean over six
            # terms instead of seven — so a rise here explains a continuity
            # drift that nothing else can. Real: 1 game in 2022, 11 in 2024.
            undersized += 1
            acc.add_error(
                crews_module.REASON_UNDERSIZED_CREW,
                f"{assignment.game_id} crew={assignment.crew_id} "
                f"members={len(assignment.members)}",
            )
        rows.append(
            build_assignment_row(
                assignment,
                continuity=continuity.get(assignment.game_id, 1.0),
                games_sampled=games_sampled.get(assignment.crew_id, 0),
            )
        )
        acc.record(assignment.game_id)

    metrics.undersized_crews(undersized)

    for reason, detail in extra_errors:
        acc.add_priority_error(reason, detail)

    return (
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=ASSIGNMENT,
            captured_at=now,
            upstream=Upstream(
                adapter=officials_adapter.UPSTREAM_ADAPTER,
                fetched_at=now,
                source_ref=officials_adapter.source_ref(),
            ),
            scope=dict(scope),
            coverage=acc.result(),
            errors=acc.errors,
            signals=rows,
        ),
        rows,
    )


def _rates_envelope(
    samples: list[rates_module.CrewSample],
    referee_names: dict[str, str],
    crews_without_sample: dict[str, str],
    *,
    season: int,
    now: datetime,
    scope: dict,
) -> tuple[Envelope, list[dict], set[str]]:
    """Build `crew_tendency_rates`, and report which crews served any rate.

    The returned set is what the continuity alarm joins against: an assignment
    below 0.6 only alarms when its crew appears there.
    """
    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[RATES])
    rows: list[dict] = []
    serving: set[str] = set()
    refused = 0

    for crew_id, reason in crews_without_sample.items():
        acc.fail(crew_id, reason)

    priors = dict(rates_module.build_all_priors(samples))

    for sample in samples:
        acc.expect(sample.crew_id)
        row = build_rates_row(
            sample,
            referee_name=referee_names.get(sample.crew_id, ""),
            priors=priors,
        )
        for rate in rates_module.RATE_NAMES:
            if row[rate]["served"]:
                serving.add(sample.crew_id)
            else:
                refused += 1
        rows.append(row)
        acc.record(sample.crew_id)

    metrics.rates_refused(refused)
    metrics.crews_sampled(len(samples))

    return (
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=RATES,
            captured_at=now,
            upstream=Upstream(
                adapter=pbp_adapter.UPSTREAM_ADAPTER,
                fetched_at=now,
                source_ref=pbp_adapter.source_ref(season),
            ),
            scope=dict(scope),
            coverage=acc.result(),
            errors=acc.errors,
            signals=rows,
        ),
        rows,
        serving,
    )


def _build_samples(
    assignments: list[CrewAssignment],
    penalties: dict[str, GamePenalties],
) -> tuple[list[rates_module.CrewSample], dict[str, str]]:
    """Group assignments into per-crew rate windows.

    A game enters a crew's window only when play-by-play carries it **with at
    least one offensive snap**. Zero snaps is not a real game — it is a game
    play-by-play has an empty record for — and admitting it would put a
    division by zero in `seconds_per_play` and a phantom 0.0 in every other
    rate, dragging the crew's mean toward a game that was never played.

    Returns the samples and the crews that have assignments but no usable
    window, so those stay expected-and-missing rather than vanishing.
    """
    grouped: dict[str, list[tuple[int, GamePenalties]]] = {}
    for assignment in assignments:
        grouped.setdefault(assignment.crew_id, [])
        game = penalties.get(assignment.game_id)
        if game is None or game.offensive_plays <= 0:
            continue
        grouped[assignment.crew_id].append((assignment.week, game))

    samples: list[rates_module.CrewSample] = []
    without: dict[str, str] = {}
    for crew_id, entries in sorted(grouped.items()):
        if not entries:
            without[crew_id] = REASON_NO_GAMES_SAMPLED
            continue
        samples.append(
            rates_module.CrewSample(
                crew_id=crew_id,
                weeks=tuple(week for week, _ in entries),
                games=tuple(game for _, game in entries),
            )
        )
    return samples, without


def _continuity(
    assignments: list[CrewAssignment], sampled_weeks: dict[str, set[int]]
) -> dict[str, float]:
    """Every assignment's crew continuity, against its crew's SAMPLED window.

    Against the sampled window rather than every assignment the crew has,
    because the spec defines it as "in the sampled window" and because the
    number is only useful next to the rates it qualifies. A crew with no
    sampled window gets 1.0 — nothing is known about its churn, and 0.0 would
    read as total churn on a crew making no claims.
    """
    by_crew: dict[str, list[CrewAssignment]] = {}
    for assignment in assignments:
        by_crew.setdefault(assignment.crew_id, []).append(assignment)

    result: dict[str, float] = {}
    for assignment in assignments:
        weeks = sampled_weeks.get(assignment.crew_id, set())
        window = [g for g in by_crew[assignment.crew_id] if g.week in weeks]
        result[assignment.game_id] = crews_module.continuity_pct(assignment, window)
    return result


def _raise_continuity_alarm(
    assignments: list[CrewAssignment],
    continuity: dict[str, float],
    serving: set[str],
    acc_errors: list[tuple[str, str]],
) -> int:
    """The spec's separate alarm, and the reason it is separate.

    *"Alarm separately when `crew_continuity_pct` drops below 0.6 for an
    assignment whose rates are being served."* Both halves are load-bearing.
    Alarming on low continuity alone fires for crews making no claim about
    anyone; alarming on served rates alone fires for the healthy majority.
    """
    alarmed = 0
    for assignment in assignments:
        if continuity.get(assignment.game_id, 1.0) >= (
            crews_module.CONTINUITY_ALARM_THRESHOLD
        ):
            continue
        if assignment.crew_id not in serving:
            continue
        alarmed += 1
        acc_errors.append(
            (
                REASON_LOW_CONTINUITY,
                f"{assignment.game_id} crew={assignment.crew_id} "
                f"continuity={continuity[assignment.game_id]:.3f}",
            )
        )
    metrics.low_continuity_assignments(alarmed)
    return alarmed


async def capture_officiating(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one season into one envelope per signal type.

    `week` is carried in the scope so the lake partitions the way every other
    collector's does, but neither signal type is week-scoped: a crew's rates
    are drawn from the whole season to date, and one week's assignments are not
    interpretable without the window the rest of the season provides.
    """
    scope = {"season": season, "week": week}
    metrics.capture_attempt()

    try:
        schedule = await games_adapter.fetch_season_games(season, client=client)
        officials = await officials_adapter.fetch_season_officials(
            season, client=client
        )
    except UpstreamUnchanged:
        # Not a failure: a 304 means the feed is byte-identical to the one this
        # process already read. Re-raised ABOVE the generic handler so it can
        # never be routed into `fail_capture`, which would write a `present: 0`
        # envelope over a healthy capture.
        raise
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Ends the pass: without the crosswalk there are no modern game ids,
        # and without the officials feed there are no crews. Do NOT call
        # `metrics.capture_failure(exc)` first — `fail_capture` records
        # `collector_capture_failures_total` itself and calling it here
        # double-counts.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            expected=EXPECTED_FLOOR,
            source_ref=officials_adapter.source_ref(),
        )

    assignments, misses = crews_module.build_assignments(
        season=season,
        games_by_legacy_id={
            game.legacy_game_id: (game.game_id, game.week) for game in schedule
        },
        officials_by_legacy_id=officials,
    )

    disagreements = _count_referee_disagreements(assignments, schedule)
    metrics.referee_disagreements(disagreements)

    referee_names = {a.crew_id: a.referee_name for a in assignments}

    penalties_failure: Exception | None = None
    try:
        penalties = await pbp_adapter.fetch_season_penalties(season, client=client)
    except UpstreamUnchanged:
        raise
    except Exception as exc:  # noqa: BLE001 — degraded, not fatal; see docstring
        penalties_failure = exc
        penalties = {}

    samples, crews_without = _build_samples(assignments, penalties)
    sampled_weeks = {s.crew_id: set(s.weeks) for s in samples}
    continuity = _continuity(assignments, sampled_weeks)

    if penalties_failure is not None:
        return await _capture_without_penalties(
            penalties_failure,
            assignments=assignments,
            misses=misses,
            continuity=continuity,
            season=season,
            now=now,
            scope=scope,
            lake=lake,
            deadline=deadline,
        )

    rates_envelope, rates_rows, serving = _rates_envelope(
        samples,
        referee_names,
        crews_without,
        season=season,
        now=now,
        scope=scope,
    )

    extra_errors: list[tuple[str, str]] = []
    _raise_continuity_alarm(assignments, continuity, serving, extra_errors)

    assignment_envelope, assignment_rows = _assignment_envelope(
        assignments,
        misses,
        continuity=continuity,
        games_sampled={s.crew_id: len(s.games) for s in samples},
        now=now,
        scope=scope,
        deadline=deadline,
        extra_errors=tuple(extra_errors),
    )

    envelopes = {ASSIGNMENT: assignment_envelope, RATES: rates_envelope}
    digests = {ASSIGNMENT: _digest(assignment_rows), RATES: _digest(rates_rows)}

    # PER SIGNAL TYPE, not per pass. One digest spanning both would append a
    # byte-identical rates envelope every time a single crew assignment
    # published, which on a post-game feed is most days of the season.
    changed = {
        signal_type: envelope
        for signal_type, envelope in envelopes.items()
        if _PUBLISHED_DIGESTS.get((season, week, signal_type)) != digests[signal_type]
    }
    if not changed:
        raise UpstreamUnchanged(
            officials_adapter.source_ref(), source_ref=digests[ASSIGNMENT]
        )

    published = await publish_capture(changed, lake=lake, metrics=metrics)

    # Gated on the write LANDING, per signal type. `publish_capture` swallows a
    # failed write, which is right for availability and wrong for a digest
    # gate: record a digest for content the lake never received and this pass
    # never retries it. Iterating `published` rather than `envelopes` is also
    # required — `landed` raises for a signal type this call did not publish,
    # and that raise costs the whole pass.
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


def _count_referee_disagreements(
    assignments: list[CrewAssignment], schedule: list[games_adapter.ScheduledGame]
) -> int:
    by_id = {game.game_id: game.referee for game in schedule}
    return sum(
        1
        for assignment in assignments
        if crews_module.referee_disagreement(
            assignment, by_id.get(assignment.game_id, "")
        )
    )


async def _capture_without_penalties(
    exc: Exception,
    *,
    assignments: list[CrewAssignment],
    misses: dict[str, str],
    continuity: dict[str, float],
    season: int,
    now: datetime,
    scope: dict,
    lake: LakeWriter,
    deadline: datetime | None,
) -> dict[str, Envelope]:
    """The degraded path: play-by-play is unreachable, the crew feeds are not.

    `game_crew_assignment` publishes normally — who is calling the game is a
    fact that does not depend on penalty history. `crew_tendency_rates` gets a
    `present: 0` envelope built by hand rather than by `fail_capture`, because
    `fail_capture` re-raises and would discard the assignment capture this
    process already holds in memory.

    The continuity alarm is deliberately NOT raised on this branch: no rates
    are being served, so no rate is describing the wrong people. That is the
    join the alarm is defined on, not an omission.

    **The two digests are treated differently, and the asymmetry is the point.**

    `crew_tendency_rates` records none. Its envelope here is a `present: 0`
    failure envelope, and letting that claim "already published" would suppress
    the first healthy pass that actually computes rates.

    `game_crew_assignment` DOES record its digest, gated on the write landing
    like every other. It is safe to do so, but the reason is narrower than the
    obvious one and the difference is worth stating precisely.

    A degraded envelope carries `games_sampled: 0` on every row. A healthy pass
    that **actually samples games** carries a non-zero count on at least one,
    so the two cannot collide and a degraded digest can never suppress it —
    pinned by `test_a_degraded_envelope_cannot_suppress_a_pass_that_sampled_games`.

    That is *not* "never". A healthy pass whose play-by-play carries no game
    this season's crews worked also produces `games_sampled: 0` everywhere, and
    its assignment envelope **is** byte-identical to the degraded one and **is**
    suppressed. Which is correct, and is the reason the two digests are treated
    differently rather than a hole in the argument: byte-identical rows carry no
    information, so not appending them is the right answer, while the *rates*
    envelope in that scenario genuinely differs — `no_games_sampled` rather than
    `penalties_unavailable`, and a different `upstream.source_ref` — is not
    gated here, and does get written. `test_the_degraded_pass_never_records_the
    _rates_digest` is that case, and it is why the rates digest is never
    recorded on this path.

    So the invariant is: a degraded envelope cannot suppress a healthy pass
    **that sampled anything**, and where it can suppress one, the suppressed
    content is identical and the informative half is published separately.

    Without it, the whole preseason is a byte-identical daily append.
    `play_by_play_<season>.csv.gz` does not exist until the season's first
    games are played, so this branch is the *normal* one for months, and the
    assignment envelope over that stretch is usually empty and always
    unchanged. That is precisely the waste the gate was built to prevent,
    across the longest window of the year.
    """
    logger.warning(
        "officiating: play-by-play is unavailable (%s: %s); publishing "
        "game_crew_assignment and a present:0 crew_tendency_rates",
        type(exc).__name__,
        exc,
    )
    # Recorded HERE because the library cannot see this one: neither
    # `fail_capture` nor a failed `publish_capture` write runs on this branch.
    metrics.capture_failure(exc, reason=REASON_PENALTIES_UNAVAILABLE)
    metrics.rates_refused(0)
    metrics.crews_sampled(0)
    metrics.low_continuity_assignments(0)

    assignment_envelope, assignment_rows = _assignment_envelope(
        assignments,
        misses,
        continuity=continuity,
        games_sampled={},
        now=now,
        scope=scope,
        deadline=deadline,
        extra_errors=(
            (REASON_PENALTIES_UNAVAILABLE, f"{type(exc).__name__}: {exc}"[:200]),
        ),
    )
    assignment_digest = _digest(assignment_rows)
    if _PUBLISHED_DIGESTS.get((season, scope["week"], ASSIGNMENT)) == assignment_digest:
        # Byte-identical to what this process already put in the lake. On a
        # preseason cadence that is every day for months.
        raise UpstreamUnchanged(
            officials_adapter.source_ref(), source_ref=assignment_digest
        )
    rates_envelope = failure_envelopes(
        exc,
        collector=COLLECTOR_NAME,
        signal_types=(RATES,),
        adapter=pbp_adapter.UPSTREAM_ADAPTER,
        now=now,
        scope=scope,
        reason=REASON_PENALTIES_UNAVAILABLE,
        expected=EXPECTED_FLOOR,
        source_ref=pbp_adapter.source_ref(season),
    )[RATES]

    published = await publish_capture(
        {ASSIGNMENT: assignment_envelope, RATES: rates_envelope},
        lake=lake,
        metrics=metrics,
    )

    # Only the assignment digest, and only if its write landed — the same
    # durability gate the healthy path applies, for the same reason. The rates
    # digest is deliberately never recorded here; see the docstring.
    if published.landed(ASSIGNMENT):
        _PUBLISHED_DIGESTS[(season, scope["week"], ASSIGNMENT)] = assignment_digest

    return published
