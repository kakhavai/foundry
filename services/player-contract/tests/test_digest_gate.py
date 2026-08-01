"""The digest gate: don't append an identical snapshot on a daily cadence.

Two failures live here and neither raises anything.

**Scoping.** The digest is keyed `(season, week, signal_type)`. A gate keyed to
a literal week suppresses a `POST /refresh {"week": 5}` backfill using week 3's
digest — the operator gets a 202, `last_capture_at` advances, and no object is
written. `officiating` hit this for real.

Parameterising a test over the scoping key catches only the **asymmetric**
mutant, where the lookup is rekeyed and the store is not: each parameter runs in
its own fresh process state, so a *symmetric* mutant — both sides keyed to a
literal — never sees two keys at once and passes every parameter. So the test
below captures two distinct weeks **live in one process**, and neither of them
is week 1.

This collector's rows do not depend on the week, which is what makes that test
sharp: weeks 3 and 5 digest **identically**, so a week-less gate suppresses the
second one. They do depend on the season, so the season half of the key cannot
be proven the same way and is pinned against the recorded keys instead.

**Durability.** A digest recorded for content the lake never received
permanently suppresses the retry: the next pass digests the same content,
matches, raises `UpstreamUnchanged`, and the object is never written again until
the data itself changes — months, on a `seasonal` cadence. The gate is recorded
from `PublishResult.landed`, after the write.
"""

import gzip

import httpx
import pytest
import respx
from collector_core.conditional import UpstreamUnchanged

from player_contract import capture as capture_mod
from player_contract.capture import CONTRACT_STATUS, capture_player_contract

from .conftest import (
    FIXTURE_CONTRACTS,
    NOW,
    SEASON,
    WEEK,
    SpyLake,
    contracts_csv,
    contracts_gz,
    mock_identity,
    scope_envelope,
)


async def capture(lake, *, season=SEASON, week=WEEK):
    async with httpx.AsyncClient() as client:
        return await capture_player_contract(
            season, week, client=client, lake=lake, now=NOW
        )


def _writes(lake):
    return [e for e in lake.writes if e.collector == "player-contract"]


class SeededRefusingLake(SpyLake):
    """Reads like a healthy lake, refuses every write.

    `SpyLake(fail_write=True)` cannot be used directly: the fixture plants the
    `roster-scope` scope envelope through `write`, so the pass would fail closed
    on `scope_unavailable` long before it reached the publish path this is
    about.
    """

    def __init__(self, scope) -> None:
        super().__init__()
        SpyLake.write(self, scope)
        self.refusing = True

    def write(self, envelope):
        if self.refusing:
            raise RuntimeError("lake unreachable")
        return SpyLake.write(self, envelope)


def _serve(mock, bodies):
    """Route the upstream at a sequence of bodies, one per call."""
    iterator = iter(bodies)

    def handler(request):
        return httpx.Response(
            200, content=next(iterator), headers={"ETag": '"fixture-etag"'}
        )

    return mock.get(capture_mod.source_ref(SEASON, WEEK)).mock(side_effect=handler)


@respx.mock
async def test_a_second_identical_pass_writes_nothing_and_raises_unchanged(lake):
    """The gate doing its job. `run_capture_loop` treats `UpstreamUnchanged` as
    a successful pass: `last_capture_at` advances and the stored envelopes are
    left alone, so `/signals` keeps serving what it already has."""
    _serve(respx.mock, [contracts_gz(), contracts_gz()])
    mock_identity(respx.mock)

    await capture(lake)
    assert len(_writes(lake)) == 1

    with pytest.raises(UpstreamUnchanged):
        await capture(lake)

    assert len(_writes(lake)) == 1


