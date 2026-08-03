"""Conditional GET across six feeds, and the two ways it goes wrong.

Both hazards below are live bugs from this fleet rather than hypotheticals:

* `defense-vs-position` had a `304` on a small auxiliary feed suppress a
  *changed* primary feed, because the small one was fetched first and gated
  the pass — and `mark_unchanged` then advanced `last_capture_at`, so
  staleness never alerted. The collector went quiet while looking healthy.
* `defensive-front` applied its failure lattice to the first request only, so
  an optional feed that `304`'d and then failed its unconditional re-fetch
  threw an uncaught `HTTPStatusError` out of the whole capture. No
  `fail_capture`, no `present: 0` envelope, no counter. **That is the ordinary
  weekly path**: play-by-play changes whenever a game finishes and the roster
  feed routinely does not, so the re-fetch runs on essentially every real
  capture.

The mock answers `If-None-Match` the way the real upstream does — a `304` only
when the request actually carries the matching validator. A mock that returned
`304` unconditionally would pass a collector that never sent the header, and
would make the re-fetch path unrepresentable.
"""

import httpx
import pytest
from collector_core.conditional import UpstreamUnchanged

from offensive_line.capture import (
    REASON_REFETCH_UNCHANGED,
    STRENGTH,
    every_feed_unchanged,
    reset_published_digests,
)

from .conftest import Feeds, SpyLake, run_capture, units

ALL_ETAGS = {name: f'"{name}-v1"' for name in Feeds.NAMES}


def test_a_pass_that_attempted_no_feed_is_not_unchanged():
    """`all({})` is `True`, so without the emptiness guard a pass that fetched
    nothing would report itself unchanged: `last_capture_at` advances,
    `collector_upstream_unchanged_total` increments, no envelope is written,
    and nothing in the metrics says why.

    The caller cannot produce an empty map today — a play-by-play failure ends
    the pass first — which is exactly why the guard is a named function.
    Inlined, the mutation that removes it would survive any suite that cannot
    construct the input distinguishing the two.
    """
    assert every_feed_unchanged({}) is False
    assert every_feed_unchanged({"pbp": True}) is True
    assert every_feed_unchanged({"pbp": True, "players": False}) is False


async def test_the_first_pass_downloads_and_the_second_is_unchanged():
    lake = SpyLake()
    feeds = Feeds(etags=ALL_ETAGS)
    await run_capture(feeds, lake=lake)
    assert all(feeds.conditional_calls(name) == 0 for name in Feeds.NAMES), (
        "nothing is stored yet, so the first pass sends no validator"
    )

    with pytest.raises(UpstreamUnchanged):
        await run_capture(feeds, lake=lake)
    assert all(feeds.conditional_calls(name) == 1 for name in Feeds.NAMES)
    assert len(lake.writes) == 1, "an unchanged pass appends nothing"


async def test_one_unchanged_feed_does_not_suppress_a_changed_one():
    """**The `defense-vs-position` hazard.** A `304` on any single feed must
    not end the pass while another feed genuinely changed — the collector
    would go quiet with `last_capture_at` still advancing."""
    lake = SpyLake()
    # Only the roster feed carries an ETag, so only it can answer `304`.
    feeds = Feeds(etags={"players": '"players-v1"'})
    await run_capture(feeds, lake=lake)
    # The digest gate is a **separate** mechanism — it suppresses a
    # byte-identical append even when every feed changed — and it is tested on
    # its own in `test_digest_gate.py`. Cleared here so this test is about the
    # feed-level `304` and nothing else; leaving it in place would make the
    # second pass raise for a reason that has nothing to do with ETags.
    reset_published_digests()
    envelopes = await run_capture(feeds, lake=lake)

    assert len(lake.writes) == 2
    assert units(envelopes), "the changed feeds still produced rows"
    # And the 304'd feed was read after all, unconditionally, so its fields
    # are not silently empty.
    assert all(row["degraded_upstreams"] == [] for row in units(envelopes).values())
    assert feeds.calls("players") == 3, (
        "one conditional attempt per pass, plus the unconditional re-fetch"
    )


