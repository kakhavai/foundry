"""Every collector's emitted envelope conforms to the committed contract.

Platform-level rather than per-service: the point is that the whole fleet
agrees, which no single service's test can assert.
"""

import json
from pathlib import Path

import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "contracts" / "signal-envelope" / "envelope.v1.schema.json").read_text()
)
FIXTURES = sorted((ROOT / "contracts" / "signal-envelope" / "fixtures").glob("*.json"))
COLLECTORS_DIR = ROOT / "contracts" / "signal-envelope" / "collectors"


def test_at_least_one_fixture_exists():
    """A conformance suite with no fixtures passes vacuously."""
    assert FIXTURES, "no envelope fixtures found — the gate would pass vacuously"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_fixture_conforms_to_the_envelope_contract(fixture):
    jsonschema.validate(json.loads(fixture.read_text()), SCHEMA)


def test_every_fixture_declares_a_known_collector():
    known = {"weather", "player-identity", "roster-scope", "usage-share"}
    for fixture in FIXTURES:
        body = json.loads(fixture.read_text())
        assert body["collector"] in known, fixture.name


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_fixture_signals_conform_to_the_collector_field_schema(fixture):
    """`envelope.v1.schema.json`'s `signals` property is deliberately opaque
    (`{"type": "array", "items": {"type": "object"}}`) — it proves an
    envelope's shape, not what is inside a collector's own rows. This is
    what actually catches a renamed or dropped field like `wind_speed_mph`
    or `playability` within a signal, which the generic schema cannot see.

    A fixture naming a collector or signal_type absent from
    `contracts/signal-envelope/collectors/` fails loudly rather than being
    skipped — the same vacuous-pass concern as having zero fixtures at all,
    just one level deeper.
    """
    body = json.loads(fixture.read_text())
    collector = body["collector"]
    signal_type = body["signal_type"]

    schema_path = COLLECTORS_DIR / f"{collector}.json"
    assert schema_path.exists(), (
        f"{fixture.name}: no per-collector field schema at {schema_path} "
        f"for collector {collector!r}"
    )
    collector_schema = json.loads(schema_path.read_text())

    signal_types = collector_schema.get("signal_types", {})
    assert signal_type in signal_types, (
        f"{fixture.name}: {schema_path} has no field schema for "
        f"signal_type {signal_type!r}"
    )
    field_schema = signal_types[signal_type]

    validator = Draft202012Validator(field_schema, format_checker=FormatChecker())
    assert body["signals"], f"{fixture.name}: fixture has no signal rows to validate"
    for row in body["signals"]:
        validator.validate(row)
