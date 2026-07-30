"""Tests for the shared streaming CSV reader.

Built against `respx`-mocked transports rather than a real upstream, and
against a fake collector's shape rather than any real one.

The property under test is memory, which a functional test cannot assert
directly. What it *can* assert is the mechanism that produces it: the body is
never requested as one string, rows arrive one at a time so a caller can
discard what it does not keep, and a chunk boundary landing mid-row does not
corrupt or drop it.
"""

import httpx
import pytest
import respx

from collector_core.streaming import (
    UpstreamSchemaError,
    UpstreamTooLarge,
    stream_csv_dicts,
)

URL = "https://upstream.test/feed.csv"

DOCUMENT = (
    "season,week,game_id,stadium\n2026,1,g1,Lambeau Field\n2026,2,g2,Soldier Field\n"
)


async def _collect(**kwargs) -> list[dict]:
    async with httpx.AsyncClient() as client:
        return [row async for row in stream_csv_dicts(client, URL, **kwargs)]


@respx.mock
async def test_rows_are_header_keyed():
    respx.get(URL).mock(return_value=httpx.Response(200, text=DOCUMENT))

    rows = await _collect()

    assert rows == [
        {"season": "2026", "week": "1", "game_id": "g1", "stadium": "Lambeau Field"},
        {"season": "2026", "week": "2", "game_id": "g2", "stadium": "Soldier Field"},
    ]


@respx.mock
async def test_a_quoted_comma_inside_a_field_is_not_split():
    """Stadium names carry commas. Splitting on `,` rather than parsing would
    silently shift every later column by one."""
    respx.get(URL).mock(
        return_value=httpx.Response(200, text='a,b\n"Foxborough, MA",x\n')
    )

    assert await _collect() == [{"a": "Foxborough, MA", "b": "x"}]


@respx.mock
async def test_a_row_split_across_chunk_boundaries_survives_intact():
    """The whole point of streaming is that the caller never sees the document
    as one string. A row straddling two chunks must not be dropped or halved --
    this is the bug the `remainder` carry-over exists to prevent."""
    body = "a,b\n" + "".join(f"row{i},v{i}\n" for i in range(500))
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))

    rows = await _collect()

    assert len(rows) == 500
    assert rows[0] == {"a": "row0", "b": "v0"}
    assert rows[-1] == {"a": "row499", "b": "v499"}


@respx.mock
async def test_a_document_with_no_trailing_newline_still_yields_its_last_row():
    respx.get(URL).mock(return_value=httpx.Response(200, text="a,b\nx,y"))

    assert await _collect() == [{"a": "x", "b": "y"}]


@respx.mock
async def test_rows_arrive_one_at_a_time_so_a_caller_can_discard_as_it_goes():
    """Rule 2 of the memory audit: filter to what you keep as you parse, not
    after. A caller must be able to `break` without the rest ever being
    materialized."""
    body = "a\n" + "".join(f"row{i}\n" for i in range(1000))
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))

    seen = []
    async with httpx.AsyncClient() as client:
        async for row in stream_csv_dicts(client, URL):
            seen.append(row)
            if len(seen) == 3:
                break

    assert len(seen) == 3


# --- schema drift ------------------------------------------------------------


@respx.mock
async def test_a_missing_required_column_fails_before_any_row_is_yielded():
    """An upstream that renames a field must fail loudly rather than map
    nulls into an append-only lake."""
    respx.get(URL).mock(return_value=httpx.Response(200, text="a,b\n1,2\n"))

    with pytest.raises(UpstreamSchemaError, match="missing column"):
        await _collect(required_columns={"a", "missing_one"})


@respx.mock
async def test_the_schema_error_names_every_missing_column():
    respx.get(URL).mock(return_value=httpx.Response(200, text="a\n1\n"))

    with pytest.raises(UpstreamSchemaError) as caught:
        await _collect(required_columns={"x", "y"})

    assert "x" in str(caught.value)
    assert "y" in str(caught.value)


@respx.mock
async def test_an_empty_document_is_an_error_not_an_empty_result():
    """Zero rows and 'the upstream served nothing' are different facts."""
    respx.get(URL).mock(return_value=httpx.Response(200, text=""))

    with pytest.raises(UpstreamSchemaError, match="empty document"):
        await _collect()


@respx.mock
async def test_a_header_only_document_is_zero_rows_not_an_error():
    respx.get(URL).mock(return_value=httpx.Response(200, text="a,b\n"))

    assert await _collect() == []


@respx.mock
async def test_an_http_error_propagates():
    respx.get(URL).mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        await _collect()


# --- the size ceiling --------------------------------------------------------


@respx.mock
async def test_an_unbounded_upstream_is_refused_rather_than_downloaded_forever():
    """Not a memory guard -- streaming already bounds that -- but a guard
    against an upstream that starts serving something unbounded, so a capture
    cannot download forever inside its deadline."""
    body = "a\n" + "".join(f"row{i}\n" for i in range(5000))
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(UpstreamTooLarge, match="exceeded"):
        await _collect(max_chars=100)


@respx.mock
async def test_a_document_inside_the_ceiling_is_not_refused():
    """Off-by-one guard: the ceiling must only fire once it is actually
    crossed."""
    respx.get(URL).mock(return_value=httpx.Response(200, text=DOCUMENT))

    assert len(await _collect(max_chars=len(DOCUMENT))) == 2


# --- short and long rows -----------------------------------------------------


@respx.mock
async def test_a_short_row_gets_empty_strings_rather_than_raising():
    respx.get(URL).mock(return_value=httpx.Response(200, text="a,b,c\n1,2\n"))

    assert await _collect() == [{"a": "1", "b": "2", "c": ""}]


@respx.mock
async def test_a_long_row_keeps_only_the_named_columns():
    respx.get(URL).mock(return_value=httpx.Response(200, text="a,b\n1,2,3,4\n"))

    assert await _collect() == [{"a": "1", "b": "2"}]


@respx.mock
async def test_blank_lines_are_skipped():
    respx.get(URL).mock(return_value=httpx.Response(200, text="a\n\n1\n\n2\n"))

    assert await _collect() == [{"a": "1"}, {"a": "2"}]
