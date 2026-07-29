"""Capture orchestration: schedule -> environment -> forecast -> envelope -> lake.

`/signals` serves from the cache this fills, never from an upstream. An upstream
outage therefore degrades freshness rather than availability.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.lake import LakeWriter

from .adapters.forecast import fetch_current_conditions, fetch_forecast_at
from .adapters.schedule import fetch_schedule
from .environment import UnresolvableVenue, resolve_environment, resolve_venue
from .metrics import metrics
from .playability import derive_playability

COLLECTOR_NAME = "weather"
CADENCE_CLASS = CadenceClass.VOLATILE
SIGNAL_TYPES = ("venue_forecast_kickoff", "venue_conditions_current")
UPSTREAM_ADAPTER = "open-meteo"


@dataclass
class CaptureState:
    envelopes: dict[str, Envelope] = field(default_factory=dict)
    last_capture_at: datetime | None = None


def assert_forecast_hour(valid_at: datetime, kickoff_at: datetime) -> None:
    """Write-time guard: the forecast must describe the kickoff hour.

    An adapter asked beyond its model horizon can quietly return current
    conditions. The record looks entirely normal — plausible temperature,
    plausible wind — and the generator treats Tuesday's weather as Sunday's.
    Comparing the hour is the cheapest way to catch it.
    """
    expected = kickoff_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    actual = valid_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if actual != expected:
        raise ValueError(
            f"forecast_valid_at {actual.isoformat()} does not match kickoff hour "
            f"{expected.isoformat()}"
        )


async def capture_week(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
) -> dict[str, Envelope]:
    games = await fetch_schedule(season, week, client)

    forecast_acc = CoverageAccumulator(g.game_id for g in games)
    forecast_signals: list[dict] = []

    resolved: dict[str, dict] = {}  # stadium_id -> venue, for current conditions

    for game in games:
        try:
            venue = resolve_venue(game)
            environment = resolve_environment(game, venue)
        except UnresolvableVenue as exc:
            forecast_acc.fail(game.game_id, exc.reason)
            continue

        lead_hours = max(0.0, (game.kickoff_at - now).total_seconds() / 3600.0)
        metrics.capture_attempt()
        try:
            forecast = await fetch_forecast_at(
                venue["latitude"],
                venue["longitude"],
                game.kickoff_at,
                client,
                lead_hours=lead_hours,
            )
        except Exception as exc:  # noqa: BLE001 — reason is classified below
            metrics.capture_failure(exc)
            forecast_acc.fail(game.game_id, metrics.reason_for(exc))
            continue

        assert_forecast_hour(forecast["forecast_valid_at"], game.kickoff_at)

        signal = {
            "game_id": game.game_id,
            "venue_id": venue["stadium_id"],
            "forecast_valid_at": forecast["forecast_valid_at"]
            .astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forecast_lead_hours": round(lead_hours, 2),
            "environment": str(environment),
            "playability": derive_playability(forecast, environment),
            # crosswind_component_mph stays absent until `venue` supplies
            # field_orientation_deg at 8E. Absent, not null-with-meaning.
            **{k: v for k, v in forecast.items() if k != "forecast_valid_at"},
        }
        forecast_signals.append(signal)
        forecast_acc.record(game.game_id)
        resolved[venue["stadium_id"]] = venue

    current_acc = CoverageAccumulator(resolved)
    current_signals: list[dict] = []
    for stadium_id, venue in resolved.items():
        metrics.capture_attempt()
        try:
            conditions = await fetch_current_conditions(
                venue["latitude"], venue["longitude"], client, now=now
            )
        except Exception as exc:  # noqa: BLE001
            metrics.capture_failure(exc)
            current_acc.fail(stadium_id, metrics.reason_for(exc))
            continue
        conditions.pop("forecast_valid_at", None)
        current_signals.append({"venue_id": stadium_id, **conditions})
        current_acc.record(stadium_id)

    upstream = Upstream(adapter=UPSTREAM_ADAPTER, fetched_at=now)
    scope = {"season": season, "week": week}

    envelopes = {
        "venue_forecast_kickoff": Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type="venue_forecast_kickoff",
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=forecast_acc.result(),
            errors=forecast_acc.errors,
            signals=forecast_signals,
        ),
        "venue_conditions_current": Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type="venue_conditions_current",
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=current_acc.result(),
            errors=current_acc.errors,
            signals=current_signals,
        ),
    }

    for signal_type, envelope in envelopes.items():
        lake.write(envelope)
        metrics.coverage(signal_type, envelope.coverage.ratio)

    return envelopes
