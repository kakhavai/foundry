"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `transactions.py` leaves it entirely
green. This file closes that gap by running the real capture path and validating
the rows it actually emits, on the degraded paths as well as the happy one.
"""

import json
from datetime import timedelta
from pathlib import Path

import httpx
import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from roster_transactions.capture import (
    SIGNAL_TYPE,
    SIGNAL_TYPES,
    capture_roster_transactions,
)
from roster_transactions.windows import week_window

from .conftest import NOW, SpyLake, capture_with

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "roster-transactions.json").read_text()
)["signal_types"]

WEEK_START, _ = week_window(2026, 1)


def validate(envelopes: dict) -> int:
    """Validate every row of every envelope; return how many were checked.

    The count is returned rather than discarded because a validator that runs
    over an empty list passes vacuously, and the callers assert on it.
    """
    checked = 0
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)
            checked += 1
    return checked


async def capture(lake, **kwargs):
    """The scaffolded adapter's own placeholder feed, end to end."""
    async with httpx.AsyncClient() as client:
        return await capture_roster_transactions(
            2026, 1, client=client, lake=lake, now=NOW, **kwargs
        )


async def test_a_complete_capture_conforms():
    envelopes = await capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    assert validate(envelopes) == 3, "all three placeholder rows must be checked"


async def test_a_complete_capture_reports_full_coverage():
    """Not decoration: `expected` counts elapsed intervals independently of the
    fetch, so a pass whose acknowledged window spans them all is what proves
    the window arithmetic agrees with the clock arithmetic."""
    envelopes = await capture(SpyLake())
    envelope = envelopes[SIGNAL_TYPE]
    assert envelope.coverage.expected > 1
    assert envelope.coverage.ratio == 1.0
    assert envelope.errors == []


async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert len(lake.writes) == len(SIGNAL_TYPES)


async def test_a_failed_capture_writes_a_present_zero_envelope(monkeypatch):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("upstream down")

    monkeypatch.setattr("roster_transactions.capture.fetch_manifest", boom)
    lake = SpyLake()

    with pytest.raises(httpx.ConnectError):
        await capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert len(lake.writes) == len(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected > 1, (
            "a mid-week outage owes hundreds of intervals; flooring it to 1 "
            "would make the ratio read better than the truth"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_a_feed_that_fails_mid_stream_also_writes_a_failure_envelope(monkeypatch):
    """The manifest can succeed and the feed still die. That path has its own
    `fail_capture` call, and without it the pass would return a half-read week
    as if it were complete."""

    async def manifest(*args, **kwargs):
        from .conftest import fake_window

        return fake_window(WEEK_START, NOW)

    async def stream(*args, **kwargs):
        raise httpx.ReadTimeout("feed stalled")
        yield  # pragma: no cover - unreachable, makes this an async generator

    monkeypatch.setattr("roster_transactions.capture.fetch_manifest", manifest)
    monkeypatch.setattr("roster_transactions.capture.stream_rows", stream)
    lake = SpyLake()

    with pytest.raises(httpx.ReadTimeout):
        await capture(lake)

    assert len(lake.writes) == 1
    assert lake.writes[0].coverage.present == 0
    assert lake.writes[0].errors


async def test_a_void_row_conforms_and_keeps_its_reason(monkeypatch):
    """A rescinded move cannot be deleted from an append-only lake, so it
    arrives as a follow-up row. The schema must accept that shape."""
    original = _raw(hours=3, player="fdy-0001")
    retraction = _raw(hours=9, player="fdy-0001") | {
        "is_void": "true",
        "void_reason": "failed physical",
        "supersedes": "rtx-0123456789abcdef",
    }
    envelopes = await capture_with(
        monkeypatch,
        rows=[original, retraction],
        now=NOW,
        covers_through=NOW,
    )
    assert validate(envelopes) == 2
    voided = [row for row in envelopes[SIGNAL_TYPE].signals if row["is_void"]]
    assert len(voided) == 1
    assert voided[0]["void_reason"] == "failed physical"
    assert voided[0]["supersedes"] == "rtx-0123456789abcdef"


async def test_an_ir_return_window_conforms(monkeypatch):
    """The one nested object in the row shape, and the one most likely to be
    emitted half-populated."""
    row = _raw(hours=5, player="fdy-0002") | {
        "transaction_type": "ir_designated_return",
        "return_window_opens_at": "2026-10-01T00:00:00Z",
        "return_window_must_activate_by": "2026-10-22T00:00:00Z",
    }
    envelopes = await capture_with(
        monkeypatch, rows=[row], now=NOW, covers_through=NOW
    )
    assert validate(envelopes) == 1
    window = envelopes[SIGNAL_TYPE].signals[0]["return_window"]
    assert window == {
        "opens_at": "2026-10-01T00:00:00Z",
        "must_activate_by": "2026-10-22T00:00:00Z",
    }


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
    assert len(SIGNAL_TYPES) == 1


def test_the_committed_fixture_matches_what_capture_emits():
    """The repo-root conformance suite validates a committed fixture. A fixture
    whose field names have drifted from the producer proves only that it is
    self-consistent, so this pins the two together."""
    fixture = json.loads(
        (CONTRACTS / "fixtures" / "roster-transactions-roster_transaction.json")
        .read_text()
    )
    assert fixture["collector"] == "roster-transactions"
    assert fixture["signal_type"] == SIGNAL_TYPE
    assert fixture["signals"], "a fixture with no rows validates vacuously"
    schema_fields = set(FIELD_SCHEMAS[SIGNAL_TYPE]["properties"])
    for row in fixture["signals"]:
        assert set(row) == schema_fields, sorted(set(row) ^ schema_fields)


def _raw(*, hours: int, player: str) -> dict[str, str]:
    announced = WEEK_START + timedelta(hours=hours)
    return {
        "transaction_type": "signing",
        "player_id": player,
        "position": "WR",
        "from_team": "",
        "to_team": "KC",
        "announced_at": announced.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "effective_at": (announced + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "eligible_from_week": "",
        "elevation_count_season": "",
        "confidence": "official",
        "is_void": "false",
        "void_reason": "",
        "supersedes": "",
        "source_ref": f"wire/{player}/{hours}",
    }
