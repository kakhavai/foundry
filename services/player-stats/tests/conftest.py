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
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

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
def _unscoped(monkeypatch):
    """The shipped configuration: no roster-scope narrowing, stub crosswalk.

    Set explicitly rather than left to the ambient environment — a developer
    with `ROSTER_SCOPE_URL` exported would otherwise see a different collector
    than CI does.
    """
    monkeypatch.setenv("ROSTER_SCOPE_URL", "")
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "")


@pytest.fixture
def client(_collector_token):
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
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
