"""The capture pass: reference table + schedule -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

Three things here are correctness rather than style.

--------------------------------------------------------------------------
1. `coverage.expected` never derives from what succeeded
--------------------------------------------------------------------------

A collector that builds its expectation from the document it just fetched
reports a truncated upstream — 100 of 2,900 records — as
`expected: 100, present: 100`, ratio 1.0. `EXPECTED_FLOOR` encodes the size the
universe is KNOWN to have, independently of any fetch, and
`CoverageAccumulator` takes it as a floor that never lowers a genuine count.
`acc.expect(key)` is called on the fact that made a key owed; `acc.record(key)`
only after the row actually landed.

--------------------------------------------------------------------------
2. The two signal types fail independently
--------------------------------------------------------------------------

`venue_static` reads a committed table and needs no network.
`venue_game_assignment` reads the nflverse game feed and does. Routing a
schedule outage through `fail_capture` would re-raise and discard a perfectly
good static capture the collector already holds in memory — over an upstream
that signal type does not even use.

So a schedule outage takes the DEGRADED path: `venue_static` publishes normally
against every venue in the table, `venue_game_assignment` gets a hand-built
`present: 0` envelope from `failure_envelopes`, and this module records
`collector_capture_failures_total` **itself**. That last part is the documented
exception to "the library owns the counter": `fail_capture` and
`publish_capture` own it for a failure that ends a pass, and a collector owns it
for a failure the library cannot see — "a degraded path that builds its own
envelopes" is exactly the case `docs/collectors.md` names, with `roster-scope`'s
`LedgerUnavailable` branch as the fleet's other example.

A failure of the REFERENCE table is a different fact and does end the pass:
`venue.reference` is imported code, so a failure there is a bug rather than an
outage, and `fail_capture` is right for it.

--------------------------------------------------------------------------
3. A `static reference` cadence must not append an identical snapshot daily
--------------------------------------------------------------------------

The loop re-reads every 24 hours. Publishing a byte-identical envelope on each
of those passes fills an append-only lake with duplicates that carry no
information — 365 objects a year saying the same thing.

So the pass computes a digest over everything it would publish and compares it
to the digest it last published **in this process**. Identical raises
`UpstreamUnchanged`, which `run_capture_loop` and `_run_capture` already handle:
`CaptureState.mark_unchanged` advances `last_capture_at` and records
`collector_upstream_unchanged_total` without touching the stored envelopes, so
`/catalog` reports a fresh pass while `/signals` keeps serving the same rows.
That is the two fields meaning different things, not drift.

**Per process, not per lake, and that is deliberate.** Reading the last
published digest back out of the lake would make a pod restart find a matching
digest, raise `UpstreamUnchanged` against an EMPTY `CaptureState`, and serve
nothing from `/signals` until the table next changed — which for a static
reference could be months. An in-memory digest costs exactly one redundant
snapshot per restart, the same trade `ETagStore` makes for the same reason.

The digest covers the published ROWS and nothing time-varying. No row carries an
`observed_at`: the capture instant belongs on the envelope's `captured_at`, and
putting it on a row would make every digest unique and turn this whole mechanism
off silently.
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
from collector_core.failure import fail_capture, failure_envelopes
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from . import reference
from .adapters.upstream import (
    REFERENCE_SOURCE_REF,
    UPSTREAM_ADAPTER,
    ScheduledGame,
    fetch_season_games,
    schedule_source_ref,
    utc_today,
)
from .metrics import metrics

logger = logging.getLogger(__name__)

__all__ = [
    "ASSIGNMENT",
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "STATIC",
    "build_assignment_row",
    "build_static_row",
    "capture_venue",
    "reset_published_digests",
]

COLLECTOR_NAME = "venue"
CADENCE_CLASS = CadenceClass.STATIC_REFERENCE

STATIC = "venue_static"
ASSIGNMENT = "venue_game_assignment"
SIGNAL_TYPES = (STATIC, ASSIGNMENT)

# The size each universe is KNOWN to have, declared independently of any fetch.
#
#   venue_static            the league's 30 home buildings. Two are shared (the
#                           two Los Angeles clubs, the two New York clubs), so
#                           32 clubs occupy 30 venues and every one of them
#                           hosts at least one game in a normal season.
#                           Neutral-site venues push the real count above this;
#                           the floor never CAPS a genuine count, only raises a
#                           short one.
#   venue_game_assignment   272 regular-season games. Playoffs push the real
#                           count above it, for the same reason.
EXPECTED_FLOOR: dict[str, int] = {
    STATIC: 30,
    ASSIGNMENT: 272,
}

# Coverage failure reasons. Named constants rather than literals, because each
# one implies a different operator action and because a test asserting on a
# typo'd string passes just as happily as one asserting on the real thing.
REASON_NO_REVISION_TODAY = "no_revision_contains_today"
REASON_VENUE_UNRESOLVED = "venue_unresolved"
REASON_KICKOFF_DATE_MISSING = "kickoff_date_missing"
REASON_REVISION_WINDOW_EXCLUDES_KICKOFF = "revision_window_excludes_kickoff"
REASON_SCHEDULE_UNAVAILABLE = "schedule_unavailable"

# `(season, week, signal_type) -> the digest THIS process last published AND
# saw land in the lake`. See the module docstring for why it is not read back
# from the lake, and `PublishResult.landed` for why "saw land" is part of the
# key's meaning rather than an aspiration.
#
# **A `static reference` collector's "next successful pass" may be months
# away.** The digest gate suppresses a pass whose content is unchanged, so
# recording a digest for content the lake never received means the snapshot is
# never written again until the venue table itself changes. One object-store
# blip on the single daily pass would cost the season's `venue_static` object.
# Reproduced before the gate was made durability-aware: pass 1 with a failing
# lake wrote nothing, pass 2 with a healthy lake raised `UpstreamUnchanged` and
# still wrote nothing. Same reasoning `_capture_without_schedule` applies to a
# degraded pass — "a degraded pass must not suppress the next healthy one" —
# carried to the case where the degradation is the write rather than the read.
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
    `default=str` so a stray `date` renders rather than raising — a digest
    helper that throws would turn a cosmetic change into a capture failure.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_static_row(revision: reference.VenueRevision) -> dict:
    """One venue revision as a `venue_static` signal row.

    Mirrored by `contracts/signal-envelope/collectors/venue.json`, which
    `tests/test_capture_contract_conformance.py` validates the REAL output of
    this function against.

    Carries no capture timestamp. That is not an omission — see the module
    docstring: a per-row `observed_at` would make every daily pass's digest
    unique and silently disable the unchanged-snapshot check that a
    `static reference` cadence exists for.
    """
    return reference.revision_to_row(revision)


def build_assignment_row(
    game: ScheduledGame, revision: reference.VenueRevision
) -> dict:
    """One game as a `venue_game_assignment` signal row.

    The spec's six fields, plus the revision window the join needs. The window
    is here because the spec says the kickoff-inside-window check is "checked as
    a join at read time" — and a consumer cannot perform that join without
    knowing which window the assignment was made against, unless it scans the
    whole `venue_static` history first. Carrying it turns the read-time check
    into a field comparison on a single row.
    """
    return {
        "game_id": game.game_id,
        "venue_id": revision.venue_id,
        "designated_home_team_id": game.home_team,
        "is_neutral_site": game.is_neutral_site,
        "is_international": revision.record.country != reference.LEAGUE_COUNTRY,
        "home_field_advantage_class": reference.home_field_advantage_class(
            revision,
            designated_home_team_id=game.home_team,
            is_neutral_site=game.is_neutral_site,
        ),
        "kickoff_on": game.kickoff_on.isoformat(),
        "venue_effective_from": revision.effective_from.isoformat(),
        "venue_effective_to": (
            None if revision.effective_to is None else revision.effective_to.isoformat()
        ),
        "week": game.week,
        "game_type": game.game_type,
    }


def _static_envelope(
    venue_ids: list[str],
    *,
    today: date,
    now: datetime,
    scope: dict,
    extra_errors: tuple[tuple[str, str], ...] = (),
) -> tuple[Envelope, list[dict]]:
    """Build `venue_static` for a set of venues, as of `today`.

    `venue_ids` is the universe this pass owes — every venue hosting at least
    one game this season, or (on the degraded path) every venue the table
    carries. It is passed IN rather than derived from what resolved, which is
    the whole point of the coverage rule.
    """
    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[STATIC])
    rows: list[dict] = []
    single_revision = 0

    for venue_id in venue_ids:
        # Expected because the venue hosts a game, never because a lookup below
        # happened to succeed.
        acc.expect(venue_id)
        if len(reference.revisions_for(venue_id)) == 1:
            single_revision += 1
        matches = reference.revisions_containing(venue_id, today)
        if len(matches) != 1:
            # Zero: the table makes no claim about this venue today — `today`
            # precedes TABLE_COMPILED_ON, or the venue is not carried. More
            # than one: overlapping windows, which `build_revisions` should
            # have made impossible. Both are recorded rather than resolved to
            # a guess.
            acc.fail(venue_id, REASON_NO_REVISION_TODAY)
            continue
        rows.append(build_static_row(matches[0]))
        acc.record(venue_id)

    for reason, detail in extra_errors:
        # `add_priority_error`, not `add_error`: an entry that explains why a
        # whole pass looks the way it does must survive the 50-entry cap, and
        # must not be queued behind a few hundred routine per-key failures.
        acc.add_priority_error(reason, detail)

    metrics.single_revision_venues(single_revision)
    metrics.rows_captured(STATIC, len(rows))

    return (
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=STATIC,
            captured_at=now,
            upstream=Upstream(
                adapter=UPSTREAM_ADAPTER,
                fetched_at=now,
                source_ref=REFERENCE_SOURCE_REF,
            ),
            scope=dict(scope),
            coverage=acc.result(),
            errors=acc.errors,
            signals=rows,
        ),
        rows,
    )


def _assignment_envelope(
    games: list[ScheduledGame],
    *,
    now: datetime,
    scope: dict,
    deadline: datetime | None,
) -> tuple[Envelope, list[dict], set[str]]:
    """Build `venue_game_assignment`, and report the venues the season uses.

    The returned set is every venue a listed game **names**, which is what the
    coverage rule means by "every venue hosting at least one game in the current
    season". It is deliberately NOT the set of venues whose assignment rows were
    built successfully.

    That distinction was a real bug, and it is the subtle form of the failure
    `EXPECTED_FLOOR` exists to prevent. This set used to be populated at the
    bottom of the loop, after the kickoff-presence check and the window join had
    both passed — so a venue whose every game failed the window join simply
    vanished from `venue_static`, taking its own coverage expectation with it.
    The envelope then reported ratio 1.0 with an empty `errors` array while two
    venues the season uses were missing entirely: expectation derived from
    success, wearing a floor as a disguise. The floor of 30 hid it until fewer
    than 30 venues resolved.

    Resolving a venue id is a fact about the SCHEDULE — this game is played at
    that building. Whether the table can then describe the building on that date
    is a separate question, and the answer to it belongs in coverage as a miss,
    never as a silently smaller universe.
    """
    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[ASSIGNMENT])
    rows: list[dict] = []
    season_venues: set[str] = set()
    window_misses = 0
    unresolved = 0

    for game in games:
        acc.expect(game.game_id)

        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing away
            # what already resolved: a truncated pass that reports itself
            # truncated is useful; one that reports itself complete is not.
            acc.fail(game.game_id, "deadline_exceeded")
            continue

        venue_id = reference.resolve_venue_id(
            home_team=game.home_team,
            stadium_name=game.stadium_name,
            is_neutral_site=game.is_neutral_site,
        )
        if venue_id is None:
            # Carried over from `schedule_context.venues`: an unrecognised
            # neutral-site stadium name resolves to NOTHING rather than to the
            # designated home club's building. Reading Jacksonville's
            # coordinates for a game played in Munich yields numbers that pass
            # every schema check and are wrong by four thousand miles.
            unresolved += 1
            acc.fail(game.game_id, REASON_VENUE_UNRESOLVED)
            continue

        # HERE, not at the bottom of the loop. The season uses this venue the
        # moment a game names it; everything below can still fail, and each of
        # those failures is a coverage miss rather than a reason to forget the
        # venue was owed. See this function's docstring.
        season_venues.add(venue_id)

        if game.kickoff_on is None:
            acc.fail(game.game_id, REASON_KICKOFF_DATE_MISSING)
            continue

        # THE assertion the spec names, enforced at write time rather than left
        # to a consumer: no game may resolve to a venue revision whose
        # [effective_from, effective_to) window excludes its kickoff date.
        # Without it, a mid-season surface replacement is retroactively applied
        # to the whole season and nothing looks broken.
        revision = reference.revision_on(venue_id, game.kickoff_on)
        if revision is None:
            window_misses += 1
            acc.fail(game.game_id, REASON_REVISION_WINDOW_EXCLUDES_KICKOFF)
            continue

        rows.append(build_assignment_row(game, revision))
        acc.record(game.game_id)

    metrics.revision_window_misses(window_misses)
    metrics.unresolved_venues(unresolved)
    metrics.rows_captured(ASSIGNMENT, len(rows))

    return (
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=ASSIGNMENT,
            captured_at=now,
            upstream=Upstream(
                adapter=UPSTREAM_ADAPTER,
                fetched_at=now,
                source_ref=schedule_source_ref(),
            ),
            scope=dict(scope),
            coverage=acc.result(),
            errors=acc.errors,
            signals=rows,
        ),
        rows,
        season_venues,
    )


async def capture_venue(
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
    collector's does, but neither signal type is week-scoped: a venue table is
    static and the assignment set is a whole season. That is the shape
    `schedule-context` uses and for the same reason — one week's rows are not
    derivable without the rest of the season's.
    """
    scope = {"season": season, "week": week}
    today = utc_today(now)

    metrics.capture_attempt()

    try:
        games = await fetch_season_games(season, client=client)
    except UpstreamUnchanged:
        # Not a failure: a 304 means the feed is byte-identical to the one this
        # process already read. Re-raised ABOVE the generic handler so it can
        # never be routed into the degraded path, which would publish a
        # `present: 0` assignment envelope over a healthy capture.
        # `stream_csv_dicts` raises it only when an `etag_key` is set — it is
        # not, today — so this arm is forward cover rather than a live path,
        # and it is cheaper to have than to remember later.
        raise
    except Exception as exc:  # noqa: BLE001 — degraded, not fatal; see docstring
        return await _capture_without_schedule(
            exc, now=now, today=today, scope=scope, lake=lake
        )

    try:
        assignment_envelope, assignment_rows, season_venues = _assignment_envelope(
            games, now=now, scope=scope, deadline=deadline
        )
        # The season's venue universe, derived from the games the feed LISTED —
        # which is legitimate, and worth stating because it looks like the
        # forbidden derivation: this is exactly what the coverage rule means by
        # "every venue hosting at least one game in the current season".
        #
        # What would be wrong is deriving it from the games whose assignment
        # rows were successfully BUILT, and `season_venues` deliberately is not
        # that — see `_assignment_envelope`'s docstring for the bug this used
        # to be.
        static_envelope, static_rows = _static_envelope(
            sorted(season_venues), today=today, now=now, scope=scope
        )
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # The reference table is imported code, so a failure building from it
        # is a bug rather than an outage, and it does end the pass. Do NOT call
        # `metrics.capture_failure(exc)` first: `fail_capture` records
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
            source_ref=REFERENCE_SOURCE_REF,
        )

    envelopes = {STATIC: static_envelope, ASSIGNMENT: assignment_envelope}
    digests = {
        STATIC: _digest(static_rows),
        ASSIGNMENT: _digest(assignment_rows),
    }

    # PER SIGNAL TYPE, not per pass. The spec says a snapshot is appended "only
    # when the content hash of a VENUE RECORD changes", and one digest spanning
    # both types breaks that: a flex reschedule moves one game, the combined
    # digest changes, and a byte-identical `venue_static` envelope is appended
    # alongside the assignment envelope that genuinely changed.
    changed = {
        signal_type: envelope
        for signal_type, envelope in envelopes.items()
        if _PUBLISHED_DIGESTS.get((season, week, signal_type)) != digests[signal_type]
    }
    if not changed:
        raise UpstreamUnchanged(REFERENCE_SOURCE_REF, source_ref=digests[STATIC])

    # The shared tail: writes each envelope off the event loop, records its
    # coverage gauge, and records `collector_capture_failures_total` if a write
    # fails — then returns the envelopes ANYWAY, because the capture succeeded
    # and only its archival copy did not.
    published = await publish_capture(changed, lake=lake, metrics=metrics)

    # Gated on the write LANDING. `publish_capture` swallows a failed write,
    # which is right for availability and wrong for a digest gate: record a
    # digest for content the lake never received and the single daily pass
    # never retries it. See `PublishResult`.
    for signal_type in published:
        if published.landed(signal_type):
            _PUBLISHED_DIGESTS[(season, week, signal_type)] = digests[signal_type]

    # An unchanged envelope is not written, but its coverage gauge is still
    # recorded — `publish_capture` only records the ones it was handed. "Record
    # every pass, including zero" is what makes the series alertable, and a
    # gauge that went quiet whenever the data was stable would read exactly like
    # a collector that had stopped.
    for signal_type, envelope in envelopes.items():
        if signal_type not in changed:
            metrics.coverage(signal_type, envelope.coverage.ratio)

    return envelopes


