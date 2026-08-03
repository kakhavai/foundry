"""Identity: what is sent, what is adopted, and what is never adopted.

`starter_id` is the only field that needs `player-identity`, which is why an
identity outage costs the five starter rows and never the pass. Three claims
here and each has a live counterpart in the repo:

* a refusal is a missing row, never a raw upstream id promoted to canonical;
* a failed *request* is a different fact from a refusal, and both are absent
  from the returned dict, so reading only the dict reports an outage as a
  quiet week;
* the position sent is one `player-identity` knows — issue #106, where one
  unmapped code fails all 500 queries in the batch.
"""

import httpx
import pytest

from offensive_line.adapters import identity as identity_adapter
from offensive_line.adapters.identity import (
    POSITION_FOR_SLOT,
    SENDABLE_POSITIONS,
    IdentityUnavailable,
    build_identity_client,
    build_query,
)
from offensive_line.capture import (
    REASON_IDENTITY_UNRESOLVED,
    REASON_IDENTITY_UPSTREAM_ERROR,
    STRENGTH,
)
from offensive_line.ratings import STARTER_POSITIONS, StarterSlot

from . import season as season_module
from .conftest import (
    IDENTITY_URL,
    Feeds,
    SpyLake,
    canonical_for,
    resolve_everything,
    run_capture,
    starters,
)

# --------------------------------------------------------------------------
# Issue #106: one unmapped position fails the whole batch
# --------------------------------------------------------------------------


def test_every_slot_label_maps_to_a_position_player_identity_knows():
    """**The specific hazard this collector carries.**

    The codes it thinks in are the spec's five slot labels, and four of them —
    `LT`, `LG`, `RG`, `RT` — are absent from `player-identity`'s
    `KNOWN_POSITIONS`. Sending one raises `HTTPException(422)` *inside*
    `resolve_queries`' loop over the batch, so a single left tackle fails 500
    queries and the pass silently loses every one of them.
    """
    assert set(POSITION_FOR_SLOT) == set(STARTER_POSITIONS)
    assert set(POSITION_FOR_SLOT.values()) <= SENDABLE_POSITIONS
    assert set(POSITION_FOR_SLOT.values()) == {"T", "G", "C"}


def test_an_unrecognised_slot_travels_as_none_rather_than_as_a_422():
    """The guard is an assertion, not a consequence of the mapping staying
    narrow. An unknown label costs one scoring signal for one query instead of
    500 resolutions."""
    slot = StarterSlot(position="OG7", gsis_id="00-1000000")
    assert build_query(slot, "AAA", None, 2026).position is None


def test_the_query_carries_what_player_identity_scores_on():
    """`jersey_number` alone is 0.20 of the server-side weighting, and `team`
    comes from the per-game snap feed rather than `players.csv`'s
    career-latest `latest_team`, which names the wrong club for anyone traded
    mid-season."""
    from offensive_line.adapters.players import PlayerRef

    reference = PlayerRef(
        gsis_id="00-1000000", pfr_id="X", position="OT", jersey_number=74, on_ir=False
    )
    query = build_query(
        StarterSlot(position="LT", gsis_id="00-1000000"), "AAA", reference, 2026
    )
    assert query.team == "AAA"
    assert query.position == "T"
    assert query.jersey_number == 74
    assert query.season == 2026
    assert query.source == "gsis"
    assert query.source_id == "00-1000000"
    # No name. A GSIS id absent from the crosswalk would otherwise fall
    # through to attribute scoring, and a feed that already carries a league
    # id and is matched by name anyway is how two linemen with one surname
    # become one player.
    assert query.name is None


# --------------------------------------------------------------------------
# Adoption, and the refusal that must never be adopted
# --------------------------------------------------------------------------


async def test_a_resolved_starter_publishes_the_canonical_id():
    rows = starters(await run_capture(Feeds(), lake=SpyLake()))
    row = next(row for row in rows["AAA"] if row["starter_position"] == "LT")
    assert row["starter_id"] == canonical_for(season_module.line_id("AAA", 0))
    assert row["starter_id"].startswith("fdy-")


