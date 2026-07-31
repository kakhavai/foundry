"""Capture orchestration: ledger -> depth charts -> resolution -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage degrades freshness rather than availability.

Two behaviours here differ from `weather`'s `capture_week` and both are
deliberate:

1. **A depth-chart fetch failure does not raise.** It produces `charts = {}`
   and flows through the *same* resolution loop, so every human slot fails
   with a classified reason, the 32 config-derived `team_defense` slots still
   fill, and an envelope with a populated `errors` array is written.
   `weather` used to raise on a schedule-fetch failure and write nothing,
   which left a gap in the lake that had to be inferred from absence — the
   exact thing the Phase 8 spec says must never be inferred. Wave 0 fixed
   that generically (`collector_core.failure.fail_capture`); this collector's
   behaviour was already the one that matched the contract.

2. **The version ledger is read before anything else**, and a failure to read
   it aborts into an explicit `scope_version: 0` envelope rather than
   restarting the version sequence at 1.

**Every lake call goes off the event loop**, via `collector_core.lake`'s
`awrite`/`aread`/`alist_keys` or an explicit `asyncio.to_thread` around a
whole synchronous helper. `LakeWriter` is boto3, which is synchronous, so
calling it directly from this coroutine runs it on the event loop thread and
blocks the whole process — including `/health`.

That is not a theoretical concern, it is what broke this service's first
deploy. `capture_scope` is started as a task by `build_collector_app`'s
lifespan, and the ledger read below is its *first* statement. With no `await`
before it, the task ran start-to-finish on the event loop before uvicorn
could finish starting, so "Application startup complete" was never reached,
`/health` never answered, the readiness probe never passed, and
`kubectl rollout status` timed out at 180s. botocore's defaults make the
worst case minutes rather than seconds: a 60-second connect timeout, retried.

`weather` had the same synchronous lake writer and was unaffected only by
accident of ordering. Relying on that ordering is exactly the kind of
invariant nobody can see, so Wave 0 made it structural rather than a
convention: `build_collector_app` now hands every collector an
`EventLoopGuardedLake`, which raises if a synchronous method is called from
the loop thread. Forgetting is now an immediate, classified error instead of
a silent readiness inversion.

The contract this restores is the one the collector spec states outright: an
upstream outage degrades *freshness*, not *availability*. A collector whose
readiness depends on the object store answering has inverted that.
"""

import asyncio
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.failure import UNKNOWN_EXPECTED_FLOOR
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from .adapters.depth_chart import DepthChart, depth_chart_url, fetch_depth_charts
from .adapters.identity import build_resolver
from .matchups import MATCHUP_SIGNAL, resolve_matchup_slots
from .metrics import metrics
from .rules import expected_slots
from .scope import (
    CHANGE_SIGNAL,
    MEMBERSHIP_SIGNAL,
    NO_VERSION,
    LedgerUnavailable,
    build_change_events,
    count_stale_depth_charts,
    distinct_rank_violations,
    load_previous_scope,
    reconcile_missed_producers,
    resolve_membership,
)

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "SIGNAL_TYPES",
    "capture_scope",
]

COLLECTOR_NAME = "roster-scope"
CADENCE_CLASS = CadenceClass.WEEKLY
SIGNAL_TYPES = (MEMBERSHIP_SIGNAL, CHANGE_SIGNAL, MATCHUP_SIGNAL)
UPSTREAM_ADAPTER = "nflverse-depth-charts"


def _wall_clock() -> datetime:
    """Real elapsed time, for deadline enforcement only.

    Distinct from `capture_scope`'s `now`, which is the single instant the
    whole pass *describes* and is deliberately frozen for its duration. A
    deadline has to be checked against a clock that actually advances.
    """
    return datetime.now(tz=UTC)


