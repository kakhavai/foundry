"""Conditional GET, so a poll that finds nothing new costs nothing.

A collector on a `volatile` cadence polls 96 times a day. `depth-chart`'s
upstream is a 37.1 MB asset that changes a few times a week, so 95 of those
96 downloads are of a document the collector already has -- ~3.4 GB/day, and
one `CAPTURE_ENABLED` flip away from being real.

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
"""

import threading


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
