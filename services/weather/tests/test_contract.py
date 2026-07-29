import json
from pathlib import Path

import httpx
import respx

from weather.client import WEATHER_URL
from weather.main import app
from weather.stadiums import STADIUMS

VALID_CURRENT = {
    "current": {
        "temperature_2m": 18.0,
        "relative_humidity_2m": 62,
        "wind_speed_10m": 11.0,
        "weather_code": 1,
        "precipitation": 0.0,
        "time": "2026-09-30T14:00",
    }
}

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
    "and include it in the same PR so the surface change is explicit in review."
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
        "/weather/stadiums",
        "/weather/stadiums/{stadium_id}",
    } <= paths


RESPONSES = (
    Path(__file__).resolve().parents[3] / "contracts" / "responses" / "weather.json"
)


def response_shape(obj, prefix: str = "") -> list[str]:
    """Dotted key paths for a JSON body. Lists are represented by their first
    element with a `[]` marker, so `stadiums[].weather.temperature_c` pins a
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
    "in the same PR so the change is explicit in review."
)


@respx.mock
def test_response_shapes_match_committed_contract(client):
    """Catches renamed or dropped response fields at any nesting depth."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=VALID_CURRENT))
    committed = json.loads(RESPONSES.read_text())
    stadium_id = next(iter(STADIUMS))

    actual = {
        "/health": response_shape(client.get("/health").json()),
        "/weather/stadiums": response_shape(client.get("/weather/stadiums").json()),
        "/weather/stadiums/{stadium_id}": response_shape(
            client.get(f"/weather/stadiums/{stadium_id}").json()
        ),
    }

    assert actual == committed, SHAPE_HINT
