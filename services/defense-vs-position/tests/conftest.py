"""Fixtures for defense-vs-position's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The three upstreams are mocked at the **wire**, with `respx`, rather than by
patching the adapters. Everything between the socket and the rated row is then
inside the test: gzip inflation, the truncation trailer check, the column
projection, header validation, the conditional-GET commit, `resolve_many`'s
chunking and its per-chunk failure handling. A patched `fetch_fold` would
exercise none of it, and every one of those has a silent failure mode.
"""

import json as _json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from collector_core.conditional import ETAGS
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from defense_vs_position.adapters import pbp, players
from defense_vs_position.main import app

from . import season

TEST_TOKEN = "test-collector-token"
IDENTITY_URL = "http://player-identity.test:8002"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC -- guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON = 2026
WEEK = 2


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
    monkeypatch.setenv("PLAYER_IDENTITY_URL", IDENTITY_URL)


@pytest.fixture(autouse=True)
def _clear_etags():
    """`ETAGS` is a process global, so one test's commit is the next test's
    304 -- and a 304 is a *successful* capture that publishes nothing, which
    surfaces as an unrelated test's assertion failure."""
    ETAGS.clear()
    yield
    ETAGS.clear()


@pytest.fixture
def client(_collector_token, upstreams):
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_collector_singletons():
    """`state` and `refresh_gate` are process-level singletons -- that is what
    lets `/signals` serve a cache and `/refresh` enforce a floor across
    requests -- so something has to reset them between tests."""
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
        season_: int,
        week: int,
        version: str = ENVELOPE_VERSION,
    ) -> list[str]:
        prefix = f"signals/{collector}/v{version}/season={season_}/week={week:02d}/"
        suffix = f"-{signal_type}.json"
        return sorted(
            k for k in self.objects if k.startswith(prefix) and k.endswith(suffix)
        )

    def read(self, key: str) -> dict:
        return self.objects[key]


@pytest.fixture
def lake() -> SpyLake:
    return SpyLake()


class Upstreams:
    """The three mocked upstreams, and the knobs a test turns on them.

    `identity_calls` records the request BODIES, so a batching test can assert
    what was chunked rather than that a method was called.
    """

    def __init__(self, router: respx.Router) -> None:
        self.router = router
        self.identity_calls: list[dict] = []
        self.identity_status = 200
        self.refuse: set[str] = set()
        self.pbp_route = None
        self.players_route = None

    def set_pbp(self, body: bytes, *, headers: dict | None = None) -> None:
        self.pbp_route.mock(
            return_value=httpx.Response(200, content=body, headers=headers or {})
        )

    def set_players(self, body: bytes, *, headers: dict | None = None) -> None:
        self.players_route.mock(
            return_value=httpx.Response(200, content=body, headers=headers or {})
        )


@pytest.fixture
def upstreams():
    """Serve the synthetic season over the real HTTP paths.

    A default healthy shape, overridable per test by calling `set_pbp` /
    `set_players` or by mutating `refuse` / `identity_status` before the
    capture runs.
    """
    with respx.mock(assert_all_called=False) as router:
        state = Upstreams(router)

        def identity(request: httpx.Request) -> httpx.Response:
            payload = _json.loads(request.read())
            state.identity_calls.append(payload)
            if state.identity_status != 200:
                return httpx.Response(state.identity_status, json={"detail": "no"})
            results = []
            for query in payload["queries"]:
                source_id = query.get("source_id") or ""
                if source_id in state.refuse:
                    # A refusal shaped the way player-identity shapes one:
                    # `resolved: false` WITH candidates. A caller that
                    # re-ranks candidates adopts an id it was refused.
                    results.append(
                        {
                            "resolved": False,
                            "player_id": None,
                            "candidates": [
                                {"player_id": f"fdy-{source_id}", "score": 0.61}
                            ],
                        }
                    )
                else:
                    results.append({"resolved": True, "player_id": f"fdy-{source_id}"})
            return httpx.Response(
                200,
                json={
                    "results": results,
                    "count": len(results),
                    "resolved_count": sum(1 for r in results if r["resolved"]),
                    "unresolved_count": sum(1 for r in results if not r["resolved"]),
                },
            )

        state.players_route = router.get(players.UPSTREAM_URL)
        state.pbp_route = router.get(pbp.source_ref(SEASON))
        router.post(f"{IDENTITY_URL}/resolve/batch").mock(side_effect=identity)

        state.set_players(season.players_document())
        state.set_pbp(season.pbp_document())
        yield state


async def run_capture(lake, *, season_=SEASON, week=WEEK, **kwargs):
    """The real capture path, against whatever `upstreams` is serving."""
    from defense_vs_position.capture import capture_defense_vs_position

    async with httpx.AsyncClient() as http:
        return await capture_defense_vs_position(
            season_, week, client=http, lake=lake, now=NOW, **kwargs
        )
