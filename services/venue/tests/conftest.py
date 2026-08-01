"""Fixtures for venue's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The rest of this file builds **upstream documents**, not row dicts.
`venue_static` comes from a committed table and needs no fixture at all — that
is the point of building this collector on one — but `venue_game_assignment`
reads the nflverse game CSV, and a fixture that skipped the CSV would never
exercise the streaming read or the header validation, which is half of what an
adapter gets wrong.

`two_revision_venue` is the fixture the two headline assertions need. The
committed table carries no dated surface change (see `venue/reference.py`'s
docstring — none is sourceable today, and the `venue_single_revision_venues`
gauge says so out loud), so the append-only machinery would otherwise be tested
only against a table where every venue has exactly one revision — the shape in
which an in-place overwrite is indistinguishable from correct behaviour.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from venue import reference
from venue.adapters.upstream import SCHEDULE_URL
from venue.capture import capture_venue, reset_published_digests
from venue.main import app

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
#
# Deliberately AFTER `reference.TABLE_COMPILED_ON`: the table makes no claim
# before that date, so a suite frozen earlier would see every venue fail its
# "exactly one revision contains today" check and every assertion below would
# be measuring the wrong thing.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON = 2026
# 2026-09-13 is a Sunday. Week N's Sunday is this plus 7(N-1) days.
WEEK_ONE_SUNDAY = date(2026, 9, 13)

TEAMS: tuple[str, ...] = tuple(reference._HOME_VENUE_IDS)

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
    return (WEEK_ONE_SUNDAY + timedelta(days=7 * (week - 1))).isoformat()


def round_robin(week: int) -> list[tuple[str, str]]:
    """16 disjoint (away, home) pairs for a week, by the circle method.

    Every club appears exactly once, so a generated season reaches every one of
    the 30 home buildings rather than a lucky subset.
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
    """One upstream row. `stadium` defaults to the home club's own building.

    For a non-neutral row the stadium name is deliberately irrelevant — the
    venue resolves from `home_team` — and that asymmetry is itself carried over
    from `schedule_context.venues`: only a neutral row's stadium NAME is
    trustworthy, because its `stadium_id` describes the designated home club's
    building.
    """
    return {
        "game_id": f"{season}_{week:02d}_{away}_{home}",
        "season": str(season),
        "game_type": game_type,
        "week": str(week),
        "gameday": sunday_of(week) if gameday is None else gameday,
        "gametime": gametime,
        "away_team": away,
        "home_team": home,
        "location": location,
        "stadium": stadium or f"{home} Stadium",
    }


def season_rows(weeks: int = 17, season: int = SEASON) -> list[dict]:
    """A full round-robin season: every club plays every week.

    Home and away are swapped on even weeks so **every club hosts**, which is
    what makes this fixture reach all 30 buildings rather than a lucky 16. Half
    the assertions in this suite are bounded by "at least 30 venues", and a
    fixture that only ever visited sixteen would satisfy them by accident on a
    smaller universe.

    17 weeks x 16 games is 272 — the real regular season, and the declared
    `EXPECTED_FLOOR` for assignments — so a healthy capture against this
    fixture genuinely reaches ratio 1.0 rather than being floored short.
    """
    rows: list[dict] = []
    for week in range(1, weeks + 1):
        for away, home in round_robin(week):
            visitor, host = (away, home) if week % 2 else (home, away)
            rows.append(game_row(week=week, away=visitor, home=host, season=season))
    return rows


def to_csv(rows: list[dict]) -> str:
    """Rows to the feed's wire format, header first.

    Written by hand rather than with `csv.writer` so the header order — which
    `stream_csv_dicts` validates against `REQUIRED_COLUMNS` — is visible in
    this file rather than implied by a dict's insertion order.
    """
    lines = [",".join(COLUMNS)]
    lines.extend(",".join(row[column] for column in COLUMNS) for row in rows)
    return "\n".join(lines) + "\n"


SEASON_GAMES = 272


def season_csv(weeks: int = 17, season: int = SEASON) -> str:
    return to_csv(season_rows(weeks=weeks, season=season))


def mock_upstream(csv: str, *, status: int = 200):
    """Serve `csv` at the real upstream URL for the duration of a `with`.

    `respx`, not a monkeypatched `fetch_season_games`: the adapter's streaming
    read and its header validation are half of what this collector can get
    wrong, and a patched fetch would exercise neither.
    """
    router = respx.mock(assert_all_called=False)
    router.get(SCHEDULE_URL).mock(return_value=httpx.Response(status, text=csv))
    return router


async def run_capture(
    lake,
    *,
    csv: str | None = None,
    season: int = SEASON,
    week: int = 2,
    status: int = 200,
    now: datetime = NOW,
    **kwargs,
):
    """One capture pass against a served CSV, through the real HTTP path."""
    document = season_csv() if csv is None else csv
    with mock_upstream(document, status=status):
        async with httpx.AsyncClient() as client:
            return await capture_venue(
                season, week, client=client, lake=lake, now=now, **kwargs
            )


def rows_of(envelopes: dict, signal_type: str) -> list[dict]:
    return envelopes[signal_type].signals


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
    """`state`, `refresh_gate` and the published-digest map are process-level
    singletons — that is what lets `/signals` serve a cache, `/refresh` enforce
    a floor, and a `static reference` cadence skip an identical snapshot — so
    something has to reset them between tests.

    The digest map matters most: leave it populated and the SECOND test to run
    a capture gets `UpstreamUnchanged` from the first one's leftovers and fails
    somewhere with no relationship to what it was checking.
    """
    spec = app.state.collector_spec
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    reset_published_digests()
    yield
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    reset_published_digests()


# The surface change the committed table cannot yet source. Green Bay is used
# because it is an ordinary single-tenant club venue, so nothing else about the
# fixture is unusual.
SURFACE_CHANGE_VENUE = "lambeau"
SURFACE_CHANGE_ON = date(2026, 10, 28)
# Week 2's Sunday is 2026-09-20, before the change. Week 11's is 2026-11-22,
# after it. That pair is the whole point: the same venue, two revisions, and a
# correct capture must attribute a different surface to each.
BEFORE_CHANGE_WEEK = 2
AFTER_CHANGE_WEEK = 11
SURFACE_BEFORE = "hybrid"
SURFACE_AFTER = "synthetic_turf"


@pytest.fixture
def two_revision_venue(monkeypatch):
    """Give one venue a real mid-season surface change, append-only.

    The whole point of this collector is that a change like this produces a NEW
    revision closing the old one, and the failure it exists to prevent is an
    adapter that edits the old record instead. A table in which every venue has
    exactly one revision cannot tell those two apart — both look identical — so
    this fixture is what makes the difference observable.

    Patches `REVISIONS` rather than building a parallel table: every lookup
    (`revisions_for`, `revisions_containing`, `revision_on`) reads that dict at
    call time, so the real code path is exercised end to end, including
    `resolve_venue_id` mapping GB onto this venue.
    """
    original = reference.REVISIONS[SURFACE_CHANGE_VENUE][0].record
    assert original.surface_class == SURFACE_BEFORE, (
        "the fixture assumes the committed record's surface; update both"
    )
    changed = replace(
        original,
        effective_from=SURFACE_CHANGE_ON,
        surface_class=SURFACE_AFTER,
        surface_installed_on=SURFACE_CHANGE_ON,
    )
    history = reference.build_revisions((original, changed))
    assert len(history) == 2, "the fixture itself must produce two revisions"

    patched = dict(reference.REVISIONS)
    patched[SURFACE_CHANGE_VENUE] = history
    monkeypatch.setattr(reference, "REVISIONS", patched)
    return history


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
