"""Identity as a gate: resolved before attributed, and never defaulted.

`player-identity` is authoritative. `resolved` is the answer and `candidates`
is its working, so an opportunity whose player it declined to name contributes
to nothing -- not under the feed's own GSIS key, and not under a candidate.

Three failure modes, all silent:

* `resolved.get(query, raw_id)` -- positionally safe, adopts every refusal.
* Resolving AFTER attribution -- identical numbers on a healthy day.
* Reading only the returned dict -- a total `player-identity` outage then
  reports itself as an ordinary quiet week.
"""

import httpx
import pytest
import respx
from collector_core.identity import BATCH_LIMIT, IdentityClient

from defense_vs_position.adapters.identity import (
    SENDABLE_POSITIONS,
    IdentityFailures,
    build_query,
    resolve_players,
)
from defense_vs_position.adapters.players import PlayerRef

from . import season
from .conftest import IDENTITY_URL, SpyLake, run_capture

SIGNAL_TYPE = "defense_positional_allowance"


def rows_of(envelope, team: str, position: str, scoring_format: str = "ppr") -> dict:
    return next(
        row
        for row in envelope.signals
        if row["team_id"] == team
        and row["position"] == position
        and row["scoring_format"] == scoring_format
    )


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


async def test_a_refused_player_contributes_nothing(upstreams):
    """`BAL`'s receiver is refused, so exactly the defenses that faced `BAL`
    lose his opportunities -- and nobody else's row moves.

    Each offense's WR is targeted four times a game here and each defense
    plays two games against two different offenses, so an untouched defense
    shows eight WR opportunities over two sampled games. A defense that faced
    BAL loses that whole game from the split -- four opportunities over one
    game -- because there was no other receiver in it.

    Note the per-GAME rate is unchanged at 4.0 and only `games_sampled` moves.
    That is deliberate and it is why `players_resolved_ratio` exists: a
    dropped player does not visibly deflate a rate, so nothing in the row
    itself would give the outage away.
    """
    upstreams.refuse = {season.gsis_id("BAL", "WR")}
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]

    faced = {team for team, _week in season.opponents_of("BAL")}
    assert faced, "the fixture must give BAL some opponents to lose"
    for defense in faced:
        row = rows_of(envelope, defense, "WR")
        assert (row["games_sampled"], row["opportunities_defended"]) == (1, 4), (
            f"{defense} still counts the refused player's game"
        )
    for defense in season.TEAMS:
        if defense in faced or defense == "BAL":
            continue
        row = rows_of(envelope, defense, "WR")
        assert (row["games_sampled"], row["opportunities_defended"]) == (2, 8), (
            f"{defense} lost a game it should not have -- the gate is global"
        )


async def test_refusing_every_receiver_empties_the_split(upstreams):
    """The saturated arm. A gate that quietly kept refused players would leave
    these rows populated; one that dropped the wrong dimension would empty a
    different position."""
    upstreams.refuse = {season.gsis_id(team, "WR") for team in season.TEAMS}
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    for team in season.TEAMS:
        assert rows_of(envelope, team, "WR")["games_sampled"] == 0
        assert rows_of(envelope, team, "TE")["games_sampled"] == 2


async def test_a_raw_upstream_id_is_never_adopted_for_a_refused_player(upstreams):
    """The `.get(query, raw_id)` mutant, isolated.

    The mock returns `candidates: [{player_id: fdy-<gsis>}]` alongside
    `resolved: false`, so a defaulting `.get` -- or a client that re-ranked
    candidates -- would produce exactly the id the refusal withheld and every
    number would look right.
    """
    upstreams.refuse = {season.gsis_id("BAL", "WR")}
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    priority = [e for e in envelope.errors if e["reason"] == "identity_unresolved"]
    assert priority, "a dropped player must be reported, not silently absorbed"
    assert priority[0]["detail"].startswith("1 of 160 player(s)")


async def test_resolution_precedes_attribution(upstreams):
    """Refusing every QB must empty the QB split rather than shrink it.

    If attribution ran first and the gate second, a per-position fold would
    already have absorbed the opportunities and the split would still carry
    them.
    """
    upstreams.refuse = {season.gsis_id(team, "QB") for team in season.TEAMS}
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    for team in season.TEAMS:
        row = rows_of(envelope, team, "QB")
        assert row["games_sampled"] == 0
        assert row["opportunities_defended"] == 0
    # And the positions that were NOT refused are untouched.
    assert rows_of(envelope, "BAL", "WR")["games_sampled"] == 2


async def test_a_player_identity_outage_is_distinguishable_from_a_quiet_week(
    upstreams,
):
    """A failed REQUEST is not a refusal.

    `resolve_many` catches per chunk and records the reason on `.failures`, so
    the queries are absent from the returned dict exactly like a genuine
    `resolved: false`. Reading only the dict reports a total outage as an
    ordinary short week whose errors say nothing but `below_expected_floor`.
    """
    upstreams.identity_status = 503
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]

    reasons = [e["reason"] for e in envelope.errors]
    assert "identity_upstream_error" in reasons, reasons
    detail = next(
        e["detail"] for e in envelope.errors if e["reason"] == "identity_upstream_error"
    )
    assert "503" in detail
    # Summarised: one entry, not one per player.
    assert reasons.count("identity_upstream_error") == 1
    # And the pass still publishes, with the hole visible.
    assert envelope.coverage.present == 0


