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

from broadcast_context.capture import EXPECTED_FLOOR, SIGNAL, SIGNAL_TYPES

from .conftest import (
    NOW,
    SpyLake,
    by_game,
    feed_document,
    run_capture,
    season_rows,
    week_rows,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "broadcast-context.json").read_text(),
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
    envelopes = await run_capture(feed_document(season_rows()), lake=SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    assert len(envelopes[SIGNAL].signals) == 272
    validate(envelopes)


async def test_a_complete_capture_reports_full_coverage():
    """Not decoration: `expected` is floored independently of the fetch, so a
    healthy pass reaching the floor is what proves the floor is right."""
    envelopes = await run_capture(feed_document(season_rows()), lake=SpyLake())
    envelope = envelopes[SIGNAL]
    assert envelope.coverage.expected == EXPECTED_FLOOR[SIGNAL]
    assert envelope.coverage.ratio == 1.0
    assert envelope.errors == []


async def test_a_degraded_capture_still_conforms():
    """A week carrying an unslotted game exercises three null branches at
    once — `window_id`, `games_in_window`/`is_standalone`/`distribution`, and
    the coverage miss — and the rows must still validate."""
    rows = [*week_rows(1, drop_kickoff_for=2), *week_rows(2)]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    validate(envelopes)
    assert any(row["window_id"] is None for row in envelopes[SIGNAL].signals)


async def test_a_whole_row_matches_a_control():
    """A whole-row comparison, not a field spot-check.

    A test that asserts on a curated subset passes when the uncovered part
    breaks — including when a field is dropped entirely, which
    `additionalProperties: false` in the schema cannot catch either.
    """
    envelopes = await run_capture(feed_document(week_rows(1)), lake=SpyLake())
    rows = by_game(envelopes)

    # The Sunday-night game of fixture week 1: 20:20 ET on 2026-09-13,
    # standalone in its instant, primetime, and first seen this pass.
    assert rows["2026_01_A14_B14"] == {
        "game_id": "2026_01_A14_B14",
        "season": 2026,
        "week": 1,
        "game_type": "REG",
        "window_id": "snf",
        "network": None,
        "distribution": "national",
        "games_in_window": 1,
        "is_standalone": True,
        "is_primetime": True,
        "kickoff_at": "2026-09-14T00:20:00Z",
        "kickoff_eastern_time": "20:20",
        "kickoff_local_time": None,
        "regional_coverage_pct": None,
        "flex_status": "original",
        "previous_window_id": None,
        "flex_decided_at": None,
        "announced_at": None,
        "first_observed_at": "2026-09-15T12:00:00Z",
        "observed_window_count": 1,
        "point_in_time_basis": "first_observed",
        "null_field_reasons": {
            "network": "no_free_feed_carries_network",
            "kickoff_local_time": "venue_timezone_unavailable",
            "announced_at": "upstream_has_no_publication_instant",
            "regional_coverage_pct": "national_broadcast",
            "previous_window_id": "no_change_observed",
            "flex_decided_at": "no_change_observed",
        },
    }


async def test_a_regional_row_carries_the_other_null_reason():
    """The two `regional_coverage_pct` nulls must stay distinguishable.

    The spec says the field is null "for national" games. Here it is null for
    ALL of them, for two different reasons — one structural, one an upstream
    gap — and collapsing them into one value costs a consumer the only thing
    the null could have told it.
    """
    envelopes = await run_capture(feed_document(week_rows(1)), lake=SpyLake())
    rows = by_game(envelopes)

    national = rows["2026_01_A14_B14"]
    regional = rows["2026_01_A00_B00"]

    assert national["distribution"] == "national"
    assert (
        national["null_field_reasons"]["regional_coverage_pct"] == "national_broadcast"
    )
    assert regional["distribution"] == "regional"
    assert (
        regional["null_field_reasons"]["regional_coverage_pct"]
        == "no_free_regional_coverage_source"
    )


async def test_every_null_field_is_explained_and_every_explanation_names_a_null():
    """Both directions. One alone lets a reason drift onto a populated field
    (which reads as a gap that is not there) or a null slip through unexplained
    (which reads as an omission rather than a decision)."""
    rows = [*week_rows(1, drop_kickoff_for=1), *week_rows(2)]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    signals = envelopes[SIGNAL].signals
    assert len(signals) == 32

    for row in signals:
        nulls = {
            field
            for field, value in row.items()
            if value is None and field != "null_field_reasons"
        }
        assert set(row["null_field_reasons"]) == nulls, row


async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await run_capture(feed_document(season_rows()), lake=lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_failed_capture_writes_a_present_zero_envelope(monkeypatch):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("upstream down")

    monkeypatch.setattr("broadcast_context.capture.fetch_season_games", boom)
    lake = SpyLake()

    with pytest.raises(httpx.ConnectError):
        await run_capture(feed_document(season_rows()), lake=lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1, (
            "expected: 0 makes Coverage.ratio read 1.0 — a total outage would "
            "report perfect coverage"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_a_history_read_failure_ends_the_pass():
    """A lake that answers the fetch but not the history read must NOT degrade
    to 'no history': that publishes `original` for games previously recorded
    as flexed, into an append-only lake where the claim becomes evidence."""

    class BrokenHistoryLake(SpyLake):
        def list_keys(self, *args, **kwargs):
            raise RuntimeError("object store unreachable")

    lake = BrokenHistoryLake()
    with pytest.raises(RuntimeError):
        await run_capture(feed_document(season_rows()), lake=lake)

    assert [e.coverage.present for e in lake.writes] == [0]
    assert lake.writes[0].errors


async def test_the_upstream_reference_is_recorded_on_the_envelope():
    envelopes = await run_capture(feed_document(week_rows(1)), lake=SpyLake())
    upstream = envelopes[SIGNAL].upstream
    assert upstream.adapter == "nflverse-games"
    assert upstream.source_ref and upstream.source_ref.endswith("games.csv")
    assert envelopes[SIGNAL].captured_at == NOW


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
