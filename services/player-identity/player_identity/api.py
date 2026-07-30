"""The HTTP adaptation of `resolution.py`: argument coercion, the 422s,
miss-queue recording, metric labelling, and response shaping.

Split out of `main.py` on purpose. Every collector's `main.py` must look the
same — descriptor, `build_collector_app`, and thin route bodies that parse
their arguments and delegate — because `player-identity` is one of the
services `new-collector.py` will template from, and whatever shape lands
here becomes the shape of collectors #4 through #26. Weather's `main.py` is
46 lines; the query validation and payload building that used to sit
alongside these routes live here instead.

Distinct from `resolution.py`, which stays free of HTTP: scoring and the
index know nothing about status codes, and this module knows nothing about
weights. Nothing here reaches for a module-level global — the index and the
miss queue arrive as arguments, from `app.state`.
"""

from datetime import UTC, datetime

from fastapi import HTTPException

from .identity import KNOWN_POSITIONS, normalized_key, position_group
from .metrics import metrics
from .resolution import (
    MAX_BATCH_QUERIES,
    MissQueue,
    ResolutionIndex,
    ResolveQuery,
    candidate_payload,
)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def build_query(spec: dict) -> ResolveQuery:
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


def resolve_query(
    spec: dict,
    *,
    index: ResolutionIndex,
    misses: MissQueue,
    now: datetime | None = None,
) -> dict:
    """Resolve one row: score it, record any miss, shape the response."""
    now = utc_now() if now is None else now
    query = build_query(spec)
    resolution = index.resolve(query, now=now)

    if not resolution.resolved:
        misses.record(query, resolution, now=now)
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


def resolve_queries(body: dict, *, index: ResolutionIndex, misses: MissQueue) -> dict:
    """Resolve a whole slate.

    Capped at `MAX_BATCH_QUERIES`; beyond that the request is rejected
    rather than silently truncated, which would return a short list a caller
    could easily read as "the rest did not resolve".

    One clock is read for the whole batch, so every row in a slate is scored
    against the same instant rather than drifting across the call.
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

    now = utc_now()
    results = []
    for entry in queries:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422,
                detail="each query must be a string or an object",
            )
        results.append(resolve_query(entry, index=index, misses=misses, now=now))

    resolved = sum(1 for r in results if r["resolved"])
    return {
        "results": results,
        "count": len(results),
        "resolved_count": resolved,
        "unresolved_count": len(results) - resolved,
    }


def unresolved_payload(misses: MissQueue, limit: int) -> dict:
    rows = misses.rows()[: max(0, limit)]
    return {"misses": rows, "count": len(rows), "total": len(misses)}
