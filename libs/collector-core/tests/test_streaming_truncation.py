"""End of stream: a gzip body that stops early must raise, not return short.

The fault this file exists for is **truncation**, and the reason it needs its
own file is that it is the one gzip fault nothing else catches. zlib's CRC
already refuses mid-stream corruption on its own. Truncation looks like
success: `decompressobj` simply runs out of input, `flush()` hands back what it
has, and the reader gets a short document whose final row is a fragment.

Measured against a body cut exactly in half, before `inflater.eof` was
consulted:

    9,895 of 20,000 rows, NO EXCEPTION, last row {'col': 'r9', 'val': ''}

**A short document is a plausible answer, which is what makes it dangerous.**
`officiating` reading half a play-by-play file would publish
`crew_tendency_rates` with `expected: 17, present: 17`, ratio 1.0 — every crew
present, every rate window silently half-size, every `rate_stderr` inflated and
every shrinkage weight overstated — into an append-only lake that is never
rewritten. Nothing in coverage can see it, because coverage counts crews and
every crew is still there. A loud failure costs one pass of freshness; a quiet
one costs a season of correctness.

Note the asymmetry this closes: the **uncompressed** path has no framing at
all, so a short read there is undetectable and always has been. A gzip member
carries a trailer, so the gzip path is the one where the question can be asked
— it simply was not being asked.
"""

import zlib

import httpx
import pytest
import respx

from collector_core.conditional import ETagStore
from collector_core.metrics import CollectorMetrics
from collector_core.streaming import (
    GZIP_WBITS,
    UpstreamSchemaError,
    UpstreamTruncated,
    _text_chunks,
    stream_csv_dicts,
)

URL = "https://upstream.test/feed.csv"
GZ_URL = "https://upstream.test/feed.csv.gz"

DOCUMENT = "col,val\n" + "".join("r{0},{0}\n".format(i) for i in range(20000))


def _gzipped(payload: str | bytes) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    compressor = zlib.compressobj(9, zlib.DEFLATED, GZIP_WBITS)
    return compressor.compress(payload) + compressor.flush()


def _halved(text: str) -> bytes:
    """A complete HTTP response carrying half a gzip member.

    Cut at the byte level and served whole, so `Content-Length` matches the
    body and httpx's own framing checks cannot fire. This is what a partially
    uploaded release asset, or a partially cached CDN object, looks like on the
    wire — the response is not itself malformed.
    """
    whole = _gzipped(text)
    return whole[: len(whole) // 2]


async def _collect(url: str, **kwargs) -> list[dict]:
    async with httpx.AsyncClient() as client:
        return [row async for row in stream_csv_dicts(client, url, **kwargs)]


class _OneChunkResponse:
    """A response that hands its whole body over in a single chunk."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def aiter_bytes(self):
        yield self._body

    async def aiter_text(self):
        yield self._body.decode("utf-8")


@respx.mock
async def test_a_truncated_gzip_body_raises_instead_of_returning_a_short_document():
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_halved(DOCUMENT)))

    with pytest.raises(UpstreamTruncated, match="before its trailer"):
        await _collect(GZ_URL, gzipped=True)


@respx.mock
async def test_a_complete_body_of_the_same_shape_does_not_raise():
    """The other arm, and it is not decoration: a check that refused everything
    would pass the test above and break every real fetch. Only the pair can
    tell a correct guard from a broken one."""
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_gzipped(DOCUMENT)))

    rows = await _collect(GZ_URL, gzipped=True)

    assert len(rows) == 20000
    assert rows[-1] == {"col": "r19999", "val": "19999"}


@respx.mock
async def test_a_truncated_read_does_not_commit_an_etag():
    """The compounding failure, and the reason this has to raise rather than
    warn.

    `stream_csv_dicts` commits the ETag from its generator tail, which an
    exception never reaches. Commit one for half a document and every later
    pass 304s: `mark_unchanged` advances `last_capture_at`, staleness resets to
    zero, the failure counter stops moving, and the collector reports itself
    healthy on half a season until the upstream happens to republish.
    """
    respx.get(GZ_URL).mock(
        return_value=httpx.Response(
            200, content=_halved(DOCUMENT), headers={"ETag": 'W/"partial"'}
        )
    )
    store = ETagStore()

    with pytest.raises(UpstreamTruncated):
        await _collect(GZ_URL, gzipped=True, etag_key="k", etag_store=store)

    assert store.get("k") is None


@respx.mock
async def test_mid_stream_corruption_still_raises_its_own_error():
    """Corruption and truncation stay distinguishable. zlib's CRC catches
    corruption unaided; collapsing the two would hide from an operator which
    one happened, and they call for different responses."""
    whole = _gzipped(DOCUMENT)
    midpoint = len(whole) // 2
    corrupted = whole[:midpoint] + bytes(40) + whole[midpoint + 40 :]
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=corrupted))

    with pytest.raises(zlib.error):
        await _collect(GZ_URL, gzipped=True)


def test_truncation_is_classified_as_malformed():
    """The Phase 8 failure-handling contract. `UpstreamTruncated` needs no new
    `reason` label because `ValueError` already routes to `malformed` — but
    that is a property of the class hierarchy, and a refactor could break it
    while every behavioural test here stays green."""
    assert CollectorMetrics.reason_for(UpstreamTruncated("x")) == "malformed"
    assert issubclass(UpstreamTruncated, ValueError)
    assert not issubclass(UpstreamTruncated, UpstreamSchemaError), (
        "a separate fault deserves a separate type: an operator retries a "
        "truncated fetch and investigates a schema change"
    )


@respx.mock
async def test_a_body_ending_mid_codepoint_raises_rather_than_losing_a_byte():
    """`final=True` on the last decode, which `inflater.eof` does NOT cover.

    This body is a **complete** gzip member — trailer present, `eof` True —
    whose decompressed bytes end in the first two bytes of a two-byte
    character. The incremental decoder is holding them back waiting for the
    rest. Without `final=True` it is never told there is no rest, and the bytes
    are dropped in silence: measured, a body ending in a partial `é` yields
    `'caf'` and loses one byte with no error at all.

    Before this test, deleting the entire final-flush block survived the whole
    library suite, because no tested input ever reached it.
    """
    partial = "col\ncafé".encode("utf-8")[:-1]
    respx.get(GZ_URL).mock(return_value=httpx.Response(200, content=_gzipped(partial)))

    with pytest.raises(UnicodeDecodeError):
        await _collect(GZ_URL, gzipped=True)


async def test_the_uncompressed_path_is_unchanged_by_the_eof_check():
    """A plain CSV has no framing, so a short read there is undetectable and
    always has been. The check must not leak into that path and begin
    rejecting bodies it cannot reason about."""
    body = "col,val\nr0,0\n"

    chunks = [
        chunk
        async for chunk in _text_chunks(
            _OneChunkResponse(body.encode("utf-8")), URL, gzipped=False
        )
    ]

    assert "".join(chunks) == body
