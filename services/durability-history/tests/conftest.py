"""Fixtures for durability-history's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The rest of this file builds the seven things a capture needs before it will do
anything at all — a published scope in the lake, a reachable `player-identity`,
and five CSV upstreams — because this collector fails closed without the first
two and a test that forgets one gets a `present: 0` envelope rather than an
error.

**The fixture population is chosen so that every branch worth testing has a
representative**, and above all so the named failure mode has one: Charlie
misses four of six games in the scoped season and **none of them is an injury**.
A collector that inferred injury from absence would give him an availability rate
of 0.78 instead of 1.00, and every assertion about him would still look
plausible. That is the whole point of him.
"""

import csv
import io
import json
from datetime import UTC, date, datetime, timedelta

import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from httpx import Response
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from durability_history.adapters import upstream
from durability_history.capture import reset_published_digests
from durability_history.main import app

TEST_TOKEN = "test-collector-token"
IDENTITY_URL = "http://player-identity.test"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

SEASON = 2026
WEEK = 3

# The window the default `DURABILITY_HISTORY_SEASONS` produces for `SEASON`.
HISTORY_SEASONS = [2024, 2025, 2026]

# Weeks in each fixture season. Six rather than eighteen so a whole three-season
# history is small enough to reason about by hand — the reconstruction logic does
# not care how many there are.
FIXTURE_WEEKS = 6
HOME_TEAM = "SEA"
AWAY_TEAM = "BUF"


def gameday(season: int, week: int) -> date:
    """The fixture calendar: week 1 on 7 September, one game a week."""
    return date(season, 9, 7) + timedelta(days=7 * (week - 1))


@pytest.fixture(scope="session", autouse=True)
def _meter_provider():
    """One real MeterProvider for the whole session.

    Instruments are created at import time and record nothing until a provider
    exists; anything recorded before it is silently lost. `set_meter_provider` is
    one-shot per process, so this must happen exactly once.
    """
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()])
    )


