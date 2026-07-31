"""Fixtures for injury-report's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.
"""

from datetime import UTC, datetime

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import EventLoopGuardedLake, lake_key
from collector_core.scope import SCOPE_COLLECTOR
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from injury_report.adapters.identity import resolve_player_id
from injury_report.adapters.scope import SCOPE_SIGNAL_TYPES
from injury_report.main import app

TEST_TOKEN = "test-collector-token"
SEASON = 2026
WEEK = 1
MEMBERSHIP_SIGNAL_TYPE, MATCHUP_SIGNAL_TYPE = SCOPE_SIGNAL_TYPES

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
#
# A **Tuesday**, which for this collector means all three of the week's
# practice days have elapsed: the report week runs Wednesday to Tuesday, so a
# capture here expects a full Wed/Thu/Fri filing from every scheduled club. See
# `report.practice_days_elapsed`.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

# Column-complete wire defaults. Tests override only the cells they are about,
# so a schema change shows up as one edit here rather than thirty.
WIRE_DEFAULTS: dict[str, str] = {
    "season": "2026",
    "week": "1",
    "team": "KC",
    "practice_day": "wednesday",
    "report_published_at": "2026-09-16T21:00:00Z",
    "is_final_report": "false",
    "is_estimated": "false",
    "player_external_id": "kc-01",
    "player_name": "KC Player",
    "position": "WR",
    "practice_status": "limited",
    "report_status": "",
    "injury_primary": "knee",
    "roster_status": "active",
    "absence_reason": "injury",
}


def wire(**overrides: str) -> dict[str, str]:
    """One upstream row, defaults filled in."""
    return {**WIRE_DEFAULTS, **overrides}


def empty_filing(**overrides: str) -> dict[str, str]:
    """A club filing a report that lists nobody — a real, complete observation.

    Distinct from a club that filed nothing, which produces no row at all. The
    whole collector turns on the two staying apart.
    """
    return wire(
        player_external_id="",
        player_name="",
        position="",
        practice_status="",
        report_status="",
        injury_primary="",
        roster_status="",
        absence_reason="",
        **overrides,
    )


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
def client(_collector_token, monkeypatch):
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        # A fresh, scope-seeded lake for every test -- the route tests drive
        # real captures through `POST /refresh`, and those now fail closed
        # without a usable membership-union-matchup scope in the lake (the
        # app's real lake is a `NullLakeWriter` absent `LAKE_BUCKET`, whose
        # `list_keys` always answers empty). Anchor-only (see `seed_scope`)
        # reproduces the old unnarrowed default for `team_injury_report`,
        # which never looks at player scope at all; `player_injury_status`
        # narrows to nothing but no route test asserts on its content
        # specifically. A test that needs its own lake overrides
        # `collector_spec.lake` afterward, same as before.
        #
        # Seeded BEFORE wrapping in `EventLoopGuardedLake`: `seed_scope`
        # writes straight into `SpyLake.objects`, a plain synchronous dict
        # write with no running loop involved yet, but the guard's job is to
        # catch a *capture-path* call made directly on the loop thread --
        # `build_collector_app` wraps every collector's real lake in this
        # exact guard, and this task added the first capture-path lake read
        # this collector makes. `monkeypatch.setattr` rather than a direct
        # assignment, so the override reverts even though `app` is a
        # session-wide singleton.
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


# Included in every seeded scope list, for both signal types. `ScopeClient`
# treats a scope envelope with zero real members as unusable (it falls back a
# week, then raises `scope_empty`), so a test that does not care about
# narrowing at all needs a non-empty-but-inert union: this id is in the scope,
# but no stub-week `player_external_id` ever resolves to it, so its presence
# never changes what a test can observe.
SCOPE_ANCHOR = "fdy-scope-anchor"


def scope_envelope(
    signal_type: str,
    members,
    *,
    season: int = SEASON,
    week: int = WEEK,
    captured_at=NOW,
) -> Envelope:
    """One `roster-scope` scope envelope (membership or matchup), as
    `ScopeClient` reads it.

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
        collector=SCOPE_COLLECTOR,
        signal_type=signal_type,
        captured_at=captured_at,
        upstream=Upstream(adapter="depth-chart", fetched_at=captured_at),
        scope={"season": season, "week": week},
        coverage=Coverage(expected=len(rows), present=len(rows), missing=[]),
        errors=[],
        signals=rows,
    )


def seed_scope(
    lake,
    *,
    membership=(),
    matchup=(),
    season: int = SEASON,
    week: int = WEEK,
    captured_at=NOW,
) -> "SpyLake":
    """Write BOTH the membership and matchup scope lists directly into
    `lake`'s storage, so a capture can narrow to their union.

    `fetch_union` is all-or-nothing: seeding only one of the two would raise
    `scope_unavailable` for the other and never reach the capture path a test
    means to exercise. Written straight into `lake.objects` rather than
    through `lake.write` -- a seeded scope is data the fixture pretends
    `roster-scope` already wrote, not something the capture *under test*
    produced. Several tests assert on `SpyLake.writes` to prove exactly what
    one capture pass wrote (one entry per `SIGNAL_TYPES` member); going
    through `.write` here would silently add two scope entries to that list.
    """
    for signal_type, members in (
        (MEMBERSHIP_SIGNAL_TYPE, membership),
        (MATCHUP_SIGNAL_TYPE, matchup),
    ):
        envelope = scope_envelope(
            signal_type,
            {*members, SCOPE_ANCHOR},
            season=season,
            week=week,
            captured_at=captured_at,
        )
        lake.objects[lake_key(envelope)] = envelope.to_dict()
    return lake


def player_ids_in(rows) -> set[str]:
    """Every canonical `player_id` these wire rows will resolve to.

    Mirrors `report.py`'s own extraction exactly -- `(row["player_external_id"]
    or "").strip()`, then `resolve_player_id` -- so a scope built from this
    reproduces "narrowing changed nothing" for a test that is not about
    narrowing at all: every player row a capture would have produced before
    narrowing shipped stays present after it, because its id is in the seeded
    scope. A row with no external id never produces a player row in the real
    pipeline either (see `report._fold_player`), so it is skipped here too.
    """
    ids: set[str] = set()
    for row in rows:
        external_id = (row.get("player_external_id") or "").strip()
        if not external_id:
            continue
        ids.add(resolve_player_id(external_id))
    return ids
