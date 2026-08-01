"""Tests for `stream_csv_dicts`'s two width-and-weight options.

Split out of `test_streaming.py` the way `test_lake_offloading.py` is split
out of `test_lake.py`: same module under test, a distinct concern with its own
setup. Here that setup is a real gzip container, which every test in this file
needs and no test in the other one does.

Both options exist because of nflverse's play-by-play release, which
`officiating` is the first collector to read and `defense-vs-position` will be
the second: **93.4 MiB** as `.csv`, **18.2 MiB** as `.csv.gz`, 48,771 rows of
**372** columns. The measurements that motivated each are in
`collector_core.streaming`'s own docstrings.

What these tests can assert is the mechanism rather than the saving: that the
inflater and the decoder are both incremental, that the size ceiling counts
what the collector parses rather than what it downloads, and that a projected
row is narrow on **every** code path that builds one — including the trailing
row, which is built somewhere else entirely.
"""

import zlib

import httpx
import pytest
import respx

from collector_core.streaming import (
    GZIP_WBITS,
    MAX_INFLATED_CHUNK,
    UpstreamSchemaError,
    UpstreamTooLarge,
    _text_chunks,
    stream_csv_dicts,
)

URL = "https://upstream.test/feed.csv"
GZ_URL = "https://upstream.test/feed.csv.gz"

DOCUMENT = (
    "season,week,game_id,stadium\n2026,1,g1,Lambeau Field\n2026,2,g2,Soldier Field\n"
)


def _gzipped(text: str) -> bytes:
    """`text` as a real gzip container, the way a release asset serves it.

    A gzip *container* — header, deflate stream, CRC and length trailer — not
    a bare deflate stream, because that is what `.csv.gz` is and the two need
    different `wbits`.
    """
    compressor = zlib.compressobj(9, zlib.DEFLATED, GZIP_WBITS)
    return compressor.compress(text.encode("utf-8")) + compressor.flush()


async def _collect(url: str, **kwargs) -> list[dict]:
    async with httpx.AsyncClient() as client:
        return [row async for row in stream_csv_dicts(client, url, **kwargs)]


# ---------------------------------------------------------------------------
# gzipped artifacts
# ---------------------------------------------------------------------------


@respx.mock
async def test_a_gzipped_document_is_inflated_into_rows():
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_gzipped(DOCUMENT)))

    rows = await _collect(GZ_URL, gzipped=True)

    assert rows == [
        {"season": "2026", "week": "1", "game_id": "g1", "stadium": "Lambeau Field"},
        {"season": "2026", "week": "2", "game_id": "g2", "stadium": "Soldier Field"},
    ]


@respx.mock
async def test_gzip_off_against_a_gzipped_body_fails_rather_than_yielding_garbage():
    """Forgetting the flag must not look like a short read.

    Compressed bytes decoded as text are either undecodable or nonsense.
    Either way this must not produce a plausible row: a capture that maps
    garbage into an append-only lake is never rewritten.
    """
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_gzipped(DOCUMENT)))

    with pytest.raises((UnicodeDecodeError, UpstreamSchemaError)):
        await _collect(GZ_URL, required_columns=frozenset({"season"}))


@respx.mock
async def test_rows_split_across_gzip_chunk_boundaries_survive():
    """The inflater and the UTF-8 decoder both have to be incremental.

    A body this size is handed over in several chunks, and a boundary lands
    wherever the network puts it: mid-deflate-member for the inflater,
    mid-codepoint for the decoder. A per-chunk `zlib.decompress` cannot inflate
    a partial member at all, and a per-chunk `bytes.decode()` raises on any
    multi-byte character that straddles a boundary — which is why the stadium
    name here is deliberately not ASCII.
    """
    rows_in = 5000
    body = "season,week,game_id,stadium\n" + "".join(
        f"2026,{i % 18 + 1},g{i},Estadio Azteca Café {i}\n" for i in range(rows_in)
    )
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_gzipped(body)))

    rows = await _collect(GZ_URL, gzipped=True)

    assert len(rows) == rows_in
    assert rows[0]["stadium"] == "Estadio Azteca Café 0"
    # The LAST row is the one a missing `inflater.flush()` / final decode drops,
    # and it drops exactly one — which reads as an upstream that is one game
    # short rather than as a bug here. Compared whole rather than field by
    # field: a spot check on `game_id` passes while `stadium` is truncated.
    assert rows[-1] == {
        "season": "2026",
        "week": str((rows_in - 1) % 18 + 1),
        "game_id": f"g{rows_in - 1}",
        "stadium": f"Estadio Azteca Café {rows_in - 1}",
    }


