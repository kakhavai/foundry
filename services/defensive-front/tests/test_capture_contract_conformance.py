"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `ratings.py` leaves it entirely
green. This file closes that gap by running the real capture path and
validating the rows it actually emits, on the degraded paths as well as the
happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from defensive_front.capture import (
    EXPECTED_FLOOR,
    NULL_FIELD_REASON,
    SIGNAL_TYPES,
    STRENGTH,
)

from .conftest import Feeds, SpyLake, by_team, run_capture

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "defensive-front.json").read_text(),
)["signal_types"]


def validate(envelopes: dict) -> None:
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)


async def test_a_complete_capture_conforms():
    envelopes = await run_capture(Feeds(), lake=SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_failed_capture_writes_a_present_zero_envelope():
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the
    last good capture is not overwritten by an empty one."""
    lake = SpyLake()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(pbp_status=503), lake=lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1
        assert envelope.errors


async def test_a_participation_outage_is_fatal_rather_than_field_level():
    """**A deliberate departure from `team-scheme`**, which degrades this same
    feed to a null field. There it bought one field of thirteen; here it
    carries seven of sixteen plus the guard's whole independent variable, and
    a row with every pressure column null is a complete-looking row describing
    nothing."""
    lake = SpyLake()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(participation_status=500), lake=lake)
    assert all(e.coverage.present == 0 for e in lake.writes)


# --------------------------------------------------------------------------
# The degraded paths — where a conformance test usually stops looking
# --------------------------------------------------------------------------


async def test_a_roster_outage_conforms_and_nulls_two_fields():
    """Field-level, not fatal: without the roster map there is no front
    membership, so continuity and absences go null with the reason on the row.
    Fourteen other fields are unaffected."""
    envelopes = await run_capture(Feeds(players_status=500), lake=SpyLake())
    validate(envelopes)
    for row in by_team(envelopes).values():
        assert row["front_continuity_index"] is None
        assert row["key_absences"] == []
        assert "players_unavailable" in row["degraded_upstreams"]
        assert row["pressure_rate_generated"] is not None, (
            "a roster outage must not touch the pressure metrics"
        )


async def test_an_injury_outage_conforms_and_nulls_only_absences():
    envelopes = await run_capture(Feeds(injuries_status=500), lake=SpyLake())
    validate(envelopes)
    for row in by_team(envelopes).values():
        assert row["key_absences"] == []
        assert row["degraded_upstreams"] == ["injuries_unavailable"]
        assert row["front_continuity_index"] is not None, (
            "the injury feed has nothing to do with continuity"
        )


async def test_a_healthy_pass_declares_no_degradation():
    """The negative arm. Without it, a collector that reported every feed as
    degraded on every pass would pass both tests above."""
    for row in by_team(await run_capture(Feeds(), lake=SpyLake())).values():
        assert row["degraded_upstreams"] == []


# --------------------------------------------------------------------------
# The nulls that are nulls BY NECESSITY
# --------------------------------------------------------------------------


async def test_the_unsourceable_fields_are_null_with_a_reason():
    """An unsourceable value is a null WITH a machine-readable reason — never
    a default, never quietly dropped from the schema, and never derived from a
    different quantity that happens to be available."""
    for row in by_team(await run_capture(Feeds(), lake=SpyLake())).values():
        for field in NULL_FIELD_REASON:
            assert row[field] is None
            assert row["null_field_reason"][field]
        assert row["null_field_reason"] == NULL_FIELD_REASON


def test_the_schema_forbids_a_value_for_the_unsourceable_fields():
    """`"type": "null"`, not `["number", "null"]`. A collector that later
    "filled in" yards before contact from adjusted line yards would then fail
    conformance rather than publish a plausible wrong number under a name the
    generator trusts."""
    properties = FIELD_SCHEMAS[STRENGTH]["properties"]
    for field in NULL_FIELD_REASON:
        assert properties[field]["type"] == "null", field


def test_the_schema_restricts_the_unit_enum():
    """The other narrowing. Left open, a row claiming a synthesised
    `interior`/`edge` split would validate."""
    assert FIELD_SCHEMAS[STRENGTH]["properties"]["unit"]["enum"] == ["overall"]
    assert FIELD_SCHEMAS[STRENGTH]["additionalProperties"] is False


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows
    go unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


def test_the_schema_carries_no_scaffold_marker():
    """`tests/test_placeholder_schemas.py` enforces this repo-wide; asserting
    it here too means the service's own suite fails first, where the fix is."""
    assert "$comment" not in FIELD_SCHEMAS[STRENGTH]
    assert not {"key", "observed_at", "value"} <= set(
        FIELD_SCHEMAS[STRENGTH]["properties"]
    )


async def test_every_declared_row_field_is_actually_emitted():
    """`required` on the schema proves the rows carry the fields; this proves
    the schema is not describing a field nothing produces — the drift that
    only ever shows up in the generator."""
    envelopes = await run_capture(Feeds(), lake=SpyLake())
    emitted = {key for row in envelopes[STRENGTH].signals for key in row}
    assert emitted == set(FIELD_SCHEMAS[STRENGTH]["properties"])
    assert emitted == set(FIELD_SCHEMAS[STRENGTH]["required"])


async def test_the_envelope_names_the_artifact_it_read():
    """`source_ref` is what makes a lake object reproducible a season later."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.upstream.source_ref.endswith("play_by_play_2026.csv.gz")
    assert envelope.upstream.adapter == "nflverse-defensive-front"
    assert envelope.coverage.expected == EXPECTED_FLOOR[STRENGTH]
