import json
from pathlib import Path

import httpx
import respx

from weather.adapters.forecast import FORECAST_URL
from weather.adapters.schedule import SCHEDULE_URL
from weather.main import app

CONTRACT = (
    Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "weather.json"
)

REGENERATE_HINT = (
    "The service's OpenAPI surface changed.\n"
    "If the change is intentional, regenerate the snapshot:\n"
    "  cd services/weather && uv run python -c "
    '"import json,pathlib; from weather.main import app; '
    "pathlib.Path('../../contracts/openapi/weather.json').write_text("
    "json.dumps(app.openapi(), indent=2, sort_keys=True) + '\\n')\"\n"
    "and include it in the same PR so the surface change is explicit in review.\n"
    "As of Task 13 this snapshot still describes the pre-collector surface "
    "(/weather/stadiums); regenerating it is Task 15's job — see CLAUDE.md."
)


def test_openapi_snapshot_matches_committed_contract():
    committed = json.loads(CONTRACT.read_text())
    live = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert live == committed, REGENERATE_HINT


def test_documented_paths_are_present():
    """Guards against a route being deleted outright."""
    paths = set(app.openapi()["paths"])
    assert {
        "/health",
        "/metrics",
        "/catalog",
        "/signals",
        "/signals/convergence",
        "/refresh",
    } <= paths


def test_old_stadium_paths_are_not_documented():
    """The inverse of the above: Task 13 removed these outright, not just from
    routing — they must not linger in the OpenAPI surface either."""
    paths = set(app.openapi()["paths"])
    assert "/weather/stadiums" not in paths
    assert "/weather/stadiums/{stadium_id}" not in paths


RESPONSES = (
    Path(__file__).resolve().parents[3] / "contracts" / "responses" / "weather.json"
)


def response_shape(obj, prefix: str = "") -> list[str]:
    """Dotted key paths for a JSON body. Lists are represented by their first
    element with a `[]` marker, so `envelopes[].signals[].game_id` pins a
    nested field name. Scalars terminate a path."""
    if isinstance(obj, dict):
        out: list[str] = []
        for key in sorted(obj):
            child = f"{prefix}.{key}" if prefix else key
            out.extend(response_shape(obj[key], child))
        return out or ([prefix] if prefix else [])
    if isinstance(obj, list):
        return response_shape(obj[0], f"{prefix}[]") if obj else [f"{prefix}[]"]
    return [prefix]


SHAPE_HINT = (
    "A response body's field names changed.\n"
    "If intentional, regenerate contracts/responses/weather.json and include it "
    "in the same PR so the change is explicit in review.\n"
    "As of Task 13 this snapshot still describes the pre-collector surface; "
    "regenerating it is Task 15's job — see CLAUDE.md."
)

SCHEDULE_HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)
SCHEDULE_ROW = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)


def _hourly_payload() -> dict:
    times = [f"2026-09-13T{h:02d}:00" for h in range(24)] + [
        f"2026-09-11T{h:02d}:00" for h in range(24)
    ]
    n = len(times)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": [68.0] * n,
            "apparent_temperature": [67.0] * n,
            "relative_humidity_2m": [62] * n,
            "wind_speed_10m": [11.0] * n,
            "wind_gusts_10m": [18.0] * n,
            "wind_direction_10m": [210] * n,
            "precipitation": [0.0] * n,
            "precipitation_probability": [10] * n,
        }
    }


@respx.mock
def test_response_shapes_match_committed_contract(client, seeded_state):
    """Catches renamed or dropped response fields at any nesting depth.

    `contracts/responses/weather.json` still describes the pre-Task-13 surface
    (`/weather/stadiums`) — regenerating it is Task 15's job, per CLAUDE.md.
    This test is therefore expected to fail on `assert actual == committed`
    until that regeneration lands; what it must not do is crash, so `/refresh`'s
    upstream calls are mocked the same way `capture_week` is mocked everywhere
    else in this suite.
    """
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=f"{SCHEDULE_HEADER}\n{SCHEDULE_ROW}\n")
    )
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=_hourly_payload())
    )
    committed = json.loads(RESPONSES.read_text())

    actual = {
        "/health": response_shape(client.get("/health").json()),
        "/catalog": response_shape(client.get("/catalog").json()),
        "/signals": response_shape(client.get("/signals").json()),
        "/signals/convergence": response_shape(
            client.get("/signals/convergence?game_id=2026_01_CHI_CAR").json()
        ),
        "/refresh": response_shape(client.post("/refresh", json={}).json()),
    }

    assert actual == committed, SHAPE_HINT
