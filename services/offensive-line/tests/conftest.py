"""Fixtures for offensive-line's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

Every test that goes through `capture_offensive_line` exercises the real
streaming parser, the real gzip inflater, the real conditional-GET path, the
real six-feed join, the real `pfr_id`/`gsis_id` crosswalk, the real opponent
adjustment, the real lineup guard and the real digest gate. **Only the socket
is fake.** `tests/season.py` builds the six documents in the real wire format;
read its docstring before touching a fixture, because the shape of the
synthetic season is what makes these tests capable of failing at all.
"""

import json
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

from offensive_line.adapters import depth as depth_adapter
from offensive_line.adapters import injuries as injuries_adapter
from offensive_line.adapters import participation as participation_adapter
from offensive_line.adapters import pbp as pbp_adapter
from offensive_line.adapters import players as players_adapter
from offensive_line.adapters import snaps as snaps_adapter
from offensive_line.adapters.identity import SENDABLE_POSITIONS
from offensive_line.capture import (
    STRENGTH,
    capture_offensive_line,
    reset_published_digests,
)
from offensive_line.main import app
from offensive_line.ratings import RECORD_STARTER, RECORD_UNIT

from . import season as season_module

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

SEASON = season_module.SEASON
# The last sampled week. `starter_availability` looks at `WEEK + 1`, which is
# what `injuries_document`'s default week is.
WEEK = season_module.WEEKS

IDENTITY_URL = "http://player-identity.test"


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
    """A `player-identity` every test can reach, because every starter row
    needs one. A test that wants the outage deletes it again.

    Set explicitly rather than inherited: a stray value from the developer's
    shell would point the identity path at a real service and make the suite's
    result depend on whose machine it ran on.
    """
    monkeypatch.setenv("PLAYER_IDENTITY_URL", IDENTITY_URL)


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
    """One pass's six upstream bodies, and how each responds.

    A record rather than two dozen keyword arguments on `run_capture`: every
    test that degrades one feed leaves the other five alone, and the call site
    should say only what it changed.

    **`etags` makes the mock answer `If-None-Match` the way the real upstream
    does** — a `304` only when the request actually carries the matching
    header, and a `200` carrying the ETag otherwise. A mock that returned
    `304` unconditionally would pass a collector that never sent the header at
    all, and it would make the unconditional-re-fetch path untestable: that
    re-fetch omits `If-None-Match` precisely so it gets a body, and a
    status-only mock cannot represent the difference.
    """

    NAMES = ("pbp", "participation", "snaps", "depth", "players", "injuries")

    def __init__(
        self,
        *,
        season: int = SEASON,
        status: Mapping[str, int] | None = None,
        etags: Mapping[str, str] | None = None,
        injury_week: int = WEEK + 1,
        bodies: Mapping[str, bytes] | None = None,
        always_304: frozenset[str] = frozenset(),
    ) -> None:
        self.season = season
        self.built = season_module.build_season(season=season)
        self.status = dict.fromkeys(self.NAMES, 200)
        self.status.update(status or {})
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
            "snaps": override.get(
                "snaps", season_module.snap_counts_document(season=season)
            ),
            "depth": override.get(
                "depth", season_module.depth_charts_document(season=season)
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
            ("snaps", snaps_adapter.source_ref(self.season)),
            ("depth", depth_adapter.source_ref(self.season)),
            ("players", players_adapter.source_ref()),
            ("injuries", injuries_adapter.source_ref(self.season)),
        ):
            router.get(url).mock(side_effect=self._responder(name))


def canonical_for(gsis_id: str) -> str:
    return f"fdy-{gsis_id.replace('-', '')}"


def resolve_everything(router, *, refuse: frozenset[str] = frozenset()) -> None:
    """A `player-identity` that adopts every GSIS id it is given.

    Tier-1 adoption is what the real service does for a published crosswalk
    key, so minting `fdy-<gsis>` here mirrors it. `refuse` names the source ids
    to answer `resolved: false` for — the refusal a collector must never adopt.

    It also **asserts the position it was sent is one `player-identity`
    knows**, which is issue #106's blast radius made into a test: one unmapped
    code fails all 500 queries server-side, and the codes this collector thinks
    in (`LT`, `LG`, `RG`, `RT`) are exactly the four it does not carry.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        results = []
        for query in body["queries"]:
            position = query.get("position")
            assert position is None or position in SENDABLE_POSITIONS, (
                f"{position!r} is outside player-identity's KNOWN_POSITIONS; "
                "one such code fails the whole batch server-side"
            )
            source_id = query.get("source_id") or ""
            if source_id in refuse:
                results.append({"resolved": False, "candidates": []})
            else:
                results.append(
                    {"resolved": True, "player_id": canonical_for(source_id)}
                )
        return httpx.Response(200, json={"results": results})

    router.post(f"{IDENTITY_URL}/resolve/batch").mock(side_effect=respond)


async def run_capture(
    feeds: Feeds | None = None,
    *,
    lake: SpyLake,
    now: datetime = NOW,
    season: int = SEASON,
    week: int = WEEK,
    deadline: datetime | None = None,
    identity_router=resolve_everything,
):
    """One real capture pass against six mocked feeds."""
    feeds = feeds if feeds is not None else Feeds(season=season)
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        if identity_router is not None:
            identity_router(router)
        async with httpx.AsyncClient() as client:
            return await capture_offensive_line(
                season,
                week,
                client=client,
                lake=lake,
                now=now,
                deadline=deadline,
            )


def rows_of(envelopes, signal_type: str = STRENGTH) -> list[dict]:
    return list(envelopes[signal_type].signals)


def units(envelopes, signal_type: str = STRENGTH) -> dict[str, dict]:
    return {
        row["team_id"]: row
        for row in rows_of(envelopes, signal_type)
        if row["record_type"] == RECORD_UNIT
    }


def starters(envelopes, signal_type: str = STRENGTH) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows_of(envelopes, signal_type):
        if row["record_type"] == RECORD_STARTER:
            grouped.setdefault(row["team_id"], []).append(row)
    return grouped
