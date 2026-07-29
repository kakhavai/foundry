"""Every collector's emitted envelope conforms to the committed contract.

Platform-level rather than per-service: the point is that the whole fleet
agrees, which no single service's test can assert.
"""

import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "contracts" / "signal-envelope" / "envelope.v1.schema.json").read_text()
)
FIXTURES = sorted((ROOT / "contracts" / "signal-envelope" / "fixtures").glob("*.json"))


def test_at_least_one_fixture_exists():
    """A conformance suite with no fixtures passes vacuously."""
    assert FIXTURES, "no envelope fixtures found — the gate would pass vacuously"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_fixture_conforms_to_the_envelope_contract(fixture):
    jsonschema.validate(json.loads(fixture.read_text()), SCHEMA)


def test_every_fixture_declares_a_known_collector():
    known = {"weather"}
    for fixture in FIXTURES:
        body = json.loads(fixture.read_text())
        assert body["collector"] in known, fixture.name
