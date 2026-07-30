"""player-identity's process wiring: the descriptor, `_signal_matches`, and
the three resolve routes. Everything else lives in `collector_core.app`.
"""

from datetime import UTC, datetime
from functools import partial

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import HTTPException

from .capture import CADENCE_CLASS, COLLECTOR_NAME, SIGNAL_TYPES, capture_identities
from .identity import KNOWN_POSITIONS, normalized_key, position_group
from .metrics import metrics
from .resolution import (
    MAX_BATCH_QUERIES,
    MissQueue,
    ResolutionIndex,
    ResolveQuery,
    candidate_payload,
)

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
        telemetry_module="player_identity.telemetry",
    )
)
app.state.miss_queue = _misses
app.state.resolution_index = _index


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _build_query(spec: dict) -> ResolveQuery:
    """Turn one caller-supplied row into a `ResolveQuery`, or 422.

    An unknown position is rejected rather than ignored, for the same reason
    `player-projections` rejects `pos=FLEX`: a client bug should surface as a
    loud error, not look like a player who does not exist.
    """
    name = spec.get("name")
    position = spec.get("position")
    if position is not None:
        position = str(position).upper()
        if position not in KNOWN_POSITIONS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown position {position!r}",
            )

    jersey = spec.get("jersey_number")
    if jersey is not None:
        try:
            jersey = int(jersey)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="jersey_number must be an integer"
            ) from None

    entry_year = spec.get("entry_year")
    if entry_year is not None:
        try:
            entry_year = int(entry_year)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="entry_year must be an integer"
            ) from None

    team = spec.get("team")
    if team is not None:
        team = str(team).upper()

    # A query with nothing to match on would score every record in the
    # league at zero and return an empty list, which reads like "no such
    # player" rather than "you asked nothing".
    if not any(
        value not in (None, "")
        for value in (name, team, position, jersey, spec.get("source_id"))
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "supply at least one of: name, team, position, jersey_number, source_id"
            ),
        )

    return ResolveQuery(
        raw_name=name,
        normalized_key=normalized_key(name) if name else None,
        team=team,
        position=position,
        position_group=position_group(position),
        jersey_number=jersey,
        entry_year=entry_year,
        birth_date=spec.get("birth_date"),
        season=spec.get("season"),
        source=spec.get("source"),
        source_id=(
            None if spec.get("source_id") is None else str(spec.get("source_id"))
        ),
    )


def _resolve_one(spec: dict, *, now: datetime) -> dict:
    query = _build_query(spec)
    index = app.state.resolution_index
    resolution = index.resolve(query, now=now)

    if not resolution.resolved:
        app.state.miss_queue.record(query, resolution, now=now)
        best = resolution.candidates[0] if resolution.candidates else None
        # Labelled by which attribute disagreed. A spike concentrated on
        # `team` is a staleness problem — a trade a book has not caught up
        # with — and reads completely differently from one spread across
        # attributes.
        attributes = list(best.disagreeing) if best and best.disagreeing else ["none"]
        for attribute in attributes:
            metrics.resolution_failure(attribute, resolution.reason or "unknown")
        if resolution.near_miss:
            metrics.near_miss()

    method = resolution.link_method or "attribute_score"
    return {
        "query": {
            "name": query.raw_name,
            "team": query.team,
            "position": query.position,
            "jersey_number": query.jersey_number,
            "season": query.season,
            "source": query.source,
            "source_id": query.source_id,
        },
        "resolved": resolution.resolved,
        "player_id": resolution.player_id,
        "link_method": resolution.link_method,
        "confidence": round(resolution.confidence, 6),
        "margin": resolution.margin,
        "reason": resolution.reason,
        "candidates": [candidate_payload(c, method) for c in resolution.candidates],
        "count": len(resolution.candidates),
    }


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
    return _resolve_one(
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
        now=_utc_now(),
    )


@app.post("/resolve/batch")
async def resolve_batch(body: dict):
    """Same semantics as `GET /resolve`, for a whole betting slate at once.

    Capped at `MAX_BATCH_QUERIES`; beyond that the request is rejected
    rather than silently truncated, which would return a short list a caller
    could easily read as "the rest did not resolve".
    """
    queries = body.get("queries")
    if not isinstance(queries, list):
        raise HTTPException(status_code=422, detail="body must carry a 'queries' array")
    if len(queries) > MAX_BATCH_QUERIES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"batch of {len(queries)} exceeds the maximum of "
                f"{MAX_BATCH_QUERIES} queries per call"
            ),
        )

    now = _utc_now()
    results = []
    for entry in queries:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422,
                detail="each query must be a string or an object",
            )
        results.append(_resolve_one(entry, now=now))

    resolved = sum(1 for r in results if r["resolved"])
    return {
        "results": results,
        "count": len(results),
        "resolved_count": resolved,
        "unresolved_count": len(results) - resolved,
    }


@app.get("/unresolved")
async def unresolved(limit: int = 100):
    """The standing miss queue, ordered by `occurrence_count`.

    A name that fails 400 times a week is the single most actionable thing
    this collector produces, so it is served as a first-class route rather
    than left to be dug out of the lake.
    """
    queue = app.state.miss_queue
    rows = queue.rows()[: max(0, limit)]
    return {"misses": rows, "count": len(rows), "total": len(queue)}
