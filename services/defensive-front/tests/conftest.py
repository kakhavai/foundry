"""Fixtures for defensive-front's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

Every test that goes through `capture_defensive_front` exercises the real
streaming parser, the real gzip inflater, the real conditional-GET path, the
real join, the real opponent adjustment, the real timing guard and the real
digest gate. **Only the socket is fake.** `tests/season.py` builds the four
documents in the real wire format; read its docstring before touching a
fixture, because the shape of the synthetic season is what makes these tests
capable of failing at all.
"""

from collections.abc import Mapping
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

from defensive_front.adapters import injuries as injuries_adapter
from defensive_front.adapters import participation as participation_adapter
from defensive_front.adapters import pbp as pbp_adapter
from defensive_front.adapters import players as players_adapter
from defensive_front.capture import capture_defensive_front, reset_published_digests
from defensive_front.main import app

from . import season as season_module

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

SEASON = season_module.SEASON
# The last sampled week. `key_absences` looks at `WEEK + 1`, which is what
# `injuries_document`'s default week is.
WEEK = 5


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
def _no_identity_by_default(monkeypatch):
    """No `PLAYER_IDENTITY_URL` unless a test asks for one.

    A stray value inherited from the developer's shell would point the
    identity path at a real service and make the suite's result depend on
    whose machine it ran on.
    """
    monkeypatch.delenv("PLAYER_IDENTITY_URL", raising=False)


@pytest.fixture
def client(_collector_token):
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_collector_singletons():
    """`state`, `refresh_gate`, the ETag store and the publish digests are all
    process-level singletons — that is what lets `/signals` serve a cache,
    `/refresh` enforce a floor, a `304` skip a download and an unchanged pass
    skip an append — so something has to reset them between tests."""
    spec = app.state.collector_spec
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    ETAGS.clear()
    reset_published_digests()
    yield
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    ETAGS.clear()
    reset_published_digests()


class SpyLake:
    """A minimal in-memory `LakeWriter`.

    `fail_signal_types` fails **one** signal type's write while the others
    land — the partial outage a per-pass digest gate passes and a per-signal
    one catches. With one signal type today it coincides with `fail_write`; it
    exists so the distinction survives a second signal type being added.
    """

    def __init__(
        self,
        *,
        fail_write: bool = False,
        fail_signal_types: frozenset[str] = frozenset(),
    ) -> None:
        self.objects: dict[str, dict] = {}
        self.writes: list[Envelope] = []
        self.fail_write = fail_write
        self.fail_signal_types = fail_signal_types

    def write(self, envelope: Envelope) -> str:
        if self.fail_write or envelope.signal_type in self.fail_signal_types:
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


class Feeds:
    """One pass's four upstream bodies, and how each responds.

    A record rather than a dozen keyword arguments on `run_capture`: every
    test that degrades one feed leaves the other three alone, and the call
    site should say only what it changed.

    **`etags` makes the mock answer `If-None-Match` the way the real upstream
    does** — a `304` only when the request actually carries the matching
    header, and a `200` carrying the ETag otherwise. A mock that returned
    `304` unconditionally would pass a collector that never sent the header at
    all, and it would make the unconditional-re-fetch path untestable: that
    re-fetch omits `If-None-Match` precisely so it gets a body, and a
    status-only mock cannot represent the difference.
    """

    def __init__(
        self,
        *,
        season: int = SEASON,
        timing_confound: float = 0.0,
        defense_release_shift: float = 0.0,
        pbp_status: int = 200,
        participation_status: int = 200,
        players_status: int = 200,
        injuries_status: int = 200,
        etags: Mapping[str, str] | None = None,
        injury_week: int = WEEK + 1,
        bodies: Mapping[str, bytes] | None = None,
        always_304: frozenset[str] = frozenset(),
    ) -> None:
        self.season = season
        self.built = season_module.build_season(
            season=season,
            timing_confound=timing_confound,
            defense_release_shift=defense_release_shift,
        )
        self.status = {
            "pbp": pbp_status,
            "participation": participation_status,
            "players": players_status,
            "injuries": injuries_status,
        }
        self.etags = dict(etags or {})
        # Feeds that answer `304` even to a request carrying no validator --
        # an upstream contract violation, and the only way to reach the
        # collector's guard against one. A well-behaved mock cannot produce it.
        self.always_304 = frozenset(always_304)
        override = dict(bodies or {})
        self.bodies = {
            "pbp": override.get("pbp", season_module.pbp_document(self.built)),
            "participation": override.get(
                "participation", season_module.participation_document(self.built)
            ),
            "players": override.get("players", season_module.players_document()),
            "injuries": override.get(
                "injuries",
                season_module.injuries_document(season=season, week=injury_week),
            ),
        }
        # Every request the mock saw, as `(feed, If-None-Match or None)`, so a
        # test can assert the header was actually sent rather than infer it
        # from a status code.
        self.requests: list[tuple[str, str | None]] = []

    def calls(self, name: str) -> int:
        return sum(1 for feed, _sent in self.requests if feed == name)

    def conditional_calls(self, name: str) -> int:
        return sum(1 for feed, sent in self.requests if feed == name and sent)

    def _responder(self, name: str):
        etag = self.etags.get(name)
        status = self.status[name]
        body = self.bodies[name]

        def respond(request: httpx.Request) -> httpx.Response:
            sent = request.headers.get("If-None-Match")
            self.requests.append((name, sent))
            if name in self.always_304:
                return httpx.Response(304, headers={"ETag": etag or '"x"'})
            if etag is not None and sent == etag:
                return httpx.Response(304, headers={"ETag": etag})
            headers = {"ETag": etag} if etag is not None else {}
            return httpx.Response(status, content=body, headers=headers)

        return respond

    def install(self, router) -> None:
        for name, url in (
            ("pbp", pbp_adapter.source_ref(self.season)),
            ("participation", participation_adapter.source_ref(self.season)),
            ("players", players_adapter.source_ref()),
            ("injuries", injuries_adapter.source_ref(self.season)),
        ):
            router.get(url).mock(side_effect=self._responder(name))


async def run_capture(
    feeds: Feeds | None = None,
    *,
    lake: SpyLake,
    now: datetime = NOW,
    season: int = SEASON,
    week: int = WEEK,
    deadline: datetime | None = None,
    identity_router=None,
):
    """One real capture pass against four mocked feeds."""
    feeds = feeds if feeds is not None else Feeds(season=season)
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        if identity_router is not None:
            identity_router(router)
        async with httpx.AsyncClient() as client:
            return await capture_defensive_front(
                season,
                week,
                client=client,
                lake=lake,
                now=now,
                deadline=deadline,
            )


def rows_of(envelopes, signal_type: str = "defensive_front_strength") -> list[dict]:
    return list(envelopes[signal_type].signals)


def by_team(
    envelopes, signal_type: str = "defensive_front_strength"
) -> dict[str, dict]:
    return {row["team_id"]: row for row in rows_of(envelopes, signal_type)}
