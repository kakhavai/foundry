"""injury-report's capture pass: schedule -> filings -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

**`coverage.expected` is one filing per club with a scheduled game, per
practice day elapsed** — the phase doc's wording for this collector, unchanged.
Both factors are deliberately sourced from outside the injury feed:

* the clubs come from `adapters/schedule.py`, a *different* upstream, so a
  truncated injury feed cannot shrink its own denominator; and
* the days come from the clock, so a Friday on which nobody filed still expects
  three filings rather than looking like a Wednesday.

`EXPECTED_FLOOR` below is the third guard, for the case where the schedule
upstream is itself truncated: thirty-two clubs minus the six that are ever on a
bye in one week is **twenty-six**, the fewest that can ever owe a report in a
scored week. Multiplied by the number of practice days elapsed, that is the
smallest honest denominator this pass can have. It never lowers a genuine
count, so a real league expansion past thirty-two still reports honestly.

**An empty report is not an outage.** A club with nobody hurt files a report
listing nobody, and that counts as coverage *present* — see `report.py`, which
owns that distinction. A club that filed nothing is `coverage.missing` with
reason `report_not_published`. The two must never converge, which is why the
capture never infers "healthy" from an absence.

**This collector narrows `player_injury_status` to `roster-scope`'s
membership UNION matchup list, fetched from the lake before anything else is
touched.** `adapters/scope.py` is the seam; see its docstring for why the
union rather than membership alone. No scope means ZERO upstream calls —
neither the schedule feed nor the injury feed — and a `present: 0` envelope
for both signal types, never an unnarrowed fallback. `team_injury_report` is
deliberately **not** narrowed: it is keyed by team, not by player, and answers
"did this club file a report" for every scheduled club regardless of which of
its players are in scope — narrowing it by player membership would silently
drop a club's filing (and the coverage tracking built on it) whenever every
player it listed was out of scope.

**A failed capture still writes an envelope.** `fail_capture` writes one
`present: 0` envelope per signal type with a populated `errors` array, then
re-raises: the write makes the gap in the append-only lake explicit rather than
something a reader infers from absence, and the re-raise stops `CaptureState`
installing an empty capture over the last good one.

Every lake call goes off the event loop via `awrite`/`aread`/`alist_keys` —
`LakeWriter` is synchronous boto3, and the lake handed to this function raises
if it is called from the loop thread. The first `await` is the scope fetch,
early on purpose: it is both the narrowing seam's own fail-closed check AND
what lets uvicorn finish starting before any upstream latency is incurred.
"""

from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture
from collector_core.scope import ScopeUnavailable
from collector_core.streaming import UpstreamSchemaError

from .adapters.schedule import fetch_scheduled_games
from .adapters.scope import fetch_scope
from .adapters.upstream import UPSTREAM_ADAPTER, fetch_report_rows, source_ref
from .metrics import metrics
from .report import build_rows, practice_days_elapsed

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "MIN_SCHEDULED_TEAMS",
    "SIGNAL_TYPES",
    "capture_injury_report",
]

COLLECTOR_NAME = "injury-report"
CADENCE_CLASS = CadenceClass.VOLATILE
PLAYER_SIGNAL = "player_injury_status"
TEAM_SIGNAL = "team_injury_report"
SIGNAL_TYPES = (PLAYER_SIGNAL, TEAM_SIGNAL)

# Thirty-two clubs, minus the six that are ever on a bye in a single week. A
# declared constant, never a count of anything fetched: an injury feed that
# returns three clubs must report three of seventy-eight, not three of three.
MIN_SCHEDULED_TEAMS = 26

# Per signal type, per practice day. The pass multiplies by the number of days
# elapsed — see `pass_floor`. Both signal types share the number because both
# describe the same universe: a club's filing for a day either arrived or did
# not, and the player rows are what that filing contained.
EXPECTED_FLOOR: dict[str, int] = {
    PLAYER_SIGNAL: MIN_SCHEDULED_TEAMS,
    TEAM_SIGNAL: MIN_SCHEDULED_TEAMS,
}


def pass_floor(now: datetime) -> dict[str, int]:
    """The smallest honest `expected` for a pass at `now`, per signal type.

    Derived from the clock and a declared constant. Nothing here has seen the
    upstream, which is the point: this is the number a *total* outage floors
    to, and it is what makes a total outage report a ratio near zero instead of
    the 1.0 that `expected: 0` would produce.
    """
    days = len(practice_days_elapsed(now))
    return {signal_type: floor * days for signal_type, floor in EXPECTED_FLOOR.items()}


