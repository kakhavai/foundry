"""Conditional GET, so a poll that finds nothing new costs nothing.

A collector on a `volatile` cadence polls 96 times a day. `depth-chart`'s
upstream is a 37.1 MB asset the publisher republishes far less often than
that -- how much less has not been measured here, so no number is claimed --
and every poll that lands between two republications downloads a document the
collector already has. At ~3.4 GB/day, and one `CAPTURE_ENABLED` flip away
from being real.

Half of the mechanism already existed and went unused: `player-identity`'s
Sleeper adapter has always read `response.headers.get("etag")` into the
envelope's `upstream.source_ref`, described there as "the upstream's own
opaque cursor". Nothing ever sent it back. This module is the other half.

Verified against the live upstreams before it was written (2026-07-31):
`raw.githubusercontent.com` and the nflverse release asset (which 302s to
Azure blob storage) both serve ETags and both answer `If-None-Match` with a
`304` carrying zero bytes. A ranged control request returns `206`, so the 304
is caused by the header rather than a dead URL.

**A 304 is not a failure**, and the distinction is load-bearing. Routing
`UpstreamUnchanged` into `collector_core.failure.fail_capture` would write a
`present: 0` envelope over a perfectly healthy capture -- the exact
destroy-good-data outcome `fail_capture`'s own docstring warns about. Every
collector that opts in must re-raise it ahead of its generic handler.

**The ETag is committed by the reader, never by this module.** See
`conditional_stream` for why the commit cannot be automatic, and what a
premature one costs.
"""

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx


class UpstreamUnchanged(Exception):
    """The upstream answered `304`: byte-identical to what we already have.

    Carries `source_ref` (the ETag that produced the 304) so a caller can
    record *which* version was confirmed, matching the shape every other
    refusal in this repo uses.
    """

    def __init__(self, url: str, source_ref: str | None = None) -> None:
        super().__init__(f"{url} unchanged (304)")
        self.url = url
        self.source_ref = source_ref


class ETagStore:
    """`key -> the ETag the last successful fetch returned`.

    In memory, with no TTL and no eviction. A pod restart therefore costs
    exactly one full download per key, which is far cheaper than reading the
    last envelope back from the lake on every capture forever.

    It is bounded only because both of today's keys are season-scoped URLs,
    stable for the whole process lifetime -- two entries, not two per pass. A
    collector that keys by a per-week or per-game URL would grow this dict
    without bound and would need eviction before it opted in.

    Locked because `LastValueGauge` established the precedent that this
    library's shared state is touched from more than one thread -- the lake
    writes go through `asyncio.to_thread`.
    """

    def __init__(self) -> None:
        self._etags: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._etags.get(key)

    def set(self, key: str, etag: str | None) -> None:
        """Store `etag`, or forget `key` when it is None/empty.

        Forgetting rather than storing a falsy value matters: an upstream
        that stops sending ETags must degrade to unconditional GETs, not
        pin the last one it ever sent.
        """
        with self._lock:
            if etag:
                self._etags[key] = etag
            else:
                self._etags.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._etags.clear()


# Process-global on purpose: exactly one collector runs per process, so this
# is process-scoped state rather than shared-between-tenants state. Passed
# explicitly as a default argument everywhere it is used so a test can supply
# its own instance instead of reaching in and clearing this one.
ETAGS = ETagStore()


def conditional_headers(key: str, store: ETagStore = ETAGS) -> dict[str, str]:
    """`{"If-None-Match": <etag>}`, or `{}` when nothing is stored."""
    etag = store.get(key)
    return {"If-None-Match": etag} if etag else {}


class ConditionalStream:
    """A streamed response, plus the ETag commit its reader still owes.

    `commit()` is deliberately **not** called for you. An ETag is a claim that
    the collector holds the whole document; committing one for a body that was
    only partly read turns a loud, self-retrying failure into a silent, sticky
    one. Every later pass sends `If-None-Match`, gets a `304`, calls
    `mark_unchanged` -- so `last_capture_at` advances, `collector_staleness_
    seconds` resets, `collector_capture_failures_total` stops incrementing, and
    the collector reports itself healthy while serving whatever it managed to
    read once. It stays that way until the upstream publishes a new version,
    which for these assets is hours to days.

    So the reader calls `commit()` as the last thing it does, once the body is
    read to completion and every check on it has passed. Any early exit --
    exception, size ceiling, schema drift, or a consumer that `break`s out of
    its read loop -- simply never reaches the call, and the next pass
    re-downloads unconditionally.
    """

    __slots__ = ("response", "_key", "_store", "committed")

    def __init__(
        self, response: httpx.Response, key: str | None, store: ETagStore
    ) -> None:
        self.response = response
        self._key = key
        self._store = store
        self.committed = False

    def commit(self) -> None:
        """Record this response's ETag. Call only after a complete read.

        A no-op when the caller did not opt in (`etag_key=None`), so an
        unconditional reader can call it unconditionally.
        """
        if self._key is not None:
            self._store.set(self._key, self.response.headers.get("etag"))
        self.committed = True


@asynccontextmanager
async def conditional_stream(
    client: httpx.AsyncClient,
    url: str,
    *,
    etag_key: str | None = None,
    etag_store: ETagStore = ETAGS,
    follow_redirects: bool = True,
) -> AsyncIterator[ConditionalStream]:
    """`client.stream`, with the conditional-GET protocol applied around it.

    Sends `If-None-Match` when `etag_key` is set and something is stored for
    it, turns a `304` into `UpstreamUnchanged` before the caller sees a byte,
    and raises on any other non-2xx. With `etag_key` left `None` this is a
    plain `client.stream` that never reads or writes the store.

    The caller reads `stream.response` however it likes -- `aiter_text`,
    `aiter_bytes`, folding rows into its own accumulator -- and calls
    `stream.commit()` once the read finished cleanly.

    **Nothing runs after this function's `yield`, on purpose.** The obvious
    shape is to commit the ETag there and let an early exit skip it. That is
    only half right: a consumer that raises does skip it, but a consumer that
    `break`s out of its read loop exits the `async with` *normally*, so
    post-`yield` code runs and commits an ETag for a truncated read --
    `depth-chart`'s deadline path does exactly that. Verified, not assumed:
    see `test_a_context_manager_cannot_commit_after_yield` in
    `tests/test_conditional.py`.

    The `304` check has to precede `raise_for_status()`, and not defensively:
    `raise_for_status()` gates on `is_success`, which is 2xx only, so a `304`
    **does** raise `HTTPStatusError`. Drop the check and every unchanged
    upstream becomes a capture failure that writes `present: 0` over healthy
    data.
    """
    headers = conditional_headers(etag_key, etag_store) if etag_key is not None else {}
    async with client.stream(
        "GET", url, follow_redirects=follow_redirects, headers=headers
    ) as response:
        if etag_key is not None and response.status_code == 304:
            raise UpstreamUnchanged(url, source_ref=etag_store.get(etag_key))
        response.raise_for_status()
        yield ConditionalStream(response, etag_key, etag_store)