async def _capture_without_schedule(
    exc: Exception,
    *,
    now: datetime,
    today: date,
    scope: dict,
    lake: LakeWriter,
) -> dict[str, Envelope]:
    """The degraded path: the game feed is unreachable, the table is not.

    `venue_static` publishes against every venue the table carries — a superset
    of the season's, and the honest expectation when the thing that would have
    narrowed it is the thing that failed. `venue_game_assignment` gets a
    `present: 0` envelope built by hand rather than by `fail_capture`, because
    `fail_capture` re-raises and would discard the static capture this process
    already holds in memory.

    The publish digest is deliberately NOT recorded here. A degraded pass must
    not be able to suppress the next healthy one as "unchanged".
    """
    logger.warning(
        "venue: the game schedule is unavailable (%s: %s); publishing "
        "venue_static from the reference table and a present:0 "
        "venue_game_assignment",
        type(exc).__name__,
        exc,
    )
    # Recorded HERE because the library cannot see this one: neither
    # `fail_capture` nor a failed `publish_capture` write runs on this branch,
    # and `docs/collectors.md` names "a degraded path that builds its own
    # envelopes" as exactly the case a collector counts for itself.
    metrics.capture_failure(exc, reason=REASON_SCHEDULE_UNAVAILABLE)

    static_envelope, _ = _static_envelope(
        sorted(reference.REVISIONS),
        today=today,
        now=now,
        scope=scope,
        extra_errors=(
            (REASON_SCHEDULE_UNAVAILABLE, f"{type(exc).__name__}: {exc}"[:200]),
        ),
    )
    assignment_envelope = failure_envelopes(
        exc,
        collector=COLLECTOR_NAME,
        signal_types=(ASSIGNMENT,),
        adapter=UPSTREAM_ADAPTER,
        now=now,
        scope=scope,
        reason=REASON_SCHEDULE_UNAVAILABLE,
        expected=EXPECTED_FLOOR,
        source_ref=schedule_source_ref(),
    )[ASSIGNMENT]

    return await publish_capture(
        {STATIC: static_envelope, ASSIGNMENT: assignment_envelope},
        lake=lake,
        metrics=metrics,
    )
