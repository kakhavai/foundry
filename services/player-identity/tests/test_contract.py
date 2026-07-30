"""The two generated outbound contracts: the OpenAPI surface and the
response field names.

`openapi/` proves a route exists; `responses/` proves what comes back, at any
nesting depth. FastAPI handlers here return bare dicts with no
`response_model=`, so the generated OpenAPI emits `"schema": {}` for every
200 body and cannot see a renamed field on its own.
"""

import json
from pathlib import Path

from player_identity.main import app

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "openapi"
    / "player-identity.json"
)

REGENERATE_HINT = (
    "The service's OpenAPI surface changed.\n"
    "If the change is intentional, regenerate the snapshot:\n"
    "  cd services/player-identity && uv run python -c "
    '"import json,pathlib; from player_identity.main import app; '
    "pathlib.Path('../../contracts/openapi/player-identity.json').write_text("
    "json.dumps(app.openapi(), indent=2, sort_keys=True) + '\\n')\"\n"
    "and include it in the same PR so the surface change is explicit in review."
)


def test_openapi_snapshot_matches_committed_contract():
    committed = json.loads(CONTRACT.read_text())
    live = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert live == committed, REGENERATE_HINT


def test_documented_paths_are_present():
    """Guards against a route being deleted outright — the five standard
    ones and the three this collector adds."""
    paths = set(app.openapi()["paths"])
    assert {
        "/health",
        "/metrics",
        "/catalog",
        "/signals",
        "/refresh",
        "/resolve",
        "/resolve/batch",
        "/unresolved",
    } <= paths


RESPONSES = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "responses"
    / "player-identity.json"
)


def response_shape(obj, prefix: str = "") -> list[str]:
    """Dotted key paths for a JSON body. Lists are represented by their first
    element with a `[]` marker, so `candidates[].confidence` pins a nested
    field name. Scalars terminate a path."""
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
    "If intentional, regenerate contracts/responses/player-identity.json and "
    "include it in the same PR so the change is explicit in review."
)


def observed_shapes(client) -> dict:
    resolved = "/resolve?name=Davante%20Adams&team=LV&position=WR&jersey_number=17"
    return {
        "/health": response_shape(client.get("/health").json()),
        "/catalog": response_shape(client.get("/catalog").json()),
        "/signals": response_shape(client.get("/signals").json()),
        "/resolve": response_shape(client.get(resolved).json()),
        "/resolve/batch": response_shape(
            client.post(
                "/resolve/batch",
                json={"queries": [{"name": "Davante Adams", "team": "LV"}]},
            ).json()
        ),
        "/unresolved": response_shape(client.get("/unresolved").json()),
    }


def test_response_shapes_match_committed_contract(client, seeded_state):
    committed = json.loads(RESPONSES.read_text())
    assert observed_shapes(client) == committed, SHAPE_HINT
