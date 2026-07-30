"""Capture orchestration: ledger -> depth charts -> resolution -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage degrades freshness rather than availability.

Two behaviours here differ from `weather`'s `capture_week` and both are
deliberate:

1. **A depth-chart fetch failure does not raise.** It produces `charts = {}`
   and flows through the *same* resolution loop, so every human slot fails
   with a classified reason, the 32 config-derived `team_defense` slots still
   fill, and an envelope with a populated `errors` array is written.
   `weather` raises on a schedule-fetch failure and writes nothing, which
   leaves a gap in the lake that has to be inferred from absence — the exact
   thing the Phase 8 spec says must never be inferred. This collector's
   behaviour is the one that matches the contract.

2. **The version ledger is read before anything else**, and a failure to read
   it aborts into an explicit `scope_version: 0` envelope rather than
   restarting the version sequence at 1.

**Every lake call goes through `asyncio.to_thread`.** `LakeWriter` is boto3,
which is synchronous, so calling it directly from this coroutine runs it on
the event loop thread and blocks the whole process — including `/health`.

That is not a theoretical concern, it is what broke this service's first
deploy. `capture_scope` is started as a task by `build_collector_app`'s
lifespan, and the ledger read below is its *first* statement. With no `await`
before it, the task ran start-to-finish on the event loop before uvicorn
could finish starting, so "Application startup complete" was never reached,
`/health` never answered, the readiness probe never passed, and
`kubectl rollout status` timed out at 180s. botocore's defaults make the
worst case minutes rather than seconds: a 60-second connect timeout, retried.

`weather` has the same synchronous lake writer and is unaffected only by
accident of ordering — its capture's first statement is
`await fetch_schedule(...)`, which yields, so its pod is Ready long before it
touches the lake. Relying on that ordering is exactly the kind of invariant
nobody can see, which is why every call here is explicit about it.

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
from collector_core.lake import LakeWriter

from .adapters.depth_chart import depth_chart_url, fetch_depth_charts
from .adapters.identity import build_resolver
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
SIGNAL_TYPES = (MEMBERSHIP_SIGNAL, CHANGE_SIGNAL)
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


async def _persist(
    envelopes: dict[str, Envelope], lake: LakeWriter
) -> dict[str, Envelope]:
    """Write both envelopes, off the event loop — see the module docstring."""
    for signal_type, envelope in envelopes.items():
        try:
            await asyncio.to_thread(lake.write, envelope)
        except Exception as exc:  # noqa: BLE001 — total-outage path (lake unreachable)
            metrics.capture_failure(exc)
            raise
        metrics.coverage(signal_type, envelope.coverage.ratio)
    return envelopes


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
                    coverage=Coverage(expected=0, present=0, missing=[]),
                    errors=errors,
                    signals=[],
                ),
            },
            lake,
        )

    version = previous.version + 1
    scope = {"season": season, "week": week, "scope_version": version}
    errors: list[dict] = []

    metrics.capture_attempt()
    try:
        charts = await fetch_depth_charts(season, week, client, now=now)
    except Exception as exc:  # noqa: BLE001 — classified, not fatal
        metrics.capture_failure(exc)
        charts = {}
        errors.append(
            {"reason": metrics.reason_for(exc), "detail": "depth_chart_fetch"}
        )

    metrics.stale_depth_charts(count_stale_depth_charts(charts, now))

    acc = CoverageAccumulator(slots)
    rows = await resolve_membership(
        charts=charts,
        resolver=build_resolver(client),
        previous=previous,
        season=season,
        week=week,
        version=version,
        acc=acc,
        deadline=deadline,
        clock=_wall_clock,
    )
    errors.extend(acc.errors)
    errors.extend(distinct_rank_violations(rows))

    events = build_change_events(previous, rows, version=version, occurred_at=now)

    # Recorded every pass, including when it is zero. Nothing supplies
    # `usage_rows` yet — realized usage arrives with `usage-share` at 8B — so
    # this is structurally always 0 today. It exists now because an absent
    # series and a healthy one are indistinguishable in PromQL, and this
    # metric's only job is to be alertable on `> 0`.
    metrics.missed_producers(len(reconcile_missed_producers([], rows)))

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
        },
        lake,
    )
