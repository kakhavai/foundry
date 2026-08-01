"""The adapter — the only module that knows the wire format.

Kept separate from the capture tests so a wire-format change fails here rather
than as a puzzling coverage number three files away.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from collector_core.conditional import ETAGS, UpstreamUnchanged
from collector_core.streaming import UpstreamSchemaError

from broadcast_context.adapters.upstream import (
    UPSTREAM_URL,
    fetch_season_games,
    parse_kickoff,
    source_ref,
)

from .conftest import FEED_HEADER, feed_document, feed_row


async def _fetch(document: str, *, season: int = 2026, **response_kwargs):
    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                response_kwargs.pop("status", 200), text=document, **response_kwargs
            )
        )
        async with httpx.AsyncClient() as client:
            return await fetch_season_games(season, client=client)


# --------------------------------------------------------------------------
# parse_kickoff — the Eastern-clock claim
# --------------------------------------------------------------------------


def test_a_september_kickoff_is_read_as_eastern_daylight_time():
    """`replace(tzinfo=UTC)` instead of the feed's zone would move every
    kickoff by four hours, and nothing downstream could tell."""
    parsed = parse_kickoff("2026-09-13", "13:00")
    assert parsed.utcoffset().total_seconds() == -4 * 3600
    assert parsed.astimezone(UTC) == datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


def test_a_december_kickoff_crosses_into_standard_time():
    """The season crosses the November DST transition, so a fixed -05:00 is
    wrong for the early games and a fixed -04:00 for the late ones. This is
    the pair that catches either."""
    parsed = parse_kickoff("2026-12-20", "13:00")
    assert parsed.utcoffset().total_seconds() == -5 * 3600
    assert parsed.astimezone(UTC) == datetime(2026, 12, 20, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("gameday", "gametime"),
    [("", "13:00"), ("2026-09-13", ""), ("2026-09-13", "TBD"), ("not-a-date", "13:00")],
)
def test_an_unusable_kickoff_is_none_rather_than_an_exception(gameday, gametime):
    """One unslotted game must not fail a whole season's capture, and the
    caller has two places to record it."""
    assert parse_kickoff(gameday, gametime) is None


# --------------------------------------------------------------------------
# fetch_season_games
# --------------------------------------------------------------------------


async def test_only_the_requested_season_is_kept():
    document = feed_document(
        [
            feed_row("2025_01_A_B", season=2025),
            feed_row("2026_01_C_D", season=2026),
        ]
    )
    games = await _fetch(document, season=2026)
    assert [g.game_id for g in games] == ["2026_01_C_D"]


async def test_preseason_is_excluded():
    """Preseason is not part of the competitive schedule and is not sold as a
    broadcast window; counting it would put phantom games in every week 1
    slot."""
    document = feed_document(
        [
            feed_row("2026_00_A_B", game_type="PRE", week=1),
            feed_row("2026_01_C_D", game_type="REG", week=1),
        ]
    )
    games = await _fetch(document)
    assert [g.game_id for g in games] == ["2026_01_C_D"]


async def test_postseason_is_kept():
    """The other arm of the same filter: `PRE` is excluded, `WC` is not."""
    document = feed_document(
        [feed_row("2026_19_A_B", game_type="WC", week=19, gameday="2027-01-09")]
    )
    games = await _fetch(document)
    assert [g.game_type for g in games] == ["WC"]


async def test_a_renamed_column_fails_the_capture_loudly():
    """Schema drift must fail with `reason=malformed` rather than map nulls
    into an append-only lake."""
    broken = feed_document([feed_row("2026_01_A_B")]).replace(
        "gametime", "kickoff_time", 1
    )
    with pytest.raises(UpstreamSchemaError):
        await _fetch(broken)


async def test_a_column_the_adapter_never_reads_may_disappear():
    """`required_columns` names exactly what is read — no more — so an
    unrelated column vanishing upstream does not fail a capture that never
    used it."""
    header = FEED_HEADER.replace(",referee", "")
    rows = [
        ",".join(
            part
            for index, part in enumerate(feed_row("2026_01_A_B").split(","))
            if FEED_HEADER.split(",")[index] != "referee"
        )
    ]
    games = await _fetch("\n".join([header, *rows]) + "\n")
    assert [g.game_id for g in games] == ["2026_01_A_B"]


async def test_a_304_raises_upstream_unchanged_rather_than_an_http_error():
    """`raise_for_status()` gates on `is_success`, which is 2xx only, so a
    `304` raises `HTTPStatusError` like any other non-2xx. Without the
    conditional-GET path every unchanged upstream would become a capture
    failure writing `present: 0` over healthy data."""
    ETAGS.set(UPSTREAM_URL, '"v1"')
    with pytest.raises(UpstreamUnchanged):
        await _fetch("", status=304)


async def test_the_etag_is_committed_only_after_a_complete_read():
    """An ETag claims we hold the whole document. Committed on the response
    headers instead, a truncated read would 304 forever and the collector
    would report itself healthy on partial data until the upstream
    republished."""
    ETAGS.clear()
    await _fetch(feed_document([feed_row("2026_01_A_B")]), headers={"ETag": '"v2"'})
    assert ETAGS.get(UPSTREAM_URL) == '"v2"'


def test_the_source_ref_is_the_etag_cache_key():
    """The same string the envelope records, deliberately: two copies of a URL
    is one copy too many, and drift between them is invisible."""
    assert source_ref(2026, 1) == UPSTREAM_URL
    assert UPSTREAM_URL.endswith("games.csv")
