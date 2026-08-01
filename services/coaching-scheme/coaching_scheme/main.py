"""Process wiring: the descriptor and `GET /teams/{team_id}/revisions`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import HTTPException, Path, Query

from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    PROFILE,
    SIGNAL_TYPES,
    STAFF,
    capture_coaching_scheme,
)
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

# Two or three upper-case letters, the shape nflverse team codes take (`KC`,
# `LAC`, `WAS`). Enforced on the path so a malformed id is a 422 from
# FastAPI's own validation rather than a 404 — "you asked wrongly" and "there
# is no such team" are different answers, and collapsing them sends a client
# looking for a data problem that does not exist. `officiating` does this
# deliberately and it is the pattern.
TEAM_ID_PATTERN = r"^[A-Z]{2,3}$"

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_coaching_scheme,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`: `seasonal` runs on its base interval and has
        # nothing perishable to escalate toward. A staff change is announced
        # off-cycle rather than at a predictable instant, so there is no
        # moment to escalate *toward* — which is exactly the case the field
        # does not serve.
    )
)


@app.get("/teams/{team_id}/revisions")
async def team_revisions(
    team_id: str = Path(pattern=TEAM_ID_PATTERN),
    season: int | None = Query(default=None, ge=1999, le=2100),
):
    """One team's ordered staff-revision timeline.

    The route the spec asks for, and its reason is in its own sentence: it is
    "the shape consumers actually want and which is awkward to reconstruct
    from filtered `/signals` calls". A `/signals` response is a flat list of
    rows across all 32 teams and every revision of each; turning that into
    "who ran this team, in order, and when did it change" is a group-by plus a
    sort plus the `effective_to_week`-is-null convention, done by every
    consumer independently.

    `season` is optional and filters; omitting it returns whatever season the
    current capture covers. It is a `Query(ge=..., le=...)` so a non-numeric
    or absurd value is a 422 rather than a silent no-match — same reasoning as
    the team-id pattern above.

    Served from the in-memory capture, not the lake. The lake would answer
    only with what some past pass happened to write, and would cost an
    object-store round trip on a route whose whole purpose is a cheap lookup.
    `spec` is reached through `app.state.collector_spec` rather than a
    module-level global, per the fleet convention.

    Each revision carries its `scheme_profile` inline. The two signal types
    are joined here rather than left to the caller because the join key
    (`revision_id`) is this collector's own invention — a consumer that had to
    perform it would be re-implementing the one thing the route exists to
    save.
    """
    spec = app.state.collector_spec

    staff_envelope = spec.state.envelopes.get(STAFF)
    profile_envelope = spec.state.envelopes.get(PROFILE)

    staff_rows = [
        row
        for row in (staff_envelope.signals if staff_envelope else [])
        if row.get("team_id") == team_id
        and (season is None or row.get("season") == season)
    ]
    if not staff_rows:
        # 404, not an empty 200. An unknown team id and a team with no data
        # are different facts, and a consumer that gets an empty timeline for
        # a typo'd code will file it as "that team never changed staff" rather
        # than as its own bug.
        detail = f"no revisions for team {team_id!r}"
        if season is not None:
            detail += f" in season {season}"
        raise HTTPException(status_code=404, detail=detail)

    profiles = {
        row["revision_id"]: row
        for row in (profile_envelope.signals if profile_envelope else [])
        if row.get("team_id") == team_id
    }

    # Ascending by the week the revision takes effect: the timeline order the
    # route promises. Ties broken on revision_id so the output is total-ordered
    # even if a feed ever produced two revisions starting the same week.
    ordered = sorted(
        staff_rows, key=lambda r: (r["effective_from_week"], r["revision_id"])
    )

    return {
        "collector": spec.name,
        "team_id": team_id,
        "season": season if season is not None else ordered[0].get("season"),
        "revisions": [
            {**row, "scheme_profile": profiles.get(row["revision_id"])}
            for row in ordered
        ],
        "last_capture_at": (
            spec.state.last_capture_at.isoformat()
            if spec.state.last_capture_at
            else None
        ),
    }
