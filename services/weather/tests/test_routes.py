"""The six-route contract surface: /health, /metrics, /catalog, /signals,
/signals/convergence, /refresh — and proof the old /weather/stadiums routes
are gone.

State-based tests use the `seeded_state` fixture from conftest.py, which
pre-populates `main._state` directly rather than driving a real capture — the
routes under test never call an upstream themselves, only `/refresh` does.
"""

import httpx
import respx
from collector_core.envelope import ENVELOPE_VERSION

from weather.adapters.forecast import FORECAST_URL
from weather.adapters.schedule import SCHEDULE_URL

NOW_ISO = "2026-09-11T12:00:00Z"

SCHEDULE_HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)
SCHEDULE_ROW = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)


def _empty_hourly() -> dict:
    return {
        "hourly": {
            "time": [f"2026-09-11T{h:02d}:00" for h in range(24)]
            + [f"2026-09-13T{h:02d}:00" for h in range(24)],
            "temperature_2m": [68.0] * 48,
            "apparent_temperature": [67.0] * 48,
            "relative_humidity_2m": [62] * 48,
            "wind_speed_10m": [11.0] * 48,
            "wind_gusts_10m": [18.0] * 48,
            "wind_direction_10m": [210] * 48,
            "precipitation": [0.0] * 48,
            "precipitation_probability": [10] * 48,
        }
    }


def mock_capture_upstreams() -> None:
    """`/refresh` drives a real `capture_week`, unlike every other route here —
    it has to be given something to capture."""
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=f"{SCHEDULE_HEADER}\n{SCHEDULE_ROW}\n")
    )
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=_empty_hourly()))


def test_old_stadium_routes_are_gone(client):
    assert client.get("/weather/stadiums").status_code == 404
    assert client.get("/weather/stadiums/lambeau").status_code == 404


def test_catalog_declares_the_collector(client, seeded_state):
    body = client.get("/catalog").json()
    assert body["collector"] == "weather"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "volatile"
    assert set(body["signal_types"]) == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }
    assert "signal_type" in body["filters"]
    assert body["last_capture_at"] == NOW_ISO


def test_catalog_reports_no_capture_yet_when_state_is_empty(client):
    body = client.get("/catalog").json()
    assert body["last_capture_at"] is None
    assert body["coverage"] == {}


def test_signals_without_filters_returns_both_types(client, seeded_state):
    body = client.get("/signals").json()
    assert {e["signal_type"] for e in body["envelopes"]} == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }


def test_signals_filtered_by_signal_type(client, seeded_state):
    body = client.get("/signals?signal_type=venue_conditions_current").json()
    assert [e["signal_type"] for e in body["envelopes"]] == ["venue_conditions_current"]


def test_unknown_signal_type_is_422_not_empty(client, seeded_state):
    """A client bug should surface rather than look like a quiet week —
    the precedent player-projections set with pos=FLEX."""
    assert client.get("/signals?signal_type=nonsense").status_code == 422


def test_signals_filtered_by_game_id(client, seeded_state):
    body = client.get("/signals?game_id=2026_01_CHI_CAR").json()
    forecast = next(
        e for e in body["envelopes"] if e["signal_type"] == "venue_forecast_kickoff"
    )
    assert [s["game_id"] for s in forecast["signals"]] == ["2026_01_CHI_CAR"]


def test_signals_filtered_by_team(client, seeded_state):
    body = client.get("/signals?team=HOU").json()
    forecast = next(
        e for e in body["envelopes"] if e["signal_type"] == "venue_forecast_kickoff"
    )
    assert [s["game_id"] for s in forecast["signals"]] == ["2026_01_BUF_HOU"]


def test_player_id_filter_is_rejected(client, seeded_state):
    """weather emits no player_id. Accepting it silently would return
    everything and look like a match."""
    assert client.get("/signals?player_id=fdy-abc").status_code == 422


def test_signals_season_mismatch_excludes_the_envelope(client, seeded_state):
    """seeded_state's envelopes are scoped to season 2026 — a different season
    must come back empty, not fall through and return everything."""
    body = client.get("/signals?season=2099").json()
    assert body == {"envelopes": [], "count": 0}


def test_signals_week_mismatch_excludes_the_envelope(client, seeded_state):
    body = client.get("/signals?week=17").json()
    assert body == {"envelopes": [], "count": 0}


