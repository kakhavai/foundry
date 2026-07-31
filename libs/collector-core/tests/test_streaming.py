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

from collector_core.conditional import ETagStore, UpstreamUnchanged
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


# --- conditional GET ---------------------------------------------------------

CSV = "team,player_name\nSF,A Player\n"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_second_request_carries_the_first_responses_etag():
    """The whole point: request two must be conditional on request one."""
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, text=CSV, headers={"ETag": 'W/"v1"'})

    store = ETagStore()
    async with _client(handler) as client:
        for _ in range(2):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert len(seen) == 2
    assert "if-none-match" not in seen[0]
    assert seen[1]["if-none-match"] == 'W/"v1"'


@pytest.mark.asyncio
async def test_a_304_raises_upstream_unchanged_and_yields_no_rows():
    store = ETagStore()
    store.set("k", 'W/"v1"')

    def handler(request):
        return httpx.Response(304)

    rows = []
    async with _client(handler) as client:
        with pytest.raises(UpstreamUnchanged) as caught:
            async for row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                rows.append(row)

    assert rows == []
    assert caught.value.source_ref == 'W/"v1"'


@pytest.mark.asyncio
async def test_without_an_etag_key_nothing_changes():
    """Every collector that has not opted in must behave exactly as before."""
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, text=CSV, headers={"ETag": 'W/"v1"'})

    store = ETagStore()
    async with _client(handler) as client:
        for _ in range(2):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_store=store
            ):
                pass

    assert len(seen) == 2
    assert all("if-none-match" not in headers for headers in seen)
    # A caller that did not opt in must leave the store untouched, not just
    # the headers -- otherwise the `etag_key is not None` guard on the write
    # is unverified and can be deleted silently. No `etag_key` is passed
    # above, so `None` is the key an unconditional write would land under --
    # that is the exact key stream_csv_dicts would use internally.
    assert store.get(None) is None


@pytest.mark.asyncio
async def test_an_upstream_that_sends_no_etag_stays_unconditional():
    """Fails open: no ETag means no conditional request, forever."""
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, text=CSV)

    store = ETagStore()
    async with _client(handler) as client:
        for _ in range(2):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert len(seen) == 2
    assert all("if-none-match" not in headers for headers in seen)
    assert store.get("k") is None


@pytest.mark.asyncio
async def test_a_changed_etag_replaces_the_stored_one():
    etags = iter(['W/"v1"', 'W/"v2"'])

    def handler(request):
        return httpx.Response(200, text=CSV, headers={"ETag": next(etags)})

    store = ETagStore()
    async with _client(handler) as client:
        for _ in range(2):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert store.get("k") == 'W/"v2"'


# --- the ETag is committed only by a COMPLETE read ---------------------------
#
# An ETag is a claim that the collector holds the whole document. Committing
# one on the response headers -- before the body is read -- makes every partial
# read sticky: the next pass sends `If-None-Match`, gets a 304, calls
# `mark_unchanged`, and the collector reports itself healthy while holding a
# truncated document, until the upstream publishes a new version. These are the
# five behaviours that shape has to satisfy.


class _ChunkedStream(httpx.AsyncByteStream):
    """A response body delivered in chunks, optionally failing part-way.

    `httpx.Response(text=...)` hands the whole body over at once, which cannot
    express "the connection died at 30 MB of 37".
    """

    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


def _recording_client(responses) -> tuple[httpx.AsyncClient, list[dict]]:
    """A client over a queue of responses, recording each request's headers."""
    seen: list[dict] = []
    pending = iter(responses)

    def handler(request):
        seen.append(dict(request.headers))
        return next(pending)

    return _client(handler), seen


LONG_CSV_CHUNKS = [b"a,b\n", b"1,2\n", b"3,4\n"]


@pytest.mark.asyncio
async def test_1_a_complete_read_commits_the_etag():
    """The baseline the other four are measured against."""
    client, seen = _recording_client(
        [
            httpx.Response(
                200,
                headers={"ETag": 'W/"v1"'},
                stream=_ChunkedStream(list(LONG_CSV_CHUNKS)),
            ),
            httpx.Response(304),
        ]
    )
    store = ETagStore()
    async with client:
        rows = [
            row
            async for row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            )
        ]
        assert len(rows) == 2

        assert store.get("k") == 'W/"v1"'

        # And the commit is load-bearing rather than cosmetic: the next pass
        # actually sends it.
        with pytest.raises(UpstreamUnchanged):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert len(seen) == 2
    assert seen[1]["if-none-match"] == 'W/"v1"'


@pytest.mark.asyncio
async def test_2a_a_connection_error_mid_body_commits_nothing():
    """The concrete failure: a `RemoteProtocolError` at 30 MB of 37.

    Committing here is what turns one loud, self-retrying failure into a
    permanently silent one.
    """
    client, seen = _recording_client(
        [
            httpx.Response(
                200,
                headers={"ETag": 'W/"v1"'},
                stream=_ChunkedStream(
                    [b"a,b\n", b"1,2\n"],
                    error=httpx.RemoteProtocolError("peer closed connection"),
                ),
            ),
            httpx.Response(200, text=CSV, headers={"ETag": 'W/"v2"'}),
        ]
    )
    store = ETagStore()
    async with client:
        with pytest.raises(httpx.RemoteProtocolError):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

        assert store.get("k") is None

        # The next pass re-downloads unconditionally rather than 304ing.
        async for _row in stream_csv_dicts(
            client, "http://x/d.csv", etag_key="k", etag_store=store
        ):
            pass

    assert len(seen) == 2
    assert "if-none-match" not in seen[1]


