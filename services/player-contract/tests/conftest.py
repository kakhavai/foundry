"""Fixtures for player-contract's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The rest of this file builds the three things a capture needs before it will do
anything at all — a published scope in the lake, a reachable `player-identity`,
and the contracts **parquet** — because this collector fails closed without the
first two and a test that forgets one gets a `present: 0` envelope rather than
an error.

**The fixture document is a real parquet, built with the upstream's own types.**
Not a CSV, and not a hand-rolled dict: the live document types `is_active` as a
bool, `otc_id` as int32, every money column as a **double denominated in
millions**, and the per-season cap table as a `list<struct<...>>` whose `year`
member is a *string* while every other season-ish column is an int32. A fixture
that flattened any of that would test a document the pod never sees, and the
units in particular are the one difference between the dead CSV artifact and the
live parquet that is silently catastrophic — the numbers stay plausible and
nothing raises.

**The population gives every two-armed guard a fixture per arm:**

| player | identity arm | the other arms it exists for |
|---|---|---|
| Alpha  | gsis | term known, deal ends AFTER the season; cap entry present |
| Bravo  | gsis | deal ends IN the season — the contract year |
| Charlie| gsis | deal already ENDED (the stale arm); NO cap entry for the season |
| Delta  | name | `years` blank — term unknown, so not counted present |
| Echo   | name | multi-club `team`, blank `guaranteed`, no cap table at all |
| Foxtrot| gsis | resolvable and deliberately OUT OF SCOPE |
| Golf   | —    | HISTORICAL (`is_active` False), for a player who IS in scope |
| Hotel  | name | position `ED` — unmapped, must not 422 the batch |

Two of those carry most of the weight.

**Golf** is a *second row for Alpha's player* — same `otc_id`, `is_active`
False. He is why `is_active` is a guard rather than a formality: a collector
that ignored the flag would publish Alpha's 2017 rookie deal alongside, or
instead of, his current one, and every assertion about him would still look
plausible.

**The gsis/name split** is the other. `gsis_id` is a Tier-1 crosswalk key that
`player-identity` adopts without scoring, and it is present on 77% of live rows;
the rest fall to Tier-3 name agreement. Both query shapes are real code paths, so
both need a fixture, and the identity mock below keys on whichever one the query
actually carries rather than accepting either.
"""

import io
import json
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
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


FIXTURE_ETAG = '"fixture-etag"'

# ── the fixture document ─────────────────────────────────────────────────────
#
# The upstream's own arrow schema. Written out in full — including the columns
# this collector does NOT read — so a test proves the projection works rather
# than proving the fixture happens to be narrow. `apy` and `inflated_value`
# carry unmistakable sentinels: nothing published may ever equal them.
SEASON_ENTRY = pa.struct(
    [
        # A STRING in the live document, while `year_signed` beside it is an
        # int32. Faithfully reproduced, because `_season_entry` compares as text
        # and a fixture typing it as an int would let an int comparison pass.
        ("year", pa.string()),
        ("team", pa.string()),
        ("base_salary", pa.float64()),
        ("prorated_bonus", pa.float64()),
        ("guaranteed_salary", pa.float64()),
        ("cap_number", pa.float64()),
    ]
)

PARQUET_SCHEMA = pa.schema(
    [
        ("player", pa.string()),
        ("position", pa.string()),
        ("team", pa.string()),
        ("is_active", pa.bool_()),
        ("year_signed", pa.int32()),
        ("years", pa.int32()),
        ("value", pa.float64()),
        ("apy", pa.float64()),
        ("guaranteed", pa.float64()),
        ("inflated_value", pa.float64()),
        ("otc_id", pa.int32()),
        ("gsis_id", pa.string()),
        ("cols", pa.list_(SEASON_ENTRY)),
    ]
)

# Sentinels for the two columns that must never be read. `apy` is average annual
# value and a cap hit is not; `inflated_value` is restated against a moving
# index. Both in the document's millions, so a naive read would publish
# 999,999,000,000 — unmistakable in any assertion.
APY_SENTINEL = 999_999.0
INFLATED_SENTINEL = 888_888.0


# The career-total cap number the fixture's `Total` pseudo-rows carry. Large,
# distinctive, and asserted absent from every published record: it is what an
# implementation reading `cols[-1]` would publish as a season cap hit.
TOTAL_SENTINEL = 339.44306


def cap(year, cap_number, prorated=None):
    """One per-season cap entry, in the document's millions.

    `year` is deliberately stringified: it is a string in the live document even
    though `year_signed` beside it is an int32.
    """
    return {
        "year": str(year),
        "team": "Fixture",
        "base_salary": 1.0,
        "prorated_bonus": prorated,
        "guaranteed_salary": 2.0,
        "cap_number": cap_number,
    }


