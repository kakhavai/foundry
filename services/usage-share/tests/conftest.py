"""Fixtures for usage-share's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The upstream is a **CSV document built here and served through respx**, not a
hand-written list of already-parsed rows. That is deliberate: the adapter's
whole job is the wire format, and a fixture that skips the wire proves nothing
about column names, blank cells, or the streaming path — which is where this
collector's memory rules live.

**Every capture now narrows**, so the two seams narrowing needs are part of the
baseline fixture rather than something each test assembles: `upstream` and
`serve_upstream` mock `player-identity` on the same respx router and seed the
`lake` fixture with a `roster-scope` membership envelope naming every player in
the document they serve. A test that wants a *narrower* scope, an unresolvable
row, or no scope at all overrides one of those — see `tests/test_narrowing.py`.
"""

import csv
import io
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from usage_share.adapters.scope import SCOPE_SIGNAL_TYPE, UPSTREAM_SOURCE
from usage_share.adapters.upstream import UPSTREAM_URL
from usage_share.main import app

TEST_TOKEN = "test-collector-token"

# Where the fake `player-identity` answers. Set into PLAYER_IDENTITY_URL by an
# autouse fixture, because narrowing fails closed without it.
IDENTITY_URL = "http://player-identity:8002"
RESOLVE_BATCH_URL = f"{IDENTITY_URL}/resolve/batch"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON, WEEK = 2026, 1
UPSTREAM_FOR_SEASON = UPSTREAM_URL.format(season=SEASON, week=WEEK)

# The columns the adapter declares it needs, plus a couple it ignores — a real
# document carries ~90 more, and a fixture with exactly the required set would
# never catch a mapping that quietly depends on column order.
CSV_COLUMNS = (
    "player_id",
    "player_display_name",
    "position",
    "season",
    "week",
    "game_id",
    "team",
    "opponent_team",
    "completions",
    "attempts",
    "sacks_suffered",
    "carries",
    "targets",
    "receiving_air_yards",
    "target_share",
)

KC_GAME = "2026_01_BUF_KC"


def _record(**overrides) -> dict:
    """One CSV record, defaulted to a blank line the adapter must tolerate.

    Blank rather than zero on purpose: the real feed leaves a stat a player
    never recorded empty, and `int("")` is the crash that finds out.
    """
    record = dict.fromkeys(CSV_COLUMNS, "")
    record.update(
        season=str(SEASON),
        week=str(WEEK),
        game_id=KC_GAME,
    )
    record.update({key: str(value) for key, value in overrides.items()})
    return record


def to_csv(records: list[dict]) -> str:
    header = ",".join(CSV_COLUMNS)
    lines = [
        ",".join(record.get(column, "") for column in CSV_COLUMNS) for record in records
    ]
    return "\n".join([header, *lines]) + "\n"


# Two teams, arranged so each one's `target_share` column sums to exactly 1.0 —
# a healthy feed — and so every branch of the mapping is exercised: a passer
# with no targets, a receiver with negative air yards, and a defender the
# position filter must drop without counting them missing.
SAMPLE_RECORDS = [
    _record(
        player_id="00-KC-QB1",
        position="QB",
        team="KC",
        attempts=30,
        sacks_suffered=2,
        carries=3,
        targets=0,
        target_share=0,
    ),
    _record(
        player_id="00-KC-WR1",
        position="WR",
        team="KC",
        targets=10,
        receiving_air_yards=120,
        carries=1,
        target_share=0.4,
    ),
    _record(
        player_id="00-KC-WR2",
        position="WR",
        team="KC",
        targets=6,
        receiving_air_yards=60,
        target_share=0.24,
    ),
    _record(
        player_id="00-KC-TE1",
        position="TE",
        team="KC",
        targets=7,
        receiving_air_yards=40,
        target_share=0.28,
    ),
    _record(
        player_id="00-KC-RB1",
        position="FB",
        team="KC",
        targets=2,
        receiving_air_yards=-5,
        carries=18,
        target_share=0.08,
    ),
    # Dropped by the position filter, but its (zero) counts still belong to
    # KC's denominators — see the adapter's docstring.
    _record(player_id="00-KC-DE1", position="DE", team="KC", target_share=0),
    _record(
        player_id="00-BUF-QB1",
        position="QB",
        team="BUF",
        attempts=25,
        sacks_suffered=3,
        targets=0,
        target_share=0,
    ),
    _record(
        player_id="00-BUF-WR1",
        position="WR",
        team="BUF",
        targets=8,
        receiving_air_yards=90,
        target_share=0.5,
    ),
    _record(
        player_id="00-BUF-RB1",
        position="RB",
        team="BUF",
        targets=8,
        receiving_air_yards=10,
        carries=20,
        target_share=0.5,
    ),
    # A different week of the same season. The adapter must discard it as it
    # parses — including from the denominators, or KC's bases would carry two
    # games' worth of targets and every share would be halved.
    _record(
        player_id="00-KC-WR1",
        position="WR",
        team="KC",
        week=2,
        game_id="2026_02_KC_LAC",
        targets=99,
        target_share=1.0,
    ),
]

