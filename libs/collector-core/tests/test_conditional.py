"""The ETag store, the 304 signal, and the shared conditional-GET helper."""

from contextlib import asynccontextmanager

import httpx
import pytest

from collector_core.conditional import (
    ETAGS,
    ETagStore,
    UpstreamUnchanged,
    conditional_headers,
    conditional_stream,
)


def test_a_stored_etag_becomes_an_if_none_match_header():
    store = ETagStore()
    store.set("http://x/doc.csv", 'W/"abc"')
    assert conditional_headers("http://x/doc.csv", store) == {
        "If-None-Match": 'W/"abc"'
    }


def test_an_unknown_key_sends_no_conditional_header():
    """A first-ever fetch must be an ordinary unconditional GET."""
    assert conditional_headers("http://x/never-seen.csv", ETagStore()) == {}


def test_setting_none_forgets_the_key_rather_than_storing_a_null():
    """An upstream that stops sending ETags must fall back to unconditional
    GETs, not send `If-None-Match: None` forever."""
    store = ETagStore()
    store.set("k", 'W/"abc"')
    store.set("k", None)
    assert store.get("k") is None
    assert conditional_headers("k", store) == {}


def test_clear_empties_the_store():
    store = ETagStore()
    store.set("k", 'W/"abc"')
    store.clear()
    assert store.get("k") is None


def test_the_module_singleton_is_an_etag_store():
    assert isinstance(ETAGS, ETagStore)


def test_upstream_unchanged_carries_the_url_and_the_source_ref():
    exc = UpstreamUnchanged("http://x/doc.csv", source_ref='W/"abc"')
    assert exc.url == "http://x/doc.csv"
    assert exc.source_ref == 'W/"abc"'
    assert "304" in str(exc)


# --- the shape of the helper -------------------------------------------------


def test_httpx_raise_for_status_rejects_a_304():
    """The reason the 304 check is REQUIRED, not defensive.

    `raise_for_status()` gates on `is_success`, which is 2xx only -- so a 304
    raises like any other non-2xx. Four comments in this repo used to claim the
    opposite ("httpx only treats 4xx/5xx as errors, so a 304 falls through"),
    which invited a maintainer to drop the check in a refactor and route every
    unchanged upstream into `fail_capture`. Pinned here rather than asserted in
    prose, so an httpx upgrade that changed it would fail rather than rot.
    """
    response = httpx.Response(304, request=httpx.Request("GET", "http://x/d.csv"))
    assert response.is_success is False
    with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()


@pytest.mark.asyncio
async def test_a_context_manager_cannot_commit_after_yield():
    """Why `conditional_stream` has no post-`yield` code, demonstrated.

    The obvious fix for "the ETag is committed too early" is to move the
    commit after an `@asynccontextmanager`'s `yield` and rely on an early exit
    skipping it. A consumer that *raises* does skip it. A consumer that
    `break`s does not -- it leaves the `async with` normally, `__aexit__` is
    called with no exception, and the post-`yield` code runs. This test is the
    proof, and it is why the commit is an explicit call the reader makes.
    """
    ran: list[str] = []

    @asynccontextmanager
    async def commits_after_yield():
        yield "resource"
        ran.append("post-yield")

    async with commits_after_yield():
        for index in range(10):
            if index == 2:
                break

    assert ran == ["post-yield"], (
        "an asynccontextmanager's post-yield code runs on a consumer break -- "
        "which is exactly why conditional_stream does not commit there"
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_helper_does_not_commit_on_its_own():
    """Entering and leaving the context is not a complete read."""
    store = ETagStore()
    async with _client(
        lambda request: httpx.Response(200, text="body", headers={"ETag": 'W/"v1"'})
    ) as client:
        async with conditional_stream(
            client, "http://x/d.csv", etag_key="k", etag_store=store
        ) as stream:
            assert stream.committed is False

    assert store.get("k") is None


@pytest.mark.asyncio
async def test_commit_records_the_responses_etag():
    store = ETagStore()
    async with _client(
        lambda request: httpx.Response(200, text="body", headers={"ETag": 'W/"v1"'})
    ) as client:
        async with conditional_stream(
            client, "http://x/d.csv", etag_key="k", etag_store=store
        ) as stream:
            stream.commit()

        assert stream.committed is True

    assert store.get("k") == 'W/"v1"'


@pytest.mark.asyncio
async def test_commit_is_a_no_op_without_an_etag_key():
    """So a reader can call `commit()` unconditionally on both paths."""
    store = ETagStore()
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, text="body", headers={"ETag": 'W/"v1"'})

    async with _client(handler) as client:
        async with conditional_stream(
            client, "http://x/d.csv", etag_store=store
        ) as stream:
            stream.commit()

    assert len(seen) == 1
    assert "if-none-match" not in seen[0]
    assert store.get("http://x/d.csv") is None
    assert store.get(None) is None


@pytest.mark.asyncio
async def test_a_manual_reader_that_breaks_mid_body_commits_nothing():
    """The Route-2 shape, and the one the post-`yield` trap actually bites.

    `roster-scope` consumes `stream.response` itself rather than going through
    `stream_csv_dicts`. When such a reader stops early it exits the `async
    with` *normally* -- no `GeneratorExit` to suppress a post-`yield` commit --
    so this is where a helper that committed for its caller would pin the ETag
    of a body it only partly read. Nothing here calls `commit()`, so nothing is
    stored, and the next pass re-downloads unconditionally.
    """
    body = b"".join(b"row%d\n" % index for index in range(500))
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, content=body, headers={"ETag": 'W/"v1"'})

    store = ETagStore()
    async with _client(handler) as client:
        async with conditional_stream(
            client, "http://x/d.csv", etag_key="k", etag_store=store
        ) as stream:
            read = 0
            async for chunk in stream.response.aiter_bytes():
                read += len(chunk)
                break  # out of budget, say -- deliberately not a complete read

        assert read > 0
        assert store.get("k") is None

        async with conditional_stream(
            client, "http://x/d.csv", etag_key="k", etag_store=store
        ) as stream:
            stream.commit()

    assert len(seen) == 2
    assert "if-none-match" not in seen[1]
    assert store.get("k") == 'W/"v1"'


@pytest.mark.asyncio
async def test_a_304_raises_before_the_caller_sees_a_response():
    store = ETagStore()
    store.set("k", 'W/"v1"')
    entered = False

    async with _client(lambda request: httpx.Response(304)) as client:
        with pytest.raises(UpstreamUnchanged) as caught:
            async with conditional_stream(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                entered = True

    assert entered is False
    assert caught.value.source_ref == 'W/"v1"'
    assert store.get("k") == 'W/"v1"'


@pytest.mark.asyncio
async def test_a_304_without_an_etag_key_is_an_http_error_not_unchanged():
    """A collector that never opted in cannot have caused a 304, so one
    arriving is an upstream doing something unexpected -- an error, not a
    healthy pass that silently advances `last_capture_at`."""
    async with _client(lambda request: httpx.Response(304)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            async with conditional_stream(client, "http://x/d.csv"):
                pass


@pytest.mark.asyncio
async def test_a_5xx_still_raises():
    async with _client(lambda request: httpx.Response(503)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            async with conditional_stream(
                client, "http://x/d.csv", etag_key="k", etag_store=ETagStore()
            ):
                pass
