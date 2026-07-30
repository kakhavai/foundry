"""Producer-side contract conformance: `capture_scope`'s **real** output must
validate against `contracts/signal-envelope/collectors/roster-scope.json`.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures against the committed collector schema — both hand-maintained
files — so it catches fixture drift and never producer drift. A field rename
inside `scope.py` would leave it entirely green. This file closes that gap by
running the real capture path against mocked upstreams and validating the rows
it actually emits, in both signal types and on the degraded paths as well as
the happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import respx
from jsonschema import Draft202012Validator, FormatChecker

from roster_scope.capture import capture_scope
from roster_scope.scope import CHANGE_SIGNAL, MEMBERSHIP_SIGNAL

from .conftest import NOW, SpyLake, full_league_csv, membership_row

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "roster-scope.json").read_text()
)["signal_types"]

FEED = "https://feed.test/{season}.csv"
FEED_2026 = "https://feed.test/2026.csv"


@pytest.fixture(autouse=True)
def _feed_url(monkeypatch):
    monkeypatch.setattr("roster_scope.adapters.depth_chart.DEPTH_CHART_URL", FEED)


def validate(envelopes: dict) -> None:
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)


async def capture(lake, *, week=1, previous_rows=None):
    if previous_rows is not None:
        from .conftest import make_membership_envelope

        # Seeded in the *previous* week's partition, so the ledger's
        # week - 1 fallback is what finds it — the path that carries grace
        # state across a week boundary.
        lake.write(
            make_membership_envelope(list(previous_rows), version=1, week=week - 1)
        )
    async with httpx.AsyncClient() as client:
        return await capture_scope(2026, week, client=client, lake=lake, now=NOW)


@respx.mock
async def test_a_complete_capture_conforms():
    respx.get(FEED_2026).mock(return_value=httpx.Response(200, text=full_league_csv()))
    result = await capture(SpyLake())
    assert result[MEMBERSHIP_SIGNAL].signals, "nothing captured to validate"
    assert result[CHANGE_SIGNAL].signals, "no change events to validate"
    validate(result)


@respx.mock
async def test_the_total_outage_envelope_conforms():
    """The degraded envelope is still a contract-bearing artifact — it lands in
    the append-only lake exactly like a healthy one."""
    respx.get(FEED_2026).mock(return_value=httpx.Response(503))
    result = await capture(SpyLake())
    assert result[MEMBERSHIP_SIGNAL].signals, "the DST slots must still be present"
    validate(result)


@respx.mock
async def test_grace_and_excluded_rows_conform():
    """Carry-forward rows take a different construction path from resolved
    ones, so they get their own conformance check."""
    respx.get(FEED_2026).mock(return_value=httpx.Response(503))
    lake = SpyLake()
    result = await capture(
        lake,
        previous_rows=[
            membership_row("fdy-falling-out", version=1),
            membership_row(
                "fdy-expiring", version=1, status="grace", grace_expires_week=1
            ),
        ],
        week=4,
    )
    statuses = {r["membership_status"] for r in result[MEMBERSHIP_SIGNAL].signals}
    assert {"grace", "excluded"} <= statuses
    validate(result)


@respx.mock
async def test_the_ledger_failure_envelope_conforms():
    respx.get(FEED_2026).mock(return_value=httpx.Response(200, text=full_league_csv()))
    async with httpx.AsyncClient() as client:
        result = await capture_scope(
            2026, 1, client=client, lake=SpyLake(fail_list=True), now=NOW
        )
    for envelope in result.values():
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows
    go unvalidated by both this file and the repo-root suite."""
    from roster_scope.capture import SIGNAL_TYPES

    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