@pytest.mark.asyncio
async def test_2b_the_size_ceiling_commits_nothing():
    body = "a\n" + "".join(f"row{i}\n" for i in range(5000))
    client, seen = _recording_client(
        [httpx.Response(200, text=body, headers={"ETag": 'W/"v1"'})]
    )
    store = ETagStore()
    async with client:
        with pytest.raises(UpstreamTooLarge):
            async for _row in stream_csv_dicts(
                client,
                "http://x/d.csv",
                etag_key="k",
                etag_store=store,
                max_chars=100,
            ):
                pass

    assert len(seen) == 1
    assert store.get("k") is None


@pytest.mark.asyncio
async def test_2c_schema_drift_commits_nothing():
    """A renamed column must not pin the ETag of the document that renamed it."""
    client, seen = _recording_client(
        [httpx.Response(200, text="a,b\n1,2\n", headers={"ETag": 'W/"v1"'})]
    )
    store = ETagStore()
    async with client:
        with pytest.raises(UpstreamSchemaError):
            async for _row in stream_csv_dicts(
                client,
                "http://x/d.csv",
                etag_key="k",
                etag_store=store,
                required_columns={"a", "missing_one"},
            ):
                pass

    assert len(seen) == 1
    assert store.get("k") is None


@pytest.mark.asyncio
async def test_2d_an_empty_document_commits_nothing():
    """The tail raises before it commits, so this stays loud on every pass
    rather than 304ing into a permanent 'unchanged and healthy'."""
    client, seen = _recording_client(
        [httpx.Response(200, text="", headers={"ETag": 'W/"v1"'})]
    )
    store = ETagStore()
    async with client:
        with pytest.raises(UpstreamSchemaError, match="empty document"):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert len(seen) == 1
    assert store.get("k") is None


@pytest.mark.asyncio
async def test_3_a_consumer_that_breaks_early_commits_nothing():
    """The case the obvious fix gets wrong.

    An `@asynccontextmanager` committing after its `yield` would pass every
    other test here and fail this one: a `break` exits the `async with`
    *normally*, so post-`yield` code runs. `depth-chart`'s deadline path takes
    exactly this exit, so a truncated capture would pin the ETag of a document
    it never finished reading.
    """
    body = "a\n" + "".join(f"row{i}\n" for i in range(1000))
    client, seen = _recording_client(
        [
            httpx.Response(200, text=body, headers={"ETag": 'W/"v1"'}),
            httpx.Response(200, text=body, headers={"ETag": 'W/"v1"'}),
        ]
    )
    store = ETagStore()
    async with client:
        kept = []
        async for row in stream_csv_dicts(
            client, "http://x/d.csv", etag_key="k", etag_store=store
        ):
            kept.append(row)
            if len(kept) == 3:
                break

        assert len(kept) == 3
        assert store.get("k") is None

        async for _row in stream_csv_dicts(
            client, "http://x/d.csv", etag_key="k", etag_store=store
        ):
            pass

    assert len(seen) == 2
    assert "if-none-match" not in seen[1]


@pytest.mark.asyncio
async def test_3b_an_explicit_aclose_commits_nothing():
    """`depth-chart` calls `aclose()` before it breaks, to release the
    connection rather than drain 53 MB in the background. That must not be a
    different answer from a plain `break`."""
    body = "a\n" + "".join(f"row{i}\n" for i in range(1000))
    client, seen = _recording_client(
        [httpx.Response(200, text=body, headers={"ETag": 'W/"v1"'})]
    )
    store = ETagStore()
    async with client:
        rows = stream_csv_dicts(
            client, "http://x/d.csv", etag_key="k", etag_store=store
        )
        kept = []
        async for row in rows:
            kept.append(row)
            if len(kept) == 3:
                await rows.aclose()
                break

    assert len(seen) == 1
    assert len(kept) == 3
    assert store.get("k") is None


@pytest.mark.asyncio
async def test_4_without_an_etag_key_the_store_is_never_touched():
    """Behaviour 4: opting out is byte-for-byte what it was before.

    Pre-seeded under every key this call could plausibly write, so a commit
    that ignored `etag_key=None` would show up as a changed value.
    """
    client, seen = _recording_client(
        [
            httpx.Response(200, text=CSV, headers={"ETag": 'W/"fresh"'}),
            httpx.Response(200, text=CSV, headers={"ETag": 'W/"fresh"'}),
        ]
    )
    store = ETagStore()
    store.set("http://x/d.csv", 'W/"pinned"')
    async with client:
        for _ in range(2):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_store=store
            ):
                pass

    assert len(seen) == 2
    assert all("if-none-match" not in headers for headers in seen)
    assert store.get("http://x/d.csv") == 'W/"pinned"'


@pytest.mark.asyncio
async def test_5_an_upstream_that_stops_sending_etags_forgets_the_stored_one():
    """Behaviour 5: fail open. Pinning the last ETag an upstream ever sent
    would 304 forever against a document that is quietly moving."""
    client, seen = _recording_client(
        [
            httpx.Response(200, text=CSV, headers={"ETag": 'W/"v1"'}),
            httpx.Response(200, text=CSV),
            httpx.Response(200, text=CSV),
        ]
    )
    store = ETagStore()
    async with client:
        for _ in range(3):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert len(seen) == 3
    assert seen[1]["if-none-match"] == 'W/"v1"'
    assert "if-none-match" not in seen[2]
    assert store.get("k") is None
