"""Fixtures for schedule-context's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The other half of this file builds **upstream documents**, not row dicts. The
collector's whole job is turning one CSV into a per-team season chain, so a
fixture that skipped the CSV would test the half that cannot break. `season_csv`
generates a real round-robin — 32 clubs, 16 games a week, genuine Sunday
kickoffs in the feed's own Eastern wall-clock — and individual tests override
single rows to make a short week, a bye, a road trip or a neutral site.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from schedule_context.adapters.upstream import UPSTREAM_URL
from schedule_context.capture import capture_schedule_context
from schedule_context.main import app
from schedule_context.venues import TEAM_VENUES

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON = 2026
# 2026-09-13 is a Sunday. Week N's Sunday is this plus 7(N-1) days.
WEEK_ONE_SUNDAY = "2026-09-13"

TEAMS: tuple[str, ...] = tuple(TEAM_VENUES)

COLUMNS = (
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "gametime",
    "away_team",
    "home_team",
    "location",
    "stadium",
)


def sunday_of(week: int) -> str:
    """The gameday string for a week's Sunday, in the feed's date format."""
    start = datetime.strptime(WEEK_ONE_SUNDAY, "%Y-%m-%d")
    return (start + timedelta(days=7 * (week - 1))).strftime("%Y-%m-%d")


def round_robin(week: int) -> list[tuple[str, str]]:
    """16 disjoint (away, home) pairs for a week, by the circle method.

    Every club appears exactly once, which is what makes a generated week
    reach the declared coverage floor of 32 team-records.
    """
    fixed, rotating = TEAMS[0], list(TEAMS[1:])
    shift = (week - 1) % len(rotating)
    rotating = rotating[shift:] + rotating[:shift]
    pairs = [(fixed, rotating[0])]
    for index in range(1, len(TEAMS) // 2):
        pairs.append((rotating[index], rotating[-index]))
    return pairs


def game_row(
    *,
    week: int,
    away: str,
    home: str,
    gameday: str | None = None,
    gametime: str = "13:00",
    location: str = "Home",
    stadium: str | None = None,
    game_type: str = "REG",
    season: int = SEASON,
) -> dict:
    """One upstream row. `stadium` defaults to the home club's own building."""
    return {
        "game_id": f"{season}_{week:02d}_{away}_{home}",
        "season": str(season),
        "game_type": game_type,
        "week": str(week),
        "gameday": gameday or sunday_of(week),
        "gametime": gametime,
        "away_team": away,
        "home_team": home,
        "location": location,
        "stadium": stadium or TEAM_VENUES[home].name,
    }


def season_rows(weeks: int = 3, season: int = SEASON) -> list[dict]:
    """A full round-robin season: every club plays every week."""
    return [
        game_row(week=week, away=away, home=home, season=season)
        for week in range(1, weeks + 1)
        for away, home in round_robin(week)
    ]


def to_csv(rows: list[dict]) -> str:
    """Rows to the feed's wire format, header first.

    Written by hand rather than with `csv.writer` so the header order — which
    `stream_csv_dicts` validates against `REQUIRED_COLUMNS` — is visible in
    this file rather than implied by a dict's insertion order.
    """
    lines = [",".join(COLUMNS)]
    lines.extend(",".join(row[column] for column in COLUMNS) for row in rows)
    return "\n".join(lines) + "\n"


def season_csv(weeks: int = 3, season: int = SEASON) -> str:
    return to_csv(season_rows(weeks=weeks, season=season))


def mock_upstream(csv: str, *, status: int = 200):
    """Serve `csv` at the real upstream URL for the duration of a `with`.

    `respx`, not a monkeypatched `fetch_season_games`: the adapter's streaming
    read and its header validation are half of what this collector does wrong
    most easily, and a patched fetch would never exercise either.
    """
    router = respx.mock(assert_all_called=False)
    router.get(UPSTREAM_URL).mock(return_value=httpx.Response(status, text=csv))
    return router


async def run_capture(
    lake,
    *,
    csv: str | None = None,
    season: int = SEASON,
    week: int = 2,
    status: int = 200,
    **kwargs,
):
    """One capture pass against a served CSV, through the real HTTP path."""
    document = season_csv() if csv is None else csv
    with mock_upstream(document, status=status):
        async with httpx.AsyncClient() as client:
            return await capture_schedule_context(
                season, week, client=client, lake=lake, now=NOW, **kwargs
            )


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
