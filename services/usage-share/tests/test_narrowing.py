"""Narrowing is behavioural: the assertion is what got fetched and published,
never the `scope_aware` flag.

There are ~1,700 players in the league and ~416 that matter. This collector's
feed carries every one of the ~1,700, and every row it publishes is now one
`roster-scope` named — resolved **forward**, GSIS id through `player-identity`
to a canonical `fdy-` id, then checked against the membership list.

Three properties, and each one is a decision that could plausibly have gone the
other way:

- **Fail closed.** No scope means ZERO upstream calls, not "fetch everything"
  and not "fetch and filter to nothing". An unnarrowed fallback would blow the
  vendor's budget precisely during an incident, so there is none — not even
  behind a flag.
- **`player-identity` is authoritative.** A row it will not resolve is dropped.
  Never published under the upstream's id, and never under a `candidates` entry
  it declined to adopt.
- **`coverage.expected` never derives from the scope.** `Coverage.ratio`
  returns 1.0 when `expected` is 0, so an expectation built from the scope
  would let a truncated scope read as a perfect week.
"""

import json

import httpx
import pytest
import respx
from collector_core.identity import BATCH_LIMIT, IdentityClient
from collector_core.scope import ScopeUnavailable

from usage_share.adapters.scope import IDENTITY_UNAVAILABLE
from usage_share.capture import EXPECTED_FLOOR, capture_usage_share

from .conftest import (
    NOW,
    RESOLVE_BATCH_URL,
    SAMPLE_PLAYER_ROWS,
    SAMPLE_RECORDS,
    SAMPLE_TEAMS,
    UPSTREAM_FOR_SEASON,
    canonical_id,
    full_league_csv,
    mock_identity,
    scope_for,
    seed_scope,
    to_csv,
)

SIGNAL_TYPE = "player_usage_weekly"
FLOOR = EXPECTED_FLOOR[SIGNAL_TYPE]


async def _capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_usage_share(
            2026, 1, client=client, lake=lake, now=NOW, **kwargs
        )


def _written(lake, signal_type: str = SIGNAL_TYPE):
    """The envelope this collector wrote, as opposed to the seeded scope one.

    Read off the lake rather than a return value because the fail-closed path
    re-raises: `fail_capture` writes and then re-raises so `CaptureState` never
    installs an empty capture over the last good one, which means the failure
    envelope is only ever observable here.
    """
    written = [
        envelope
        for envelope in lake.writes
        if envelope.collector == "usage-share" and envelope.signal_type == signal_type
    ]
    assert written, f"nothing written for {signal_type}"
    return written[-1]


