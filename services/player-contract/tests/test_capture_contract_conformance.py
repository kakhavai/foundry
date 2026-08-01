"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.

The schema is doing real work here rather than restating the code. Two clauses
in particular are enforcement, not documentation:

* `player_id` is pinned to `^fdy-`. The upstream keys players by display NAME,
  so the single most dangerous bug available to this collector is publishing a
  raw upstream string as an identity. That pattern fails the conformance test
  rather than the generator six weeks later.
* the six cap-accounting fields are typed `"null"`, not `["integer", "null"]`.
  Fabricating one — from `apy`, most plausibly — becomes a contract violation.
"""

import gzip
import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import respx
from jsonschema import Draft202012Validator, FormatChecker

from player_contract.capture import (
    CONTRACT_STATUS,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    UNSOURCED_FIELDS,
    capture_player_contract,
)

from .conftest import (
    CANONICAL_IDS,
    NOW,
    SEASON,
    WEEK,
    contracts_csv,
    mock_identity,
    mock_upstream,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "player-contract.json").read_text(),
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


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_contract(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


@respx.mock
async def test_a_complete_capture_conforms(lake):
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


@respx.mock
async def test_every_envelope_is_written_to_the_lake(lake):
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    await capture(lake)

    written = {e.signal_type for e in lake.writes if e.collector == "player-contract"}
    assert written == set(SIGNAL_TYPES)


@respx.mock
async def test_a_failed_capture_writes_a_present_zero_envelope(lake):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""
    mock_upstream(respx.mock, status=503)
    mock_identity(respx.mock)

    with pytest.raises(httpx.HTTPStatusError):
        await capture(lake)

    written = [e for e in lake.writes if e.collector == "player-contract"]
    assert {e.signal_type for e in written} == set(SIGNAL_TYPES)
    for envelope in written:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1, (
            "expected: 0 makes Coverage.ratio read 1.0 — a total outage would "
            "report perfect coverage"
        )
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        assert envelope.errors, "a failure envelope with no errors explains nothing"


@respx.mock
async def test_a_degraded_capture_still_conforms(lake):
    """A pass where the identity seam is down and half the terms are blank still
    has to produce rows the generator can read — degraded paths are where a
    schema violation actually escapes, because the happy path is the one
    everybody looks at."""
    rows = [
        ("1", "Alpha Passer", "QB", "Packers", "TRUE", None, None, None, None),
        ("2", "Bravo Runner", "RB", "Bratislava Fog", "TRUE", 2023, 4, 0, 0),
    ]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert envelopes[CONTRACT_STATUS].signals
    validate(envelopes)


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


def test_the_deferred_incentive_half_has_no_surface_here():
    """`player_incentive_progress` was split out during 8E — the free feed
    carries no incentive data of any kind. Pinned so a later edit cannot
    reintroduce an empty signal type, an empty route or a stub field set that
    would make the deferral invisible."""
    assert SIGNAL_TYPES == (CONTRACT_STATUS,)
    document = json.dumps(FIELD_SCHEMAS).lower()
    for token in ("incentive", "escalator", "ltbe", "nltbe", "threshold"):
        assert token not in document, token


# ── the two kinds of null, one fixture per arm ───────────────────────────────


@respx.mock
async def test_the_six_unsourceable_fields_are_present_and_null_on_every_row(lake):
    """Present and null, never omitted. An absent key cannot distinguish "this
    source will never supply it" from "this row did not say", and a consumer
    deciding whether to buy a cap-accounting feed needs exactly that
    distinction."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.signals, "no rows; the loop below would pass vacuously"
    assert len(UNSOURCED_FIELDS) == 6
    for row in envelope.signals:
        for name in UNSOURCED_FIELDS:
            assert name in row, name
            assert row[name] is None, name
            assert row["null_field_reasons"][name] == "unsourced_by_upstream"


@respx.mock
async def test_a_row_nullable_field_is_reasoned_DIFFERENTLY_from_an_unsourced_one(
    lake,
):
    """The other arm. Echo's `guaranteed` is blank in the row and Echo's
    `team` is an ambiguous multi-club string — both null, and both null for a
    reason a paid feed could not fix, unlike the six above."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]
    echo = next(
        row
        for row in envelope.signals
        if row["player_id"] == CANONICAL_IDS["Echo Kicker"]
    )

    assert echo["guaranteed_total_usd"] is None
    reasons = echo["null_field_reasons"]
    assert reasons["guaranteed_total_usd"] == "absent_in_upstream_row"
    assert reasons["cap_hit_current_usd"] == "unsourced_by_upstream"


@respx.mock
async def test_a_populated_field_gets_no_null_reason(lake):
    """A reason for a field that is not null would make the block meaningless —
    a consumer filtering on it would discard perfectly good numbers."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]
    alpha = next(
        row
        for row in envelope.signals
        if row["player_id"] == CANONICAL_IDS["Alpha Passer"]
    )

    assert alpha["total_value_usd"] == 150000000
    assert "total_value_usd" not in alpha["null_field_reasons"]
    assert "team" not in alpha["null_field_reasons"]
    for name in UNSOURCED_FIELDS:
        assert name in alpha["null_field_reasons"]


@respx.mock
async def test_no_cap_field_is_ever_derived_from_apy(lake):
    """`apy` is average annual value; a cap hit is not, and the two agreeing on
    some rows is exactly what makes the substitution dangerous. The fixture sets
    `apy` to an unmistakable sentinel and the column is not even read."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert "999999999" not in json.dumps(envelope.to_dict())
