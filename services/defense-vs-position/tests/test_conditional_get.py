"""Conditional GET on both feeds. A 304 is a SUCCESSFUL capture.

Two halves, and an incomplete opt-in fails differently depending on which one
is missing:

* without `etag_key`, nothing is saved -- every weekly poll re-downloads
  20.61 MiB it already has;
* without `except UpstreamUnchanged: raise` placed ABOVE the generic handler,
  a 304 is routed into `fail_capture`, which writes `present: 0` over a
  perfectly healthy capture and counts a failure that did not happen.

The second is the dangerous one and it is what most of this file is about.
"""

import httpx
import pytest
from collector_core.conditional import ETAGS, UpstreamUnchanged

from . import season
from .conftest import SpyLake, run_capture


async def test_a_second_pass_sends_if_none_match(upstreams):
    """The saving half. The ETag is committed by the generator's tail, so it
    exists only after a complete, valid read."""
    upstreams.set_pbp(season.pbp_document(), headers={"etag": '"pbp-v1"'})
    upstreams.set_players(season.players_document(), headers={"etag": '"pl-v1"'})
    await run_capture(SpyLake())

    await run_capture(SpyLake())
    sent = upstreams.pbp_route.calls[-1].request.headers.get("if-none-match")
    assert sent == '"pbp-v1"'


async def test_a_304_does_not_write_a_present_zero_envelope(upstreams):
    """The arm that destroys good data if it is missing.

    `raise_for_status()` gates on `is_success`, which is 2xx only, so a 304
    raises `HTTPStatusError` like any other non-2xx -- and routed into
    `fail_capture` it would overwrite a healthy capture with `present: 0`.
    """
    upstreams.set_pbp(season.pbp_document(), headers={"etag": '"pbp-v1"'})
    await run_capture(SpyLake())

    upstreams.pbp_route.mock(return_value=httpx.Response(304))
    lake = SpyLake()
    with pytest.raises(UpstreamUnchanged):
        await run_capture(lake)

    assert lake.writes == [], (
        "a 304 wrote an envelope -- UpstreamUnchanged was routed into "
        "fail_capture instead of being re-raised above it"
    )


async def test_a_truncated_read_does_not_commit_an_etag(upstreams):
    """An ETag claims the whole document was held.

    Commit one for a body that died mid-stream and every later pass 304s:
    `mark_unchanged` advances `last_capture_at`, staleness resets to ~0, the
    failure counter stops moving, and the collector reports itself healthy on
    a half-read document until the upstream republishes. A loud, self-retrying
    failure becomes a silent, sticky one.
    """
    upstreams.set_pbp(season.pbp_document()[:5000], headers={"etag": '"pbp-partial"'})
    with pytest.raises(Exception):
        await run_capture(SpyLake())
    assert ETAGS.get(upstreams.pbp_route.pattern.value) is None


async def test_the_etag_key_is_the_source_ref(upstreams):
    """Keyed by the same string the envelope records, so the cache key and the
    provenance cannot drift apart."""
    from defense_vs_position.adapters import pbp

    upstreams.set_pbp(season.pbp_document(), headers={"etag": '"pbp-v1"'})
    envelopes = await run_capture(SpyLake())
    source_ref = envelopes["defense_positional_allowance"].upstream.source_ref
    assert source_ref == pbp.source_ref(2026)
    assert ETAGS.get(source_ref) == '"pbp-v1"'


async def test_an_upstream_that_stops_sending_etags_degrades_to_unconditional(
    upstreams,
):
    """`ETagStore.set` forgets on a falsy value rather than pinning the last
    one it ever saw."""
    upstreams.set_pbp(season.pbp_document(), headers={"etag": '"pbp-v1"'})
    await run_capture(SpyLake())
    upstreams.set_pbp(season.pbp_document())
    await run_capture(SpyLake())
    assert ETAGS.get(upstreams.pbp_route.pattern.value) is None
