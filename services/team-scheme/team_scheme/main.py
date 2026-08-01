"""Process wiring: the descriptor, and nothing else.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.

**No routes beyond the standard five, deliberately.** The original
`coaching-scheme` spec had a `GET /teams/{team_id}/revisions` timeline route;
it served the staff-revision timeline and moved to the deferred
`coaching-staff` collector along with everything that made it meaningful. With
rates keyed to a team-season there is exactly one row per team per season, so
`/signals?team_id=KC` already is the whole answer and a bespoke route would be
a second way to spell it. There is also no `smoke.sh` — the standard contract
surface is asserted for every registered collector automatically, and a
collector with no extra routes writes no hook.
"""

from collector_core.app import CollectorDescriptor, build_collector_app

from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    capture_team_scheme,
)
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_team_scheme,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`: `seasonal` runs on its base interval and has
        # nothing perishable to escalate toward. The rates move after a week's
        # games, and the nflverse release lands some hours later on no
        # announced schedule, so there is no instant to escalate *toward* —
        # which is exactly the case the field does not serve.
    )
)