@respx.mock
async def test_the_size_ceiling_counts_inflated_characters():
    """A gzip bomb is caught on what it costs to parse, not what it costs to
    fetch. Counting compressed bytes would let a body under a kilobyte expand
    past any ceiling."""
    body = "a\n" + "row\n" * 20000
    assert len(body) > 20 * 1000  # what it costs to parse
    assert len(_gzipped(body)) < 1000  # what it costs to fetch

    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_gzipped(body)))

    with pytest.raises(UpstreamTooLarge):
        await _collect(GZ_URL, gzipped=True, max_chars=1000)


@respx.mock
async def test_a_gzipped_document_still_validates_required_columns():
    """Schema drift is detected on the header of the INFLATED document."""
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_gzipped(DOCUMENT)))

    with pytest.raises(UpstreamSchemaError, match="referee"):
        await _collect(
            GZ_URL, gzipped=True, required_columns=frozenset({"season", "referee"})
        )


@respx.mock
async def test_a_gzipped_document_with_no_trailing_newline_keeps_its_last_row():
    """The trailing row arrives from the generator tail, after the inflater has
    been flushed — two separate ways to lose exactly one row, on one path."""
    respx.get(GZ_URL).mock(
        return_value=httpx.Response(
            200,
            content=_gzipped("season,week,game_id,stadium\n2026,1,g1,Lambeau Field"),
        )
    )

    rows = await _collect(GZ_URL, gzipped=True)

    assert rows == [
        {"season": "2026", "week": "1", "game_id": "g1", "stadium": "Lambeau Field"}
    ]


