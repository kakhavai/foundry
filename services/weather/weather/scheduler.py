"""weather's binding to the shared capture loop.

The loop itself, the escalation decision, staleness recording, and surviving
a failed capture are fleet machinery -- identical for every collector -- and
live in `collector_core.scheduler`. `next_kickoff` is the one weather-shaped
piece: it reads `forecast_valid_at` out of weather's own `venue_forecast_kickoff`
signals, which no other collector has, so it stays here and is handed to the
shared loop as a `next_event_at` callable rather than being baked into the
library.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from collector_core.lake import LakeWriter
from collector_core.routes import DEFAULT_CAPTURE_DEADLINE, CaptureState
from collector_core.scheduler import interval_for_state as _interval_for_state
from collector_core.scheduler import run_capture_loop as _run_capture_loop

from .capture import CADENCE_CLASS, capture_week
from .metrics import metrics

# How long after kickoff a game still counts as in progress. Games run roughly
# three hours; beyond that the dense cadence has nothing left to observe.
GAME_DURATION = timedelta(hours=4)


def next_kickoff(state: CaptureState, now: datetime) -> datetime | None:
    """The soonest kickoff still worth watching -- upcoming, or in progress.

    A game in progress returns a past timestamp, which the shared loop reads
    as a negative delta and keeps escalated. That is deliberate: the window
    closes at the final whistle, not at kickoff.
    """
    envelope = state.envelopes.get("venue_forecast_kickoff")
    if envelope is None:
        return None

    candidates: list[datetime] = []
    for signal in envelope.signals:
        raw = signal.get("forecast_valid_at")
        if not raw:
            continue
        kickoff = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if kickoff + GAME_DURATION >= now:
            candidates.append(kickoff)
    return min(candidates) if candidates else None


def interval_for_state(state: CaptureState, now: datetime) -> timedelta:
    """weather's cadence class and kickoff lookup, bound to the shared
    `interval_for_state` -- the escalation decision itself lives there."""
    return _interval_for_state(
        state, now, cadence_class=CADENCE_CLASS, next_event_at=next_kickoff
    )


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


async def run_capture_loop(
    state: CaptureState,
    *,
    lake: LakeWriter,
    season: int,
    week: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] | None = None,
    capture_deadline: timedelta | None = DEFAULT_CAPTURE_DEADLINE,
    client_factory: Callable[[], httpx.AsyncClient] = _client_factory,
) -> None:
    """weather's capture loop -- the shared loop with weather's cadence
    class, capture function, metrics, and kickoff lookup bound in, so
    `main.py`'s lifespan needs to know none of that wiring."""
    kwargs = {} if clock is None else {"clock": clock}
    await _run_capture_loop(
        state,
        capture=capture_week,
        lake=lake,
        season=season,
        week=week,
        cadence_class=CADENCE_CLASS,
        next_event_at=next_kickoff,
        metrics=metrics,
        sleep=sleep,
        capture_deadline=capture_deadline,
        client_factory=client_factory,
        **kwargs,
    )
