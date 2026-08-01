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


# --------------------------------------------------------------------------
# The roster feed is deliberately UNconditional. See adapters/players.py.
# --------------------------------------------------------------------------


async def test_the_roster_feed_never_sends_if_none_match(upstreams):
    """`players.csv.gz` is fetched unconditionally, on purpose.

    It is fetched FIRST, because it gates which players the fold accumulates.
    With an `etag_key` an unchanged roster raised `UpstreamUnchanged` before
    the play-by-play was requested at all, so a roster that had not moved
    suppressed a play-by-play carrying a whole new week of games.
    """
    upstreams.set_players(season.players_document(), headers={"etag": '"pl-v1"'})
    await run_capture(SpyLake())
    await run_capture(SpyLake())

    for call in upstreams.players_route.calls:
        assert "if-none-match" not in call.request.headers, (
            "the roster feed is conditional again -- an unchanged roster will "
            "suppress a changed play-by-play"
        )


async def test_an_unchanged_roster_does_not_suppress_a_changed_play_by_play(
    upstreams,
):
    """The freshness bug itself, driven end to end.

    Pass 1 ETags both feeds. Pass 2 serves a roster the upstream would answer
    `304` for and a play-by-play with a THIRD week in it. The new week must
    reach `/signals`.

    Note the failure this guards is silent in every direction: `mark_unchanged`
    advances `last_capture_at`, so `collector_staleness_seconds` resets, the
    failure counter stays flat, and the collector serves last week's ratings
    while every gauge reads healthy.
    """
    # A roster server that honours `If-None-Match` the way a real one does:
    # 200 with a body when the request is unconditional, 304 when the caller
    # says it already holds this version. Mocking the *protocol* rather than a
    # bare 304 is what makes this test fail if `etag_key` is ever restored --
    # a fixture that returns 304 unconditionally would instead just error.
    roster = season.players_document()

    def conditional_roster(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"pl-v1"':
            return httpx.Response(304, headers={"etag": '"pl-v1"'})
        return httpx.Response(200, content=roster, headers={"etag": '"pl-v1"'})

    upstreams.players_route.mock(side_effect=conditional_roster)
    upstreams.set_pbp(season.pbp_document(weeks=2), headers={"etag": '"pbp-v1"'})
    first = await run_capture(SpyLake(), week=2)
    assert {
        r["adjustment_window_weeks"]
        for r in first["defense_positional_allowance"].signals
    } == {2}

    # The roster has not changed; the play-by-play has.
    upstreams.set_pbp(season.pbp_document(weeks=3), headers={"etag": '"pbp-v2"'})

    second = await run_capture(SpyLake(), week=3)
    assert {
        r["adjustment_window_weeks"]
        for r in second["defense_positional_allowance"].signals
    } == {3}, "the new week never reached the capture"
    assert upstreams.pbp_route.called


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
