"""Producer-side contract conformance: `capture_identities`'s **real**
output must validate against the committed schemas.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both hand-maintained files — so it catches fixture drift
and never producer drift. A field rename inside `capture.py` would pass it.
This runs the real capture path against a mocked upstream and validates what
actually comes out, against both the generic envelope schema and the
per-collector field schema.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
from conftest import SpyLake, player, sleeper_document
from jsonschema import Draft202012Validator, FormatChecker

from player_identity.adapters.sleeper import PLAYERS_URL
from player_identity.capture import capture_identities
from player_identity.resolution import MissQueue, ResolutionIndex, ResolveQuery

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "player-identity.json").read_text()
)["signal_types"]


async def _capture() -> dict:
    """A capture exercising every optional field shape at least once: a
    rostered veteran, a suffixed name, a free agent with no number, and one
    queued miss."""
    document = sleeper_document(
        player("1"),
        player(
            "2",
            full_name="Odell Beckham Jr.",
            first_name="Odell",
            last_name="Beckham",
            team="MIA",
            number=3,
            position="WR",
            crosswalk=False,
        ),
        player(
            "3",
            full_name="Free Agent",
            first_name="Free",
            last_name="Agent",
            team=None,
            number=None,
            status="Active",
            birth_date=None,
            years_exp=None,
            position="K",
            crosswalk=False,
        ),
    )
    respx.get(PLAYERS_URL).mock(
        return_value=httpx.Response(200, json=document, headers={"ETag": 'W/"x"'})
    )

    misses = MissQueue()
    index = ResolutionIndex()
    query = ResolveQuery(raw_name="P. Mahomes", source="book", team="KC")
    misses.record(query, index.resolve(query, now=NOW), now=NOW)

    async with httpx.AsyncClient() as client:
        return await capture_identities(
            2026,
            1,
            client=client,
            lake=SpyLake(),
            now=NOW,
            misses=misses,
            index=index,
            roster_floor=0,
        )


@respx.mock
async def test_real_capture_output_conforms_to_the_envelope_contract():
    result = await _capture()

    validator = Draft202012Validator(ENVELOPE_SCHEMA, format_checker=FormatChecker())
    for envelope in result.values():
        validator.validate(envelope.to_dict())


@respx.mock
async def test_real_capture_signals_conform_to_the_collector_field_schema():
    """Validating a committed fixture only proves the fixture is
    self-consistent. This runs the producer."""
    result = await _capture()

    assert set(result) == set(FIELD_SCHEMAS), (
        "every emitted signal type needs a committed field schema"
    )
    for signal_type, envelope in result.items():
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        assert envelope.signals, f"{signal_type}: no rows captured to validate"
        for row in envelope.signals:
            validator.validate(row)


@respx.mock
async def test_every_contracted_field_is_actually_populated_somewhere():
    """A schema of all-optional properties passes on an empty object. This
    asserts the producer emits every key the contract declares required."""
    result = await _capture()

    for signal_type, envelope in result.items():
        required = set(FIELD_SCHEMAS[signal_type]["required"])
        for row in envelope.signals:
            assert required <= set(row), (
                f"{signal_type}: missing {sorted(required - set(row))}"
            )
