"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap: it runs the real `capture_depth_chart` over the real
adapter (only the transport is mocked) and validates the rows it actually emits,
on the degraded paths as well as the happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import respx
from jsonschema import Draft202012Validator, FormatChecker

from depth_chart.adapters.upstream import source_ref
from depth_chart.capture import (
    CHART_SIGNAL,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    STABILITY_SIGNAL,
    capture_depth_chart,
)

from .conftest import NOW, SpyLake, depth_csv, full_chart_rows

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads((CONTRACTS / "collectors" / "depth-chart.json").read_text())[
    "signal_types"
]

FEED = source_ref(2026, 1)


def validate(envelopes: dict) -> None:
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)


async def capture(lake, rows=None, **kwargs):
    respx.get(FEED).mock(
        return_value=httpx.Response(
            200, text=depth_csv(full_chart_rows() if rows is None else rows)
        )
    )
    async with httpx.AsyncClient() as client:
        return await capture_depth_chart(
            2026, 1, client=client, lake=lake, now=NOW, **kwargs
        )


@respx.mock
async def test_a_complete_capture_conforms():
    envelopes = await capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


@respx.mock
async def test_every_declared_field_is_actually_emitted():
    """`additionalProperties: false` catches an *extra* field and `required`
    catches a *missing* one — `validate` above asserts both. This pins the key
    set exactly, so a rewrite of `build_chart_signal` cannot quietly drop a
    field the contract still promises."""
    envelopes = await capture(SpyLake())
    chart_row = envelopes[CHART_SIGNAL].signals[0]
    stability_row = envelopes[STABILITY_SIGNAL].signals[0]
    assert set(chart_row) == set(FIELD_SCHEMAS[CHART_SIGNAL]["required"])
    assert set(stability_row) == set(FIELD_SCHEMAS[STABILITY_SIGNAL]["required"])


@respx.mock
async def test_a_complete_capture_reports_full_coverage():
    """Not decoration: `expected` is declared from config independently of the
    fetch, so a healthy pass reaching it is what proves the floor is right."""
    envelopes = await capture(SpyLake())
    assert envelopes
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.ratio == 1.0
        assert envelope.coverage.missing == []
        assert envelope.errors == []


@respx.mock
async def test_the_two_signal_types_agree_on_the_chart_they_describe():
    """One hash per group, shared. Two independent hashings could disagree, and
    a consumer joining an ordering to its stability row would then silently
    pair mismatched snapshots."""
    envelopes = await capture(SpyLake())
    chart_hashes = {
        (row["team"], row["position"]): row["chart_hash"]
        for row in envelopes[CHART_SIGNAL].signals
    }
    stability_hashes = {
        (row["team"], row["position"]): row["chart_hash"]
        for row in envelopes[STABILITY_SIGNAL].signals
    }
    assert len(chart_hashes) == EXPECTED_FLOOR[CHART_SIGNAL]
    assert chart_hashes == stability_hashes


@respx.mock
async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


@respx.mock
async def test_a_failed_capture_writes_a_present_zero_envelope():
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""
    respx.get(FEED).mock(side_effect=httpx.ConnectError("upstream down"))
    lake = SpyLake()

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await capture_depth_chart(2026, 1, client=client, lake=lake, now=NOW)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert len(lake.writes) == len(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        assert envelope.coverage.ratio == 0.0, (
            "a total outage must not report perfect coverage — `expected: 0` "
            "makes Coverage.ratio read 1.0"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


@respx.mock
async def test_a_schema_break_fails_the_capture_rather_than_writing_nulls():
    """A renamed upstream column must fail loudly with `reason=malformed`. The
    alternative — mapping nulls into an append-only lake — is unrecoverable,
    because nothing ever rewrites a lake object."""
    broken = depth_csv(full_chart_rows()).replace("pos_rank", "position_rank", 1)
    respx.get(FEED).mock(return_value=httpx.Response(200, text=broken))
    lake = SpyLake()

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await capture_depth_chart(2026, 1, client=client, lake=lake, now=NOW)

    assert len(lake.writes) == len(SIGNAL_TYPES)
    reasons = {error["reason"] for e in lake.writes for error in e.errors}
    assert reasons == {"malformed"}, reasons


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
