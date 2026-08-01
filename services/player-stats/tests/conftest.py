"""Fixtures for player-stats' suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

`feed_row` builds a row carrying every column `adapters/upstream.py` declares
required, so a test that drops one is testing schema drift on purpose rather
than by accident.
"""

import hashlib
from datetime import UTC, datetime

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import EventLoopGuardedLake, lake_key
from collector_core.scope import SCOPE_COLLECTOR
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from player_stats.adapters.scope import SCOPE_SIGNAL_TYPE, TEAM_DEFENSE_PREFIX
from player_stats.adapters.upstream import REQUIRED_COLUMNS
from player_stats.main import app

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON = 2026
WEEK = 1


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
def _stub_identity(monkeypatch):
    """The shipped configuration for the identity crosswalk: no
    `player-identity` deployment, so `build_id_resolver` falls back to the
    deterministic stub. Set explicitly rather than left to the ambient
    environment — a developer with `PLAYER_IDENTITY_URL` exported would
    otherwise see a different collector than CI does.

    The watchlist has no on/off switch to set here any more: it is read from
    the lake unconditionally (`adapters/scope.py`), so every test that drives
    a real capture must seed one — see `seed_scope` below and the `client`
    fixture, which seeds a default for every route test.
    """
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "")


@pytest.fixture
def client(_collector_token, monkeypatch):
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        # A fresh, scope-seeded lake for every test — the route tests drive
        # real captures through `POST /refresh`, and those now fail closed
        # without a usable watchlist in the lake. Anchor-only (see
        # `seed_scope`) reproduces the old unscoped default: an empty player
        # watchlist that is nonetheless successfully fetched. A test that
        # needs its own lake (to inspect writes, or to simulate a read
        # failure) overrides `collector_spec.lake` afterward, same as before.
        #
        # Seeded BEFORE wrapping in `EventLoopGuardedLake`: `seed_scope`
        # writes straight into `SpyLake.objects`, which is a plain
        # synchronous dict write with no running loop involved yet, but the
        # guard's job is to catch a *capture-path* call made directly on the
        # loop thread — `build_collector_app` wraps every collector's real
        # lake in this exact guard (`collector_core/app.py`), and this task
        # added the first capture-path lake read besides the box-score
        # upstream (`ScopeClient` inside `fetch_watchlist`). Leaving the test
        # lake unwrapped would let a future direct `lake.list_keys(...)` call
        # from inside the capture coroutine pass here while still breaking a
        # real deployment. `monkeypatch.setattr` rather than a direct
        # assignment, so the override reverts even though `app` is a
        # session-wide singleton — matching `_reset_collector_singletons`
        # below and the swaps in `test_routes.py`.
        spy = SpyLake()
        seed_scope(spy)
        monkeypatch.setattr(
            c.app.state.collector_spec, "lake", EventLoopGuardedLake(spy)
        )
        yield c


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

    def __init__(self, *, fail_write: bool = False, fail_read: bool = False) -> None:
        self.objects: dict[str, dict] = {}
        self.writes: list[Envelope] = []
        self.fail_write = fail_write
        self.fail_read = fail_read

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
        if self.fail_read:
            raise RuntimeError("lake unreachable")
        prefix = f"signals/{collector}/v{version}/season={season}/week={week:02d}/"
        suffix = f"-{signal_type}.json"
        return sorted(
            k for k in self.objects if k.startswith(prefix) and k.endswith(suffix)
        )

    def read(self, key: str) -> dict:
        if self.fail_read:
            raise RuntimeError("lake unreachable")
        return self.objects[key]


@pytest.fixture
def lake() -> SpyLake:
    return SpyLake()