@respx.mock
async def test_TWO_DISTINCT_WEEKS_both_publish_in_ONE_process(lake):
    """The symmetric mutant, which parameterisation cannot reach.

    A gate whose lookup AND store are both keyed to a literal week passes every
    single-week test and every parameterised one — each parameter runs in fresh
    process state and only ever sees one key. It fails only when two keys are
    live at once, which is what this does. Neither week is 1, and the two passes
    digest identically, so a week-less gate suppresses the second.
    """
    _serve(respx.mock, [contracts_gz(), contracts_gz()])
    mock_identity(respx.mock)
    lake.write(scope_envelope(week=WEEK + 2))

    await capture(lake, week=WEEK)
    await capture(lake, week=WEEK + 2)

    weeks = sorted(e.scope["week"] for e in _writes(lake))
    assert weeks == [WEEK, WEEK + 2], weeks


@respx.mock
async def test_a_week_that_was_already_captured_is_still_suppressed(lake):
    """The other arm of the test above: proof the gate is not simply disabled.

    Without this, "both weeks publish" is satisfied by a gate that never
    suppresses anything at all.
    """
    _serve(respx.mock, [contracts_gz()] * 4)
    mock_identity(respx.mock)
    lake.write(scope_envelope(week=WEEK + 2))

    await capture(lake, week=WEEK)
    await capture(lake, week=WEEK + 2)
    with pytest.raises(UpstreamUnchanged):
        await capture(lake, week=WEEK)
    with pytest.raises(UpstreamUnchanged):
        await capture(lake, week=WEEK + 2)

    assert len(_writes(lake)) == 2


@respx.mock
async def test_the_digest_is_keyed_by_season_as_well_as_week(lake):
    """The season half of the key.

    It cannot be proven the way the week half is: `is_contract_year` and
    `seasons_remaining` are both taken against `scope.season`, so two seasons
    never digest identically and a season-less gate would let both through
    anyway. Asserted against the recorded keys instead — white-box, and
    deliberately so, because the alternative is an assertion that passes for the
    wrong reason.
    """
    _serve(respx.mock, [contracts_gz(), contracts_gz()])
    mock_identity(respx.mock)
    lake.write(scope_envelope(season=SEASON + 1))

    await capture(lake, season=SEASON)
    await capture(lake, season=SEASON + 1)

    assert set(capture_mod._PUBLISHED_DIGESTS) == {
        (SEASON, WEEK, CONTRACT_STATUS),
        (SEASON + 1, WEEK, CONTRACT_STATUS),
    }


@respx.mock
async def test_a_failed_lake_write_does_NOT_record_the_digest():
    """`PublishResult.landed`, and the reason it exists.

    Pass 1's write fails; `publish_capture` swallows it and returns the
    envelopes anyway, because availability wins over durability. If the digest
    were recorded from the local dict rather than from what LANDED, pass 2 would
    match, raise `UpstreamUnchanged`, and the object would never be written
    again until the contracts themselves changed. Reproduced on `venue` before
    the fix.
    """
    _serve(respx.mock, [contracts_gz(), contracts_gz()])
    mock_identity(respx.mock)

    broken = SeededRefusingLake(scope_envelope())
    envelopes = await capture(broken)
    assert envelopes[CONTRACT_STATUS].signals, (
        "publish_capture must return the envelopes even when the write failed"
    )
    assert not _writes(broken)

    # The same process, the same digest, a lake that now works.
    broken.refusing = False
    await capture(broken)

    assert len(_writes(broken)) == 1, (
        "the retry was suppressed by a digest recorded for a write that never landed"
    )


@respx.mock
async def test_changed_content_publishes_again(lake):
    """The gate must not be a one-write latch. A contract the upstream restated
    has to reach the lake on the very next pass."""
    restated = [
        r if r[1] != "Alpha Passer" or r[4] != "TRUE" else (*r[:7], 175_000_000, r[8])
        for r in FIXTURE_CONTRACTS
    ]
    _serve(
        respx.mock,
        [contracts_gz(), gzip.compress(contracts_csv(restated).encode())],
    )
    mock_identity(respx.mock)

    await capture(lake)
    await capture(lake)

    written = _writes(lake)
    assert len(written) == 2
    values = [
        row["total_value_usd"]
        for envelope in written
        for row in envelope.signals
        if row["contract_start_season"] == 2025 and row["seasons_remaining"] == 3
    ]
    assert 150_000_000 in values and 175_000_000 in values, values