async def test_a_refused_starter_takes_the_whole_five_with_it():
    """**`resolved.get(query)`, never `resolved.get(query, raw_id)`.**

    A defaulting `.get` is positionally safe — the code runs, the types line
    up, every test stays green — and it adopts nflverse's own GSIS key for
    exactly the players the authoritative service refused to name. Those ids
    would be published as `starter_id` and a generator joining on them would
    match nothing.

    All five go, because the spec's clause is all-or-nothing: a partial five
    silently changes what `lineup_hash` means.
    """
    refused = frozenset({season_module.line_id("AAA", 2)})
    envelopes = await run_capture(
        Feeds(),
        lake=SpyLake(),
        identity_router=lambda router: resolve_everything(router, refuse=refused),
    )
    rows = starters(envelopes)
    assert "AAA" not in rows
    assert "BBB" in rows

    envelope = envelopes[STRENGTH]
    assert f"AAA:{STARTER_POSITIONS[0]}" in envelope.coverage.missing
    reasons = {error["reason"] for error in envelope.errors}
    assert REASON_IDENTITY_UNRESOLVED in reasons, reasons


async def test_an_identity_outage_is_distinguishable_from_a_quiet_week():
    """A failed *request* and a refusal are absent from the returned dict in
    exactly the same way, so a caller reading only the dict reports a total
    outage as a week in which no team could field five linemen."""

    def unreachable(router):
        router.post(f"{IDENTITY_URL}/resolve/batch").mock(
            side_effect=httpx.ConnectError("identity down")
        )

    envelopes = await run_capture(Feeds(), lake=SpyLake(), identity_router=unreachable)
    assert starters(envelopes) == {}
    reasons = {error["reason"] for error in envelopes[STRENGTH].errors}
    assert REASON_IDENTITY_UPSTREAM_ERROR in reasons, reasons


async def test_an_unset_identity_url_is_a_reason_not_a_stub_resolver(monkeypatch):
    """Every stub in this fleet mints an `fdy-` id from a hash of the upstream
    key. Here that would publish ids `player-identity` never issued as
    `starter_id`, where a generator would join on them and match nothing — a
    total join failure wearing a complete-looking field.

    `identity_router=None` registers no route at all, which is the point: an
    unconfigured seam must make **zero** requests rather than one that fails.
    """
    monkeypatch.delenv("PLAYER_IDENTITY_URL", raising=False)
    envelopes = await run_capture(Feeds(), lake=SpyLake(), identity_router=None)
    assert starters(envelopes) == {}
    reasons = {error["reason"] for error in envelopes[STRENGTH].errors}
    assert identity_adapter.IDENTITY_UNAVAILABLE in reasons, reasons


def test_no_identity_url_raises_rather_than_returning_a_stub(monkeypatch):
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "")
    with pytest.raises(IdentityUnavailable):
        build_identity_client(httpx.AsyncClient())


def test_the_client_carries_the_bearer_token(monkeypatch):
    """`player-identity` enforces auth in-process on every data route, so a
    tokenless client resolves nothing and reports it as an upstream error."""
    monkeypatch.setenv("PLAYER_IDENTITY_URL", IDENTITY_URL)
    monkeypatch.setenv("COLLECTOR_TOKEN", "abc")
    client = build_identity_client(httpx.AsyncClient())
    assert client._headers()["Authorization"] == "Bearer abc"


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------


async def test_the_collector_does_not_chunk_the_batch_itself():
    """`resolve_many` chunks internally at `BATCH_LIMIT`. A second chunker is
    a second place for the limit to drift from `player-identity`'s own
    `MAX_BATCH_QUERIES`."""
    seen: list[int] = []

    def counting(router):
        def respond(request: httpx.Request) -> httpx.Response:
            import json

            queries = json.loads(request.content)["queries"]
            seen.append(len(queries))
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "resolved": True,
                            "player_id": canonical_for(q["source_id"]),
                        }
                        for q in queries
                    ]
                },
            )

        router.post(f"{IDENTITY_URL}/resolve/batch").mock(side_effect=respond)

    await run_capture(Feeds(), lake=SpyLake(), identity_router=counting)
    assert len(seen) == 1, "the eight-team fixture fits in one chunk"
    # Every slot the snap feed could identify, which is one short of the full
    # forty: `DDD`'s centre has no depth-chart label, so nobody occupies it.
    # A collector that queried the slots it could not identify would send
    # forty and quietly resolve a man it had no evidence played.
    assert seen[0] == len(season_module.TEAMS) * len(STARTER_POSITIONS) - 1


def test_the_failure_summary_is_one_entry_not_one_per_row():
    """A ~1,700-row feed against a dead seam would otherwise fill
    `CoverageAccumulator`'s 50-entry cap by itself and push every other reason
    off the list."""
    failures = identity_adapter.IdentityFailures()
    failures.record({f"q{i}": "identity_upstream_error: boom" for i in range(300)})
    detail = failures.detail()
    assert detail.startswith("300 starter(s) unresolved")
    assert len(detail) < 300
