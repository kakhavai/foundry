"""Fixtures for depth-chart's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The CSV builders below emit the **real** nflverse header, verified against the
live asset on 2026-07-30:

    dt,team,player_name,espn_id,gsis_id,pos_grp_id,pos_grp,pos_id,pos_name,
    pos_abb,pos_slot,pos_rank

A fabricated header would let the adapter's `REQUIRED_COLUMNS` assertion pass
against a shape the upstream does not actually publish, which is the one thing
that assertion exists to prevent.
"""

import zlib
from datetime import UTC, datetime, timedelta

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from depth_chart.main import app

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


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


# ── the upstream, as it really looks ──────────────────────────────────────────

DEPTH_HEADER = (
    "dt,team,player_name,espn_id,gsis_id,pos_grp_id,pos_grp,pos_id,pos_name,"
    "pos_abb,pos_slot,pos_rank"
)

# The publication instants the suite describes, newest last. Spaced a week apart
# so each lands in its own ISO-week bucket, which is the grain the stability
# window counts over.
WEEK_DTS: tuple[str, ...] = (
    "2026-08-18T07:00:00Z",
    "2026-08-25T07:00:00Z",
    "2026-09-01T07:00:00Z",
    "2026-09-08T07:00:00Z",
)
CURRENT_DT = WEEK_DTS[-1]


def gsis_for(name: str) -> str:
    """A deterministic stand-in for the feed's crosswalk id.

    Deterministic rather than `hash()`-derived: PYTHONHASHSEED is randomized per
    process, so a hash-derived id would make `chart_hash` assertions pass or
    fail depending on the run.
    """
    return f"00-{zlib.crc32(name.encode()) % 10_000_000:07d}"


def depth_row(
    team: str,
    pos_abb: str,
    rank: int,
    name: str,
    *,
    dt: str = CURRENT_DT,
    gsis_id: str | None = None,
) -> str:
    gid = gsis_for(name) if gsis_id is None else gsis_id
    return f"{dt},{team},{name},1,{gid},1,3WR 1TE,1,Position,{pos_abb},{rank},{rank}"


def depth_csv(rows: list[str], header: str = DEPTH_HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


def full_chart_rows(dt: str = CURRENT_DT) -> list[str]:
    """One charted player for every `(team, position)` group the config demands.

    Imported from `universe` rather than restated, so widening the position set
    widens the "complete capture" fixture with it instead of silently leaving a
    group untested.
    """
    from depth_chart.universe import POSITIONS, TEAMS

    return [
        depth_row(team, position, 1, f"{team} {position} One", dt=dt)
        for team in TEAMS
        for position in POSITIONS
    ]


def iso_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def dt_plus_days(value: str, days: int) -> str:
    return (iso_dt(value) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