async def test_the_unconditional_refetch_goes_through_the_failure_lattice():
    """**The `defensive-front` hazard, and the ordinary weekly path.**

    The roster feed answers `304` to the conditional request and then `500` to
    the unconditional re-fetch. Before the lattice covered both call sites
    that propagated an uncaught `HTTPStatusError` out of the whole capture. It
    must degrade one half of the rows instead.
    """
    lake = SpyLake()
    feeds = Feeds(etags={"players": '"players-v1"'})
    await run_capture(feeds, lake=lake)

    second = Feeds(etags={"players": '"players-v1"'}, status={"players": 500})
    envelopes = await run_capture(second, lake=lake)
    for row in units(envelopes).values():
        assert "players_unavailable" in row["degraded_upstreams"]
        assert row["pressure_rate_allowed"] is not None


async def test_a_fatal_feed_answering_304_unconditionally_fails_the_pass():
    """A `304` to a request that carried no validator is an upstream contract
    violation, and we still have no body. Letting the `None` reach the fold
    would raise `AttributeError` outside every handler."""
    lake = SpyLake()
    feeds = Feeds(etags={"players": '"players-v1"'}, always_304={"pbp"})
    with pytest.raises(Exception) as caught:
        await run_capture(feeds, lake=lake)
    assert "304" in str(caught.value)
    assert lake.writes, "a failed capture must still write an envelope"
    assert {error["reason"] for error in lake.writes[0].errors} == {
        REASON_REFETCH_UNCHANGED
    }


async def test_a_304_never_reaches_fail_capture():
    """Routing `UpstreamUnchanged` into `fail_capture` would write a
    `present: 0` envelope over a perfectly healthy capture — the exact
    destroy-good-data outcome that module's docstring warns about."""
    lake = SpyLake()
    feeds = Feeds(etags=ALL_ETAGS)
    await run_capture(feeds, lake=lake)
    before = len(lake.writes)
    with pytest.raises(UpstreamUnchanged):
        await run_capture(feeds, lake=lake)
    assert len(lake.writes) == before
    assert all(envelope.coverage.present > 0 for envelope in lake.writes)


async def test_an_etag_is_not_committed_for_a_body_that_failed():
    """The ETag is committed by `stream_csv_dicts`' generator tail, which an
    exception cannot reach. Committing one for a partial read turns a loud,
    self-retrying failure into a silent, sticky one: every later pass 304s,
    staleness resets to zero and the collector reports itself healthy on a
    truncated document until the upstream republishes."""
    whole = Feeds().bodies["pbp"]
    feeds = Feeds(etags=ALL_ETAGS, bodies={"pbp": whole[: len(whole) // 2]})
    with pytest.raises(Exception):
        await run_capture(feeds, lake=SpyLake())

    healthy = Feeds(etags=ALL_ETAGS)
    envelopes = await run_capture(healthy, lake=SpyLake())
    assert healthy.conditional_calls("pbp") == 0, (
        "a truncated read must leave the store untouched"
    )
    assert units(envelopes)


async def test_the_envelope_records_the_artifact_not_the_etag():
    """`source_ref` names the artifact a lake object was built from, which is
    what makes it reproducible. The ETag is the cache key and is deliberately
    the same string as the URL, so the two cannot drift."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.upstream.source_ref.startswith("https://")


async def test_a_connection_error_on_an_optional_feed_is_degradation():
    """Not every failure is a status code. A transport error must be
    classified by the same lattice."""

    feeds = Feeds()

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    original = feeds.install

    def install(router):
        original(router)
        from offensive_line.adapters import injuries as injuries_adapter

        router.get(injuries_adapter.source_ref(feeds.season)).mock(side_effect=broken)

    feeds.install = install
    envelopes = await run_capture(feeds, lake=SpyLake())
    for row in units(envelopes).values():
        assert row["degraded_upstreams"] == ["injuries_unavailable"]
