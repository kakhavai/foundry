"""schedule-context's process wiring: the descriptor, and nothing else.

Its spec declares no routes beyond the standard five, so there is nothing
below the `build_collector_app` call and no `smoke.sh` — the shared contract
surface is asserted for every registered collector automatically.

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
    capture_schedule_context,
)
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_schedule_context,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`, and therefore no `scheduler.py`. weather has one
        # because a forecast decays right up to kickoff, so the closer the
        # game the more a re-capture is worth. Nothing here works that way:
        # rest, travel and bye adjacency are settled the moment the kickoff
        # time is published, days ahead, and they do not change again as the
        # game approaches. What *does* change them is a flex or a
        # postponement, which arrives on the league's schedule rather than on
        # the game's — which is precisely what the weekly cadence's daily
        # re-derivation is for. Escalating toward kickoff would poll a 2 MB
        # feed harder for an answer that stopped moving days earlier.
    )
)