# Retained rows and teams in SAMPLE_RECORDS: 5 KC skill players + 3 BUF, over
# two teams. Named rather than recomputed in each test so a fixture edit that
# changes them fails loudly in one place.
SAMPLE_PLAYER_ROWS = 8
SAMPLE_TEAMS = 2


def full_league_csv(*, teams: int = 32, players_per_team: int = 11) -> str:
    """A document large enough to reach `EXPECTED_FLOOR` exactly.

    The point is not size. A floor nothing can ever reach is as wrong as no
    floor at all, and only a document carrying the whole declared universe can
    show that 32 x (11 + 1) is reachable rather than aspirational.
    """
    records = []
    for team_index in range(teams):
        team = f"T{team_index:02d}"
        game = f"2026_01_{team}_OPP"
        for slot in range(players_per_team):
            records.append(
                _record(
                    player_id=f"00-{team}-{slot:02d}",
                    position="WR",
                    team=team,
                    game_id=game,
                    targets=1,
                    receiving_air_yards=10,
                    carries=1,
                    attempts=1,
                    target_share=round(1.0 / players_per_team, 6),
                )
            )
    return to_csv(records)


# ── narrowing fixtures ────────────────────────────────────────────────────────
#
# The canonical id the fake `player-identity` issues for a GSIS id. `fdy-` +
# the upstream key, so a test can name either side of the join by hand and the
# relationship between them is legible in the assertion rather than opaque.
# The real service mints ids that look nothing like this; nothing here depends
# on the shape, only on the mapping being total and stable.
def canonical_id(upstream_player_id: str) -> str:
    return f"fdy-{upstream_player_id}"


# A scope member no fixture document carries a row for. Present in every seeded
# scope because `ScopeClient` treats a membership envelope with zero signals as
# `scope_empty` and falls back a week — so a document with no players (the 503
# and empty-document tests) would otherwise fail closed for a reason those
# tests are not about. It is also realistic: roster-scope's universe is 416
# slots and no week's feed carries all of them.
UNPLAYED_SCOPE_MEMBER = canonical_id("00-SCOPE-ONLY")


def upstream_player_ids(body: str) -> list[str]:
    """Every `player_id` in a served CSV body, in document order."""
    reader = csv.DictReader(io.StringIO(body))
    return [
        (record.get("player_id") or "").strip()
        for record in reader
        if (record.get("player_id") or "").strip()
    ]


def scope_for(body: str) -> set[str]:
    """A scope naming every player the document carries — the "narrowing is
    switched on but excludes nobody" baseline the pre-existing suite assumes."""
    return {canonical_id(player_id) for player_id in upstream_player_ids(body)}


