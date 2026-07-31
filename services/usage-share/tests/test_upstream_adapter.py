"""The adapter — the only module that knows the wire format.

What is worth testing here is not that CSV parses. It is the four decisions the
adapter makes on the way past: the header is validated before a single row is
mapped, the scoped week is selected as it parses rather than after, the
denominators are summed over the whole team rather than over the retained rows,
and a position the platform has no canonical form for is discarded rather than
counted missing.
"""

import httpx
import pytest
import respx
from collector_core.streaming import UpstreamSchemaError
from datetime import UTC, datetime, timedelta

from usage_share.adapters.upstream import (
    REQUIRED_COLUMNS,
    UpstreamRowError,
    fetch_week_usage,
    parse_row,
    source_ref,
)

from .conftest import (
    CSV_COLUMNS,
    NOW,
    SAMPLE_RECORDS,
    UPSTREAM_FOR_SEASON,
    to_csv,
)


async def _fetch(body: str, **kwargs):
    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=body)
        )
        async with httpx.AsyncClient() as client:
            return await fetch_week_usage(
                2026, 1, client=client, now=NOW, **kwargs
            )


def test_source_ref_substitutes_the_season():
    """A URL without the placeholder would silently pin every capture to
    whichever season was baked in."""
    assert "2026" in source_ref(2026, 1)
    assert source_ref(2026, 1) != source_ref(2025, 1)


def test_the_required_columns_are_all_present_in_the_documented_shape():
    """The fixture is built from the real feed's header. A required column the
    fixture does not carry would make every test here fail for the wrong
    reason, and one it carries that the adapter forgot to require would let
    real drift through."""
    assert REQUIRED_COLUMNS
    assert REQUIRED_COLUMNS <= set(CSV_COLUMNS)


async def test_a_renamed_column_fails_loudly_before_any_row_is_mapped():
    """The Phase 8 failure-handling rule: an upstream that renames a field must
    fail the capture rather than map nulls into an append-only lake."""
    body = to_csv(SAMPLE_RECORDS).replace("targets", "tgts", 1)
    with pytest.raises(UpstreamSchemaError) as caught:
        await _fetch(body)
    assert "targets" in str(caught.value)


async def test_only_the_scoped_week_is_retained():
    """`SAMPLE_RECORDS` carries a week-2 row for a week-1 player. Keeping it
    would double KC's targets and halve every KC share."""
    usage = await _fetch(to_csv(SAMPLE_RECORDS))
    assert len(usage.rows) == 8
    assert usage.denominators["KC"].targets == 25


async def test_denominators_include_rows_the_position_filter_discards():
    """A lineman who catches a trick-play pass belongs in the team's targets.
    Narrowing first would shrink the base and inflate every receiver's share."""
    records = [
        record
        for record in SAMPLE_RECORDS
        if record["team"] == "KC" and record["week"] == "1"
    ]
    trick_play = dict(records[-1])
    trick_play.update(
        player_id="00-KC-OL1", position="T", targets="3", target_share="0"
    )
    usage = await _fetch(to_csv([*records, trick_play]))

    assert "00-KC-OL1" not in {row.upstream_player_id for row in usage.rows}
    assert usage.denominators["KC"].targets == 28


async def test_fb_and_hb_collapse_onto_rb():
    """The feed's roster vocabulary must not reach a consumer. `FB` is in the
    sample deliberately."""
    usage = await _fetch(to_csv(SAMPLE_RECORDS))
    positions = {row.upstream_player_id: row.position for row in usage.rows}
    assert positions["00-KC-RB1"] == "RB"
    assert set(positions.values()) <= {"QB", "RB", "WR", "TE"}


async def test_an_out_of_scope_position_is_not_an_error():
    """A defender is not a missing usage row — this collector never owed one."""
    usage = await _fetch(to_csv(SAMPLE_RECORDS))
    assert "00-KC-DE1" not in {row.upstream_player_id for row in usage.rows}
    assert usage.truncated is False


async def test_blank_count_cells_read_as_zero():
    """The real feed leaves a stat a player never recorded empty, not `0`."""
    usage = await _fetch(to_csv(SAMPLE_RECORDS))
    qb = next(r for r in usage.rows if r.upstream_player_id == "00-KC-QB1")
    assert qb.targets == 0
    assert qb.air_yards == 0.0


async def test_dropbacks_are_attempts_plus_sacks():
    usage = await _fetch(to_csv(SAMPLE_RECORDS))
    assert usage.denominators["KC"].dropbacks == 32
    assert usage.denominators["BUF"].dropbacks == 28


async def test_an_expired_deadline_truncates_rather_than_raising():
    """A pass that reports itself truncated is useful; one that throws away
    what it already parsed is not.

    Measured against the **wall clock**, not against the frozen `NOW` the pass
    describes — a deadline has to be checked against a clock that advances, and
    `NOW - 1h` is still in the future for a suite that describes a date next
    month. Getting that backwards is how a deadline silently never fires.
    """
    expired = datetime.now(tz=UTC) - timedelta(hours=1)
    usage = await _fetch(to_csv(SAMPLE_RECORDS), deadline=expired)
    assert usage.truncated is True
    assert usage.rows == []


async def test_an_upstream_error_raises_rather_than_returning_empty():
    """An empty result would be recorded as a successful capture of nothing."""
    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_week_usage(2026, 1, client=client, now=NOW)


def test_a_row_with_no_player_id_is_refused_not_keyed_on_nothing():
    record = dict.fromkeys(CSV_COLUMNS, "")
    record.update(position="WR", team="KC", game_id="g")
    with pytest.raises(UpstreamRowError) as caught:
        parse_row(record)
    assert caught.value.reason == "schema"


def test_a_row_with_no_game_id_is_refused():
    record = dict.fromkeys(CSV_COLUMNS, "")
    record.update(player_id="p", position="WR", team="KC")
    with pytest.raises(UpstreamRowError) as caught:
        parse_row(record)
    assert caught.value.reason == "schema"


def test_a_non_numeric_count_is_refused_rather_than_silently_zeroed():
    record = dict.fromkeys(CSV_COLUMNS, "")
    record.update(player_id="p", position="WR", team="KC", game_id="g", targets="lots")
    with pytest.raises(UpstreamRowError) as caught:
        parse_row(record)
    assert caught.value.reason == "malformed"
