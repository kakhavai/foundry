"""roster-scope's process wiring: the descriptor and its three extra routes.

Everything else — environment parsing, state, the capture loop, bearer auth,
and the standard five routes — lives in `collector_core.app`.
"""

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import Query

from .capture import CADENCE_CLASS, COLLECTOR_NAME, SIGNAL_TYPES, capture_scope
from .metrics import metrics
from .scope import (
    SUPPORTED_FILTERS,
    scope_diff_view,
    scope_players_view,
    scope_rules_view,
    signal_matches,
)

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_scope,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `next_event_at`: weekly cadence with nothing perishable to
        # escalate toward. The loop runs on its base interval.
        #
        # A dotted string, not a callable. `build_collector_app` imports it
        # with importlib *inside* the OTEL_EXPORTER_OTLP_ENDPOINT guard;
        # importing `.telemetry` here and passing the function would defeat
        # that guard while leaving every test green.
        telemetry_module="roster_scope.telemetry",
    )
)


@app.get("/scope/players")
async def scope_players(scope_version: int | None = None, status: str | None = None):
    """The concrete resolved list — the route every other collector calls."""
    return scope_players_view(
        app.state.collector_spec, scope_version=scope_version, status=status
    )


@app.get("/scope/rules")
async def scope_rules():
    """The config as *applied*, including rules that produced zero slots."""
    return scope_rules_view(app.state.collector_spec)


@app.get("/scope/diff")
async def scope_diff(
    version_from: int = Query(..., alias="from"), to: int = Query(...)
):
    """Membership transitions between two versions."""
    return scope_diff_view(
        app.state.collector_spec, version_from=version_from, version_to=to
    )
