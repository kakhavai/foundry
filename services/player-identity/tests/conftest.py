from datetime import UTC, datetime

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import REGISTRY, generate_latest

from player_identity.main import app

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
STAMP = "2026-09-11T12:00:00Z"


@pytest.fixture(scope="session", autouse=True)
def _meter_provider():
    """Install one real MeterProvider for the whole test session.

    Instruments are created at import time and record nothing until a
    provider exists; anything recorded before it is silently lost. Because
    `set_meter_provider` is one-shot per process this must happen exactly
    once, before any test records. test_telemetry's `patched_sdk` fixture
    no-ops `set_meter_provider`, so `setup_telemetry` cannot clobber it.
    """
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()])
    )


@pytest.fixture
def metric_value():
    """Read one series out of real /metrics output.

    Asserting on `generate_latest` rather than the SDK's internal view means
    these tests check exactly what Prometheus scrapes, including OTel's name
    mangling. Returns None when the series is absent, which is distinct from
    a series present with value 0.0.
    """

    def read(name: str, **labels: str) -> float | None:
        for line in generate_latest(REGISTRY).decode().splitlines():
            if line.startswith("#"):
                continue
            head, _, raw_value = line.rpartition(" ")
            if "{" in head:
                series, _, raw_labels = head.partition("{")
                if series != name:
                    continue
                found = {
                    k: v.strip('"')
                    for k, v in (
                        pair.split("=", 1)
                        for pair in raw_labels.rstrip("}").split(",")
                        if pair
                    )
                }
            else:
                if head != name:
                    continue
                found = {}
            if all(found.get(k) == v for k, v in labels.items()):
                return float(raw_value)
        return None

    return read


TEST_TOKEN = "test-collector-token"


@pytest.fixture(autouse=True)
def _collector_token(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", TEST_TOKEN)


@pytest.fixture
def collector_token(_collector_token) -> str:
    return TEST_TOKEN


@pytest.fixture
def client(_collector_token):
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_collector_singletons():
    """The capture cache, the refresh gate, the miss queue, and the
    resolution index are all process-level singletons — that is what lets
    `/signals` serve from a cache, `/refresh` enforce an interval floor, and
    `/resolve` see what the last capture built. It also makes them a
    shared-state hazard between tests unless something resets them.
    """
    spec = app.state.collector_spec

    def reset() -> None:
        spec.state.envelopes = {}
        spec.state.last_capture_at = None
        spec.refresh_gate._last_allowed_at = None
        app.state.miss_queue.clear()
        app.state.resolution_index.replace([])

    reset()
    yield
    reset()


class SpyLake:
    """A lake writer that records what it was handed.

    Deliberately not `moto`: CI prunes `collector-core`'s dev dependencies
    inside `services/`, so a `moto` import passes locally off a shared
    virtualenv and fails only in CI.
    """

    def __init__(self, fail: bool = False) -> None:
        self.written: list[Envelope] = []
        self.fail = fail

    def write(self, envelope: Envelope) -> str:
        if self.fail:
            raise OSError("lake unreachable")
        self.written.append(envelope)
        return "key"

    def list_keys(self, *args, **kwargs) -> list[str]:
        return []

    def read(self, key: str) -> dict:
        raise KeyError(key)


def player(
    key: str,
    *,
    full_name: str = "Davante Adams",
    first_name: str = "Davante",
    last_name: str = "Adams",
    position: str | None = "WR",
    team: str | None = "LV",
    number: int | None = 17,
    status: str | None = "Active",
    birth_date: str | None = "1992-12-24",
    years_exp: int | None = 12,
    crosswalk: bool = True,
    **overrides,
) -> dict:
    """One Sleeper players record with every structurally required key."""
    record = {
        "player_id": key,
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "position": position,
        "team": team,
        "number": number,
        "status": status,
        "birth_date": birth_date,
        "years_exp": years_exp,
    }
    if crosswalk:
        record.update(
            {
                "gsis_id": f"00-00{key}",
                "espn_id": f"e{key}",
                "yahoo_id": f"y{key}",
                "rotowire_id": f"r{key}",
                "sportradar_id": f"s{key}",
                "fantasy_data_id": f"f{key}",
            }
        )
    record.update(overrides)
    return record


def sleeper_document(*records: dict) -> dict:
    return {record["player_id"]: record for record in records}


def canonical_row(
    player_id: str = "fdy-000000000001",
    *,
    full_name: str = "Davante Adams",
    normalized_key: str = "davante adams",
    team: str | None = "LV",
    jersey_number: int | None = 17,
    position: str = "WR",
    position_group: str = "offense_skill",
    entry_year: int | None = None,
    birth_date: str | None = None,
    team_as_of: str = STAMP,
    jersey_as_of: str = STAMP,
    external_ids: dict | None = None,
) -> dict:
    """A canonical crosswalk row, hand-built for index/scoring tests."""
    return {
        "player_id": player_id,
        "full_name": full_name,
        "normalized_key": normalized_key,
        "team": team,
        "team_as_of": team_as_of,
        "jersey_number": jersey_number,
        "jersey_as_of": jersey_as_of,
        "position": position,
        "position_group": position_group,
        "entry_year": entry_year,
        "birth_date": birth_date,
        "external_ids": external_ids if external_ids is not None else {},
    }


def make_envelope(signal_type: str, signals: list[dict]) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="player-identity",
        signal_type=signal_type,
        captured_at=NOW,
        upstream=Upstream("sleeper-players", NOW, source_ref='W/"abc"'),
        scope={"season": 2026, "week": 1},
        coverage=Coverage(expected=len(signals), present=len(signals), missing=[]),
        errors=[],
        signals=signals,
    )


@pytest.fixture
def seeded_state():
    """Route tests pre-populate the cache directly, per the repo convention —
    the routes never call an upstream."""
    state = app.state.collector_spec.state
    rows = [
        canonical_row(
            "fdy-000000000001",
            external_ids={
                "gsis": {
                    "id": "00-0031381",
                    "linked_at": STAMP,
                    "link_method": "crosswalk",
                    "match_score": None,
                    "match_margin": None,
                }
            },
        ),
        canonical_row(
            "fdy-000000000002",
            full_name="Puka Nacua",
            normalized_key="puka nacua",
            team="LAR",
            jersey_number=12,
        ),
    ]
    state.envelopes = {
        "player_identity_crosswalk": make_envelope("player_identity_crosswalk", rows),
        "name_resolution_miss": make_envelope("name_resolution_miss", []),
    }
    state.last_capture_at = NOW
    app.state.resolution_index.replace(rows)
    yield rows
    state.envelopes = {}
    state.last_capture_at = None
