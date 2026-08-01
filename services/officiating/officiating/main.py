"""officiating's process wiring: the descriptor and `GET /crews/{crew_id}`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import HTTPException, Path

from .capture import (
    ASSIGNMENT,
    CADENCE_CLASS,
    COLLECTOR_NAME,
    RATES,
    SIGNAL_TYPES,
    capture_officiating,
)
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

# `<season>-ref<official_id>`, the shape `officiating.crews.crew_id_for`
# produces. Enforced on the path so a malformed id is a 422 from FastAPI's own
# validation rather than a 404 — "you asked wrongly" and "there is no such
# crew" are different answers, and collapsing them sends a client looking for a
# data problem that does not exist.
CREW_ID_PATTERN = r"^\d{4}-ref\d+$"

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_officiating,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`: `weekly` runs on its base interval of one day and
        # has nothing perishable to escalate toward. A crew's season-to-date
        # penalty rate does not become more urgent as kickoff approaches, and
        # the assignment feed that would is post-game.
    )
)


@app.get("/crews/{crew_id}")
async def crew_profile(crew_id: str = Path(pattern=CREW_ID_PATTERN)):
    """One crew's rate profile and member roster, independent of any game.

    The route the spec asks for, and its reason is in its own sentence: *"so a
    consumer can evaluate a crew before assignments publish"*. Every other way
    to reach a crew's rates goes through `/signals`, which is organised by
    game — useless in the week before a game is assigned, which is exactly when
    a consumer wants to know who might work it.

    Served from the in-memory capture, not the lake. The lake would answer only
    with what some past pass happened to write, and would cost an object-store
    round trip on a route whose whole purpose is a cheap lookup. This is the
    same call `venue`'s extra route makes, for the same reason.

    The roster is assembled from the crew's `game_crew_assignment` rows rather
    than stored separately, because a crew's membership is *observed* — it is
    whoever has actually worked its games — and a second copy could only drift
    from the assignments it was derived from. `games_worked` per member is what
    makes churn legible here without a second request: a member on 2 of 16 is a
    substitute, and the crew-level `crew_continuity_pct` alone cannot say which
    member dragged it down.

    `spec` is reached through `app.state.collector_spec` rather than a
    module-level global, per the fleet convention, so this route reports the
    collector name the router itself is serving under.
    """
    spec = app.state.collector_spec

    rates_envelope = spec.state.envelopes.get(RATES)
    assignment_envelope = spec.state.envelopes.get(ASSIGNMENT)

    rates_row = next(
        (
            row
            for row in (rates_envelope.signals if rates_envelope else [])
            if row.get("crew_id") == crew_id
        ),
        None,
    )
    assignments = [
        row
        for row in (assignment_envelope.signals if assignment_envelope else [])
        if row.get("crew_id") == crew_id
    ]

    if rates_row is None and not assignments:
        # 404, not an empty 200. An unknown crew id and a crew with no data are
        # different facts, and a consumer that gets an empty profile for a
        # typo'd id will file it as "that crew has no history" rather than as
        # its own bug. A crew known only from assignments (its rates refused,
        # or play-by-play unavailable) is NOT unknown and answers 200 with a
        # null profile — that is the collector working.
        raise HTTPException(status_code=404, detail=f"no crew with id {crew_id!r}")

    return {
        "collector": spec.name,
        "crew_id": crew_id,
        "referee_name": _referee_name(rates_row, assignments),
        "rates": rates_row,
        "members": _members(assignments),
        "games_assigned": len(assignments),
        "last_capture_at": (
            spec.state.last_capture_at.isoformat()
            if spec.state.last_capture_at
            else None
        ),
    }


def _referee_name(rates_row: dict | None, assignments: list[dict]) -> str | None:
    if rates_row and rates_row.get("referee_name"):
        return rates_row["referee_name"]
    for row in assignments:
        if row.get("referee_name"):
            return row["referee_name"]
    return None


def _members(assignments: list[dict]) -> list[dict]:
    """The crew's observed roster, most-frequent first.

    Deduplicated by `official_id` rather than by name — the whole reason this
    collector resolves ids is that the same person appears under more than one
    display form across feeds.
    """
    seen: dict[str, dict] = {}
    for row in assignments:
        for member in row.get("crew_members", []):
            entry = seen.setdefault(
                member["official_id"],
                {
                    "official_id": member["official_id"],
                    "name": member["name"],
                    "position": member["position"],
                    "games_worked": 0,
                },
            )
            entry["games_worked"] += 1
    return sorted(
        seen.values(), key=lambda m: (-m["games_worked"], m["position"], m["name"])
    )
