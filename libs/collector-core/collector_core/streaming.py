"""Reading a large upstream document without holding it in memory.

Two rules fell out of `roster-scope` being `OOMKilled` at a 256Mi limit on its
first deploy — exit 137, `CrashLoopBackOff`, probes reporting `connection
refused` because the process was simply gone. Neither its 171 passing tests nor
a local `docker run` could see it, because neither had a memory limit.

**1. Never hold an upstream response in memory more than once.** `resp.text`
plus a decode plus a `io.StringIO` copy handed to `csv` is three copies of the
document. At 36.8 MB that is over 110 MB before a single row is mapped.

**2. Filter to what you actually keep as you parse, not after.** `roster-scope`
took 300,000 rows in and retained 983; materializing the other 299,017 first
peaked at 38.5 MiB of pure waste.

`stream_csv_dicts` is rule 1 made reusable, and it hands rows out one at a time
so a caller can obey rule 2 with an ordinary `continue`. Raising the memory
limit instead is the wrong fix and was deliberately reverted during 8A: it
hides the bug and re-sizes the pod against an upstream nobody controls.

Two later additions, both driven by nflverse's play-by-play release — 93.4 MiB
as `.csv`, 18.2 MiB as `.csv.gz`, 48,771 rows of 372 columns — and both here
rather than in a collector because `officiating` is simply the first to hit the
wall that every play-by-play consumer hits:

**`gzipped=True`** inflates as it streams. The compression is part of the
*file*, not the transfer, so httpx does not and cannot do it; see
`_text_chunks`. 5.1x less bandwidth on every poll, same flat memory.

**`columns=`** keeps only the columns the caller reads. Rule 2 applied to
width rather than length: at 372 columns the dict build alone measured 23.0s
of CPU per pass against 5.8s for the eight columns `officiating` reads.
"""

import codecs
import csv
import logging
import zlib
from collections.abc import AsyncIterator

import httpx

from .conditional import ETAGS, ETagStore, conditional_stream

logger = logging.getLogger(__name__)

# `zlib.decompressobj` with this window size accepts a gzip container (header,
# CRC and length trailer) rather than a bare deflate stream. `gzip.GzipFile`
# cannot be used here at all: it wants a synchronous file object to `read()`
# from, which is the one thing an async chunk iterator is not.
GZIP_WBITS = 16 + zlib.MAX_WBITS

# A ceiling on how much a collector will pull before giving up. Not a memory
# guard -- streaming already bounds that -- but a guard against an upstream
# that starts serving something unbounded, so a capture cannot download
# forever inside its deadline.
MAX_UPSTREAM_CHARS = 256 * 1024 * 1024

# The most inflated data a single decompression step may produce. Bounds peak
# memory on a gzipped read independently of the transport's chunk size -- see
# `_text_chunks`, where relying on the transport measured 281 MiB against a
# 256Mi pod limit. 1 MiB is small enough that ~5x expansion is irrelevant and
# large enough that a 93 MiB document costs ~93 extra loop iterations.
MAX_INFLATED_CHUNK = 1024 * 1024


class UpstreamTooLarge(ValueError):
    """The upstream exceeded its character ceiling before it finished."""


class UpstreamSchemaError(ValueError):
    """The document did not carry the columns the caller depends on.

    Subclasses `ValueError` so `CollectorMetrics.reason_for` classifies it as
    `malformed`, per the Phase 8 failure-handling contract: an upstream that
    renames a field must fail the capture loudly rather than map nulls into an
    append-only lake that is never rewritten.
    """