@pytest.fixture(autouse=True)
def _collector_token(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", TEST_TOKEN)


@pytest.fixture(autouse=True)
def _identity_url(monkeypatch):
    """Pointed at a mock by default.

    Empty is a *refusal* in this collector (see `adapters/scope.py`), so leaving
    it unset would make every test exercise the fail-closed path while looking
    like it exercised the happy one.
    """
    monkeypatch.setenv("PLAYER_IDENTITY_URL", IDENTITY_URL)


@pytest.fixture(autouse=True)
def _reset_process_state():
    """Forget both process-lifetime caches between tests.

    `_PUBLISHED_DIGESTS` and the upstream memo/ETag store outlive a test, so a
    second test asserting a real publish would otherwise get `UpstreamUnchanged`
    from the first one's leftovers and fail somewhere unrelated to what it was
    checking.
    """
    reset_published_digests()
    upstream.reset_upstream_memo()
    yield
    reset_published_digests()
    upstream.reset_upstream_memo()


@pytest.fixture
def client(_collector_token, lake):
    """The real app, with the `lake` fixture standing in for its own writer.

    Swapped rather than left alone because a dispatched `/refresh` narrows
    against `spec.lake`, and the process's own is a `NullLakeWriter` (no
    `LAKE_BUCKET` in tests) whose empty listing reads as `scope_unavailable`.
    Every route test would otherwise exercise the fail-closed path by accident
    while looking like it tested the route.
    """
    spec = app.state.collector_spec
    original = spec.lake
    spec.lake = lake
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
            yield c
    finally:
        spec.lake = original


@pytest.fixture(autouse=True)
def _reset_collector_singletons():
    """`state` and `refresh_gate` are process-level singletons — that is what lets
    `/signals` serve a cache and `/refresh` enforce a floor across requests — so
    something has to reset them between tests."""
    spec = app.state.collector_spec
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    yield
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None


class SpyLake:
    """A minimal in-memory `LakeWriter`."""

    def __init__(self, *, fail_write: bool = False) -> None:
        self.objects: dict[str, dict] = {}
        self.writes: list[Envelope] = []
        self.fail_write = fail_write

    def write(self, envelope: Envelope) -> str:
        if self.fail_write:
            raise RuntimeError("lake unreachable")
        key = lake_key(envelope)
        self.objects[key] = envelope.to_dict()
        self.writes.append(envelope)
        return key

    def list_keys(
        self,
        collector: str,
        signal_type: str,
        season: int,
        week: int,
        version: str = ENVELOPE_VERSION,
    ) -> list[str]:
        prefix = f"signals/{collector}/v{version}/season={season}/week={week:02d}/"
        suffix = f"-{signal_type}.json"
        return sorted(
            k for k in self.objects if k.startswith(prefix) and k.endswith(suffix)
        )

    def read(self, key: str) -> dict:
        return self.objects[key]


# ── the fixture population ───────────────────────────────────────────────────

ALPHA = "00-0000001"  # QB, never hurt. The "a clean history is data" case.
BRAVO = "00-0000002"  # RB, three resolved events including one recurrence.
CHARLIE = "00-0000003"  # WR, four missed games and NOT ONE of them an injury.
DELTA = "00-0000004"  # TE, no pfr_id: designation-only tenure, unresolved event.
ECHO = "00-0000005"  # K, never hurt.
FOXTROT = "00-0000006"  # WR, resolvable and deliberately OUT OF SCOPE.

# gsis, pfr, name, pos, team, birth, rookie
FIXTURE_PLAYERS = [
    (ALPHA, "AlphQb00", "Alpha Passer", "QB", HOME_TEAM, "1996-03-01", 2018),
    (BRAVO, "BravRb00", "Bravo Runner", "RB", HOME_TEAM, "2001-07-15", 2024),
    (CHARLIE, "CharWr00", "Charlie Catcher", "WR", AWAY_TEAM, "1994-01-20", 2016),
    (DELTA, "", "Delta Blocker", "TE", HOME_TEAM, "1998-09-09", 2020),
    (ECHO, "EchoK000", "Echo Kicker", "K", AWAY_TEAM, "1989-05-05", 2013),
    (FOXTROT, "FoxtWr00", "Foxtrot Wideout", "WR", AWAY_TEAM, "1999-02-02", 2021),
]

# Canonical ids `player-identity` resolves each GSIS id to. Deliberately NOT
# derived from the GSIS id: a test whose ids are a pure function of the upstream
# key cannot tell a real crosswalk hop from a collector minting its own.
CANONICAL_IDS = {
    ALPHA: "fdy-aaaa11112222",
    BRAVO: "fdy-bbbb11112222",
    CHARLIE: "fdy-cccc11112222",
    DELTA: "fdy-dddd11112222",
    ECHO: "fdy-eeee11112222",
    FOXTROT: "fdy-ffff11112222",
}

OUT_OF_SCOPE_GSIS = FOXTROT
SCOPED_GSIS = [g for g in CANONICAL_IDS if g != OUT_OF_SCOPE_GSIS]
SCOPED_IDS = [CANONICAL_IDS[g] for g in SCOPED_GSIS]

# roster-scope mints one of these per club and they are not players.
TEAM_DEFENSE_IDS = ["fdy-dst-sea", "fdy-dst-buf"]

# `(gsis, season) -> the weeks the player took a snap in`. Everything else about
# a player's tenure follows from this and from the designations below.
PLAYED_WEEKS: dict[tuple[str, int], list[int]] = {
    # Alpha and Echo: every game, every season. Never designated.
    **{(ALPHA, s): [1, 2, 3, 4, 5, 6] for s in HISTORY_SEASONS},
    **{(ECHO, s): [1, 2, 3, 4, 5, 6] for s in HISTORY_SEASONS},
    # Bravo: hamstring in 2025 weeks 2 and 5, knee in 2026 week 1.
    (BRAVO, 2024): [1, 2, 3, 4, 5, 6],
    (BRAVO, 2025): [1, 3, 4, 6],
    (BRAVO, 2026): [2, 3, 4, 5, 6],
    # Charlie: four missed games in 2026, none of them an injury.
    (CHARLIE, 2024): [1, 2, 3, 4, 5, 6],
    (CHARLIE, 2025): [1, 2, 3, 4, 5, 6],
    (CHARLIE, 2026): [5, 6],
    # Delta has no pfr_id, so no snap-count row can exist for him at all.
}

# `(gsis, season, week) -> (report_primary_injury, report_status, practice_status)`
FIXTURE_DESIGNATIONS: dict[tuple[str, int, int], tuple[str, str, str]] = {
    (BRAVO, 2025, 2): ("Hamstring", "Out", "Did Not Participate In Practice"),
    (BRAVO, 2025, 5): ("Hamstring", "Out", "Did Not Participate In Practice"),
    (BRAVO, 2026, 1): ("Knee", "Out", "Did Not Participate In Practice"),
    # Charlie: three designations, every one of them explicitly NOT an injury.
    # Week 4 is deliberately absent from this table AND from PLAYED_WEEKS — an
    # absence with nothing to source a reason from.
    (CHARLIE, 2026, 1): (
        "Not injury related - coach's decision",
        "Out",
        "Did Not Participate In Practice",
    ),
    (CHARLIE, 2026, 2): (
        "Not injury related - coach's decision",
        "Out",
        "Did Not Participate In Practice",
    ),
    (CHARLIE, 2026, 3): (
        "Not injury related - personal matter",
        "Out",
        "Did Not Participate In Practice",
    ),
    # Delta: one ankle event he never returns from inside the window.
    (DELTA, 2026, 2): ("Ankle", "Out", "Did Not Participate In Practice"),
}

# `(gsis, season, week) -> offense_pct`. Anything played and not named here gets
# the default, so a trajectory has a flat baseline to move away from.
DEFAULT_SNAP_PCT = 0.80
SNAP_PCT_OVERRIDES: dict[tuple[str, int, int], float] = {
    # Bravo comes back at reduced usage after each return — which is the whole
    # thing `post_return_snap_trajectory` exists to measure.
    (BRAVO, 2025, 3): 0.40,
    (BRAVO, 2025, 4): 0.60,
    (BRAVO, 2025, 6): 0.40,
    (BRAVO, 2026, 2): 0.40,
    (BRAVO, 2026, 3): 0.60,
}

DEFAULT_POINTS = 12.0
POINTS_OVERRIDES: dict[tuple[str, int, int], float] = {
    (BRAVO, 2025, 3): 4.0,
    (BRAVO, 2025, 4): 6.0,
    (BRAVO, 2025, 6): 4.0,
    (BRAVO, 2026, 2): 4.0,
    (BRAVO, 2026, 3): 6.0,
}


def _writer(header: list[str]) -> tuple[io.StringIO, csv.writer]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    return buffer, writer


def games_csv(seasons=None, *, unplayed_from: int | None = None) -> str:
    """The schedule feed. `unplayed_from` leaves later weeks with no `result`.

    A game with no result is not a game anybody could have missed, and the
    adapter drops it — `unplayed_from` is how a test proves that rather than
    assuming it.
    """
    buffer, writer = _writer(
        ["game_id", "season", "game_type", "week", "gameday", "weekday", "gametime",
         "away_team", "home_team", "location", "result"]
    )  # fmt: skip
    for season in seasons if seasons is not None else HISTORY_SEASONS:
        for week in range(1, FIXTURE_WEEKS + 1):
            played = unplayed_from is None or week < unplayed_from
            writer.writerow([
                f"{season}_{week:02d}_{AWAY_TEAM}_{HOME_TEAM}", season, "REG", week,
                gameday(season, week).isoformat(), "Sunday", "13:00",
                AWAY_TEAM, HOME_TEAM, "Home", "3" if played else "",
            ])  # fmt: skip
    # A season outside the window, to prove the window filter runs.
    writer.writerow([
        f"2015_01_{AWAY_TEAM}_{HOME_TEAM}", 2015, "REG", 1, "2015-09-13", "Sunday",
        "13:00", AWAY_TEAM, HOME_TEAM, "Home", "7",
    ])  # fmt: skip
    return buffer.getvalue()


def players_csv(rows=None) -> str:
    buffer, writer = _writer(
        ["gsis_id", "display_name", "pfr_id", "birth_date", "position", "height",
         "weight", "college_name", "jersey_number", "rookie_season", "last_season",
         "latest_team", "years_of_experience", "status"]
    )  # fmt: skip
    for index, record in enumerate(FIXTURE_PLAYERS if rows is None else rows):
        gsis, pfr, name, pos, team, birth, rookie = record
        writer.writerow([
            gsis, name, pfr, birth, pos, 72, 210, "State", 10 + index, rookie,
            SEASON, team, SEASON - rookie, "ACT",
        ])  # fmt: skip
    # A historical player the recency filter must drop.
    writer.writerow([
        "00-0009999", "Ancient Runner", "AncRb000", "1970-01-01", "RB", 70, 200,
        "Old", 99, 1995, 1999, HOME_TEAM, 4, "RET",
    ])  # fmt: skip
    # A defensive lineman: a position this collector does not carry.
    writer.writerow([
        "00-0008888", "Golf Tackle", "GolfDt00", "1997-02-02", "DT", 76, 300,
        "Big", 88, 2019, SEASON, HOME_TEAM, 7, "ACT",
    ])  # fmt: skip
    return buffer.getvalue()


def _team_of(gsis: str) -> str:
    for record in FIXTURE_PLAYERS:
        if record[0] == gsis:
            return record[4]
    return HOME_TEAM


def injuries_csv(season: int, designations=None) -> str:
    buffer, writer = _writer(
        ["season", "game_type", "team", "week", "gsis_id", "position", "full_name",
         "first_name", "last_name", "report_primary_injury",
         "report_secondary_injury", "report_status", "practice_primary_injury",
         "practice_secondary_injury", "practice_status", "date_modified"]
    )  # fmt: skip
    table = FIXTURE_DESIGNATIONS if designations is None else designations
    for (gsis, one_season, week), values in sorted(table.items()):
        if one_season != season:
            continue
        primary, status, practice = values
        # Filed on the Friday before the Sunday game, which is what makes
        # `onset_date` a real upstream date rather than a derived one.
        reported = gameday(season, week) - timedelta(days=2)
        writer.writerow([
            season, "REG", _team_of(gsis), week, gsis, "NA", "n/a", "n", "a",
            primary, "", status, primary, "", practice,
            f"{reported.isoformat()}T19:05:30Z",
        ])  # fmt: skip
    return buffer.getvalue()


def snap_counts_csv(season: int, played=None) -> str:
    buffer, writer = _writer(
        ["game_id", "pfr_game_id", "season", "game_type", "week", "player",
         "pfr_player_id", "position", "team", "opponent", "offense_snaps",
         "offense_pct", "defense_snaps", "defense_pct", "st_snaps", "st_pct"]
    )  # fmt: skip
    table = PLAYED_WEEKS if played is None else played
    for record in FIXTURE_PLAYERS:
        gsis, pfr = record[0], record[1]
        if not pfr:
            continue
        for week in table.get((gsis, season), []):
            pct = SNAP_PCT_OVERRIDES.get((gsis, season, week), DEFAULT_SNAP_PCT)
            writer.writerow([
                f"{season}_{week:02d}_{AWAY_TEAM}_{HOME_TEAM}", "x", season, "REG",
                week, "n/a", pfr, "n/a", _team_of(gsis), "n/a", int(pct * 60), pct,
                0, 0, 0, 0,
            ])  # fmt: skip
    return buffer.getvalue()


def stats_csv(season: int) -> str:
    buffer, writer = _writer(
        ["player_id", "player_display_name", "position", "season", "week",
         "game_id", "team", "opponent_team", "fantasy_points", "fantasy_points_ppr"]
    )  # fmt: skip
    for record in FIXTURE_PLAYERS:
        gsis = record[0]
        for week in PLAYED_WEEKS.get((gsis, season), []):
            points = POINTS_OVERRIDES.get((gsis, season, week), DEFAULT_POINTS)
            writer.writerow([
                gsis, "n/a", record[3], season, week,
                f"{season}_{week:02d}_{AWAY_TEAM}_{HOME_TEAM}", _team_of(gsis),
                "n/a", points, points,
            ])  # fmt: skip
    return buffer.getvalue()


def upstream_urls(seasons=None) -> list[str]:
    """Every URL this collector could possibly fetch, for a `call_count == 0`
    assertion that cannot be satisfied by checking only the one URL a test
    happened to remember."""
    urls = [upstream.GAMES_URL, upstream.PLAYERS_URL]
    for season in seasons if seasons is not None else HISTORY_SEASONS:
        urls.append(upstream.INJURIES_URL.format(season=season))
        urls.append(upstream.SNAP_COUNTS_URL.format(season=season))
        urls.append(upstream.STATS_URL.format(season=season))
    return urls


def mock_upstreams(mock: respx.MockRouter, **overrides) -> None:
    """Route all five upstreams at in-memory documents.

    `overrides` accepts `games=`, `players=`, `injuries={season: text}`,
    `snaps={season: text}`, `stats={season: text}` so one feed can be broken
    without rebuilding the rest.
    """
    mock.get(upstream.GAMES_URL).respond(200, text=overrides.get("games", games_csv()))
    mock.get(upstream.PLAYERS_URL).respond(
        200, text=overrides.get("players", players_csv())
    )
    for season in HISTORY_SEASONS:
        mock.get(upstream.INJURIES_URL.format(season=season)).respond(
            200, text=overrides.get("injuries", {}).get(season, injuries_csv(season))
        )
        mock.get(upstream.SNAP_COUNTS_URL.format(season=season)).respond(
            200, text=overrides.get("snaps", {}).get(season, snap_counts_csv(season))
        )
        mock.get(upstream.STATS_URL.format(season=season)).respond(
            200, text=overrides.get("stats", {}).get(season, stats_csv(season))
        )


def mock_identity(
    mock: respx.MockRouter,
    *,
    resolvable: dict[str, str] | None = None,
    status: int = 200,
) -> None:
    """Route `POST /resolve/batch`, honouring the `resolved` flag.

    `resolvable` maps GSIS id -> canonical id. Anything absent comes back
    `resolved: false` **with candidates**, which is the shape `player-identity`
    uses when it has deliberately refused — a client that adopts one of those is
    the bug `IdentityClient` exists to prevent.
    """
    table = CANONICAL_IDS if resolvable is None else resolvable

    def handler(request):
        if status != 200:
            return Response(status)
        queries = json.loads(request.content)["queries"]
        results = []
        for query in queries:
            player_id = table.get(query.get("source_id"))
            if player_id:
                results.append({"resolved": True, "player_id": player_id,
                                "confidence": 1.0, "candidates": []})  # fmt: skip
            else:
                results.append({
                    "resolved": False, "player_id": None, "confidence": 0.42,
                    # Populated precisely because it refused. A caller that
                    # re-ranks these adopts an identity it was declined.
                    "candidates": [{"player_id": "fdy-decoy00000", "score": 0.42}],
                })  # fmt: skip
        return Response(200, json={"results": results})

    mock.post(f"{IDENTITY_URL}/resolve/batch").mock(side_effect=handler)


def scope_envelope(
    *,
    season: int = SEASON,
    week: int = WEEK,
    player_ids=None,
    captured_at: datetime | None = None,
    include_team_defenses: bool = True,
) -> Envelope:
    """A `roster-scope` membership envelope, as `ScopeClient` reads it."""
    members = list(SCOPED_IDS if player_ids is None else player_ids)
    if include_team_defenses:
        members = members + TEAM_DEFENSE_IDS
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="roster-scope",
        signal_type="scope_membership_weekly",
        captured_at=captured_at or (NOW - timedelta(hours=2)),
        upstream=Upstream(adapter="test", fetched_at=NOW - timedelta(hours=2)),
        scope={"season": season, "week": week},
        coverage=Coverage(expected=len(members), present=len(members)),
        errors=[],
        signals=[
            {
                "player_id": member,
                "membership_status": "active",
                "entity_type": (
                    "team_defense" if member.startswith("fdy-dst-") else "player"
                ),
            }
            for member in members
        ],
    )


@pytest.fixture
def lake() -> SpyLake:
    """A lake with a published scope already in it.

    Without one this collector fails closed, so every test that is not ABOUT
    failing closed needs it. The tests that are about it use `SpyLake()` directly.
    """
    spy = SpyLake()
    spy.write(scope_envelope())
    return spy
