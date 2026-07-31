"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from usage_share.capture import EXPECTED_FLOOR, SIGNAL_TYPES, capture_usage_share

from .conftest import (
    SAMPLE_PLAYER_ROWS,
    SAMPLE_TEAMS,
    NOW,
    SpyLake,
    full_league_csv,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "usage-share.json").read_text()
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
        return await capture_usage_share(
            2026, 1, client=client, lake=lake, now=NOW, **kwargs
        )


async def test_a_complete_capture_conforms(upstream):
    envelopes = await capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


async def test_the_published_shares_are_computed_from_the_published_bases(upstream):
    """The spec's whole premise: every share is recomputable from the
    `denominators` object travelling in the same row. If it is not, a consumer
    cannot audit a number that looks plausible and is wrong."""
    envelopes = await capture(SpyLake())
    rows = envelopes["player_usage_weekly"].signals
    assert len(rows) == SAMPLE_PLAYER_ROWS

    row = next(r for r in rows if r["player_id"] == "00-KC-WR1")
    bases = row["denominators"]
    # KC: 25 targets, 215 air yards, 22 carries, 32 dropbacks — summed over
    # every KC row in the week, including the defender the position filter drops.
    assert bases == {
        "team_offense_snaps": None,
        "team_dropbacks": 32,
        "team_targets": 25,
        "team_air_yards": 215.0,
        "team_carries": 22,
    }
    assert row["target_share"] == round(10 / 25, 6)
    assert row["air_yards_share"] == round(120 / 215, 6)
    assert row["carry_share"] == round(1 / 22, 6)
    assert row["wopr"] == round(1.5 * row["target_share"] + 0.7 * row["air_yards_share"], 6)


async def test_snaps_and_routes_are_null_rather_than_zero(upstream):
    """This feed carries no snap counts. `0.0` would read as "took no snaps",
    which is a fact this collector does not have."""
    envelopes = await capture(SpyLake())
    rows = envelopes["player_usage_weekly"].signals
    assert rows, "no rows to assert against"
    for row in rows:
        assert row["snap_share"] is None
        assert row["route_participation"] is None
        assert row["denominators"]["team_offense_snaps"] is None
        assert row["usage_source"] == "derived"


async def test_a_full_league_document_reaches_the_declared_floor(serve_upstream):
    """A floor nothing can ever reach is as wrong as no floor at all.

    32 teams x (11 offensive-skill slots + 1 denominators object) = 384, and a
    document carrying exactly that reports ratio 1.0 with no shortfall error.
    """
    serve_upstream(full_league_csv())
    envelopes = await capture(SpyLake())
    envelope = envelopes["player_usage_weekly"]

    assert envelope.coverage.expected == EXPECTED_FLOOR["player_usage_weekly"]
    assert envelope.coverage.present == EXPECTED_FLOOR["player_usage_weekly"]
    assert envelope.coverage.ratio == 1.0
    assert envelope.errors == []
    validate(envelopes)


async def test_every_envelope_is_written_to_the_lake(upstream):
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_failed_capture_writes_a_present_zero_envelope(serve_upstream):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""
    serve_upstream("", status=503)
    lake = SpyLake()

    with pytest.raises(httpx.HTTPStatusError):
        await capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert lake.writes, "nothing written — the loop below would pass vacuously"
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1, (
            "expected: 0 makes Coverage.ratio read 1.0 — a total outage would "
            "report perfect coverage"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_a_total_outage_reports_a_low_ratio_not_a_perfect_one(serve_upstream):
    """The single most consequential silent failure this collector can have.

    `Coverage.ratio` returns 1.0 when `expected` is 0, so a failure envelope
    that forgot to pass `expected=EXPECTED_FLOOR` would floor to 1 and report
    0/1 — bad, but nothing like as loud as 0/384. Anything at or above 0.05
    here means the floor stopped reaching the failure path.
    """
    serve_upstream("", status=503)
    lake = SpyLake()

    with pytest.raises(httpx.HTTPStatusError):
        await capture(lake)

    assert len(lake.writes) == len(SIGNAL_TYPES)
    for envelope in lake.writes:
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        assert envelope.coverage.ratio == 0.0


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


async def test_the_sample_document_yields_the_expected_shape(upstream):
    """Guards the fixture itself: several assertions elsewhere are stated in
    terms of `SAMPLE_PLAYER_ROWS` and `SAMPLE_TEAMS`, and a fixture edit that
    silently changed either would weaken them without failing anything."""
    envelopes = await capture(SpyLake())
    envelope = envelopes["player_usage_weekly"]
    assert len(envelope.signals) == SAMPLE_PLAYER_ROWS
    assert len({row["team"] for row in envelope.signals}) == SAMPLE_TEAMS