# A team-defense id, included in every seeded scope. `fetch_watchlist` drops
# anything under `TEAM_DEFENSE_PREFIX`, so a scope containing ONLY this id
# reproduces the old unscoped default: `ScopeClient` sees a non-empty
# envelope (so it neither falls back a week nor raises `scope_unavailable`/
# `scope_empty`), and `fetch_watchlist` still returns an empty player
# watchlist. Every test that drives a real capture without caring about
# narrowing seeds this by default — see `seed_scope`.
SCOPE_ANCHOR = f"{TEAM_DEFENSE_PREFIX}anchor"


def scope_envelope(members, *, season: int = SEASON, week: int = WEEK, captured_at=NOW):
    """One `roster-scope` membership envelope, as `ScopeClient` reads it.

    A real `Envelope` through the real `lake_key`, not a hand-placed dict: a
    fixture that writes its own key is a second implementation of the layout
    `ScopeClient` navigates, and would keep passing after that layout changed.
    """
    rows = [
        {
            "player_id": player_id,
            "entity_type": (
                "team_defense"
                if player_id.startswith(TEAM_DEFENSE_PREFIX)
                else "player"
            ),
            "membership_status": "active",
        }
        for player_id in sorted(members)
    ]
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=SCOPE_COLLECTOR,
        signal_type=SCOPE_SIGNAL_TYPE,
        captured_at=captured_at,
        upstream=Upstream(adapter="depth-chart", fetched_at=captured_at),
        scope={"season": season, "week": week},
        coverage=Coverage(expected=len(rows), present=len(rows), missing=[]),
        errors=[],
        signals=rows,
    )


def seed_scope(
    lake, members=(), *, season: int = SEASON, week: int = WEEK, captured_at=NOW
):
    """Put a membership envelope directly into `lake`'s storage, so a capture
    can narrow to it.

    Written straight into `lake.objects` rather than through `lake.write` —
    a seeded scope is data the fixture pretends `roster-scope` already wrote,
    not something the capture *under test* produced. Several tests assert on
    `SpyLake.writes` to prove exactly what one capture pass wrote (one entry
    per `SIGNAL_TYPES` member); going through `.write` here would silently
    add a `scope_membership_weekly` entry to that list and break every one of
    them the moment narrowing shipped on.

    Always includes `SCOPE_ANCHOR`, so the written envelope is never empty —
    an empty one is exactly what `ScopeClient` treats as unusable (it falls
    back a week, then raises `scope_empty`). Passing no `members` seeds the
    old unscoped default: an empty, but successfully fetched, watchlist.
    """
    envelope = scope_envelope(
        {*members, SCOPE_ANCHOR}, season=season, week=week, captured_at=captured_at
    )
    lake.objects[lake_key(envelope)] = envelope.to_dict()
    return lake


def feed_row(**overrides) -> dict:
    """One upstream CSV row as `stream_csv_dicts` would yield it.

    Every required column present and numeric-as-string, because that is what
    a CSV reader produces. Override only what a test is actually about.
    """
    row = {column: "0" for column in REQUIRED_COLUMNS}
    row.update(
        {
            "player_id": "00-0000001",
            "player_display_name": "Test Player",
            "position": "WR",
            "season": str(SEASON),
            "week": str(WEEK),
            "game_id": f"{SEASON}_01_BUF_KC",
            "team": "KC",
            "opponent_team": "BUF",
        }
    )
    row.update({key: str(value) for key, value in overrides.items()})
    return row


def stub_player_id(upstream_id: str) -> str:
    """What `StubIdCrosswalk` mints for an upstream id.

    Recomputed here rather than imported, so a test pinning a canonical id is
    pinning a value rather than calling the code it is checking.
    """
    digest = hashlib.sha256(f"gsis|{upstream_id}".encode()).hexdigest()
    return f"fdy-{digest[:12]}"


def feed_csv(rows) -> str:
    """Those rows as a CSV document, header first."""
    columns = sorted(REQUIRED_COLUMNS)
    lines = [",".join(columns)]
    lines.extend(",".join(str(row.get(c, "")) for c in columns) for row in rows)
    return "\n".join(lines) + "\n"
