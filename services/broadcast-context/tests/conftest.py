"""Fixtures for broadcast-context's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The CSV builders below produce the **real** wire format — the nflverse game
table's header, with the six columns the adapter reads carrying real values —
so every test that goes through `capture_broadcast_context` exercises the real
streaming parser, the real conditional-GET path and the real classifier rather
than a mock of them.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.conditional import ETAGS
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from broadcast_context.adapters.upstream import UPSTREAM_URL
from broadcast_context.capture import capture_broadcast_context, reset_published_digests
from broadcast_context.main import app

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON = 2026

# The feed's real header. Deliberately the whole thing rather than only the six
# columns the adapter reads: `required_columns` validates the full header, and
# a fixture carrying only the read columns could not catch a projection bug.
FEED_HEADER = (
    "game_id,season,game_type,week,gameday,weekday,gametime,away_team,"
    "away_score,home_team,home_score,location,result,total,overtime,"
    "old_game_id,gsis,nfl_detail_id,pfr,pff,espn,ftn,away_rest,home_rest,"
    "away_moneyline,home_moneyline,spread_line,away_spread_odds,"
    "home_spread_odds,total_line,under_odds,over_odds,div_game,roof,surface,"
    "temp,wind,away_qb_id,home_qb_id,away_qb_name,home_qb_name,away_coach,"
    "home_coach,referee,stadium_id,stadium"
)
_FEED_COLUMNS = FEED_HEADER.split(",")


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


def feed_row(
    game_id: str,
    *,
    season: int = SEASON,
    game_type: str = "REG",
    week: int = 1,
    gameday: str = "2026-09-13",
    gametime: str = "13:00",
) -> str:
    """One line of the feed. Columns the adapter never reads are left empty."""
    values = dict.fromkeys(_FEED_COLUMNS, "")
    values.update(
        game_id=game_id,
        season=str(season),
        game_type=game_type,
        week=str(week),
        gameday=gameday,
        gametime=gametime,
    )
    return ",".join(values[column] for column in _FEED_COLUMNS)


def feed_document(rows: Iterable[str]) -> str:
    return "\n".join([FEED_HEADER, *rows]) + "\n"


# The Sunday each fixture week is built around. Week 1 is 2026-09-13; no week
# below lands on Thanksgiving, the Friday after it, or Christmas, so the
# holiday branch is exercised only by the tests that mean to.
_WEEK_ONE_SUNDAY = datetime(2026, 9, 13, tzinfo=UTC).date()


def week_rows(
    week: int,
    *,
    season: int = SEASON,
    games: int = 16,
    drop_kickoff_for: int = 0,
) -> list[str]:
    """One realistic week: an early block, a late pair, an SNF and an MNF.

    `drop_kickoff_for` blanks that many games' `gametime`, which is how the
    feed represents a game whose slot is not yet decided — and the input the
    incomplete-slate guard exists for.
    """
    sunday = _WEEK_ONE_SUNDAY + timedelta(days=7 * (week - 1))
    monday = sunday + timedelta(days=1)

    slots: list[tuple[str, str]] = []
    slots += [(sunday.isoformat(), "13:00")] * (games - 4)
    slots += [(sunday.isoformat(), "16:25")] * 2
    slots.append((sunday.isoformat(), "20:20"))
    slots.append((monday.isoformat(), "20:15"))

    rows = []
    for index, (gameday, gametime) in enumerate(slots):
        rows.append(
            feed_row(
                f"{season}_{week:02d}_A{index:02d}_B{index:02d}",
                season=season,
                week=week,
                gameday=gameday,
                gametime="" if index < drop_kickoff_for else gametime,
            )
        )
    return rows


def season_rows(
    *,
    season: int = SEASON,
    weeks: int = 17,
    games_per_week: int = 16,
) -> list[str]:
    """A whole scheduled season: 17 x 16 = 272 games, the declared floor."""
    return [
        row
        for week in range(1, weeks + 1)
        for row in week_rows(week, season=season, games=games_per_week)
    ]


async def run_capture(
    document: str | None,
    *,
    lake: SpyLake,
    now: datetime = NOW,
    season: int = SEASON,
    week: int = 1,
    deadline: datetime | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
):
    """One real capture pass against a mocked feed.

    Goes through `stream_csv_dicts` and `conditional_stream`, so the streaming
    parser, the header check and the `304` branch are all real. Only the
    socket is fake.
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                status, text=document or "", headers=headers or {}
            )
        )
        async with httpx.AsyncClient() as client:
            return await capture_broadcast_context(
                season,
                week,
                client=client,
                lake=lake,
                now=now,
                deadline=deadline,
            )


def rows_of(envelopes) -> list[dict]:
    """The one signal type's rows, keyed by game_id for whole-row comparison."""
    from broadcast_context.capture import SIGNAL

    return list(envelopes[SIGNAL].signals)


def by_game(envelopes) -> dict[str, dict]:
    return {row["game_id"]: row for row in rows_of(envelopes)}


def snapshot(
    lake: SpyLake,
    rows: Sequence[dict],
    *,
    captured_at: datetime,
    season: int = SEASON,
    week: int = 1,
    collector: str = "broadcast-context",
    signal_type: str = "game_broadcast_window",
) -> None:
    """Plant a prior lake snapshot, the way an earlier pass would have.

    Used by the history tests so a "second capture" has a genuine first one to
    compare against without running two passes end to end.
    """
    envelope = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=collector,
        signal_type=signal_type,
        captured_at=captured_at,
        upstream=Upstream(adapter="nflverse-games", fetched_at=captured_at),
        scope={"season": season, "week": week},
        coverage=Coverage(expected=len(rows), present=len(rows)),
        errors=[],
        signals=list(rows),
    )
    lake.write(envelope)
