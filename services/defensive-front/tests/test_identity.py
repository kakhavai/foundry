"""`key_absences`: the one field that reaches `player-identity`.

Three failures are pinned here and each of them is silent — the code runs, the
types line up, the row validates, and the field is populated with something
wrong or empty with no explanation.
"""

import httpx
import pytest
from collector_core.identity import BATCH_LIMIT

from defensive_front.adapters.identity import (
    SENDABLE_POSITIONS,
    IdentityFailures,
    build_query,
)
from defensive_front.adapters.injuries import Absence
from defensive_front.adapters.players import PlayerRef
from defensive_front.capture import (
    REASON_IDENTITY_UNRESOLVED,
    REASON_IDENTITY_UPSTREAM_ERROR,
    STRENGTH,
)

from . import season as season_module
from .conftest import Feeds, SpyLake, by_team, run_capture

IDENTITY_URL = "http://player-identity.test"


class Identity:
    """A `player-identity` stand-in that records what it was actually sent."""

    def __init__(self, *, status: int = 200, resolve: bool = True) -> None:
        self.status = status
        self.resolve = resolve
        self.batches: list[list[dict]] = []

    def __call__(self, router) -> None:
        router.post(f"{IDENTITY_URL}/resolve/batch").mock(side_effect=self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        import json

        queries = json.loads(request.content)["queries"]
        self.batches.append(queries)
        if self.status != 200:
            return httpx.Response(self.status)
        return httpx.Response(
            200,
            json={
                "results": [
                    (
                        {"resolved": True, "player_id": f"fdy-{query['source_id']}"}
                        if self.resolve
                        else {"resolved": False, "candidates": [{"player_id": "fdy-x"}]}
                    )
                    for query in queries
                ]
            },
        )

    @property
    def queries(self) -> list[dict]:
        return [query for batch in self.batches for query in batch]


@pytest.fixture
def identity_url(monkeypatch):
    monkeypatch.setenv("PLAYER_IDENTITY_URL", IDENTITY_URL)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_absent_front_starters_are_published_as_canonical_ids(identity_url):
    identity = Identity()
    rows = by_team(await run_capture(Feeds(), lake=SpyLake(), identity_router=identity))
    assert rows["AAA"]["key_absences"] == sorted(
        {
            f"fdy-{season_module.front_id('AAA', 0)}",
            f"fdy-{season_module.front_id('AAA', 1)}",
        }
    )
    assert rows["BBB"]["key_absences"] == [f"fdy-{season_module.front_id('BBB', 0)}"]
    assert rows["DDD"]["key_absences"] == []


async def test_only_out_and_doubtful_reach_the_field(identity_url):
    """`Questionable` is in the fixture on purpose. A collector that counted
    it would publish a third id for AAA."""
    identity = Identity()
    rows = by_team(await run_capture(Feeds(), lake=SpyLake(), identity_router=identity))
    questionable = f"fdy-{season_module.front_id('AAA', 2)}"
    assert questionable not in rows["AAA"]["key_absences"]


async def test_a_hurt_defensive_back_is_not_a_front_absence(identity_url):
    """The fixture lists a cornerback out for AAA. `key_absences` is a FRONT
    field; a collector that skipped the narrowing publishes him."""
    identity = Identity()
    rows = by_team(await run_capture(Feeds(), lake=SpyLake(), identity_router=identity))
    back = f"fdy-{season_module.secondary_id('AAA', 1)}"
    assert back not in rows["AAA"]["key_absences"]
    assert all(
        "S0" not in player for row in rows.values() for player in row["key_absences"]
    )


# --------------------------------------------------------------------------
# The unmapped-position hazard
# --------------------------------------------------------------------------


def test_an_unknown_position_travels_as_none_rather_than_a_422():
    """**One bad code fails all 500 queries, not one row of it.**

    `player-identity`'s `build_query` raises `HTTPException(422)` for a
    position outside `KNOWN_POSITIONS`, and `resolve_queries` calls it INSIDE
    the loop over the batch. `IdentityClient` then records the 422 as an
    upstream error for the whole chunk and moves on.

    nflverse publishes `SAF` for 345 players and `KNOWN_POSITIONS` carries
    `S`, `FS` and `SS` but not `SAF`. Audited live for this build: `SAF` is
    the ONLY unmapped code in either `players.csv` (25 codes) or
    `injuries_2025.csv` (16 codes).

    This collector's front filter already excludes safeties — but that is a
    consequence of another decision and would survive exactly until somebody
    widened it. `SENDABLE_POSITIONS` makes it an assertion.
    """
    absence = Absence(gsis_id="00-0000001", team="AAA", position="DT", status="Out")
    unknown = PlayerRef(
        gsis_id="00-0000001", position="SAF", team="ZZZ", jersey_number=21
    )
    assert build_query(absence, unknown, 2026).position is None

    known = PlayerRef(gsis_id="00-0000001", position="NT", team="ZZZ", jersey_number=95)
    assert build_query(absence, known, 2026).position == "NT"


def test_every_sendable_position_is_a_front_code():
    """The mirror has to stay a subset of what the front filter can produce,
    or it stops being an assertion about anything."""
    assert SENDABLE_POSITIONS <= set(season_module.FRONT_POSITIONS) | {
        "DE",
        "DL",
        "DT",
        "ILB",
        "LB",
        "MLB",
        "NT",
        "OLB",
    }
    assert "SAF" not in SENDABLE_POSITIONS
    assert "CB" not in SENDABLE_POSITIONS


def test_the_query_carries_the_injury_report_team_not_the_career_latest_one():
    """`players.csv`'s `latest_team` is a career-latest field and names the
    wrong club for anyone traded mid-season. `team` carries 0.20 of the
    server-side weighting, so a stale one actively pushes resolution away."""
    absence = Absence(gsis_id="00-0000001", team="AAA", position="DT", status="Out")
    reference = PlayerRef(
        gsis_id="00-0000001", position="DT", team="ZZZ", jersey_number=95
    )
    query = build_query(absence, reference, 2026)
    assert query.team == "AAA"
    assert query.jersey_number == 95


def test_no_name_is_sent():
    """A GSIS id absent from the crosswalk would otherwise fall through to
    attribute scoring, and a feed that already carries a league id and is
    matched by name anyway is how two players with one name become one."""
    absence = Absence(gsis_id="00-0000001", team="AAA", position="DT", status="Out")
    reference = PlayerRef(
        gsis_id="00-0000001", position="DT", team="AAA", jersey_number=95
    )
    query = build_query(absence, reference, 2026)
    assert query.name is None
    assert query.source == "gsis"
    assert query.source_id == "00-0000001"


# --------------------------------------------------------------------------
# Refusals and outages, which are different facts
# --------------------------------------------------------------------------


async def test_a_raw_upstream_id_is_never_adopted_for_a_refused_player(identity_url):
    """**`resolved.get(query)`, never `resolved.get(query, raw_id)`.**

    The default is positionally safe — the code runs, the types line up, every
    test stays green — and it publishes the upstream's own GSIS key for
    exactly the players the authoritative service refused to name. A generator
    joining `key_absences` on those ids matches nothing, silently.
    """
    identity = Identity(resolve=False)
    envelopes = await run_capture(Feeds(), lake=SpyLake(), identity_router=identity)
    rows = by_team(envelopes)
    assert all(row["key_absences"] == [] for row in rows.values())
    assert not any(
        player.startswith("00-")
        for row in rows.values()
        for player in row["key_absences"]
    )
    reasons = {error["reason"] for error in envelopes[STRENGTH].errors}
    assert REASON_IDENTITY_UNRESOLVED in reasons, reasons


async def test_an_identity_outage_is_distinguishable_from_a_healthy_week(identity_url):
    """A chunk `player-identity` could not be reached for is absent from the
    returned dict in exactly the same way a genuine refusal is. Read only the
    dict and a total outage reports itself as a week in which nobody was hurt."""
    identity = Identity(status=503)
    envelopes = await run_capture(Feeds(), lake=SpyLake(), identity_router=identity)
    reasons = {error["reason"] for error in envelopes[STRENGTH].errors}
    assert REASON_IDENTITY_UPSTREAM_ERROR in reasons, reasons
    assert all(row["key_absences"] == [] for row in by_team(envelopes).values())


async def test_an_unconfigured_identity_is_its_own_reason():
    """ "This pod was never pointed at `player-identity`" is a configuration
    fact, distinct from a request that failed and distinct again from a
    refusal. Flattening the three costs an operator the only thing the
    envelope could have told them."""
    envelopes = await run_capture(Feeds(), lake=SpyLake())
    reasons = {error["reason"] for error in envelopes[STRENGTH].errors}
    assert "identity_unavailable" in reasons, reasons


async def test_a_healthy_pass_files_no_identity_error(identity_url):
    """The negative arm for all three reasons above."""
    envelopes = await run_capture(Feeds(), lake=SpyLake(), identity_router=Identity())
    reasons = {error["reason"] for error in envelopes[STRENGTH].errors}
    assert reasons == {"below_expected_floor"}, reasons


async def test_identity_failures_are_summarised_not_filed_per_row(identity_url):
    """One dead seam would otherwise fill `CoverageAccumulator`'s 50-entry cap
    by itself and push every other reason off the list."""
    identity = Identity(status=503)
    envelopes = await run_capture(Feeds(), lake=SpyLake(), identity_router=identity)
    matching = [
        error
        for error in envelopes[STRENGTH].errors
        if error["reason"] == REASON_IDENTITY_UPSTREAM_ERROR
    ]
    assert len(matching) == 1, matching
    assert "player(s) unresolved" in matching[0]["detail"]


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------


async def test_there_is_no_caller_side_batching(identity_url):
    """`resolve_many` chunks internally at `BATCH_LIMIT`. A second chunker is
    a second place for the limit to drift from `player-identity`'s own
    `MAX_BATCH_QUERIES`."""
    identity = Identity()
    await run_capture(Feeds(), lake=SpyLake(), identity_router=identity)
    assert len(identity.batches) == 1
    assert len(identity.queries) <= BATCH_LIMIT


def test_the_failure_roll_up_deduplicates_reasons():
    failures = IdentityFailures()
    failures.record({"a": "boom", "b": "boom", "c": "other"})
    assert failures.rows == 3
    assert failures.reasons == ["boom", "other"]
    assert "3 absent player(s) unresolved" in failures.detail()
