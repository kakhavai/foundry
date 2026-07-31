"""Fixtures for usage-share's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The upstream is a **CSV document built here and served through respx**, not a
hand-written list of already-parsed rows. That is deliberate: the adapter's
whole job is the wire format, and a fixture that skips the wire proves nothing
about column names, blank cells, or the streaming path — which is where this
collector's memory rules live.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from usage_share.adapters.upstream import UPSTREAM_URL
from usage_share.main import app

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON, WEEK = 2026, 1
UPSTREAM_FOR_SEASON = UPSTREAM_URL.format(season=SEASON, week=WEEK)

# The columns the adapter declares it needs, plus a couple it ignores — a real
# document carries ~90 more, and a fixture with exactly the required set would
# never catch a mapping that quietly depends on column order.
CSV_COLUMNS = (
    "player_id",
    "player_display_name",
    "position",
    "season",
    "week",
    "game_id",
    "team",
    "opponent_team",
    "completions",
    "attempts",
    "sacks_suffered",
    "carries",
    "targets",
    "receiving_air_yards",
    "target_share",
)

KC_GAME = "2026_01_BUF_KC"


def _record(**overrides) -> dict:
    """One CSV record, defaulted to a blank line the adapter must tolerate.

    Blank rather than zero on purpose: the real feed leaves a stat a player
    never recorded empty, and `int("")` is the crash that finds out.
    """
    record = dict.fromkeys(CSV_COLUMNS, "")
    record.update(
        season=str(SEASON),
        week=str(WEEK),
        game_id=KC_GAME,
    )
    record.update({key: str(value) for key, value in overrides.items()})
    return record


def to_csv(records: list[dict]) -> str:
    header = ",".join(CSV_COLUMNS)
    lines = [",".join(record.get(column, "") for column in CSV_COLUMNS) for record in records]
    return "\n".join([header, *lines]) + "\n"


# Two teams, arranged so each one's `target_share` column sums to exactly 1.0 —
# a healthy feed — and so every branch of the mapping is exercised: a passer
# with no targets, a receiver with negative air yards, and a defender the
# position filter must drop without counting them missing.
SAMPLE_RECORDS = [
    _record(player_id="00-KC-QB1", position="QB", team="KC", attempts=30,
            sacks_suffered=2, carries=3, targets=0, target_share=0),
    _record(player_id="00-KC-WR1", position="WR", team="KC", targets=10,
            receiving_air_yards=120, carries=1, target_share=0.4),
    _record(player_id="00-KC-WR2", position="WR", team="KC", targets=6,
            receiving_air_yards=60, target_share=0.24),
    _record(player_id="00-KC-TE1", position="TE", team="KC", targets=7,
            receiving_air_yards=40, target_share=0.28),
    _record(player_id="00-KC-RB1", position="FB", team="KC", targets=2,
            receiving_air_yards=-5, carries=18, target_share=0.08),
    # Dropped by the position filter, but its (zero) counts still belong to
    # KC's denominators — see the adapter's docstring.
    _record(player_id="00-KC-DE1", position="DE", team="KC", target_share=0),
    _record(player_id="00-BUF-QB1", position="QB", team="BUF", attempts=25,
            sacks_suffered=3, targets=0, target_share=0),
    _record(player_id="00-BUF-WR1", position="WR", team="BUF", targets=8,
            receiving_air_yards=90, target_share=0.5),
    _record(player_id="00-BUF-RB1", position="RB", team="BUF", targets=8,
            receiving_air_yards=10, carries=20, target_share=0.5),
    # A different week of the same season. The adapter must discard it as it
    # parses — including from the denominators, or KC's bases would carry two
    # games' worth of targets and every share would be halved.
    _record(player_id="00-KC-WR1", position="WR", team="KC", week=2,
            game_id="2026_02_KC_LAC", targets=99, target_share=1.0),
]

# Retained rows and teams in SAMPLE_RECORDS: 5 KC skill players + 3 BUF, over
# two teams. Named rather than recomputed in each test so a fixture edit that
# changes them fails loudly in one place.
SAMPLE_PLAYER_ROWS = 8
SAMPLE_TEAMS = 2


def full_league_csv(*, teams: int = 32, players_per_team: int = 11) -> str:
    """A document large enough to reach `EXPECTED_FLOOR` exactly.

    The point is not size. A floor nothing can ever reach is as wrong as no
    floor at all, and only a document carrying the whole declared universe can
    show that 32 x (11 + 1) is reachable rather than aspirational.
    """
    records = []
    for team_index in range(teams):
        team = f"T{team_index:02d}"
        game = f"2026_01_{team}_OPP"
        for slot in range(players_per_team):
            records.append(
                _record(
                    player_id=f"00-{team}-{slot:02d}",
                    position="WR",
                    team=team,
                    game_id=game,
                    targets=1,
                    receiving_air_yards=10,
                    carries=1,
                    attempts=1,
                    target_share=round(1.0 / players_per_team, 6),
                )
            )
    return to_csv(records)


@pytest.fixture
def upstream():
    """Serve `SAMPLE_RECORDS` at the adapter's real URL."""
    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=to_csv(SAMPLE_RECORDS))
        )
        yield router


@pytest.fixture
def serve_upstream():
    """Serve an arbitrary document body at the adapter's real URL.

    A factory rather than a second fixture per body: the coverage tests each
    need a different document, and parameterising the mock is cheaper than
    thirteen near-identical fixtures.
    """
    def _serve(body: str, *, status: int = 200):
        router = respx.mock(assert_all_called=False)
        router.start()
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(status, text=body)
        )
        return router

    routers = []

    def factory(body: str, *, status: int = 200):
        router = _serve(body, status=status)
        routers.append(router)
        return router

    yield factory
    for router in routers:
        router.stop()


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