@respx.mock
def test_refresh_returns_202_with_a_refresh_id(client):
    mock_capture_upstreams()
    body = client.post("/refresh", json={})
    assert body.status_code == 202
    assert body.json()["refresh_id"]


@respx.mock
def test_second_refresh_inside_the_floor_is_429(client):
    mock_capture_upstreams()
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


@respx.mock
def test_refresh_populates_state_for_subsequent_signals_calls(client):
    mock_capture_upstreams()
    client.post("/refresh", json={})
    body = client.get("/signals").json()
    assert body["count"] == 2


def test_convergence_requires_a_game_id(client):
    assert client.get("/signals/convergence").status_code == 422


def test_convergence_empty_lake_returns_empty_series(client):
    """No captures have ever been written — the lake is empty, not broken."""
    body = client.get("/signals/convergence?game_id=2026_01_CHI_CAR").json()
    assert body == {"game_id": "2026_01_CHI_CAR", "series": [], "count": 0}


class _FakeLakeWriter:
    """A minimal stand-in for the `LakeWriter` protocol, holding a fixed set of
    already-written envelope bodies in read order. Not moto/S3 — same reasoning
    as `SpyLakeWriter` in test_capture.py: the storage layer's own correctness
    is collector-core's test to own, not this route's."""

    def __init__(self, bodies: list[dict]) -> None:
        self._bodies = bodies

    def write(self, envelope) -> str:
        raise NotImplementedError

    def list_keys(self, collector, signal_type, season, week) -> list[str]:
        return [str(i) for i in range(len(self._bodies))]

    def read(self, key: str) -> dict:
        return self._bodies[int(key)]


def test_convergence_orders_snapshots_and_computes_deltas(client, monkeypatch):
    """The point of the route: a consumer should not have to reimplement
    reading the lake, filtering to one game, and diffing consecutive snapshots
    just to see whether a forecast is converging or flip-flopping."""
    bodies = [
        {
            "signal_type": "venue_forecast_kickoff",
            "captured_at": "2026-09-11T12:00:00Z",
            "signals": [
                {
                    "game_id": "2026_01_CHI_CAR",
                    "forecast_lead_hours": 48.0,
                    "temperature_f": 70.0,
                    "wind_speed_mph": 10.0,
                    "bands": {"temperature_f": {"p10": 65.0, "p50": 70.0, "p90": 75.0}},
                }
            ],
        },
        # A different signal type in the same partition must be skipped, not
        # mistaken for a forecast snapshot.
        {
            "signal_type": "venue_conditions_current",
            "captured_at": "2026-09-11T18:00:00Z",
            "signals": [{"game_id": "2026_01_CHI_CAR", "temperature_f": 71.0}],
        },
        {
            "signal_type": "venue_forecast_kickoff",
            "captured_at": "2026-09-12T12:00:00Z",
            "signals": [
                {
                    "game_id": "2026_01_CHI_CAR",
                    "forecast_lead_hours": 24.0,
                    "temperature_f": 68.0,
                    "wind_speed_mph": 12.0,
                    "bands": {"temperature_f": {"p10": 66.0, "p50": 68.0, "p90": 70.0}},
                }
            ],
        },
    ]
    monkeypatch.setattr("weather.main._lake", _FakeLakeWriter(bodies))

    body = client.get("/signals/convergence?game_id=2026_01_CHI_CAR").json()

    assert body["count"] == 2
    first, second = body["series"]
    assert first["temperature_f"] == 70.0
    assert first["delta"] is None
    assert second["temperature_f"] == 68.0
    assert second["delta"] == {"temperature_f": -2.0, "wind_speed_mph": 2.0}


def test_convergence_skips_snapshots_missing_the_requested_game(client, monkeypatch):
    bodies = [
        {
            "signal_type": "venue_forecast_kickoff",
            "captured_at": "2026-09-11T12:00:00Z",
            "signals": [
                {
                    "game_id": "2026_01_BUF_HOU",
                    "forecast_lead_hours": 48.0,
                    "temperature_f": 70.0,
                    "wind_speed_mph": 10.0,
                    "bands": None,
                }
            ],
        }
    ]
    monkeypatch.setattr("weather.main._lake", _FakeLakeWriter(bodies))

    body = client.get("/signals/convergence?game_id=2026_01_CHI_CAR").json()

    assert body == {"game_id": "2026_01_CHI_CAR", "series": [], "count": 0}


def test_health_and_metrics_still_work(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "# HELP" in client.get("/metrics").text
