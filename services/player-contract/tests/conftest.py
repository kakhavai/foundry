"""Fixtures for player-contract's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The rest of this file builds the three things a capture needs before it will do
anything at all — a published scope in the lake, a reachable `player-identity`,
and the gzipped contracts CSV — because this collector fails closed without the
first two and a test that forgets one gets a `present: 0` envelope rather than
an error.

**The fixture population is chosen so every branch worth testing has its own
representative, and above all so each two-armed guard has a fixture per arm:**

| player | the arm it exists for |
|---|---|
| Alpha  | active, in scope, term known, deal ends AFTER the season |
| Bravo  | active, in scope, deal ends IN the season — the contract year |
| Charlie| active, in scope, deal already ENDED — the stale-source arm |
| Delta  | active, in scope, `years` blank — term unknown, not present |
| Echo   | active, in scope, multi-club `team` and blank `guaranteed` |
| Foxtrot| active, resolvable, deliberately OUT OF SCOPE |
| Golf   | HISTORICAL (`is_active` FALSE) for a player who is in scope |
| Hotel  | active, in scope, position `ED` — unmapped, must not 422 the batch |

Golf is the one that matters most. He is a *second row for Alpha's player*, and
he is the reason `is_active` is a guard rather than a formality: a collector
that ignored the flag would publish Alpha's 2016 rookie deal alongside — or
instead of — his current one, and every assertion about him would still look
plausible.
"""

import csv
import gzip
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
import respx
from collector_core.conditional import ETAGS
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from httpx import Response
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from player_contract.adapters import upstream
from player_contract.capture import reset_published_digests
from player_contract.main import app

TEST_TOKEN = "test-collector-token"
IDENTITY_URL = "http://player-identity.test"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON = 2026
# Deliberately not 1. A digest, a scope read or a lake key accidentally pinned
# to week 1 is invisible in a suite that only ever captures week 1 — this bit
# `officiating` for real.
WEEK = 3

# The CSV header, in the document's real column order. The four columns this
# collector does NOT read are present so a test proves the projection works
# rather than proving the fixture is narrow.
CSV_COLUMNS = [
    "player",
    "position",
    "team",
    "is_active",
    "year_signed",
    "years",
    "value",
    "apy",
    "guaranteed",
    "inflated_value",
    "otc_id",
    "season_history",
]


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
    """Pointed at a mock by default.

    Empty is a *refusal* in this collector (see `adapters/scope.py`), so leaving
    it unset would make every test exercise the fail-closed path while looking
    like it exercised the happy one.
    """
    monkeypatch.setenv("PLAYER_IDENTITY_URL", IDENTITY_URL)


@pytest.fixture(autouse=True)
def _reset_process_state():
    """Forget both process-lifetime caches between tests.

    `_PUBLISHED_DIGESTS` and the shared `ETagStore` outlive a test, so a second
    test asserting a real publish would otherwise get `UpstreamUnchanged` from
    the first one's leftovers and fail somewhere unrelated to what it checked.
    """
    reset_published_digests()
    ETAGS.clear()
    yield
    reset_published_digests()
    ETAGS.clear()


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


# ── the fixture population ───────────────────────────────────────────────────
#
# `(otc_id, name, otc_position, otc_team, is_active, year_signed, years, value,
#   guaranteed)`

ALPHA = "1001"  # QB, deal runs to 2029: seasons_remaining > 0.
BRAVO = "1002"  # RB, deal ends 2026: THE contract year.
CHARLIE = "1003"  # WR, deal ended 2024: seasons_remaining < 0, the stale arm.
DELTA = "1004"  # TE, `years` blank: term unknown, NOT counted present.
ECHO = "1005"  # K, multi-club team and blank guaranteed.
FOXTROT = "1006"  # WR, resolvable and deliberately OUT OF SCOPE.
GOLF = "1001"  # Alpha's expired rookie deal: is_active FALSE. Same otc_id.
HOTEL = "1008"  # ED — an OTC position player-identity does not know.

