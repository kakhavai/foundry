"""Fixtures for player-profile's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The rest of this file builds the three things a capture needs before it will do
anything at all — a published scope in the lake, a reachable `player-identity`,
and three CSV upstreams — because this collector fails closed without all three
and a test that forgets one gets a `present: 0` envelope rather than an error.
"""

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from httpx import Response
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from player_profile.adapters import upstream
from player_profile.capture import reset_published_digests
from player_profile.main import app

TEST_TOKEN = "test-collector-token"
IDENTITY_URL = "http://player-identity.test"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON = 2026
WEEK = 3


@pytest.fixture(scope="session", autouse=True)
def _meter_provider():
    """One real MeterProvider for the whole session.

    Instruments are created at import time and record nothing until a provider
    exists; anything recorded before it is silently lost. `set_meter_provider`
    is one-shot per process, so this must happen exactly once.
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
    """`state` and `refresh_gate` are process-level singletons — that is what
    lets `/signals` serve a cache and `/refresh` enforce a floor across
    requests — so something has to reset them between tests."""
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
#
# Six players chosen so that every branch worth testing has a representative:
# a drafted veteran, an undrafted one, a rookie, a player with no `pfr_id` (so
# no combine row and no snap total), a kicker (a different age curve), and one
# who is deliberately NOT in the scope so the narrowing has something to drop.

FIXTURE_PLAYERS = [
    # gsis, pfr, name, pos, birth, ht, wt, college, rookie, exp, dyear, drd, dpick
    ("00-0000001", "AlphQb00", "Alpha Passer", "QB", "1996-03-01", 75, 220,
     "State", 2018, 8, 2018, 1, 3),
    ("00-0000002", "BravRb00", "Bravo Runner", "RB", "2001-07-15", 70, 215,
     "Tech", 2023, 3, 2023, 5, 143),
    ("00-0000003", "Char Wr00", "Charlie Catcher", "WR", "1994-01-20", 73, 200,
     "Coastal", 2016, 10, "", "", ""),
    ("00-0000004", "", "Delta Undrafted", "WR", "2003-11-02", 71, 190,
     "Small", 2026, 0, "", "", ""),
    ("00-0000005", "EchoK000", "Echo Kicker", "K", "1989-05-05", 72, 195,
     "Northern", 2013, 13, 2013, 7, 240),
    ("00-0000006", "FoxtTe00", "Foxtrot Blocker", "TE", "1998-09-09", 78, 250,
     "Eastern", 2020, 6, 2020, 3, 88),
]  # fmt: skip

# Canonical ids `player-identity` resolves each GSIS id to. Deliberately not
# derived from the GSIS id: a test whose ids are a pure function of the upstream
# key cannot tell a real crosswalk hop from a collector minting its own.
CANONICAL_IDS = {
    "00-0000001": "fdy-aaaa11112222",
    "00-0000002": "fdy-bbbb11112222",
    "00-0000003": "fdy-cccc11112222",
    "00-0000004": "fdy-dddd11112222",
    "00-0000005": "fdy-eeee11112222",
    "00-0000006": "fdy-ffff11112222",
}

# Foxtrot is resolvable but NOT in the scope. That is the narrowing's job, and a
# fixture where every resolvable player is also in scope cannot prove it happens.
OUT_OF_SCOPE_GSIS = "00-0000006"

SCOPED_GSIS = [g for g in CANONICAL_IDS if g != OUT_OF_SCOPE_GSIS]
SCOPED_IDS = [CANONICAL_IDS[g] for g in SCOPED_GSIS]

# roster-scope mints one of these per club and they are not players.
TEAM_DEFENSE_IDS = ["fdy-dst-sea", "fdy-dst-buf"]

FIXTURE_COMBINE = {
    "AlphQb00": {"forty": "4.85", "bench": "", "vertical": "30.0",
                 "broad_jump": "112", "cone": "7.10", "shuttle": "4.35"},
    "BravRb00": {"forty": "4.45", "bench": "18", "vertical": "38.5",
                 "broad_jump": "126", "cone": "6.95", "shuttle": "4.20"},
    "Char Wr00": {"forty": "4.52", "bench": "", "vertical": "36.0",
                  "broad_jump": "", "cone": "", "shuttle": ""},
    # EchoK000 and FoxtTe00 have no combine row at all — the explicitly-optional
    # case the spec calls out.
}  # fmt: skip

FIXTURE_SNAPS = {
    2026: {"AlphQb00": 300, "BravRb00": 210, "Char Wr00": 400, "EchoK000": 0},
    2025: {"AlphQb00": 1100, "BravRb00": 480, "Char Wr00": 950},
    2024: {"AlphQb00": 1050, "Char Wr00": 900},
}


def players_csv(rows=None) -> str:
    header = [
        "gsis_id", "display_name", "pfr_id", "birth_date", "position", "height",
        "weight", "college_name", "jersey_number", "rookie_season", "last_season",
        "latest_team", "years_of_experience", "draft_year", "draft_round",
        "draft_pick", "status",
    ]  # fmt: skip
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for index, record in enumerate(FIXTURE_PLAYERS if rows is None else rows):
        (gsis, pfr, name, pos, birth, ht, wt, college, rookie, exp,
         dyear, drd, dpick) = record  # fmt: skip
        writer.writerow([
            gsis, name, pfr, birth, pos, ht, wt, college, 10 + index, rookie,
            SEASON, "SEA", exp, dyear, drd, dpick, "ACT",
        ])  # fmt: skip
    # A historical player who must be filtered out by `last_season`. Present so
    # that "the recency filter runs" is provable rather than assumed.
    writer.writerow([
        "00-0009999", "Ancient Runner", "AncRb000", "1970-01-01", "RB", 70, 200,
        "Old", 99, 1995, 1999, "SEA", 4, 1995, 2, 40, "RET",
    ])  # fmt: skip
    # A defensive lineman: a position this collector does not carry.
    writer.writerow([
        "00-0008888", "Golf Tackle", "GolfDt00", "1997-02-02", "DT", 76, 300,
        "Big", 88, 2019, SEASON, "SEA", 7, 2019, 4, 120,
    ])  # fmt: skip
    return buffer.getvalue()


def combine_csv(table=None) -> str:
    header = ["pfr_id", "player_name", "pos", "forty", "bench", "vertical",
              "broad_jump", "cone", "shuttle"]  # fmt: skip
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for pfr_id, values in (FIXTURE_COMBINE if table is None else table).items():
        writer.writerow([
            pfr_id, "n/a", "n/a", values["forty"], values["bench"],
            values["vertical"], values["broad_jump"], values["cone"],
            values["shuttle"],
        ])  # fmt: skip
    return buffer.getvalue()


def snap_counts_csv(season: int) -> str:
    header = ["game_id", "season", "game_type", "week", "player",
              "pfr_player_id", "position", "team", "offense_snaps"]  # fmt: skip
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for pfr_id, snaps in FIXTURE_SNAPS.get(season, {}).items():
        # Two half-rows, so summing rather than last-write-wins is provable.
        for half in (snaps // 2, snaps - snaps // 2):
            writer.writerow([
                f"{season}_01_SEA_BUF", season, "REG", 1, "n/a", pfr_id,
                "n/a", "SEA", half,
            ])  # fmt: skip
    return buffer.getvalue()


def mock_upstreams(
    mock: respx.MockRouter,
    *,
    players: str | None = None,
    combine: str | None = None,
    first_season: int | None = None,
) -> None:
    """Route all three upstreams at in-memory documents."""
    mock.get(upstream.PLAYERS_URL).respond(
        200, text=players if players is not None else players_csv()
    )
    mock.get(upstream.COMBINE_URL).respond(
        200, text=combine if combine is not None else combine_csv()
    )
    start = upstream.CAREER_SNAP_FIRST_SEASON if first_season is None else first_season
    for season in range(start, SEASON + 1):
        mock.get(upstream.SNAP_COUNTS_URL.format(season=season)).respond(
            200, text=snap_counts_csv(season)
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
        import json

        queries = json.loads(request.content)["queries"]
        results = []
        for query in queries:
            player_id = table.get(query.get("source_id"))
            if player_id:
                results.append({"resolved": True, "player_id": player_id,
                                "confidence": 1.0, "candidates": []})  # fmt: skip
            else:
                results.append(
                    {
                        "resolved": False,
                        "player_id": None,
                        "confidence": 0.42,
                        # Populated precisely because it refused. A caller that
                        # re-ranks these adopts an identity it was declined.
                        "candidates": [{"player_id": "fdy-decoy00000", "score": 0.42}],
                    }
                )
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
    failing closed needs it. The tests that are about it use `SpyLake()`
    directly.
    """
    spy = SpyLake()
    spy.write(scope_envelope())
    return spy
