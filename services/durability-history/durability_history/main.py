"""Process wiring: the descriptor and `GET /signals/return-profile`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import HTTPException, Query

from . import derive
from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    INJURY_HISTORY,
    SIGNAL_TYPES,
    capture_durability_history,
)
from .events import BODY_PARTS
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_durability_history,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`: the loop runs on its cadence class's base interval
        # and never escalates. A durability history has no perishable moment.
    )
)


@app.get("/signals/return-profile")
async def return_profile(
    player_id: str = Query(
        description="Canonical `fdy-` player id, as published in `/signals`."
    ),
    body_part: str = Query(
        description=(
            "One of the spec's ten body parts. Case-insensitive. An unknown "
            "value is 422, never an empty distribution."
        )
    ),
):
    """The conditional return DISTRIBUTION for one player and one body part.

    The route the spec asks for, and its sentence gives the reason: it is "the
    shape the generator wants when a player is mid-recovery and a point estimate
    would hide the variance". So three things are published together, because a
    point estimate alone is exactly what the route exists to avoid:

    * **this player's own returns** at that body part, every resolved event with
      its `days_to_return`, plus the quantiles over them;
    * **the captured population's** returns at the same body part, so a
      three-observation median has something to be read against;
    * **`min_sample_events`** and the player's own `sample_size_events`, so a
      null aggregate on the `/signals` row is distinguishable from a bug.

    Unresolved events are reported as a count rather than dropped: a player still
    out is the single most relevant fact when the question is "when does he come
    back", and silently excluding him would make a player who has never returned
    look like one with no history.

    Served from the in-memory capture rather than from the lake, deliberately:
    the answer must describe the same rows `/signals` is serving right now. A
    lake-backed answer could only describe whichever pass last wrote an object,
    and the digest gate means that is routinely not this one.

    `spec` is reached through `app.state.collector_spec` rather than a
    module-level global, per the fleet convention, so this route reports the
    collector name the router itself is serving under.
    """
    spec = app.state.collector_spec

    canonical = (body_part or "").strip().lower()
    if canonical not in BODY_PARTS:
        # 422, not an empty distribution. An unknown body part and a body part
        # this player has never injured are different facts, and a client that
        # gets `count: 0` for a typo will file it as "he has never hurt that".
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown body_part {body_part!r}; expected one of "
                f"{', '.join(BODY_PARTS)}"
            ),
        )

    envelope = spec.state.envelopes.get(INJURY_HISTORY)
    if envelope is None:
        # 404 rather than an empty body: no capture has landed yet, which is a
        # different fact from "this player has no events at that body part".
        raise HTTPException(status_code=404, detail=f"no {INJURY_HISTORY} capture yet")

    row = next(
        (r for r in envelope.signals if r.get("player_id") == player_id),
        None,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"player_id {player_id!r} is not in the current "
                f"{INJURY_HISTORY} capture"
            ),
        )

    def _at_body_part(signal_row: dict) -> list[dict]:
        return [
            event
            for event in signal_row.get("injury_events", [])
            if event.get("body_part") == canonical
        ]

    own = _at_body_part(row)
    own_resolved = [e["days_to_return"] for e in own if e["days_to_return"] is not None]
    population = [
        event["days_to_return"]
        for signal_row in envelope.signals
        for event in _at_body_part(signal_row)
        if event["days_to_return"] is not None
    ]

    return {
        "collector": spec.name,
        "player_id": player_id,
        "body_part": canonical,
        "season": envelope.scope.get("season"),
        "week": envelope.scope.get("week"),
        "captured_at": envelope.captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "history_seasons": envelope.scope.get("history_seasons"),
        "recurrence_window_days": envelope.scope.get("recurrence_window_days"),
        "min_sample_events": derive.MIN_SAMPLE_EVENTS,
        "sample_size_events": row.get("sample_size_events"),
        "player": {
            "distribution": derive.distribution([float(v) for v in own_resolved]),
            "unresolved_events": len(own) - len(own_resolved),
            "events": own,
        },
        "population": {
            "distribution": derive.distribution([float(v) for v in population]),
            "players": len(
                [
                    r
                    for r in envelope.signals
                    if any(e["days_to_return"] is not None for e in _at_body_part(r))
                ]
            ),
        },
    }