def _wall_clock() -> datetime:
    """Real elapsed time, for deadline enforcement only — distinct from `now`,
    which is the single instant the whole pass describes and stays frozen."""
    return datetime.now(tz=UTC)


def _reason(exc: BaseException) -> str | None:
    """`schema` for a drifted upstream, per the phase doc's failure table.

    `UpstreamSchemaError` subclasses `ValueError`, which the shared classifier
    reads as `malformed` — true but less useful. A renamed column and a
    non-numeric value are different incidents and page differently.
    """
    return "schema" if isinstance(exc, UpstreamSchemaError) else None


def _envelope(
    signal_type: str,
    *,
    now: datetime,
    upstream: Upstream,
    scope: dict,
    acc: CoverageAccumulator,
    signals: list[dict],
) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=COLLECTOR_NAME,
        signal_type=signal_type,
        captured_at=now,
        upstream=upstream,
        scope=scope,
        # Both envelopes carry the SAME coverage, deliberately. The unit of
        # completeness for this collector is a club's filing for a practice
        # day; the player rows are that filing's contents, so a second,
        # player-cardinality denominator would be a number nobody could state
        # the meaning of ("how many injured players should there have been?").
        # `errors` therefore also carries `scope_dropped_everything` into
        # `team_injury_report`, even though that reason is entirely about
        # `player_injury_status`'s own filter and `team_injury_report` never
        # narrows at all -- an accepted consequence of the one shared `errors`
        # channel, not a claim that the team-level signal narrowed.
        coverage=acc.result(),
        errors=acc.errors,
        signals=signals,
    )