FIXTURE_CONTRACTS = [
    (ALPHA, "Alpha Passer", "QB", "Packers", "TRUE", 2025, 5, 150000000, 100000000),
    (BRAVO, "Bravo Runner", "RB", "Bills", "TRUE", 2023, 4, 40000000, 20000000),
    (CHARLIE, "Charlie Catcher", "WR", "49ers", "TRUE", 2021, 4, 60000000, 30000000),
    (DELTA, "Delta Blocker", "TE", "Chiefs", "TRUE", 2024, None, 12000000, 6000000),
    (ECHO, "Echo Kicker", "K", "DEN/SEA", "TRUE", 2024, 3, 9000000, None),
    (FOXTROT, "Foxtrot Wideout", "WR", "Jets", "TRUE", 2025, 3, 21000000, 10000000),
    # Alpha's PREVIOUS deal. Same player, same otc_id, is_active FALSE. A
    # collector that ignored the flag would publish a deal that ended in 2020.
    (GOLF, "Alpha Passer", "QB", "Packers", "FALSE", 2017, 4, 20000000, 8000000),
    (HOTEL, "Hotel Rusher", "ED", "Ravens", "TRUE", 2025, 2, 18000000, 9000000),
]

# Canonical ids `player-identity` resolves each display NAME to. Deliberately
# NOT derived from the name or the otc_id: a test whose ids are a pure function
# of the upstream key cannot tell a real crosswalk hop from a collector minting
# its own.
CANONICAL_IDS = {
    "Alpha Passer": "fdy-aaaa11112222",
    "Bravo Runner": "fdy-bbbb11112222",
    "Charlie Catcher": "fdy-cccc11112222",
    "Delta Blocker": "fdy-dddd11112222",
    "Echo Kicker": "fdy-eeee11112222",
    "Foxtrot Wideout": "fdy-ffff11112222",
    "Hotel Rusher": "fdy-hhhh11112222",
}

OUT_OF_SCOPE_NAME = "Foxtrot Wideout"
SCOPED_NAMES = [n for n in CANONICAL_IDS if n != OUT_OF_SCOPE_NAME]
SCOPED_IDS = [CANONICAL_IDS[n] for n in SCOPED_NAMES]

# roster-scope mints one of these per club and they cannot hold a contract.
TEAM_DEFENSE_IDS = ["fdy-dst-gb", "fdy-dst-buf"]


