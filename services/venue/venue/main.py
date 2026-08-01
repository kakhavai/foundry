"""venue's process wiring: the descriptor and `GET /venues/{id}/revisions`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from datetime import date

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import HTTPException, Query

from . import reference
from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    build_static_row,
    capture_venue,
)
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_venue,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`: `static reference` runs on its base interval of
        # one day and has nothing perishable to escalate toward. A venue's
        # roof does not become more urgent as kickoff approaches; that is
        # `weather`'s job about the same building.
    )
)


@app.get("/venues/{venue_id}/revisions")
async def venue_revisions(
    venue_id: str,
    on: date | None = Query(
        default=None,
        description=(
            "Resolve the single revision true on this date (YYYY-MM-DD) "
            "instead of returning the whole history."
        ),
    ),
):
    """One venue's full ordered revision history.

    The route the spec asks for, and the reason it exists is in its own
    sentence: "so a consumer can resolve the record that was true on a given
    date without scanning the lake". `?on=` is that resolution done here, since
    a consumer that has to reimplement half-open window arithmetic will get the
    install-date boundary wrong in one direction or the other.

    Served from the committed reference table rather than from the lake, and
    that is the point: the table is the authority, it is in-process, and
    answering from it needs no prefix scan and no object-store round trip. A
    lake-backed answer would also be *wrong* in a specific way — it could only
    return revisions some past capture happened to publish, so a revision added
    to the table would be invisible here until the next successful pass.

    `spec` is reached through `app.state.collector_spec` rather than a
    module-level global, per the fleet convention, so this route reports the
    collector name the router itself is serving under.
    """
    spec = app.state.collector_spec

    history = reference.revisions_for(venue_id)
    if not history:
        # 404, not an empty list. An unknown venue id and a venue with no
        # revisions are different facts, and a consumer that gets `[]` for a
        # typo'd id will file it as "that venue has no history" rather than as
        # its own bug.
        raise HTTPException(status_code=404, detail=f"no venue with id {venue_id!r}")

    if on is not None:
        matches = reference.revisions_containing(venue_id, on)
        return {
            "collector": spec.name,
            "venue_id": venue_id,
            "on": on.isoformat(),
            # A list, not an object, and never a "closest" fallback. Zero means
            # the table makes no claim about that date — everything before
            # `reference.TABLE_COMPILED_ON` — and returning the nearest
            # revision instead is precisely the retroactive attribution this
            # collector exists to prevent.
            "revisions": [build_static_row(match) for match in matches],
            "count": len(matches),
            "table_compiled_on": reference.TABLE_COMPILED_ON.isoformat(),
        }

    return {
        "collector": spec.name,
        "venue_id": venue_id,
        "revisions": [build_static_row(revision) for revision in history],
        "count": len(history),
        "table_compiled_on": reference.TABLE_COMPILED_ON.isoformat(),
    }
