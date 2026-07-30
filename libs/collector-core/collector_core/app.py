"""Process wiring shared by every collector.

`CollectorSpec` (`routes.py`) already carries what a single collector's five
standard routes need, and `run_capture_loop` (`scheduler.py`) already carries
the loop itself. What every collector process still assembled by hand,
identical across the fleet, was: parse the same four environment variables,
build `CaptureState`/`RefreshGate`/the lake writer, wire a lifespan that
starts and cleanly tears down the capture loop, mount the bearer middleware,
and include the router. `CollectorDescriptor` is the handful of values that
genuinely differ per collector; `build_collector_app` is everything else.
"""

import asyncio
import contextlib
import importlib
import os
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI

from .auth import DEFAULT_EXEMPT_PATHS, build_bearer_middleware
from .cadence import CadenceClass
from .lake import build_lake_writer_from_env
from .metrics import CollectorMetrics
from .refresh import RefreshGate
from .routes import CaptureFn, CaptureState, CollectorSpec, build_collector_router
from .scheduler import NextEventAt, run_capture_loop


def _no_event(state: CaptureState, now: datetime) -> datetime | None:
    """The default `next_event_at` when a descriptor declares none: the loop
    still runs on its base cadence, it simply never escalates."""
    return None


@dataclass(frozen=True)
class CollectorDescriptor:
    """The handful of values that differ per collector. Everything else --
    environment parsing, state/gate/lake construction, the lifespan, auth,
    and the standard five routes -- is `build_collector_app`'s job, and is
    identical across the fleet.
    """

    name: str
    cadence_class: CadenceClass
    signal_types: tuple[str, ...]
    supported_filters: tuple[str, ...]
    capture: CaptureFn
    signal_matches: Callable[[dict, Mapping[str, str]], bool]
    # Passed in rather than constructed here: the collector's own `capture`
    # already imports its metrics instance, and there must be exactly one.
    metrics: CollectorMetrics
    # A collector's own lookup from its cached state to the soonest event
    # still worth watching (weather's kickoff time, a future collector's own
    # perishable moment). `None` means the loop never escalates -- it is not
    # required.
    next_event_at: NextEventAt | None = None
    # A dotted module path (e.g. "weather.telemetry"), not a callable. A
    # callable would let a collector author import its telemetry module at
    # their own file's top level and hand the function in already bound --
    # legal Python, every test green, and it silently defeats the whole
    # point of the OTel guard below. A string cannot import anything by
    # itself: `importlib.import_module` only runs, and only inside the env
    # check, so the deferred-import invariant is structural, not a
    # convention every collector has to independently reinvent.
    telemetry_module: str | None = None
    client_factory: Callable[[], httpx.AsyncClient] | None = None


def build_collector_app(descriptor: CollectorDescriptor) -> FastAPI:
    """Build one collector's process: environment parsing, state, the
    lifespan, auth, and the standard five routes -- everything a collector's
    `main.py` used to assemble by hand, apart from the descriptor's own
    values and any extra routes a collector adds on top (e.g. weather's
    `/signals/convergence`).
    """
    refresh_floor = timedelta(
        seconds=int(os.getenv("REFRESH_MIN_INTERVAL_SECONDS", "300"))
    )
    # Bounds a single capture pass in wall-clock time -- background loop tick
    # and dispatched `/refresh` alike -- so total upstream failure costs at
    # most this much rather than `items x per-call timeout`. Read once here
    # (mirroring refresh_floor above) so both call sites share one value.
    capture_deadline = timedelta(
        seconds=int(os.getenv("CAPTURE_DEADLINE_SECONDS", "300"))
    )
    # The scope the background loop captures, and what a bare `POST /refresh`
    # (no body scope) falls back to -- read once here so both share one
    # source, via `CollectorSpec.default_scope`.
    capture_season = int(os.getenv("CAPTURE_SEASON", "2026"))
    capture_week = int(os.getenv("CAPTURE_WEEK", "1"))

    state = CaptureState()
    refresh_gate = RefreshGate(refresh_floor)
    lake = build_lake_writer_from_env()

    spec_kwargs = dict(
        name=descriptor.name,
        cadence_class=descriptor.cadence_class,
        signal_types=descriptor.signal_types,
        supported_filters=descriptor.supported_filters,
        capture=descriptor.capture,
        state=state,
        lake=lake,
        metrics=descriptor.metrics,
        refresh_gate=refresh_gate,
        signal_matches=descriptor.signal_matches,
        default_scope={"season": capture_season, "week": capture_week},
        capture_deadline=capture_deadline,
    )
    # Only override CollectorSpec's own default when the descriptor supplies
    # one, so a collector that does not care keeps today's transport exactly.
    if descriptor.client_factory is not None:
        spec_kwargs["client_factory"] = descriptor.client_factory
    spec = CollectorSpec(**spec_kwargs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The import happens here, inside the guard, and nowhere else --
        # `descriptor.telemetry_module` being a string rather than a
        # pre-imported callable is what makes that non-negotiable.
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") and descriptor.telemetry_module:
            telemetry = importlib.import_module(descriptor.telemetry_module)
            telemetry.setup_telemetry(app)

        # Guarded so tests and local runs do not reach an upstream on import.
        task: asyncio.Task | None = None
        if os.getenv("CAPTURE_ENABLED", "").lower() in {"1", "true", "yes"}:
            task = asyncio.create_task(
                run_capture_loop(
                    state,
                    capture=descriptor.capture,
                    lake=lake,
                    season=capture_season,
                    week=capture_week,
                    cadence_class=descriptor.cadence_class,
                    next_event_at=descriptor.next_event_at or _no_event,
                    metrics=descriptor.metrics,
                    capture_deadline=capture_deadline,
                    client_factory=spec.client_factory,
                )
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # A refresh dispatched right before shutdown must not outlive
            # the app.
            await state.cancel_in_flight()

    app = FastAPI(lifespan=lifespan)
    # A call, not a decorator, so no collector's routes module needs to
    # import app assembly -- one-way dependency, same shape as the bearer
    # middleware it replaces in each collector's former auth.py.
    app.middleware("http")(
        build_bearer_middleware(descriptor.metrics, DEFAULT_EXEMPT_PATHS)
    )
    app.include_router(build_collector_router(spec))
    # A collector's own extra routes (e.g. weather's `/signals/convergence`)
    # reach the lake and the collector name through here, rather than a
    # module-level global only that one service's main.py could see.
    app.state.collector_spec = spec

    return app