def contracts_csv(rows=None) -> str:
    """The upstream document, as text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for record in FIXTURE_CONTRACTS if rows is None else rows:
        otc, name, pos, team, active, signed, years, value, guaranteed = record
        writer.writerow([
            name, pos, team, active,
            "" if signed is None else signed,
            "" if years is None else years,
            "" if value is None else value,
            # `apy` and `inflated_value` are deliberately WRONG-looking values:
            # nothing this collector publishes may ever equal them.
            999_999_999, "" if guaranteed is None else guaranteed, 888_888_888,
            otc, "",
        ])  # fmt: skip
    return buffer.getvalue()


def contracts_gz(rows=None) -> bytes:
    """The upstream document, gzipped — which is how it is actually served.

    Gzipped rather than plain text because `gzipped=True` is what gives this
    collector `UpstreamTruncated` on a short body, and a fixture served as plain
    text would exercise a code path the pod never takes.
    """
    return gzip.compress(contracts_csv(rows).encode("utf-8"))


def mock_upstream(mock: respx.MockRouter, *, body: bytes | None = None, status=200):
    """Route the contracts feed. Returns the respx route, for `call_count`."""
    route = mock.get(upstream.UPSTREAM_URL)
    if status != 200:
        route.respond(status)
    else:
        route.respond(
            200,
            content=contracts_gz() if body is None else body,
            headers={"ETag": '"fixture-etag"'},
        )
    return route


def mock_identity(
    mock: respx.MockRouter,
    *,
    resolvable: dict[str, str] | None = None,
    status: int = 200,
) -> None:
    """Route `POST /resolve/batch`, honouring the `resolved` flag.

    `resolvable` maps display NAME -> canonical id. Anything absent comes back
    `resolved: false` **with candidates**, which is the shape `player-identity`
    uses when it has deliberately refused — a client that adopts one of those is
    the bug `IdentityClient` exists to prevent.

    The handler also reproduces `player-identity`'s **422 on an unknown
    position**, because that is the failure this collector's position mapping
    exists to prevent and a permissive mock would hide it entirely.
    """
    table = CANONICAL_IDS if resolvable is None else resolvable

    def handler(request):
        if status != 200:
            return Response(status)
        queries = json.loads(request.content)["queries"]
        for query in queries:
            position = query.get("position")
            if position is not None and position not in KNOWN_POSITIONS:
                # The real service raises HTTPException(422) from build_query,
                # and FastAPI turns that into a 422 for the WHOLE body.
                return Response(422, json={"detail": f"unknown position {position!r}"})
        results = []
        for query in queries:
            player_id = table.get(query.get("name"))
            if player_id:
                results.append({"resolved": True, "player_id": player_id,
                                "confidence": 1.0, "candidates": []})  # fmt: skip
            else:
                results.append({
                    "resolved": False, "player_id": None, "confidence": 0.42,
                    # Populated precisely because it refused. A caller that
                    # re-ranks these adopts an identity it was declined.
                    "candidates": [{"player_id": "fdy-decoy00000", "score": 0.42}],
                })  # fmt: skip
        return Response(200, json={"results": results})

    mock.post(f"{IDENTITY_URL}/resolve/batch").mock(side_effect=handler)


# Mirrors `player_identity.identity.KNOWN_POSITIONS`. Duplicated here rather
# than imported because `player-identity` is a separate uv package that is not
# a dependency of this one — `tests/test_upstream_adapter.py` reads the real
# dictionary out of its source and fails if the two ever drift.
KNOWN_POSITIONS = frozenset(
    {
        "QB", "RB", "FB", "WR", "TE", "C", "G", "OG", "OL", "OT", "T",
        "DE", "DT", "DL", "NT", "LB", "ILB", "OLB", "MLB", "CB", "DB",
        "S", "FS", "SS", "K", "P", "LS", "DST", "DEF",
    }
)  # fmt: skip


def scope_envelope(
    *,
    season: int = SEASON,
    week: int = WEEK,
    player_ids=None,
    include_team_defenses: bool = True,
) -> Envelope:
    """A `roster-scope` membership envelope, as `ScopeClient` reads it."""
    members = list(SCOPED_IDS if player_ids is None else player_ids)
    if include_team_defenses:
        members = members + TEAM_DEFENSE_IDS
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="roster-scope",
        signal_type="scope_membership_weekly",
        captured_at=NOW - timedelta(hours=2),
        upstream=Upstream(adapter="test", fetched_at=NOW - timedelta(hours=2)),
        scope={"season": season, "week": week},
        coverage=Coverage(expected=len(members), present=len(members)),
        errors=[],
        signals=[
            {
                "player_id": member,
                "membership_status": "active",
                "entity_type": (
                    "team_defense" if member.startswith("fdy-dst-") else "player"
                ),
            }
            for member in members
        ],
    )


@pytest.fixture
def lake() -> SpyLake:
    """A lake with a published scope already in it.

    Without one this collector fails closed, so every test that is not ABOUT
    failing closed needs it. The tests that are about it use `SpyLake()`
    directly.
    """
    spy = SpyLake()
    spy.write(scope_envelope())
    return spy


@pytest.fixture
def client(_collector_token, lake):
    """The real app, with the `lake` fixture standing in for its own writer.

    Swapped rather than left alone because a dispatched `/refresh` narrows
    against `spec.lake`, and the process's own is a `NullLakeWriter` (no
    `LAKE_BUCKET` in tests) whose empty listing reads as `scope_unavailable`.
    Every route test would otherwise exercise the fail-closed path by accident
    while looking like it tested the route.
    """
    spec = app.state.collector_spec
    original = spec.lake
    spec.lake = lake
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
            yield c
    finally:
        spec.lake = original


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
