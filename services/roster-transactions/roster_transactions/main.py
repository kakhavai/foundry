"""roster-transactions's process wiring: the descriptor, and nothing else yet.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from collector_core.app import CollectorDescriptor, build_collector_app

from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    capture_roster_transactions,
)
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_roster_transactions,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`: the loop runs on its cadence class's base
        # interval and never escalates. Add one only if this collector has a
        # genuinely perishable moment to escalate toward — weather's
        # `next_kickoff` is the only example in the fleet.
    )
)

# Routes beyond the standard five go below this line, as plain `@app.get` /
# `@app.post` handlers. Reach the lake and the collector name through
# `app.state.collector_spec` — never a module-level global, which only this
# file could see. Anything that touches the lake must be offloaded with
# `asyncio.to_thread` (or `collector_core.lake`'s awrite/aread/alist_keys);
# the lake you are handed refuses a synchronous call from the loop thread.
#
# Remember to publish any new path in this collector's Helm values under
# `gateway.publicPaths`, or it 404s at the gateway while working in-cluster.