def scope_envelope(
    members, *, season: int = SEASON, week: int = WEEK, captured_at=NOW
) -> Envelope:
    """One `roster-scope` membership envelope, as `ScopeClient` reads it.

    A real `Envelope` through the real `lake_key`, not a hand-placed dict: a
    fixture that writes its own key is a second implementation of the layout
    `ScopeClient` navigates, and would keep passing after that layout changed.
    """
    rows = [
        {"player_id": player_id, "membership_status": "active"}
        for player_id in sorted(members)
    ]
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="roster-scope",
        signal_type=SCOPE_SIGNAL_TYPE,
        captured_at=captured_at,
        upstream=Upstream(adapter="depth-chart", fetched_at=captured_at),
        scope={"season": season, "week": week},
        coverage=Coverage(expected=len(rows), present=len(rows), missing=[]),
        errors=[],
        signals=rows,
    )


def seed_scope(lake, members, *, season: int = SEASON, week: int = WEEK, **kwargs):
    """Write a membership envelope into `lake` so a capture can narrow to it."""
    lake.write(
        scope_envelope(
            {*members, UNPLAYED_SCOPE_MEMBER}, season=season, week=week, **kwargs
        )
    )
    return lake


def mock_identity(router, *, unresolvable: set[str] = frozenset()):
    """Answer `POST /resolve/batch` the way `player-identity` does.

    Every query resolves to `canonical_id(source_id)` except the ids named in
    `unresolvable`, which come back `resolved: false` **with a high-confidence
    candidate attached** — the shape that matters, because a caller that
    re-ranks `candidates` against a local floor would adopt exactly that id.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        queries = json.loads(request.content)["queries"]
        results = []
        for query in queries:
            source_id = query.get("source_id") or ""
            assert query.get("source") == UPSTREAM_SOURCE, query
            if not source_id or source_id in unresolvable:
                results.append(
                    {
                        "resolved": False,
                        "player_id": None,
                        "candidates": [
                            {
                                "player_id": canonical_id(source_id),
                                "confidence": 0.99,
                            }
                        ],
                    }
                )
                continue
            results.append(
                {
                    "resolved": True,
                    "player_id": canonical_id(source_id),
                    "confidence": 1.0,
                    "candidates": [],
                }
            )
        return httpx.Response(200, json={"results": results})

    return router.post(RESOLVE_BATCH_URL).mock(side_effect=handler)


@pytest.fixture(autouse=True)
def _player_identity_url(monkeypatch):
    """Narrowing fails closed without a `player-identity` to resolve against,
    so the suite's baseline is one that exists."""
    monkeypatch.setenv("PLAYER_IDENTITY_URL", IDENTITY_URL)


@pytest.fixture
def upstream(lake):
    """Serve `SAMPLE_RECORDS`, with a scope and an identity that pass it all."""
    with respx.mock(assert_all_called=False) as router:
        body = to_csv(SAMPLE_RECORDS)
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=body)
        )
        mock_identity(router)
        seed_scope(lake, scope_for(body))
        yield router


@pytest.fixture
def serve_upstream(lake):
    """Serve an arbitrary document body at the adapter's real URL.

    A factory rather than a second fixture per body: the coverage tests each
    need a different document, and parameterising the mock is cheaper than
    thirteen near-identical fixtures.

    It also seeds the `lake` fixture with a scope naming every player in the
    body it was handed, so a test about coverage stays a test about coverage
    rather than accidentally becoming one about narrowing.
    """

    def _serve(body: str, *, status: int = 200):
        router = respx.mock(assert_all_called=False)
        router.start()
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(status, text=body)
        )
        mock_identity(router)
        return router

    routers = []

    def factory(body: str, *, status: int = 200):
        router = _serve(body, status=status)
        routers.append(router)
        seed_scope(lake, scope_for(body))
        return router

    yield factory
    for router in routers:
        router.stop()


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


@pytest.fixture
def client(_collector_token, lake):
    """The real app, with the `lake` fixture standing in for its own writer.

    Swapped rather than left alone because a dispatched `/refresh` narrows
    against `spec.lake`, and the process's own is a `NullLakeWriter` (no
    `LAKE_BUCKET` in tests) whose empty listing reads as `scope_unavailable`.
    Every route test would then exercise the fail-closed path by accident.
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


@pytest.fixture
def lake() -> SpyLake:
    return SpyLake()