class _OneChunkResponse:
    """A response that hands its whole body over in a single chunk.

    Not a contrived shape: `respx` does exactly this, and so does any transport
    that has already buffered. The point is that **nothing in this process
    controls the transport's chunk size**, so peak memory must not depend on
    it.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def aiter_bytes(self):
        yield self._body


async def test_inflated_output_is_bounded_regardless_of_the_transport():
    """The `roster-scope` failure mode, in the gzip path.

    A gzip stream expands about 5x. Handing each network chunk straight to
    `decompress()` lets one buffered 18.2 MiB body become one 93.4 MiB string,
    which the row splitter then copies — measured at **281 MiB peak against a
    256Mi pod limit**, which is an OOMKill, not a slowdown.

    So this asserts the bound holds when the transport provides no chunking at
    all, and that the document still comes back whole. A test that only checked
    the rows were correct passes either way; a test that only checked the
    bound would pass for an implementation that dropped the tail.
    """
    body = "col\n" + "".join(f"{i:07d}\n" for i in range(400_000))
    assert len(body) > 3 * MAX_INFLATED_CHUNK, "the fixture must exceed the bound"

    chunks = [
        chunk
        async for chunk in _text_chunks(_OneChunkResponse(_gzipped(body)), gzipped=True)
    ]

    assert len(chunks) > 1, "a single chunk means the bound was not applied"
    assert max(len(chunk) for chunk in chunks) <= MAX_INFLATED_CHUNK
    assert "".join(chunks) == body


async def test_a_multibyte_character_split_by_the_inflation_bound_survives():
    """The bound cuts at a BYTE offset, so it lands mid-codepoint eventually.

    `MAX_INFLATED_CHUNK` is a byte count and UTF-8 characters are one to four
    bytes, so on a document long enough one of those cuts falls inside a
    character. A per-chunk `bytes.decode()` raises there; worse, a per-chunk
    `decode(errors="replace")` would silently corrupt one character per
    megabyte. The incremental decoder holds the partial sequence across the
    boundary.

    The padding is sized so the multi-byte characters march across the cut
    point rather than sitting at a fixed offset from it — otherwise the test
    passes or fails on luck.
    """
    body = "col\n" + "".join(f"é{i:06d}ü\n" for i in range(300_000))
    assert len(body.encode("utf-8")) > 3 * MAX_INFLATED_CHUNK

    chunks = [
        chunk
        async for chunk in _text_chunks(_OneChunkResponse(_gzipped(body)), gzipped=True)
    ]

    assert len(chunks) > 3, "the bound must actually have cut the stream"
    assert "".join(chunks) == body
    assert "�" not in "".join(chunks), "a replacement character is corruption"


async def test_the_bound_does_not_drop_the_tail_of_the_last_chunk():
    """`unconsumed_tail` is drained inside the loop, not on the next network
    chunk — which for the last chunk of a body never arrives. Getting that
    wrong loses everything after the first megabyte, silently, and only for
    documents large enough to be worth compressing in the first place."""
    body = "col\n" + "x" * (MAX_INFLATED_CHUNK * 2)

    chunks = [
        chunk
        async for chunk in _text_chunks(_OneChunkResponse(_gzipped(body)), gzipped=True)
    ]

    assert "".join(chunks) == body


# ---------------------------------------------------------------------------
# column projection
# ---------------------------------------------------------------------------


@respx.mock
async def test_columns_narrows_the_row_to_what_the_caller_reads():
    respx.get(URL).mock(return_value=httpx.Response(200, text=DOCUMENT))

    rows = await _collect(URL, columns=frozenset({"game_id", "stadium"}))

    # Whole-row equality, not a spot check on the two kept keys: the point of
    # a projection is what is ABSENT, and `rows[0]["game_id"] == "g1"` passes
    # just as happily when nothing was dropped at all.
    assert rows == [
        {"game_id": "g1", "stadium": "Lambeau Field"},
        {"game_id": "g2", "stadium": "Soldier Field"},
    ]


@respx.mock
async def test_without_columns_every_field_is_still_carried():
    """Opting out is byte-for-byte what it was before the option existed."""
    respx.get(URL).mock(return_value=httpx.Response(200, text=DOCUMENT))

    rows = await _collect(URL)

    assert rows == [
        {"season": "2026", "week": "1", "game_id": "g1", "stadium": "Lambeau Field"},
        {"season": "2026", "week": "2", "game_id": "g2", "stadium": "Soldier Field"},
    ]


@respx.mock
async def test_a_short_row_keeps_its_projected_keys_as_empty_strings():
    """Projection and short-row tolerance compose.

    A row shorter than the header must still carry every kept column, rather
    than dropping the key — a caller reading `row["stadium"]` must not start
    raising `KeyError` on a ragged upstream.
    """
    respx.get(URL).mock(
        return_value=httpx.Response(200, text="season,week,game_id,stadium\n2026,1\n")
    )

    rows = await _collect(URL, columns=frozenset({"week", "stadium"}))

    assert rows == [{"week": "1", "stadium": ""}]


@respx.mock
async def test_columns_does_not_relax_the_schema_check():
    """`columns` narrows the ROWS; `required_columns` still guards the header.

    Otherwise a caller could project away the very column whose disappearance
    schema-drift detection exists to catch.
    """
    respx.get(URL).mock(return_value=httpx.Response(200, text=DOCUMENT))

    with pytest.raises(UpstreamSchemaError, match="referee"):
        await _collect(
            URL,
            columns=frozenset({"game_id"}),
            required_columns=frozenset({"game_id", "referee"}),
        )


@respx.mock
async def test_a_projected_name_the_header_lacks_is_simply_absent():
    """Not an error: `columns` is a filter over the header, and a caller that
    needs a column to exist says so with `required_columns`."""
    respx.get(URL).mock(return_value=httpx.Response(200, text=DOCUMENT))

    rows = await _collect(URL, columns=frozenset({"game_id", "referee"}))

    assert rows == [{"game_id": "g1"}, {"game_id": "g2"}]


@respx.mock
async def test_projection_applies_to_the_trailing_row_too():
    """The last row of a document with no trailing newline is built on a
    different code path from every other row, so an unprojected tail would emit
    one full-width row among thousands of narrow ones."""
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, text="season,week,game_id,stadium\n2026,1,g1,Lambeau Field"
        )
    )

    rows = await _collect(URL, columns=frozenset({"game_id"}))

    assert rows == [{"game_id": "g1"}]


@respx.mock
async def test_gzip_and_projection_compose():
    """The combination `officiating` actually uses."""
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_gzipped(DOCUMENT)))

    rows = await _collect(GZ_URL, gzipped=True, columns=frozenset({"game_id"}))

    assert rows == [{"game_id": "g1"}, {"game_id": "g2"}]
