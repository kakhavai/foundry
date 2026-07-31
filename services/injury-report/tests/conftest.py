"""Fixtures for injury-report's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.
"""

from datetime import UTC, datetime

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from injury_report.main import app

TEST_TOKEN = "test-collector-token"

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
