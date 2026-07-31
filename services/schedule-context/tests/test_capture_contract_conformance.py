"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path, over the real
adapter and a real (mocked-transport) HTTP read, and validating the rows it
actually emits — on the degraded paths as well as the happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from schedule_context.capture import (
    REST,
    SIGNAL_TYPES,
    SITUATIONAL,
    expected_floor,
    expected_floors,
)

from .conftest import NOW, SpyLake, run_capture, season_rows, to_csv

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "schedule-context.json").read_text()
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
    envelopes = await run_capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        # `all(...)` over an empty list is True, so the count is asserted
        # separately: a capture that emitted nothing would otherwise validate
        # vacuously and this file would be decoration.
        assert len(envelope.signals) == 32, envelope.signal_type
    validate(envelopes)


async def test_a_complete_capture_reports_full_coverage():
    """Not decoration: `expected` is floored independently of the fetch, so a
    healthy pass reaching the floor is what proves the floor is right."""
    envelopes = await run_capture(SpyLake())
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == expected_floors(2)[signal_type]
        assert envelope.coverage.present == 32
        assert envelope.coverage.ratio == 1.0
        assert envelope.errors == []


async def test_two_records_per_game_one_per_participating_club():
    """The spec's own definition of a complete capture."""
    envelopes = await run_capture(SpyLake())
    for envelope in envelopes.values():
        by_game: dict[str, set[str]] = {}
        for row in envelope.signals:
            by_game.setdefault(row["game_id"], set()).add(row["team_id"])
        assert len(by_game) == 16
        assert all(len(teams) == 2 for teams in by_game.values())
        assert len({row["team_id"] for row in envelope.signals}) == 32


async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await run_capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_failed_capture_writes_a_present_zero_envelope(monkeypatch):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("upstream down")

    monkeypatch.setattr("schedule_context.capture.fetch_season_games", boom)
    lake = SpyLake()

    with pytest.raises(httpx.ConnectError):
        await run_capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert len(lake.writes) == len(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_a_total_outage_reports_a_low_ratio_not_a_perfect_one(monkeypatch):
    """`Coverage.ratio` reads 1.0 when `expected` is 0, so a failure envelope
    that floored to nothing would report a dead upstream as perfect coverage.
    The floor passed to `fail_capture` is the week's real one, not 1."""

    async def boom(*args, **kwargs):
        raise httpx.ReadTimeout("upstream gone")

    monkeypatch.setattr("schedule_context.capture.fetch_season_games", boom)
    lake = SpyLake()

    with pytest.raises(httpx.ReadTimeout):
        await run_capture(lake, week=3)

    assert len(lake.writes) == len(SIGNAL_TYPES)
    for envelope in lake.writes:
        assert envelope.coverage.expected == expected_floor(3) == 32
        assert envelope.coverage.ratio == 0.0


async def test_an_http_error_from_the_real_adapter_is_classified_not_swallowed():
    """The failure arrives through the adapter's own streaming read rather
    than a patched function, so a 500 the adapter forgot to `raise_for_status`
    on would show up here rather than as an empty successful capture."""
    lake = SpyLake()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(lake, status=503, week=4)
    assert len(lake.writes) == len(SIGNAL_TYPES)
    assert [e.coverage.present for e in lake.writes] == [0, 0]
    assert all(
        e["reason"] == "http_status" for lake_e in lake.writes for e in lake_e.errors
    )


def _patched_week_two(**overrides) -> str:
    """A three-week season with week 2's first game altered."""
    rows = season_rows(weeks=3)
    first = next(row for row in rows if row["week"] == "2")
    first.update(overrides)
    return to_csv(rows)


async def test_an_unresolvable_venue_costs_a_situational_row_and_nothing_else():
    """It must not silently become the home club's stadium — that produces
    plausible, schema-valid travel numbers that are wrong by four thousand
    miles."""
    csv = _patched_week_two(location="Neutral", stadium="Somewhere Unnamed")
    envelopes = await run_capture(SpyLake(), csv=csv)

    situational, rest = envelopes[SITUATIONAL], envelopes[REST]
    assert situational.coverage.present == 30
    assert len(situational.coverage.missing) == 2
    assert len(situational.errors) == 2
    assert {e["reason"] for e in situational.errors} == {"venue_unresolved"}
    # Rest needs no venue, so it is untouched — splitting the two signal types
    # is what keeps one upstream gap from costing both.
    assert rest.coverage.present == 32
    assert rest.errors == []


async def test_an_unslotted_kickoff_is_recorded_rather_than_guessed():
    """A game the feed lists with no kickoff time is a real ambiguity. Both
    signal types drop the row with `kickoff_unscheduled` rather than invent an
    instant, which would corrupt both clubs' rest chains for the season."""
    envelopes = await run_capture(SpyLake(), csv=_patched_week_two(gametime=""))

    for envelope in envelopes.values():
        assert envelope.coverage.present == 30
        assert len(envelope.errors) == 2
        assert {e["reason"] for e in envelope.errors} == {"kickoff_unscheduled"}


async def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


async def test_captured_at_is_the_frozen_instant_not_wall_clock():
    """Both envelopes of one pass must carry the same `captured_at`: the lake
    key is `<captured_at>-<signal_type>`, and two instants would scatter one
    pass across two prefixes."""
    envelopes = await run_capture(SpyLake())
    assert {e.captured_at for e in envelopes.values()} == {NOW}
