import json
from pathlib import Path

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from player_projections.client import fetch_projections

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


def test_fixture_validates_against_ppr_schema():
    schema = json.loads((CONTRACTS / "ppr.v1.schema.json").read_text())
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    Draft202012Validator(schema).validate(fixture)


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

    assert len(players) == 2
    assert {p["id"] for p in players} == {"p_8f3a21", "p_1c9e04"}
    assert players[0]["proj_points"]["expected"] == 12.4