def _envelope(
    signal_type: str,
    *,
    now: datetime,
    upstream: Upstream,
    scope: dict,
    coverage: Coverage,
    errors: list[dict],
    signals: list[dict],
) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=COLLECTOR_NAME,
        signal_type=signal_type,
        captured_at=now,
        upstream=upstream,
        scope=scope,
        coverage=coverage,
        errors=errors,
        signals=signals,
    )


def _matchup_rows(charts: dict[str, DepthChart]) -> list[dict]:
    """Flatten every team's depth-chart rows into the shape
    `resolve_matchup_slots` expects.

    `DepthChartRow` already carries what a matchup row needs, just under
    this module's own field names (`position_raw`, `depth_order`,
    `name_raw`) rather than the canonical ones -- canonicalizing the raw
    label happens inside `matchups.py` itself, the same way `scope.py`
    canonicalizes a player position from the raw chart rather than trusting
    the adapter to have done it.
    """
    return [
        {
            "team": row.team,
            "position": row.position_raw,
            "depth_rank": row.depth_order,
            "name": row.name_raw,
        }
        for chart in charts.values()
        for row in chart.rows
    ]


async def _persist(
    envelopes: dict[str, Envelope], lake: LakeWriter
) -> dict[str, Envelope]:
    """Write both envelopes, off the event loop — see the module docstring.

    A thin binding of this collector's `metrics` onto the shared tail, which
    also decides what a failed write means: the capture succeeded, so the
    envelopes are returned and the failure is counted rather than raised.
    """
    return await publish_capture(envelopes, lake=lake, metrics=metrics)