async def capture_injury_report(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one envelope per signal type."""
    scope = {"season": season, "week": week}
    floor = pass_floor(now)
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER, fetched_at=now, source_ref=source_ref(season, week)
    )

    metrics.capture_attempt()

    # BEFORE any upstream call, deliberately: this is the whole of failing
    # closed. A pass that cannot narrow costs zero calls to the schedule or
    # injury feed, not "fetch everything" and not "fetch and filter to
    # nothing". This is also the first `await`, which is what lets uvicorn
    # finish starting before any upstream or lake latency is incurred.
    try:
        player_scope = await fetch_scope(lake, season, week)
    except ScopeUnavailable as exc:
        # `exc.reason` rather than a literal: `scope_unavailable` and
        # `scope_empty` have two different fixes, and collapsing them costs
        # an operator the one thing the envelope could have told them. Never
        # returns — see `fail_capture`.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            reason=exc.reason,
            expected=floor,
            source_ref=source_ref(season, week),
        )
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # The scope read is I/O, and `ScopeUnavailable` is only what
        # `ScopeClient` raises when the lake answered and had nothing usable.
        # The lake can also fail outright: botocore errors, a JSON decode
        # failure, or `ScopeClient._parse_captured_at` raising `ValueError` on
        # a timestamp it does not recognise. Without this arm every one of
        # those escapes this coroutine entirely — no `present: 0` envelope, no
        # `collector_capture_failures_total`, just a log line. No explicit
        # `reason=`: `fail_capture` falls back to
        # `CollectorMetrics.reason_for(exc)`, which classifies a decode or
        # timestamp failure as `malformed` and anything else as `unknown` —
        # both true, and neither mistakable for `scope_unavailable`.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            expected=floor,
            source_ref=source_ref(season, week),
        )

    try:
        scheduled = await fetch_scheduled_games(season, week, client=client)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Never returns. Writes a `present: 0` envelope per signal type first,
        # so the gap is explicit in the lake rather than inferred from absence.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            reason=_reason(exc),
            expected=floor,
            source_ref=source_ref(season, week),
        )

    days = practice_days_elapsed(now)
    # The observed denominator, floored. `len(scheduled)` is honest when the
    # schedule fetch was complete and too small when it was truncated, which is
    # exactly what the floor is for.
    owed = {
        signal_type: max(len(scheduled) * len(days), value)
        for signal_type, value in floor.items()
    }

    if deadline is not None and _wall_clock() >= deadline:
        # Out of budget before the report feed was even reached. Reported as a
        # failure rather than as an empty capture, so the last good capture
        # survives on `/signals` — a truncated pass installed over a complete
        # one turns a slow upstream into a loss of availability.
        exc = TimeoutError(f"capture deadline passed before {UPSTREAM_ADAPTER}")
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            reason="deadline_exceeded",
            expected=owed,
            source_ref=source_ref(season, week),
        )

    try:
        rows = await fetch_report_rows(
            season,
            week,
            client=client,
            teams=sorted(scheduled),
            days=list(days),
        )
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            reason=_reason(exc),
            expected=owed,
            source_ref=source_ref(season, week),
        )

    acc = CoverageAccumulator(floor=floor[TEAM_SIGNAL])
    aggregate = build_rows(scheduled, rows, now=now, acc=acc, metrics=metrics)

    # Narrowing's actual filter. Deliberately AFTER `build_rows` and against
    # its own coverage — `acc` above tracks one thing, "did this club file",
    # which is unaffected by which of its players are in scope, so an
    # individual player dropped here is neither `coverage.missing` nor its own
    # `errors` entry: the scope excluded it, which is not this collector's own
    # gap. Never applied to `aggregate.team_rows` — see `adapters/scope.py`
    # and this module's own docstring for why the team-level signal does not
    # narrow.
    offered_players = len(aggregate.player_rows)
    aggregate.player_rows = [
        row for row in aggregate.player_rows if row["player_id"] in player_scope.members
    ]

    if offered_players and not aggregate.player_rows:
        # The all-or-nothing case, and the one that is NOT silent: rows were
        # resolved and offered, and the union kept none of them.
        # `coverage.ratio` cannot reveal this — it is team-keyed, unaffected
        # by which players survive this filter — so `signals: []` on a pass
        # that genuinely had players to publish would otherwise look
        # identical to a quiet week with nobody hurt. That conflation is
        # exactly what `collector_core.failure` refuses to allow one level up
        # (a poll that fails still writes a loud, explicit envelope rather
        # than an inferred gap); this is the same refusal against a scope
        # that resolves but cannot ever intersect this collector's own ids —
        # see `adapters/identity.py`'s docstring for why that is the case
        # today. Guarded on `offered_players` so a pass with nothing to
        # narrow in the first place (no player rows at all) does not trip it.
        #
        # Inserted at the FRONT of `acc._errors`, not appended via
        # `add_error` — the same reasoning `CoverageAccumulator.errors`
        # itself applies to `below_expected_floor` ("First, not last, so it
        # survives capping"). `add_error` only appends, and `errors` caps at
        # `MAX_ERRORS` (50); a week where half the league's feed breaks can
        # produce up to ~78 `report_not_published` entries, which would push
        # this one off the list entirely if it were appended after them.
        # `CoverageAccumulator` has no public "insert first" API — only
        # `add_error`, which appends — so this reaches into `_errors`
        # directly rather than leaving the one entry that makes a total
        # narrowing drop visible to be silently capped away. Safe because
        # `errors` recomputes from `_errors` on every access (never cached)
        # and prepends its own `below_expected_floor` entry ahead of
        # whatever `_errors` contains, so this still lands second if a floor
        # shortfall is also present, first otherwise.
        acc._errors.insert(
            0,
            {
                "reason": "scope_dropped_everything",
                "detail": (
                    f"{offered_players} player row(s) resolved, 0 survived "
                    "the membership/matchup union"
                ),
            },
        )
        metrics.scope_dropped_everything()

    for day in days:
        # Every elapsed day, every pass, including the days on which nothing
        # was filed — an absent series and a healthy one look identical in
        # PromQL.
        metrics.filings(
            day,
            published=aggregate.filed_by_day.get(day, 0),
            with_games=aggregate.owed_by_day.get(day, 0),
        )

    envelopes = {
        PLAYER_SIGNAL: _envelope(
            PLAYER_SIGNAL,
            now=now,
            upstream=upstream,
            scope=scope,
            acc=acc,
            signals=aggregate.player_rows,
        ),
        TEAM_SIGNAL: _envelope(
            TEAM_SIGNAL,
            now=now,
            upstream=upstream,
            scope=scope,
            acc=acc,
            signals=aggregate.team_rows,
        ),
    }
    # `publish_capture` classifies and counts a failed write rather than
    # letting it propagate. That counter staying flat through an object-store
    # outage — while `/signals` quietly kept serving the last good capture —
    # is what this collector observed against a container whose MinIO endpoint
    # did not resolve; re-raising then also cost the capture that had just
    # succeeded.
    return await publish_capture(envelopes, lake=lake, metrics=metrics)
