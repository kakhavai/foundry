"""player-identity's process wiring: the descriptor, `_signal_matches`, and
the three resolve routes. Everything else lives in `collector_core.app`, and
the routes' own logic lives in `api.py`.
"""

from functools import partial

from collector_core.app import CollectorDescriptor, build_collector_app

from .api import resolve_queries, resolve_query, unresolved_payload
from .capture import CADENCE_CLASS, COLLECTOR_NAME, SIGNAL_TYPES, capture_identities
from .metrics import metrics
from .resolution import MissQueue, ResolutionIndex

SUPPORTED_FILTERS = ("season", "week", "signal_type", "player_id", "team", "position")


def _signal_matches(row: dict, params: dict) -> bool:
    for key in ("player_id", "team", "position"):
        if key in params and row.get(key) != params[key]:
            return False
    return True


# Constructed here and bound into `capture` below, because the library fixes
# `capture`'s positional signature and both objects are shared with the
# routes. They are reached through `app.state` from every route — never as
# module-level globals, which only this one service's main.py could see.
_misses = MissQueue()
_index = ResolutionIndex()

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=partial(capture_identities, misses=_misses, index=_index),
        signal_matches=_signal_matches,
        metrics=metrics,
        # Shared telemetry wiring by default -- see collector_core.telemetry.
    )
)
app.state.miss_queue = _misses
app.state.resolution_index = _index


def _shared() -> dict:
    """The index and the miss queue, off `app.state` rather than the
    module-level names above — the routes must read the same objects a
    capture just replaced."""
    return {"index": app.state.resolution_index, "misses": app.state.miss_queue}


@app.get("/resolve")
async def resolve(
    name: str | None = None,
    team: str | None = None,
    position: str | None = None,
    jersey_number: int | None = None,
    entry_year: int | None = None,
    birth_date: str | None = None,
    season: int | None = None,
    source: str | None = None,
    source_id: str | None = None,
):
    """Free-text name plus optional hints -> ranked candidates.

    Every candidate carries a `confidence` in [0, 1] and the `link_method`
    that produced it, so a caller can tell an adopted crosswalk link apart
    from a scored one without a second request.
    """
    return resolve_query(
        {
            "name": name,
            "team": team,
            "position": position,
            "jersey_number": jersey_number,
            "entry_year": entry_year,
            "birth_date": birth_date,
            "season": season,
            "source": source,
            "source_id": source_id,
        },
        **_shared(),
    )


@app.post("/resolve/batch")
async def resolve_batch(body: dict):
    """Same semantics as `GET /resolve`, for a whole betting slate at once.

    Capped at `MAX_BATCH_QUERIES`; beyond that the request is rejected
    rather than silently truncated, which would return a short list a caller
    could easily read as "the rest did not resolve".
    """
    return resolve_queries(body, **_shared())


@app.get("/unresolved")
async def unresolved(limit: int = 100):
    """The standing miss queue, ordered by `occurrence_count`.

    A name that fails 400 times a week is the single most actionable thing
    this collector produces, so it is served as a first-class route rather
    than left to be dug out of the lake.
    """
    return unresolved_payload(app.state.miss_queue, limit)
