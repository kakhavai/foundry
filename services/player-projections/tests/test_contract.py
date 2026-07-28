import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from player_projections import main
from player_projections.client import fetch_projections
from player_projections.main import app

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "player-data"
FORMATS = ["standard", "half-ppr", "ppr"]


@pytest.mark.parametrize("fmt", FORMATS)
def test_schema_is_valid_json_schema(fmt):
    schema = json.loads((CONTRACTS / f"{fmt}.v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("fmt", FORMATS)
def test_schema_declares_its_own_format(fmt):
    """Each schema pins `format` to its own scoring type — files can't be mixed up."""
    schema = json.loads((CONTRACTS / f"{fmt}.v1.schema.json").read_text())
    assert schema["properties"]["format"]["const"] == fmt


def test_fixture_spreads_are_ordered():
    """JSON Schema cannot express this; assert it as a business rule instead."""
    doc = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())

    for player in doc["players"]:
        spread = player.get("proj_points")
        if spread is None:
            continue
        assert spread["floor"] <= spread["expected"] <= spread["ceiling"], (
            f"{player['id']} has a spread out of order: {spread}"
        )


def test_fixture_validates_against_ppr_schema():
    schema = json.loads((CONTRACTS / "ppr.v1.schema.json").read_text())
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(fixture)


def test_bad_generated_at_is_rejected():
    """`format: date-time` is only enforced when a FormatChecker is attached."""
    schema = json.loads((CONTRACTS / "ppr.v1.schema.json").read_text())
    doc = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    doc["generated_at"] = "not-a-date-at-all"

    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(doc)
    )

    assert errors, "a malformed generated_at must fail validation"


def test_dst_row_carrying_skill_player_fields_is_rejected():
    """A DST row must not also carry rank/proj_points — the conditional's
    `then` branch must positively exclude the `else` branch's shape."""
    schema = json.loads((CONTRACTS / "ppr.v1.schema.json").read_text())
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    dst = next(p for p in fixture["players"] if p["pos"] == "DST")
    dst["rank"] = 5
    dst["proj_points"] = {"floor": 1, "expected": 2, "ceiling": 3}

    errors = list(Draft202012Validator(schema).iter_errors(fixture))

    assert errors, "a DST row carrying rank/proj_points must fail validation"


def test_wr_row_carrying_dst_fields_is_rejected():
    """A non-DST row must not also carry yahoo_rank/espn_rank — the
    conditional's `else` branch must positively exclude the `then` shape."""
    schema = json.loads((CONTRACTS / "ppr.v1.schema.json").read_text())
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    wr = next(p for p in fixture["players"] if p["pos"] == "WR")
    wr["yahoo_rank"] = 1

    errors = list(Draft202012Validator(schema).iter_errors(fixture))

    assert errors, "a non-DST row carrying yahoo_rank must fail validation"


def test_schemas_are_structurally_identical_across_formats():
    """Scoring changes values, not shape. Divergence here is a contract bug."""
    defs = []
    for fmt in FORMATS:
        schema = json.loads((CONTRACTS / f"{fmt}.v1.schema.json").read_text())
        defs.append(schema["$defs"])
    assert defs[0] == defs[1] == defs[2]


@respx.mock
async def test_consumer_parses_a_schema_valid_snapshot():
    """The real contract assertion: a schema-valid document parses cleanly."""
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    url = "https://example.test/ppr.json"
    respx.get(url).mock(return_value=httpx.Response(200, json=fixture))

    players = await fetch_projections(url)

    assert len(players) == 4
    assert {p["id"] for p in players} == {
        "p_8f3a21",
        "p_1c9e04",
        "p_4d7b12",
        "p_9a2f77",
    }
    assert players[0]["proj_points"]["expected"] == 12.4


OPENAPI = Path(__file__).resolve().parents[3] / "contracts" / "openapi"

REGENERATE_HINT = (
    "The service's OpenAPI surface changed.\n"
    "If the change is intentional, regenerate the snapshot:\n"
    "  cd services/player-projections && uv run python -c "
    '"import json,pathlib; from player_projections.main import app; '
    "pathlib.Path('../../contracts/openapi/player-projections.json').write_text("
    "json.dumps(app.openapi(), indent=2, sort_keys=True) + '\\n')\"\n"
    "and include it in the same PR so the surface change is explicit in review."
)


def test_openapi_snapshot_matches_committed_contract():
    committed = json.loads((OPENAPI / "player-projections.json").read_text())
    live = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert live == committed, REGENERATE_HINT


def test_documented_paths_are_present():
    paths = set(app.openapi()["paths"])
    assert paths == {"/health", "/metrics", "/projections"}


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


RESPONSES = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "responses"
    / "player-projections.json"
)

SHAPE_HINT = (
    "A response body's field names changed.\n"
    "If intentional, regenerate contracts/responses/player-projections.json and "
    "include it in the same PR so the change is explicit in review."
)


def test_response_shapes_match_committed_contract(monkeypatch):
    """Catches renamed or dropped response fields at any nesting depth.

    The cache is pre-populated so nested projection fields appear in the shape —
    an empty stub-mode response would bake a weaker contract into the fixture.
    """
    monkeypatch.setenv("PLAYER_DATA_URL", "")
    # `response_shape` represents a list by its FIRST element only, so this
    # contract pins skill-player fields (rank, proj_points.*, blurb) and does
    # NOT cover DST's yahoo_rank/espn_rank. Those are asserted directly in
    # tests/integration/test_app.py::test_populated_cache_is_served.
    main._state["projections"] = [
        {
            "id": "p_8f3a21",
            "name": "Deebo Samuel",
            "pos": "WR",
            "team": "SF",
            "rank": 3,
            "proj_points": {"floor": 5.2, "expected": 12.4, "ceiling": 20.1},
            "blurb": "Faces a secondary allowing the third-most yards to WRs.",
        },
        {
            "id": "p_9a2f77",
            "name": "Baltimore",
            "pos": "DST",
            "team": "BAL",
            "yahoo_rank": 1,
            "espn_rank": 3,
        },
    ]
    main._state["upstream_healthy"] = True
    main._state["last_updated"] = "2026-09-30T14:00:00+00:00"
    committed = json.loads(RESPONSES.read_text())

    with TestClient(app) as client:
        actual = {
            "/health": response_shape(client.get("/health").json()),
            "/projections": response_shape(client.get("/projections").json()),
        }

    main._state["projections"] = []
    main._state["upstream_healthy"] = False
    main._state["last_updated"] = None

    assert actual == committed, SHAPE_HINT