async def _text_chunks(
    response: httpx.Response, *, gzipped: bool
) -> AsyncIterator[str]:
    """The response body as text chunks, gunzipping on the way past if asked.

    `response.aiter_text()` already does this for an uncompressed body. It
    cannot do it for a **gzipped artifact** — a `.csv.gz` release asset is
    served as `application/octet-stream` with no `Content-Encoding`, because
    the compression is part of the file rather than of the transfer. httpx
    decodes transfer encodings and nothing else, so `aiter_text()` on one of
    those yields mojibake rather than CSV.

    So the gzip branch reads **bytes**, pushes them through one incremental
    inflater and one incremental UTF-8 decoder, and yields text. Both have to
    be incremental for the same reason the caller streams at all: a chunk
    boundary lands wherever the network puts it, which is mid-member for the
    inflater and mid-codepoint for the decoder. `bytes.decode()` per chunk
    would raise on any multi-byte character that straddles a boundary, and it
    would do so only for the documents unlucky enough to contain one.

    **The inflated output is bounded here, not by the transport**, and that is
    the difference between flat memory and `roster-scope`'s OOMKill. A gzip
    stream expands about 5x, so a caller that simply hands each network chunk
    to `decompress()` has peak memory set by whatever chunk size the transport
    happened to choose — and nothing in this process controls that. Measured:
    a single-chunk 18.2 MiB body inflates to one 93.4 MiB string, which the
    row splitter then copies, peaking at **281 MiB** against a 256Mi pod limit.
    `decompress(data, max_length)` caps each step at `MAX_INFLATED_CHUNK` and
    parks the rest in `unconsumed_tail`, so peak is a property of this module
    rather than of the network.

    A server that also applies `Content-Encoding: gzip` to an already-gzipped
    artifact would have httpx inflate it once before this sees it, and the
    inflater here then raises `zlib.error` on the plain CSV. That is loud and
    correct: it fails the capture rather than mapping garbage into an
    append-only lake. None of the fleet's upstreams does it.
    """
    if not gzipped:
        async for chunk in response.aiter_text():
            yield chunk
        return

    inflater = zlib.decompressobj(GZIP_WBITS)
    decoder = codecs.getincrementaldecoder("utf-8")()
    async for chunk in response.aiter_bytes():
        pending = chunk
        while True:
            inflated = inflater.decompress(pending, MAX_INFLATED_CHUNK)
            text = decoder.decode(inflated)
            if text:
                yield text
            # Whatever `max_length` held back. Drained here rather than on the
            # next network chunk, which may never arrive — the last chunk of
            # the body would otherwise leave most of the document unread.
            pending = inflater.unconsumed_tail
            if not pending:
                break
    # Whatever the inflater and decoder are still holding at end of stream.
    # Skipping this drops the document's last partial chunk, which for a CSV
    # is its final row — a silent one-row truncation on every fetch.
    tail = decoder.decode(inflater.flush(), True)
    if tail:
        yield tail


def _split_line(line: str) -> list[str] | None:
    """One CSV line to fields, or `None` for a blank one.

    Parsed a line at a time rather than by handing `csv` the whole document,
    which is what keeps memory flat. The trade-off is that a quoted field
    containing a literal newline would be split across two rows; the feeds
    this reads carry timestamps, team codes, ids, position codes and player
    names, none of which can contain one. A feed that can must not use this.
    """
    line = line.rstrip("\r")
    if not line:
        return None
    return next(csv.reader([line]))


