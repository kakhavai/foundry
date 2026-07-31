"""Fixtures for roster-transactions's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

import httpx
import pytest
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from roster_transactions.adapters.upstream import CoverageWindow
from roster_transactions.capture import capture_roster_transactions
from roster_transactions.main import app
from roster_transactions.windows import week_window

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites. Deliberately inside week 1's own window
# (see `windows.week_window`) so the placeholder rows the scaffolded adapter
# emits land in the week the tests capture.
NOW = week_window(2026, 1)[0] + timedelta(days=4)


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


def fake_window(
    covers_from: datetime, covers_through: datetime, feed_url: str = "https://feed"
) -> CoverageWindow:
    """A manifest acknowledging exactly `[covers_from, covers_through]`.

    The acknowledged window is the number the whole coverage block turns on, so
    the suite sets it explicitly rather than letting a fixture imply it.
    """
    return CoverageWindow(
        covers_from=covers_from, covers_through=covers_through, feed_url=feed_url
    )


async def capture_with(
    monkeypatch,
    *,
    rows: Iterable[dict],
    now: datetime,
    covers_through: datetime,
    season: int = 2026,
    week: int = 1,
    lake: SpyLake | None = None,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Run the **real** capture path against a stated window and row set.

    Both halves of the upstream are faked at the adapter seam rather than over
    HTTP, so what is under test is the orchestration — coverage accounting,
    envelopes, the lake — and not a mock of httpx.
    """
    start, _ = week_window(season, week)

    async def manifest(*args, **kwargs):
        # `min` because a week that has not started yet is a real case: the
        # upstream cannot acknowledge through an instant earlier than the
        # window it opens at, and `CoverageWindow` refuses that pair outright.
        return fake_window(min(start, covers_through), covers_through)

    async def stream(*args, **kwargs):
        for row in rows:
            yield row

    monkeypatch.setattr("roster_transactions.capture.fetch_manifest", manifest)
    monkeypatch.setattr("roster_transactions.capture.stream_rows", stream)
    async with httpx.AsyncClient() as client:
        return await capture_roster_transactions(
            season,
            week,
            client=client,
            lake=lake if lake is not None else SpyLake(),
            now=now,
            deadline=deadline,
        )