async def test_the_outage_error_survives_the_fifty_entry_cap(upstreams):
    """`add_priority_error`, not `add_error`.

    A skewed league files ~90 `rank_divergence` entries. Appended, the
    identity entry lands past `CoverageAccumulator`'s 50-entry cap and the one
    error that explains the pass is the one that gets deleted.
    """
    upstreams.identity_status = 503
    upstreams.set_pbp(season.pbp_document(drives=season.volume_skewed("BAL")))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]

    reasons = [e["reason"] for e in envelope.errors]
    assert "identity_upstream_error" in reasons
    assert "identity_unresolved" in reasons


async def test_the_resolved_ratio_reaches_the_metric(upstreams, monkeypatch):
    """Coverage cannot see this hole: a dropped opportunity deflates a rate
    without removing a row, so 32 complete-looking defenses can all be
    wrong."""
    recorded: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "defense_vs_position.capture.metrics.players_resolved",
        lambda resolved, seen: recorded.append((resolved, seen)),
    )
    upstreams.refuse = {season.gsis_id("BAL", "WR")}
    await run_capture(SpyLake())
    assert recorded == [(159, 160)]


# --------------------------------------------------------------------------
# The query shape
# --------------------------------------------------------------------------


def test_the_query_carries_the_tier_one_crosswalk_key():
    """`source`/`source_id` are what earn adoption with no scoring at all.
    Without them a GSIS-keyed feed falls through to attribute matching, which
    is how two Josh Allens become one player."""
    query = build_query(
        PlayerRef("00-0011000", "WR", "WR", "A Receiver", "PHI", 18), 2026
    )
    assert query.source == "gsis"
    assert query.source_id == "00-0011000"
    assert query.name is None, "a name would re-open attribute matching"
    assert (query.team, query.position, query.jersey_number, query.season) == (
        "PHI",
        "WR",
        18,
        2026,
    )


def test_an_unknown_position_travels_as_none_rather_than_killing_the_batch():
    """`player-identity`'s `build_query` raises 422 for a position outside
    `KNOWN_POSITIONS`, INSIDE the loop over the batch -- so one bad code fails
    all 500 queries, not one.

    Live and reproducible: nflverse `players.csv` publishes `SAF` (345
    players), which `KNOWN_POSITIONS` does not carry. This collector narrows
    to six codes before resolving so it does not hit it today, but that is a
    consequence of another decision rather than a guarantee.
    """
    assert "SAF" not in SENDABLE_POSITIONS
    query = build_query(
        PlayerRef("00-0011000", "SAF", "WR", "A Safety", "PHI", 18), 2026
    )
    assert query.position is None
    assert query.source_id == "00-0011000", "the crosswalk key must still travel"


def test_every_sendable_position_is_one_this_collector_can_produce():
    """The set must not drift wider than `ROSTER_TO_FANTASY`: a code in here
    that the fantasy map does not carry is a code that could be sent and 422
    a batch."""
    from defense_vs_position.scoring import ROSTER_TO_FANTASY

    assert SENDABLE_POSITIONS == frozenset(ROSTER_TO_FANTASY)


async def test_resolve_many_chunks_at_the_batch_limit_without_caller_help():
    """No caller-side batching: a second chunker is a second place for the
    limit to drift from `player-identity`'s own `MAX_BATCH_QUERIES`."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        queries = json.loads(request.read())["queries"]
        seen.append(len(queries))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"resolved": True, "player_id": f"fdy-{q['source_id']}"}
                    for q in queries
                ],
                "count": len(queries),
                "resolved_count": len(queries),
                "unresolved_count": 0,
            },
        )

    count = BATCH_LIMIT + 7
    positions = {
        f"00-{index:07d}": PlayerRef(
            f"00-{index:07d}", "WR", "WR", f"P{index}", "PHI", 1
        )
        for index in range(count)
    }
    with respx.mock:
        respx.post(f"{IDENTITY_URL}/resolve/batch").mock(side_effect=handler)
        async with httpx.AsyncClient() as http:
            resolved = await resolve_players(
                set(positions),
                season=2026,
                positions=positions,
                identity=IdentityClient(IDENTITY_URL, http),
                failures=IdentityFailures(),
            )
    assert len(resolved) == count
    assert seen == [BATCH_LIMIT, 7]
    assert max(seen) <= BATCH_LIMIT


def test_identity_failures_summarises_rather_than_listing():
    """A ~650-row feed against a dead seam would file 650 entries and push
    every other reason past the 50-entry cap."""
    failures = IdentityFailures()
    failures.record({f"q{i}": "identity_upstream_error: boom" for i in range(650)})
    failures.record({"q651": "identity_upstream_error: boom"})
    assert failures.rows == 651
    assert failures.reasons == ["identity_upstream_error: boom"]
    assert (
        failures.detail() == "651 player(s) unresolved: identity_upstream_error: boom"
    )


def test_a_failure_detail_is_bounded():
    """One entry lands in an append-only lake object, so it is bounded."""
    failures = IdentityFailures()
    failures.record({"q": "x" * 5000})
    assert len(failures.detail()) < 300


async def test_a_missing_url_raises_rather_than_stubbing(monkeypatch):
    """Every stub in this fleet mints an `fdy-` id from a hash of the upstream
    key. Here that would gate attribution on an identity `player-identity`
    never issued -- a total join failure that publishes complete-looking
    ratings."""
    from defense_vs_position.adapters.identity import (
        IdentityUnavailable,
        build_identity_client,
    )

    monkeypatch.setenv("PLAYER_IDENTITY_URL", "   ")
    async with httpx.AsyncClient() as http:
        with pytest.raises(IdentityUnavailable) as caught:
            build_identity_client(http)
    assert caught.value.reason == "identity_unavailable"