# `otc_id` is an int32 in the live parquet, not the CSV's string.
ALPHA = 1001  # QB, deal runs to 2029. gsis arm. 2026 cap entry present.
BRAVO = 1002  # RB, deal ends 2026: THE contract year. gsis arm.
CHARLIE = 1003  # WR, deal ended 2024: the stale arm. gsis, NO 2026 cap entry.
DELTA = 1004  # TE, `years` blank: term unknown. NAME arm.
ECHO = 1005  # K, multi-club team, blank guaranteed, NO cap table at all. NAME.
FOXTROT = 1006  # WR, resolvable and deliberately OUT OF SCOPE.
GOLF = 1001  # Alpha's expired rookie deal: is_active False. Same otc_id.
HOTEL = 1008  # ED — a position player-identity does not know. NAME arm.

# Money is in MILLIONS, as the live document has it: 150.0 is a $150M contract.
# A fixture in whole dollars would let a collector that forgot to scale pass
# every test while publishing numbers a millionth of their true size.
FIXTURE_CONTRACTS = [
    {
        "otc_id": ALPHA,
        "player": "Alpha Passer",
        "position": "QB",
        "team": "Packers",
        "is_active": True,
        "year_signed": 2025,
        "years": 5,
        "value": 150.0,
        "guaranteed": 100.0,
        "gsis_id": "00-0000001",
        "cols": [cap(2025, 12.5, 4.0), cap(2026, 30.25, 8.5)],
    },
    {
        "otc_id": BRAVO,
        "player": "Bravo Runner",
        "position": "RB",
        "team": "Bills",
        "is_active": True,
        "year_signed": 2023,
        "years": 4,
        "value": 40.0,
        "guaranteed": 20.0,
        "gsis_id": "00-0000002",
        # A proration of exactly 0 — a real value, not a null.
        "cols": [cap(2026, 11.125, 0.0)],
    },
    {
        "otc_id": CHARLIE,
        "player": "Charlie Catcher",
        "position": "WR",
        "team": "49ers",
        "is_active": True,
        "year_signed": 2021,
        "years": 4,
        "value": 60.0,
        "guaranteed": 30.0,
        "gsis_id": "00-0000003",
        # A cap table that stops before the capture season: the row exists, the
        # season does not. That is `absent_in_upstream_row`, not `unsourced`.
        "cols": [cap(2023, 9.0, 3.0), cap(2024, 10.0, 3.0)],
    },
    {
        "otc_id": DELTA,
        "player": "Delta Blocker",
        "position": "TE",
        "team": "Chiefs",
        "is_active": True,
        "year_signed": 2024,
        "years": None,
        "value": 12.0,
        "guaranteed": 6.0,
        "gsis_id": None,
        # Present for the season, but with no proration recorded.
        "cols": [cap(2026, 4.5, None)],
    },
    {
        "otc_id": ECHO,
        "player": "Echo Kicker",
        "position": "K",
        "team": "DEN/SEA",
        "is_active": True,
        "year_signed": 2024,
        "years": 3,
        "value": 9.0,
        "guaranteed": None,
        "gsis_id": None,
        "cols": [],
    },
    {
        "otc_id": FOXTROT,
        "player": "Foxtrot Wideout",
        "position": "WR",
        "team": "Jets",
        "is_active": True,
        "year_signed": 2025,
        "years": 3,
        "value": 21.0,
        "guaranteed": 10.0,
        "gsis_id": "00-0000006",
        "cols": [cap(2026, 7.0, 2.0)],
    },
    {
        # Alpha's PREVIOUS deal. Same player, same otc_id, is_active False. A
        # collector that ignored the flag would publish a deal that ended 2020.
        "otc_id": GOLF,
        "player": "Alpha Passer",
        "position": "QB",
        "team": "Packers",
        "is_active": False,
        "year_signed": 2017,
        "years": 4,
        "value": 20.0,
        "guaranteed": 8.0,
        "gsis_id": "00-0000001",
        "cols": [cap(2019, 5.0, 1.0)],
    },
    {
        "otc_id": HOTEL,
        "player": "Hotel Rusher",
        "position": "ED",
        "team": "Ravens",
        "is_active": True,
        "year_signed": 2025,
        "years": 2,
        "value": 18.0,
        "guaranteed": 9.0,
        "gsis_id": None,
        "cols": [cap(2026, 6.0, 1.5)],
    },
]

# Canonical ids `player-identity` resolves each row to.
CANONICAL_IDS = {
    "Alpha Passer": "fdy-aaaa11112222",
    "Bravo Runner": "fdy-bbbb11112222",
    "Charlie Catcher": "fdy-cccc11112222",
    "Delta Blocker": "fdy-dddd11112222",
    "Echo Kicker": "fdy-eeee11112222",
    "Foxtrot Wideout": "fdy-ffff11112222",
    "Hotel Rusher": "fdy-hhhh11112222",
}

# gsis id -> display name, so the identity mock can answer the Tier-1 arm.
# Deliberately NOT a derivation from the gsis id: a test whose canonical ids are
# a pure function of the upstream key cannot tell a real crosswalk hop from a
# collector minting its own.
GSIS_TO_NAME = {
    row["gsis_id"]: row["player"]
    for row in FIXTURE_CONTRACTS
    if row["gsis_id"] and row["is_active"]
}

OUT_OF_SCOPE_NAME = "Foxtrot Wideout"
SCOPED_NAMES = [n for n in CANONICAL_IDS if n != OUT_OF_SCOPE_NAME]
SCOPED_IDS = [CANONICAL_IDS[n] for n in SCOPED_NAMES]

