"""The unchanged-content gate, and the durability condition on it.

A `weekly` cadence polls daily and this collector's data changes about once a
week. Publishing a byte-identical envelope on the other six days fills an
append-only lake with objects carrying no information.

The subtle half is `PublishResult.landed`. `publish_capture` swallows a failed
lake write — right for availability, wrong for a gate that records "we have
published this content". Record a digest for content the lake never received
and the next pass digests the same content, matches, raises
`UpstreamUnchanged`, and **the object is never written again until the upstream
itself changes**. It takes TWO passes to see, and the single-pass availability
test stays green throughout.

**Per signal type, not per pass.** `if any(published.landed(st) for st in
published)` looks equivalent and is not, and only a PARTIAL lake failure can
tell them apart — under a total outage nothing lands and the two gates agree.
`SpyLake(fail_signal_types=...)` exists for exactly that arm.
"""

import httpx
import pytest
import respx
from collector_core.conditional import UpstreamUnchanged

from officiating.capture import (
    ASSIGNMENT,
    RATES,
    capture_officiating,
    reset_published_digests,
)

from .conftest import (
    NOW,
    SEASON,
    FakeGame,
    SpyLake,
    gzipped,
    mock_upstreams,
    pbp_csv,
    season_of,
)


async def _capture(lake, *, games=None, week: int = 1):
    mock_upstreams(games if games is not None else season_of())
    async with httpx.AsyncClient() as client:
        return await capture_officiating(
            SEASON, week, client=client, lake=lake, now=NOW
        )


@respx.mock
async def test_an_unchanged_second_pass_raises_upstream_unchanged():
    """The gate itself. A 304-equivalent: `run_capture_loop` catches this and
    calls `mark_unchanged`, so `last_capture_at` advances and `/signals` keeps
    serving the same rows."""
    lake = SpyLake()

    await _capture(lake)
    assert len(lake.writes) == 2

    with pytest.raises(UpstreamUnchanged):
        await _capture(lake)

    assert len(lake.writes) == 2, "a byte-identical envelope was appended anyway"


@respx.mock
async def test_changed_content_publishes_again():
    """The gate must not be a one-way door. Without this, a collector that
    published once would never publish anything else."""
    lake = SpyLake()
    games = season_of()

    await _capture(lake, games=games)
    games[0].members = (("90701", "Substitute", "Umpire"),)
    await _capture(lake, games=games)

    assert len(lake.writes) == 3, [w.signal_type for w in lake.writes]
    # The assignment content changed; the rates did not, so only one of the two
    # was re-appended. That is the per-signal-type gate doing its job.
    assert [w.signal_type for w in lake.writes[2:]] == [ASSIGNMENT]


@respx.mock
async def test_a_total_lake_outage_does_not_record_the_digest():
    """Pass 1 fails to write; pass 2 must still try.

    This is the arm that a per-pass gate ALSO passes — kept because it is the
    common case and because it pins the availability half: the envelopes are
    still returned, because the capture succeeded and only its archival copy
    did not.
    """
    failing = SpyLake(fail_write=True)

    envelopes = await _capture(failing)
    assert set(envelopes) == {ASSIGNMENT, RATES}, "availability survives the outage"
    assert failing.writes == []

    healthy = SpyLake()
    await _capture(healthy)

    assert {w.signal_type for w in healthy.writes} == {ASSIGNMENT, RATES}


@respx.mock
async def test_a_partial_lake_outage_only_records_the_digest_that_landed():
    """THE arm. A per-pass gate passes every other test in this file and fails
    this one.

    `crew_tendency_rates` writes fail; `game_crew_assignment` writes succeed.
    A gate keyed on "did anything land" records BOTH digests, and the rates
    object is then never written again until a crew's penalties change — which
    on a weekly cadence can be the rest of the season.
    """
    partial = SpyLake(fail_signal_types=frozenset({RATES}))

    await _capture(partial)
    assert [w.signal_type for w in partial.writes] == [ASSIGNMENT]

    # Same content, healthy lake. The assignment digest WAS recorded and must
    # be suppressed; the rates digest was not, and must be retried.
    healthy = SpyLake()
    await _capture(healthy)

    assert [w.signal_type for w in healthy.writes] == [RATES], (
        "a per-pass gate records the failed type's digest too, and the rates "
        "envelope is never written again"
    )