async def capture_scope(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Resolve one week's scope into a new, immutable `scope_version`."""
    slots = expected_slots()
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=depth_chart_url(season),
    )

    # `to_thread` rather than a direct call, and deliberately the very first
    # thing this coroutine does: it is both the offload AND the `await` that
    # lets uvicorn finish starting before any lake latency is incurred.
    try:
        previous = await asyncio.to_thread(
            load_previous_scope, lake, COLLECTOR_NAME, season, week
        )
    except LedgerUnavailable as exc:
        # No version can be minted. Minting 1 anyway would collide with a real
        # version 1 already in the lake and break the immutable-additive model
        # every consumer pins against, so the pass reports itself as producing
        # nothing rather than producing something wrong.
        metrics.capture_failure(exc)
        metrics.missed_producers(0)
        scope = {"season": season, "week": week, "scope_version": NO_VERSION}
        errors = [{"reason": "ledger_unavailable", "detail": str(exc)}]
        empty = Coverage(expected=len(slots), present=0, missing=sorted(slots))
        # No chart was ever fetched on this path -- the ledger read is the
        # very first thing this coroutine does -- so the matchup accumulator
        # sees zero rows too. Built through the real function rather than a
        # second hand-rolled empty Coverage, so the 608-key universe it seeds
        # cannot drift from the one the success path uses.
        _, matchup_acc = await resolve_matchup_slots(
            [], season=season, week=week, now=now, resolver=build_resolver(client)
        )
        return await _persist(
            {
                MEMBERSHIP_SIGNAL: _envelope(
                    MEMBERSHIP_SIGNAL,
                    now=now,
                    upstream=upstream,
                    scope=scope,
                    coverage=empty,
                    errors=errors,
                    signals=[],
                ),
                CHANGE_SIGNAL: _envelope(
                    CHANGE_SIGNAL,
                    now=now,
                    upstream=upstream,
                    scope=scope,
                    # Floored rather than 0/0. The change stream has no
                    # a-priori cardinality on the *success* path, where 0/0
                    # correctly reads as "nothing changed". On this path
                    # nothing was captured at all, and `Coverage.ratio`
                    # returns 1.0 for expected=0 -- a pass that produced
                    # nothing would otherwise report perfect coverage, which
                    # is the precise thing the coverage block exists to catch.
                    coverage=Coverage(
                        expected=UNKNOWN_EXPECTED_FLOOR, present=0, missing=[]
                    ),
                    errors=errors,
                    signals=[],
                ),
                MATCHUP_SIGNAL: _envelope(
                    MATCHUP_SIGNAL,
                    now=now,
                    upstream=upstream,
                    scope=scope,
                    coverage=matchup_acc.result(),
                    errors=errors,
                    signals=[],
                ),
            },
            lake,
        )

    version = previous.version + 1
    scope = {"season": season, "week": week, "scope_version": version}

    # Built before the fetch so every error this pass produces -- the fetch
    # failure, the per-slot resolution failures, the rank violations -- lands
    # in one accumulator and is capped in one place. `slots` is the config's
    # own 416-slot universe, so `expected` here never derives from what the
    # upstream returned and needs no floor on top of it.
    acc = CoverageAccumulator(slots)

    metrics.capture_attempt()
    resolver = build_resolver(client)
    fetch_error: Exception | None = None
    try:
        charts = await fetch_depth_charts(season, week, client, now=now)
    except Exception as exc:  # noqa: BLE001 — classified, not fatal
        metrics.capture_failure(exc)
        charts = {}
        fetch_error = exc
        acc.add_error(metrics.reason_for(exc), "depth_chart_fetch")

    metrics.stale_depth_charts(count_stale_depth_charts(charts, now))

    rows = await resolve_membership(
        charts=charts,
        resolver=resolver,
        previous=previous,
        season=season,
        week=week,
        version=version,
        acc=acc,
        deadline=deadline,
        clock=_wall_clock,
    )
    for violation in distinct_rank_violations(rows):
        acc.add_error(violation["reason"], violation.get("detail", ""))
    errors = acc.errors

    events = build_change_events(previous, rows, version=version, occurred_at=now)

    # Recorded every pass, including when it is zero. Nothing supplies
    # `usage_rows` yet — realized usage arrives with `usage-share` at 8B — so
    # this is structurally always 0 today. It exists now because an absent
    # series and a healthy one are indistinguishable in PromQL, and this
    # metric's only job is to be alertable on `> 0`.
    metrics.missed_producers(len(reconcile_missed_producers([], rows)))

    # A second, independent accumulator -- see the module and matchups.py
    # docstrings. Sharing `acc` here would blend a matchup outage into the
    # membership envelope's own ratio and vice versa; a resolution failure in
    # one must never mask a healthy result in the other.
    matchup_signals, matchup_acc = await resolve_matchup_slots(
        _matchup_rows(charts),
        season=season,
        week=week,
        now=now,
        resolver=resolver,
    )
    if fetch_error is not None:
        # Both envelopes are built from the same fetch, so a fetch failure
        # belongs in both accumulators' errors -- recorded independently in
        # each rather than by sharing one accumulator between them.
        matchup_acc.add_error(metrics.reason_for(fetch_error), "depth_chart_fetch")

    return await _persist(
        {
            MEMBERSHIP_SIGNAL: _envelope(
                MEMBERSHIP_SIGNAL,
                now=now,
                upstream=upstream,
                scope=scope,
                coverage=acc.result(),
                errors=errors,
                signals=rows,
            ),
            CHANGE_SIGNAL: _envelope(
                CHANGE_SIGNAL,
                now=now,
                upstream=upstream,
                scope=scope,
                # A change stream has no a-priori cardinality: the transitions
                # are *derived* from two membership versions, not captured, so
                # there is no expectation for them to fall short of. The
                # meaningful coverage number for this collector is the
                # membership envelope's, above.
                coverage=Coverage(expected=0, present=0, missing=[]),
                errors=errors,
                signals=events,
            ),
            MATCHUP_SIGNAL: _envelope(
                MATCHUP_SIGNAL,
                now=now,
                upstream=upstream,
                scope=scope,
                coverage=matchup_acc.result(),
                errors=matchup_acc.errors,
                signals=matchup_signals,
            ),
        },
        lake,
    )