def _refusing_upstream(router, calls: list):
    """An upstream route that records the call and then fails loudly.

    Loud rather than a plain recorder: a fetch that must not happen should not
    be able to quietly succeed and let the assertion below carry the whole
    weight of noticing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("the upstream must not be reached without a scope")

    return router.get(UPSTREAM_FOR_SEASON).mock(side_effect=handler)


async def test_only_scoped_players_are_published(lake):
    """The document carries eight skill players; the scope names two."""
    body = to_csv(SAMPLE_RECORDS)
    scoped = {canonical_id("00-KC-WR1"), canonical_id("00-BUF-RB1")}

    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=body)
        )
        mock_identity(router)
        seed_scope(lake, scoped)
        envelopes = await _capture(lake)

    published = {row["player_id"] for row in envelopes[SIGNAL_TYPE].signals}
    assert published == scoped
    assert len(published) == 2
    assert SAMPLE_PLAYER_ROWS > 2, "the document must carry rows the scope excludes"


async def test_no_scope_means_zero_upstream_calls(lake):
    """Fail closed. Not 'fetch everything', not 'fetch and filter to nothing' —
    zero calls to the upstream and a `present: 0` envelope."""
    calls: list[str] = []

    with respx.mock(assert_all_called=False) as router:
        _refusing_upstream(router, calls)
        mock_identity(router)
        with pytest.raises(ScopeUnavailable) as caught:
            await _capture(lake)

    assert calls == []
    assert caught.value.reason == "scope_unavailable"

    envelope = _written(lake)
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == FLOOR
    assert envelope.coverage.expected >= 1, (
        "expected: 0 makes Coverage.ratio read 1.0 — failing closed would "
        "report perfect coverage"
    )
    assert envelope.coverage.ratio == 0.0
    reasons = [error["reason"] for error in envelope.errors]
    assert len(reasons) >= 1, "a failure envelope with no errors explains nothing"
    assert any(reason == "scope_unavailable" for reason in reasons), reasons


async def test_no_identity_deployment_also_means_zero_upstream_calls(lake, monkeypatch):
    """The other half of failing closed, and the one that is easy to miss.

    A perfectly good scope is useless without the join that reaches it: every
    row would resolve to nothing, every row would be dropped, and the ~8.3 MB
    season CSV would have been fetched to publish an empty envelope.
    """
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "")
    calls: list[str] = []
    body = to_csv(SAMPLE_RECORDS)

    with respx.mock(assert_all_called=False) as router:
        _refusing_upstream(router, calls)
        seed_scope(lake, scope_for(body))
        with pytest.raises(ScopeUnavailable) as caught:
            await _capture(lake)

    assert calls == []
    assert caught.value.reason == IDENTITY_UNAVAILABLE

    envelope = _written(lake)
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == FLOOR
    reasons = [error["reason"] for error in envelope.errors]
    assert len(reasons) >= 1
    assert any(reason == IDENTITY_UNAVAILABLE for reason in reasons), reasons


async def test_an_unresolved_row_is_dropped_not_adopted(lake):
    """`player-identity` refusing is the answer, not the start of a negotiation.

    The fake attaches a 0.99-confidence candidate to its refusal, which is
    precisely the id a caller that re-ranked `candidates` against a local floor
    would adopt. It must not appear, and neither must the upstream's own id.
    """
    body = to_csv(SAMPLE_RECORDS)
    refused = "00-KC-WR1"

    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=body)
        )
        mock_identity(router, unresolvable={refused})
        seed_scope(lake, scope_for(body))
        envelopes = await _capture(lake)

    published = {row["player_id"] for row in envelopes[SIGNAL_TYPE].signals}
    assert canonical_id(refused) not in published, "a refused candidate was adopted"
    assert refused not in published, "the upstream id was published as canonical"
    assert canonical_id("00-KC-WR2") in published, "the rest of the week was lost"
    assert len(published) == SAMPLE_PLAYER_ROWS - 1
    assert all(player_id.startswith("fdy-") for player_id in published)


async def test_resolution_is_batched_within_the_limit(lake, monkeypatch):
    """A 1,700-row feed must not become 1,700 requests — and must not become
    one 1,700-query list either.

    Two assertions, because they catch different mutations and only one of them
    is this collector's own work. The **request** sizes are guaranteed by
    `IdentityClient.resolve_many`, which chunks whatever it is handed; deleting
    this adapter's buffering entirely leaves them at 500/500/200 and looks
    perfectly healthy. What that mutation actually costs is the **query list**:
    one per row of the feed, built and held at once. So the sizes handed *to*
    `resolve_many` are asserted as well, which is the bound `resolve_in_scope`
    is responsible for and the only one a rewrite can lose.
    """
    body = full_league_csv(teams=30, players_per_team=40)
    batch_sizes: list[int] = []
    handed_over: list[int] = []

    unbuffered = IdentityClient.resolve_many

    async def spy(self, queries):
        handed_over.append(len(queries))
        return await unbuffered(self, queries)

    monkeypatch.setattr(IdentityClient, "resolve_many", spy)

    def handler(request: httpx.Request) -> httpx.Response:
        queries = json.loads(request.content)["queries"]
        batch_sizes.append(len(queries))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "resolved": True,
                        "player_id": canonical_id(query["source_id"]),
                        "confidence": 1.0,
                    }
                    for query in queries
                ]
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=body)
        )
        router.post(RESOLVE_BATCH_URL).mock(side_effect=handler)
        seed_scope(lake, scope_for(body))
        envelopes = await _capture(lake)

    assert sum(batch_sizes) == 1200
    assert len(batch_sizes) == 3
    assert all(size <= BATCH_LIMIT for size in batch_sizes), batch_sizes

    assert sum(handed_over) == 1200
    assert len(handed_over) == 3
    assert all(size <= BATCH_LIMIT for size in handed_over), handed_over

    assert len(envelopes[SIGNAL_TYPE].signals) == 1200


async def test_expected_is_the_declared_floor_not_the_scope_size(lake):
    """The failure lesson 2 names: an expectation derived from the scope makes
    a truncated scope — two members instead of 416 — read as a perfect week."""
    body = to_csv(SAMPLE_RECORDS)
    scoped = {canonical_id("00-KC-WR1"), canonical_id("00-BUF-RB1")}

    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=body)
        )
        mock_identity(router)
        seed_scope(lake, scoped)
        envelopes = await _capture(lake)

    envelope = envelopes[SIGNAL_TYPE]
    assert envelope.coverage.expected == FLOOR
    assert envelope.coverage.expected > len(scoped) + 1
    assert envelope.coverage.present == len(scoped) + SAMPLE_TEAMS
    assert envelope.coverage.ratio < 0.05
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


async def test_an_out_of_scope_row_is_not_counted_missing(lake):
    """A player the scope excludes is not a hole this collector left.

    Recording one would make narrowing read as a coverage regression every
    week, and would bury the rows that genuinely did fail. The shortfall
    against the declared floor is what stays visible instead.
    """
    body = to_csv(SAMPLE_RECORDS)

    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=body)
        )
        mock_identity(router)
        seed_scope(lake, {canonical_id("00-KC-WR1")})
        envelopes = await _capture(lake)

    envelope = envelopes[SIGNAL_TYPE]
    assert envelope.coverage.missing == []
    assert envelope.coverage.present == 1 + SAMPLE_TEAMS
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


async def test_the_published_id_is_the_canonical_one(lake, upstream):
    """`player_id` is documented as "the canonical id from player-identity",
    and it finally is one. A consumer must be able to tell from the row."""
    envelopes = await _capture(lake)
    rows = envelopes[SIGNAL_TYPE].signals

    assert len(rows) == SAMPLE_PLAYER_ROWS
    for row in rows:
        assert row["player_id_source"] == "player_identity"
        assert row["player_id"].startswith("fdy-")
