import httpx
import pytest
import respx

from player_projections.client import MalformedSnapshotError, fetch_projections

S3_URL = "https://foundry-projections.s3.amazonaws.com/projections/latest.json"

MOCK_PLAYERS = [
    {
        "id": "p_allenjosh",
        "name": "Josh Allen",
        "team": "BUF",
        "pos": "QB",
        "rank": 1,
        "proj_points": {"floor": 18.4, "expected": 32.1, "ceiling": 41.7},
    },
    {
        "id": "p_jeffersonjustin",
        "name": "Justin Jefferson",
        "team": "MIN",
        "pos": "WR",
        "rank": 1,
        "proj_points": {"floor": 12.0, "expected": 26.4, "ceiling": 38.9},
    },
]


@respx.mock
async def test_fetch_projections_returns_players():
    respx.get(S3_URL).mock(
        return_value=httpx.Response(200, json={"players": MOCK_PLAYERS})
    )
    players = await fetch_projections(S3_URL)
    assert len(players) == 2
    assert players[0]["id"] == "p_allenjosh"


@respx.mock
async def test_fetch_projections_raises_on_upstream_error():
    respx.get(S3_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_projections(S3_URL)


@respx.mock
async def test_fetch_projections_raises_on_network_failure():
    respx.get(S3_URL).mock(side_effect=httpx.ConnectError("unreachable"))
    with pytest.raises(httpx.ConnectError):
        await fetch_projections(S3_URL)


@respx.mock
async def test_fetch_projections_drops_non_dict_elements():
    """Malformed elements in `players` must not reach callers as if they were
    valid records — a downstream frontend can't render a bare string or int."""
    respx.get(S3_URL).mock(
        return_value=httpx.Response(
            200, json={"players": ["garbage", 42, None, MOCK_PLAYERS[0]]}
        )
    )
    players = await fetch_projections(S3_URL)
    assert players == [MOCK_PLAYERS[0]]


@respx.mock
async def test_fetch_projections_all_non_dict_yields_empty_list():
    respx.get(S3_URL).mock(
        return_value=httpx.Response(200, json={"players": ["garbage", 42, None]})
    )
    players = await fetch_projections(S3_URL)
    assert players == []


@respx.mock
async def test_fetch_projections_wraps_invalid_encoding():
    """A body that fails to decode as text must raise MalformedSnapshotError,
    not leak an untyped UnicodeDecodeError. `\\x80\\x80\\x80\\x80` has no BOM
    and no null bytes, so json's encoding sniff picks utf-8, which then fails
    to decode the invalid start byte.
    """
    respx.get(S3_URL).mock(
        return_value=httpx.Response(200, content=b"\x80\x80\x80\x80")
    )
    with pytest.raises(MalformedSnapshotError):
        await fetch_projections(S3_URL)


@respx.mock
async def test_wrong_format_snapshot_is_rejected():
    """The schema's `format` is an enum across all three modes, so it cannot
    pin a document to its own URL. This check does — it catches a PPR document
    served at the standard URL, which is what a `PROJECTIONS_SNAPSHOT_URL` missing its
    `{format}` placeholder produces.
    """
    respx.get(S3_URL).mock(
        return_value=httpx.Response(
            200, json={"format": "ppr", "players": MOCK_PLAYERS}
        )
    )

    with pytest.raises(MalformedSnapshotError, match="standard"):
        await fetch_projections(S3_URL, expect_format="standard")


@respx.mock
async def test_matching_format_snapshot_is_accepted():
    respx.get(S3_URL).mock(
        return_value=httpx.Response(
            200, json={"format": "ppr", "players": MOCK_PLAYERS}
        )
    )

    players = await fetch_projections(S3_URL, expect_format="ppr")

    assert players == MOCK_PLAYERS


@respx.mock
async def test_missing_format_field_is_rejected_when_one_is_expected():
    """A document with no `format` at all cannot be confirmed as the right one."""
    respx.get(S3_URL).mock(
        return_value=httpx.Response(200, json={"players": MOCK_PLAYERS})
    )

    with pytest.raises(MalformedSnapshotError):
        await fetch_projections(S3_URL, expect_format="ppr")


@respx.mock
async def test_format_check_is_skipped_when_not_requested():
    """expect_format=None preserves the original behaviour for callers that
    do not know or care which document they are reading."""
    respx.get(S3_URL).mock(
        return_value=httpx.Response(
            200, json={"format": "half-ppr", "players": MOCK_PLAYERS}
        )
    )

    assert await fetch_projections(S3_URL) == MOCK_PLAYERS
