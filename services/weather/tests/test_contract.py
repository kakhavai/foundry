import json
from pathlib import Path

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