async def stream_csv_dicts(
    client: httpx.AsyncClient,
    url: str,
    *,
    required_columns: frozenset[str] | set[str] | None = None,
    columns: frozenset[str] | set[str] | None = None,
    gzipped: bool = False,
    max_chars: int = MAX_UPSTREAM_CHARS,
    follow_redirects: bool = True,
    etag_key: str | None = None,
    etag_store: ETagStore = ETAGS,
) -> AsyncIterator[dict[str, str]]:
    """Stream a CSV document, yielding one header-keyed dict per row.

    Peak memory is one chunk plus one row, independent of the document's size.
    The caller filters as it iterates, so nothing it does not keep is ever
    retained.

    `required_columns`, when given, is asserted against the header before any
    row is yielded — schema drift fails immediately rather than after a
    million rows have been mapped to nulls.

    `gzipped=True` inflates the body as it streams, for an upstream published
    as a `.csv.gz` **artifact** — not as a `Content-Encoding`, which httpx
    already handles. nflverse's play-by-play release is 93.4 MiB as `.csv` and
    18.2 MiB as `.csv.gz`; taking the compressed one is a 5.1x bandwidth saving
    for a weekly poll, at no cost in peak memory. See `_text_chunks`.

    `columns`, when given, keeps only those columns in each yielded row. The
    header is still validated in full, so `required_columns` is unaffected —
    this narrows the **row dicts**, not the schema check. That is a real cost,
    not a tidiness preference: play-by-play carries 372 columns, and building
    a 372-key dict for each of 48,771 rows measured **23.0s** of CPU against
    **5.8s** for the eight columns a caller actually reads. The rows are
    yielded one at a time either way, so this is CPU and allocation churn
    rather than peak memory. A name in `columns` that the header does not carry
    is simply absent from the rows; pair it with `required_columns` when its
    presence matters.

    `etag_key` opts into conditional GET. When set, the request carries
    `If-None-Match` from `etag_store` and a `304` raises `UpstreamUnchanged`
    before a single row is yielded. Left unset (the default), this function
    behaves exactly as it did before conditional GET existed — which is what
    lets a collector opt in one at a time.

    **The ETag is committed by the last statement of this generator's body**,
    and that placement is the whole guarantee. A generator's tail runs only
    when the generator is exhausted: a consumer that `break`s gets
    `GeneratorExit` thrown in at the `yield`, and an upstream error, the size
    ceiling or schema drift all raise before the tail is reached. So every
    partial read leaves the store untouched and the next pass re-downloads
    unconditionally. Committing on the response headers instead — before the
    body is read — is the bug this shape exists to make unrepresentable; see
    `ConditionalStream.commit`.
    """
    header: list[str] | None = None
    # `(index, name)` for the columns the caller asked to keep, computed once
    # from the header rather than per row — a `name in columns` test inside the
    # row builder would put the projection's cost back where its saving was.
    kept: list[tuple[int, str]] | None = None
    consumed = 0
    remainder = ""

    async with conditional_stream(
        client,
        url,
        etag_key=etag_key,
        etag_store=etag_store,
        follow_redirects=follow_redirects,
    ) as stream:
        async for chunk in _text_chunks(stream.response, gzipped=gzipped):
            consumed += len(chunk)
            if consumed > max_chars:
                raise UpstreamTooLarge(
                    f"{url} exceeded {max_chars} characters before it finished"
                )
            remainder += chunk
            lines = remainder.split("\n")
            # The last element is a partial line unless the chunk happened to
            # end on a boundary; either way it belongs to the next chunk.
            remainder = lines.pop()
            for line in lines:
                fields = _split_line(line)
                if fields is None:
                    continue
                if header is None:
                    header = _validated_header(fields, required_columns, url)
                    kept = _projection(header, columns)
                    continue
                yield _row(kept, fields)

    trailing = _split_line(remainder)
    if trailing is not None:
        if header is None:
            header = _validated_header(trailing, required_columns, url)
            kept = _projection(header, columns)
        else:
            yield _row(kept, trailing)

    if header is None:
        raise UpstreamSchemaError(f"{url} returned an empty document")

    # The tail. Reached only on a complete, valid read — see the docstring.
    stream.commit()


def _validated_header(
    fields: list[str],
    required_columns: frozenset[str] | set[str] | None,
    url: str,
) -> list[str]:
    if required_columns:
        missing = set(required_columns) - set(fields)
        if missing:
            raise UpstreamSchemaError(
                f"{url} is missing column(s): {', '.join(sorted(missing))}"
            )
    return fields


def _projection(
    header: list[str], columns: frozenset[str] | set[str] | None
) -> list[tuple[int, str]]:
    """The `(index, name)` pairs each row should carry, resolved once.

    Without `columns` that is the whole header, so an unprojected read builds
    exactly the dict it always did.
    """
    if columns is None:
        return list(enumerate(header))
    return [(index, name) for index, name in enumerate(header) if name in columns]


def _row(kept: list[tuple[int, str]], fields: list[str]) -> dict[str, str]:
    """Header-keyed, tolerating a short row.

    A row with fewer fields than the header gets empty strings rather than
    raising: `csv.DictReader` fills with `None`, and every caller here treats
    an absent value as empty anyway. A row with *more* fields than the header
    keeps only the named ones.
    """
    return {name: fields[index] if index < len(fields) else "" for index, name in kept}