@respx.mock
async def test_the_degraded_pass_does_not_suppress_the_next_healthy_one():
    """A pass that could not read play-by-play must not be able to claim it has
    already published the rates it never computed."""
    lake = SpyLake()
    games = season_of()

    mock_upstreams(games, pbp_response=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        await capture_officiating(SEASON, 1, client=client, lake=lake, now=NOW)
    assert {w.signal_type for w in lake.writes} == {ASSIGNMENT, RATES}

    respx.reset()
    healthy = SpyLake()
    await _capture(healthy, games=games)

    assert {w.signal_type for w in healthy.writes} == {ASSIGNMENT, RATES}


@respx.mock
async def test_a_repeated_degraded_pass_stops_re_appending_the_assignments():
    """The preseason case, and the reason the degraded path gates at all.

    `play_by_play_<season>.csv.gz` does not exist until a season's first games
    are played, so the degraded branch is the NORMAL one for months. Ungated,
    a `weekly` cadence appends a byte-identical — and usually empty —
    assignment envelope to an append-only lake every day for that whole
    stretch: precisely the waste the gate was built to prevent, across the
    longest window of the year.

    The rates digest is deliberately still not recorded, which the next test
    covers.
    """
    games = season_of()
    lake = SpyLake()

    mock_upstreams(games, pbp_response=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        await capture_officiating(SEASON, 1, client=client, lake=lake, now=NOW)
    assert {w.signal_type for w in lake.writes} == {ASSIGNMENT, RATES}

    # Second identical degraded pass: nothing changed, so nothing is appended.
    with pytest.raises(UpstreamUnchanged):
        async with httpx.AsyncClient() as client:
            await capture_officiating(SEASON, 1, client=client, lake=lake, now=NOW)

    assert len(lake.writes) == 2, "a byte-identical envelope was appended anyway"


@respx.mock
async def test_a_degraded_pass_whose_write_failed_still_retries():
    """The durability gate applies on the degraded path too.

    Recording the assignment digest for content the lake never received would
    suppress the retry for the whole preseason — the same permanent-suppression
    failure `PublishResult.landed` exists for, on the branch that runs most
    often.
    """
    games = season_of()

    mock_upstreams(games, pbp_response=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        await capture_officiating(
            SEASON, 1, client=client, lake=SpyLake(fail_write=True), now=NOW
        )

    respx.reset()
    healthy = SpyLake()
    mock_upstreams(games, pbp_response=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        await capture_officiating(SEASON, 1, client=client, lake=healthy, now=NOW)

    assert {w.signal_type for w in healthy.writes} == {ASSIGNMENT, RATES}


@respx.mock
async def test_the_degraded_pass_never_records_the_rates_digest():
    """The asymmetry between the two digests, at the one input that shows it.

    The degraded rates envelope is a `present: 0` failure envelope, and its
    `signals` list is empty. So is the rates envelope of a *healthy* pass whose
    play-by-play carries no game this season's crews worked — a real state, and
    a different one: `errors` says `no_games_sampled` rather than
    `penalties_unavailable`, and `upstream.source_ref` differs.

    Digests are taken over the ROWS, so both hash to `_digest([])`. If the
    degraded path recorded that digest, the healthy-but-empty pass would be
    suppressed and the lake would never learn that play-by-play came back —
    it would still say the feed was unreachable.

    Found by mutation: adding the rates digest to the degraded path survives
    every other test in this file, because every other fixture's healthy pass
    produces eight crews.
    """
    games = season_of()
    lake = SpyLake()

    mock_upstreams(games, pbp_response=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        await capture_officiating(SEASON, 1, client=client, lake=lake, now=NOW)
    assert {w.signal_type for w in lake.writes} == {ASSIGNMENT, RATES}

    # Play-by-play is readable now, but describes a game no crew here worked,
    # so every crew's window is empty and `rates_rows` is `[]` again.
    respx.reset()
    stranger = FakeGame(
        game_id="9999_01_XX_YY",
        legacy_game_id="9999010100",
        week=1,
        referee_id="999",
        referee_name="Nobody",
    )
    healthy = SpyLake()
    mock_upstreams(
        games,
        pbp_response=httpx.Response(200, content=gzipped(pbp_csv([stranger]))),
    )
    async with httpx.AsyncClient() as client:
        envelopes = await capture_officiating(
            SEASON, 1, client=client, lake=healthy, now=NOW
        )

    assert envelopes[RATES].signals == []
    assert RATES in {w.signal_type for w in healthy.writes}, (
        "the degraded pass suppressed a genuinely different rates envelope"
    )
    reasons = {e["reason"] for e in envelopes[RATES].errors}
    assert "no_games_sampled" in reasons, reasons
    assert "penalties_unavailable" not in reasons


@respx.mock
async def test_a_degraded_assignment_envelope_can_never_match_a_healthy_one():
    """The structural property that makes the degraded path safe, pinned.

    `_capture_without_penalties` deliberately records no digest, and the test
    above proves the next healthy pass still publishes. But there is a *second*
    reason that holds, and it is load-bearing enough to be checked rather than
    relied on: `games_sampled` on every `game_crew_assignment` row is 0 when
    play-by-play was unavailable and non-zero when it was not, so the two
    envelopes cannot be byte-identical even if a digest were recorded.

    Found by mutation. Adding a digest record to the degraded path is a
    survivor — an equivalent mutant — *only* because of this field. Drop
    `games_sampled` from the assignment row, or default it to 0 on the healthy
    path, and that latent bug becomes a real one with nothing to catch it. This
    test is what turns an accident into an invariant.
    """
    games = season_of()

    mock_upstreams(games, pbp_response=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        degraded = await capture_officiating(
            SEASON, 1, client=client, lake=SpyLake(), now=NOW
        )

    respx.reset()
    reset_published_digests()
    healthy = await _capture(SpyLake(), games=games)

    degraded_rows = degraded[ASSIGNMENT].signals
    healthy_rows = healthy[ASSIGNMENT].signals
    assert len(degraded_rows) == len(healthy_rows) > 0
    assert all(row["games_sampled"] == 0 for row in degraded_rows)
    assert all(row["games_sampled"] > 0 for row in healthy_rows)
    assert degraded_rows != healthy_rows


@respx.mock
async def test_the_gate_is_scoped_per_season_and_week():
    """Two scopes are two lake partitions, so identical content in each is two
    legitimate objects rather than a duplicate."""
    lake = SpyLake()

    await _capture(lake, week=1)
    await _capture(lake, week=2)

    assert len(lake.writes) == 4
