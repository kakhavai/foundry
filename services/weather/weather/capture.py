"""Capture orchestration: schedule -> environment -> forecast -> envelope -> lake.

`/signals` serves from the cache this fills, never from an upstream. An upstream
outage therefore degrades freshness rather than availability.
"""

from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.lake import LakeWriter
from collector_core.routes import CaptureState

from .adapters.forecast import fetch_current_conditions, fetch_forecast_at
from .adapters.schedule import fetch_schedule
from .environment import UnresolvableVenue, resolve_environment, resolve_venue
from .metrics import metrics
from .playability import derive_playability

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "SIGNAL_TYPES",
    "CaptureState",
    "assert_forecast_hour",
    "capture_week",
]

COLLECTOR_NAME = "weather"
CADENCE_CLASS = CadenceClass.VOLATILE
SIGNAL_TYPES = ("venue_forecast_kickoff", "venue_conditions_current")
UPSTREAM_ADAPTER = "open-meteo"


def _wall_clock() -> datetime:
    """Real elapsed time, for deadline enforcement only.

    Distinct from `capture_week`'s own `now` parameter, which is the single
    instant the whole pass *describes* (`captured_at`, `Upstream.fetched_at`,
    `forecast_lead_hours`) and is deliberately frozen for the duration of a
    pass, including in tests. A deadline has to be checked against the clock
    that is actually advancing while upstream calls run, which `now` is not.
    Wrapped in its own function so a test can substitute a deterministic
    sequence rather than depending on real wall-clock time elapsing between
    mocked, near-instant upstream calls.
    """
    return datetime.now(tz=UTC)


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
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one week's forecast and current-conditions signals.

    `deadline`, when given, bounds the whole pass in real wall-clock time --
    checked between games (forecast) and between venues (current conditions),
    never by wrapping the coroutine in a timeout. A wrapper that cancels the
    coroutine on expiry would discard everything already captured; checking
    between iterations instead preserves the partial capture and records the
    truncation as `deadline_exceeded` in `coverage.missing`/`errors`, the same
    accounting a genuine upstream failure gets.
    """
    try:
        games = await fetch_schedule(season, week, client)
    except Exception as exc:  # noqa: BLE001 — total-outage path, classified below
        metrics.capture_failure(exc)
        raise

    forecast_acc = CoverageAccumulator(g.game_id for g in games)
    forecast_signals: list[dict] = []

    resolved: dict[str, dict] = {}  # stadium_id -> venue, for current conditions

    for index, game in enumerate(games):
        if deadline is not None and _wall_clock() >= deadline:
            for remaining in games[index:]:
                forecast_acc.fail(remaining.game_id, "deadline_exceeded")
            break
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

        try:
            assert_forecast_hour(forecast["forecast_valid_at"], game.kickoff_at)
        except ValueError as exc:
            # A per-game guard failure degrades like `UnresolvableVenue` --
            # one missing record, not a total-failure week. Left unguarded,
            # this used to propagate out of `capture_week` entirely and take
            # every other game in the week down with it.
            metrics.capture_failure(exc)
            forecast_acc.fail(game.game_id, "forecast_hour_mismatch")
            continue

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
    resolved_items = list(resolved.items())
    for index, (stadium_id, venue) in enumerate(resolved_items):
        if deadline is not None and _wall_clock() >= deadline:
            for remaining_id, _ in resolved_items[index:]:
                current_acc.fail(remaining_id, "deadline_exceeded")
            break
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
        try:
            lake.write(envelope)
        except Exception as exc:  # noqa: BLE001 — total-outage path (lake unreachable)
            metrics.capture_failure(exc)
            raise
        metrics.coverage(signal_type, envelope.coverage.ratio)

    return envelopes
