"""Process wiring: the descriptor and `GET /signals/age-curve`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import HTTPException, Query

from . import derive
from .capture import (
    BIOGRAPHICAL,
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    capture_player_profile,
)
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_player_profile,
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


@app.get("/signals/age-curve")
async def age_curve(
    position: str = Query(
        description=(
            "Canonical position — QB, RB, WR, TE or K. Case-insensitive. "
            "Unknown positions are 422, never an empty distribution."
        )
    ),
    season: int | None = Query(
        default=None, description="Defaults to the season of the current capture."
    ),
):
    """The position-relative age distribution `position_age_percentile` used.

    The route the spec asks for, and its own sentence gives the reason: it
    exists "so a consumer can reproduce the bucketing rather than trust it".
    Two things are therefore published together, because either alone is
    unreproducible:

    * the **distribution** — the captured ages for that position, plus the
      quantiles and the count `position_age_percentile` was taken against. Both
      percentiles rank against the pass's own captured population rather than
      all of history (see `derive`'s module docstring), and this is what makes
      that population recoverable.
    * the **curve table** — the position-specific age boundaries
      `position_age_curve_stage` bucketed against. It is a modelling prior
      rather than upstream data, so a consumer that cannot see it cannot check
      the stage at all.

    Served from the in-memory capture rather than from the lake, deliberately:
    the answer must describe the same rows `/signals` is serving right now. A
    lake-backed answer could only describe whichever pass last wrote an object,
    and the digest gate means that is routinely not this one.

    `min_cohort` is published because a null `position_age_percentile` is
    otherwise indistinguishable from a bug; below that count the collector
    refuses to compute one.

    `spec` is reached through `app.state.collector_spec` rather than a
    module-level global, per the fleet convention, so this route reports the
    collector name the router itself is serving under.
    """
    spec = app.state.collector_spec

    canonical = (position or "").strip().upper()
    if canonical not in derive.POSITION_AGE_CURVE:
        # 422, not an empty distribution. An unknown position and a position
        # with no captured players are different facts, and a client that gets
        # `count: 0` for a typo will file it as "nobody plays that position".
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown position {position!r}; expected one of "
                f"{', '.join(sorted(derive.POSITION_AGE_CURVE))}"
            ),
        )

    envelope = spec.state.envelopes.get(BIOGRAPHICAL)
    if envelope is None:
        # 404 rather than an empty body: no capture has landed yet, which is a
        # different fact from "this position had no players in the last one".
        raise HTTPException(
            status_code=404,
            detail=f"no {BIOGRAPHICAL} capture yet",
        )

    scope = envelope.scope
    if season is not None and int(scope.get("season", 0)) != season:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no {BIOGRAPHICAL} capture for season {season}; "
                f"the current capture is season {scope.get('season')}"
            ),
        )

    ages = [
        row["age_years"]
        for row in envelope.signals
        if row.get("position") == canonical and row.get("age_years") is not None
    ]
    peak_from, post_peak_from, cliff_from = derive.POSITION_AGE_CURVE[canonical]

    return {
        "collector": spec.name,
        "position": canonical,
        "season": scope.get("season"),
        "week": scope.get("week"),
        "as_of_date": scope.get("as_of_date"),
        "captured_at": envelope.captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "distribution": derive.position_age_distribution(ages),
        "min_cohort": derive.MIN_COHORT,
        "curve_stages": {
            "order": list(derive.CURVE_STAGES),
            "pre_peak_below": peak_from,
            "peak_from": peak_from,
            "post_peak_from": post_peak_from,
            "cliff_from": cliff_from,
        },
        "ages": sorted(ages),
    }
