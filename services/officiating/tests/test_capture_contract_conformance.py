"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path from the wire format
up, and validating the rows it actually emits, on the degraded and failed paths
as well as the happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import respx
from jsonschema import Draft202012Validator, FormatChecker

from officiating.capture import (
    ASSIGNMENT,
    EXPECTED_FLOOR,
    RATES,
    SIGNAL_TYPES,
    capture_officiating,
)

from .conftest import NOW, SEASON, SpyLake, mock_upstreams, season_of

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "officiating.json").read_text(),
)["signal_types"]


def validate(envelopes: dict) -> int:
    """Validate every row of every envelope, and report how many there were.

    The count is returned rather than discarded because a loop over an empty
    list validates nothing and passes — the `all([]) is True` failure wearing a
    different hat. Every caller asserts on it.
    """
    validated = 0
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)
            validated += 1
    return validated


async def capture(lake, *, week: int = 1, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_officiating(
            SEASON,
            week,
            client=client,
            lake=lake,
            now=NOW,
            **kwargs,
        )


@respx.mock
async def test_a_complete_capture_conforms():
    games = season_of()
    mock_upstreams(games)

    envelopes = await capture(SpyLake())

    assert set(envelopes) == set(SIGNAL_TYPES)
    assert len(envelopes[ASSIGNMENT].signals) == len(games)
    assert len(envelopes[RATES].signals) == 8
    assert validate(envelopes) == len(games) + 8


@respx.mock
async def test_the_crosswalk_produces_modern_game_ids():
    """The spec says the penalty/crew join is "by hand-maintained crosswalk".
    It is not: `games.csv` publishes `old_game_id`. This is that finding pinned
    — every emitted `game_id` is the modern form, never the legacy gamekey the
    officials feed speaks."""
    games = season_of()
    mock_upstreams(games)

    envelopes = await capture(SpyLake())

    emitted = {row["game_id"] for row in envelopes[ASSIGNMENT].signals}
    assert emitted == {game.game_id for game in games}
    assert not emitted & {game.legacy_game_id for game in games}


@respx.mock
async def test_the_two_spec_fields_with_no_upstream_are_null_with_a_reason():
    """Honest nulls, and the distinction that makes them honest.

    `assignment_announced_at` and `is_provisional` have no free source: the
    officials feed is post-game. They are emitted as `null` WITH a reason, not
    defaulted to `false`/now, and not dropped from the row — an untested value
    is a null with a reason, not a zero and not an absent key.
    """
    mock_upstreams(season_of())

    envelopes = await capture(SpyLake())
    rows = envelopes[ASSIGNMENT].signals

    assert rows
    for row in rows:
        assert "assignment_announced_at" in row, "dropped, not nulled"
        assert "is_provisional" in row, "dropped, not nulled"
        assert row["assignment_announced_at"] is None
        assert row["is_provisional"] is None
        assert (
            row["null_field_reason"]
            == "post_game_feed_has_no_assignment_publication_record"
        )


@respx.mock
async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    mock_upstreams(season_of())
    lake = SpyLake()

    await capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


@respx.mock
async def test_a_failed_crew_feed_writes_present_zero_and_re_raises():
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""
    mock_upstreams(
        season_of(), officials_response=httpx.Response(503, text="upstream down")
    )
    lake = SpyLake()

    with pytest.raises(httpx.HTTPStatusError):
        await capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1, (
            "expected: 0 makes Coverage.ratio read 1.0 — a total outage would "
            "report perfect coverage"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


@respx.mock
async def test_a_failed_schedule_feed_also_ends_the_pass():
    """Without the crosswalk there are no modern game ids, so neither signal
    type has anything to say. Asserted separately from the officials feed
    because they are fetched by different adapters and only one of them was
    wired into the failure path when this was written."""
    mock_upstreams(season_of(), games_response=httpx.Response(500, text="boom"))
    lake = SpyLake()

    with pytest.raises(httpx.HTTPStatusError):
        await capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


@respx.mock
async def test_missing_play_by_play_degrades_rather_than_failing():
    """The independent-failure design, and the case it was built for.

    `play_by_play_<season>.csv.gz` does not exist until a season's first games
    are played, so its absence is a *routine* condition rather than an outage.
    Routing it through `fail_capture` would re-raise and discard a perfectly
    good assignment capture already built in memory, over an upstream that
    signal type does not use.
    """
    games = season_of()
    mock_upstreams(games, pbp_response=httpx.Response(404, text="not found"))
    lake = SpyLake()

    envelopes = await capture(lake)

    # The assignment capture survives, complete and conformant.
    assert len(envelopes[ASSIGNMENT].signals) == len(games)
    assert envelopes[ASSIGNMENT].coverage.present == len(games)
    # The rates envelope is present:0 with a reason, not absent.
    assert envelopes[RATES].signals == []
    assert envelopes[RATES].coverage.present == 0
    assert envelopes[RATES].coverage.expected == EXPECTED_FLOOR[RATES]
    assert any(
        e["reason"] == "penalties_unavailable" for e in envelopes[RATES].errors
    ), envelopes[RATES].errors
    # Both are written: a gap in the lake must be explicit.
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert validate(envelopes) == len(games)


@respx.mock
async def test_the_degraded_pass_says_so_on_the_assignment_envelope_too():
    """A consumer reading only `game_crew_assignment` must be able to tell that
    `games_sampled` is 0 because play-by-play was missing, not because the
    season had not started."""
    mock_upstreams(season_of(), pbp_response=httpx.Response(404))

    envelopes = await capture(SpyLake())

    # Among the first entries, not necessarily the first: the library prepends
    # its own floor shortfall ahead of every priority error. What matters is
    # that it survived the 50-entry cap, not its exact index.
    reasons = [e["reason"] for e in envelopes[ASSIGNMENT].errors[:3]]
    assert "penalties_unavailable" in reasons, envelopes[ASSIGNMENT].errors[:3]
    assert all(row["games_sampled"] == 0 for row in envelopes[ASSIGNMENT].signals)


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


def test_the_schema_carries_no_placeholder_marker():
    """Belt-and-braces beside `tests/test_placeholder_schemas.py` at the repo
    root: that gate runs without this service's dependencies installed, and
    this one runs where the schema is actually exercised."""
    raw = (CONTRACTS / "collectors" / "officiating.json").read_text()
    assert "TODO(new-collector)" not in raw
    for signal_type, schema in FIELD_SCHEMAS.items():
        assert "$comment" not in schema, signal_type
        assert set(schema["required"]) != {"key", "observed_at", "value"}