# roster-scope mints one of these per club and they cannot hold a contract.
TEAM_DEFENSE_IDS = ["fdy-dst-gb", "fdy-dst-buf"]


def row(
    player,
    *,
    otc_id=9001,
    position="QB",
    team="Bears",
    is_active=True,
    year_signed=2025,
    years=3,
    value=5.0,
    guaranteed=0.0,
    gsis_id=None,
    cols=None,
):
    """One upstream row, by keyword, for a test that needs a specific shape.

    Money defaults are in the document's MILLIONS, like the real feed: `value=5.0`
    is a $5M contract. Every default is deliberately boring so a test states only
    the field it is about.
    """
    return {
        "otc_id": otc_id,
        "player": player,
        "position": position,
        "team": team,
        "is_active": is_active,
        "year_signed": year_signed,
        "years": years,
        "value": value,
        "guaranteed": guaranteed,
        "gsis_id": gsis_id,
        "cols": [] if cols is None else cols,
    }


def with_total(entries):
    """A cap table as the live document actually ships it: a `Total` LAST.

    Every one of the 2,250 active rows carrying a cap table ends with a
    pseudo-row whose `year` is the string `"Total"` and whose `cap_number` is
    the CAREER total. `cols[-1]` — the most natural "give me the latest" shortcut
    anyone would reach for — therefore publishes a career total as a
    current-season cap hit, for every player, with a plausible magnitude.

    Appended by `contracts_parquet` to every non-empty table so no fixture can
    accidentally describe a document shape the upstream does not produce.
    """
    return [*entries, cap("Total", TOTAL_SENTINEL, TOTAL_SENTINEL)]


def contracts_parquet(rows=None) -> bytes:
    """The upstream document, as parquet bytes — which is how it is served.

    A real parquet rather than a stub: the adapter buffers the body and hands it
    to `pyarrow`, so anything less would exercise a code path the pod does not
    take, and the footer-at-the-end layout is precisely what makes this format
    different from every other upstream in the fleet.
    """
    records = [dict(r) for r in (FIXTURE_CONTRACTS if rows is None else rows)]
    for record in records:
        record.setdefault("apy", APY_SENTINEL)
        record.setdefault("inflated_value", INFLATED_SENTINEL)
        record.setdefault("cols", [])
        if record["cols"]:
            record["cols"] = with_total(record["cols"])
    table = pa.Table.from_pylist(
        [{name: r.get(name) for name in PARQUET_SCHEMA.names} for r in records],
        schema=PARQUET_SCHEMA,
    )
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def mock_upstream(mock: respx.MockRouter, *, body: bytes | None = None, status=200):
    """Route the contracts feed. Returns the respx route, for `call_count`."""
    route = mock.get(upstream.UPSTREAM_URL)
    if status != 200:
        route.respond(status)
    else:
        route.respond(
            200,
            content=contracts_parquet() if body is None else body,
            headers={"ETag": FIXTURE_ETAG},
        )
    return route


def mock_identity(
    mock: respx.MockRouter,
    *,
    resolvable: dict[str, str] | None = None,
    status: int = 200,
) -> None:
    """Route `POST /resolve/batch`, honouring the `resolved` flag and BOTH arms.

    `resolvable` maps display NAME -> canonical id. Anything absent comes back
    `resolved: false` **with candidates**, which is the shape `player-identity`
    uses when it has deliberately refused — a client that adopts one of those is
    the bug `IdentityClient` exists to prevent.

    **A query is answered on whichever key it actually carries**, never on
    either-will-do:

    * `source == "gsis"` -> resolved through `GSIS_TO_NAME`, and a `name` on the
      same query is a **failure**, because the Tier-1 arm withholds the name on
      purpose (a crosswalk miss must not fall through to attribute scoring).
    * otherwise -> resolved on `name`.

    A mock that accepted either key would let an adapter send the wrong shape —
    a name where a crosswalk id belongs, or a `source_id` under the wrong source
    name — and still go green, which is exactly the class of bug the two-arm
    split introduces.

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
            if query.get("source") and query.get("source") not in CROSSWALK_SOURCES:
                # Not what the real service does — it ignores an unknown source
                # silently, which is precisely why sending `otc` would be inert
                # rather than an error. Made loud HERE so a test notices.
                return Response(400, json={"detail": "unknown crosswalk source"})
        results = []
        for query in queries:
            if query.get("source") == "gsis":
                assert not query.get("name"), (
                    "the Tier-1 arm sent a name alongside a crosswalk id; a "
                    "crosswalk miss would fall through to attribute scoring"
                )
                name = GSIS_TO_NAME.get(query.get("source_id"))
            else:
                name = query.get("name")
            player_id = table.get(name) if name else None
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


# Mirrors `player_identity.identity.CROSSWALK_SOURCES`. See the note on
# KNOWN_POSITIONS below for why these are duplicated rather than imported.
CROSSWALK_SOURCES = frozenset(
    {"gsis", "espn", "yahoo", "rotowire", "sportradar", "fantasy_data"}
)


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
