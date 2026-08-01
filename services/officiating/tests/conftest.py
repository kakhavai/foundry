"""Fixtures for officiating's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

The rest of this file builds the three upstreams **in their real wire
formats** — `games.csv`, `officials.csv` and a gzipped `play_by_play.csv` —
and serves them through `respx`. That is deliberately more work than handing
`capture` a list of parsed rows, and it is the only way three of this
collector's failure modes are reachable at all: the `old_game_id` crosswalk,
the gzip inflater, and the column projection all live between the wire and the
parsed row. A fixture that starts at the parsed row cannot see any of them.
"""

import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
import respx
from collector_core.conditional import ETAGS
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from officiating.adapters import games as games_adapter
from officiating.adapters import officials as officials_adapter
from officiating.adapters import pbp as pbp_adapter
from officiating.capture import reset_published_digests
from officiating.main import app

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

SEASON = 2026


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
    requests — so something has to reset them between tests.

    `_PUBLISHED_DIGESTS` and `ETAGS` are the same problem one layer down: both
    outlive a test, and a leftover entry makes the *next* test's capture raise
    `UpstreamUnchanged` somewhere unrelated to what it was checking.
    """
    spec = app.state.collector_spec
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    reset_published_digests()
    ETAGS.clear()
    yield
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    reset_published_digests()
    ETAGS.clear()


class SpyLake:
    """A minimal in-memory `LakeWriter`.

    `fail_write` fails every write; `fail_signal_types` fails only some. The
    second is not a refinement of the first — it is the only way to tell a
    per-signal-type digest gate from a per-pass one, because under a total
    outage the two behave identically.
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


# ---------------------------------------------------------------------------
# Synthetic upstreams, in their real wire formats
# ---------------------------------------------------------------------------

# The seven on-field positions, referee first. A crew's non-referee members are
# generated against this list so a full crew is always exactly seven — the
# shape all 272 games of the real 2025 regular season have.
CREW_POSITIONS = (
    "Referee",
    "Umpire",
    "Down Judge",
    "Line Judge",
    "Field Judge",
    "Side Judge",
    "Back Judge",
)

# One ordinary game's penalties, as `(penalty_type, yards)` pairs. Spelled out
# rather than summarised so a test can state the totals independently of the
# builder: 5 penalties, 45 yards, 1 DPI worth 15, 2 offensive holdings, 1
# defensive holding. A fixture that carried the totals directly would make
# every aggregation assertion tautological.
DEFAULT_PENALTIES = (
    ("Offensive Holding", 10.0),
    ("False Start", 5.0),
    ("Defensive Pass Interference", 15.0),
    ("Defensive Holding", 5.0),
    ("Offensive Holding", 10.0),
)


@dataclass
class FakeGame:
    """One game, in enough detail to render all three feeds consistently."""

    game_id: str
    legacy_game_id: str
    week: int
    referee_id: str
    referee_name: str
    # `(official_id, name, position)` for the six non-referee on-field
    # officials. Overridden per game to inject a substitute.
    members: tuple[tuple[str, str, str], ...] = ()
    penalties: tuple[tuple[str, float], ...] = DEFAULT_PENALTIES
    offensive_plays: int = 120
    # What `games.csv` claims the referee is called. Defaults to agreeing with
    # `officials.csv`; a test overrides it to exercise the cross-check.
    schedule_referee: str | None = None
    in_pbp: bool = True
    positions: tuple[str, ...] = field(default=CREW_POSITIONS[1:])

    def crew(self) -> tuple[tuple[str, str, str], ...]:
        if self.members:
            return self.members
        return tuple(
            (f"{self.referee_id}{index}", f"Official {self.referee_id}{index}", pos)
            for index, pos in enumerate(self.positions)
        )


def crew_penalties(crew: int, week: int) -> tuple[tuple[str, float], ...]:
    """One game's penalties, varying by crew and by week.

    **Crews must differ, and each crew must vary week to week**, or the whole
    rate pipeline degenerates in a way that quietly disables the tests built on
    top of it. With every game identical, the pooled within-crew variance and
    the between-crew variance are both exactly zero, so no rate is shrunk, no
    correlation is measurable, and *every* rate is refused. A fixture like that
    passes any assertion of the form "nothing was served" without the code
    under test doing anything at all.

    So: crew index drives the level (between-crew signal), week parity drives
    a swing around it (within-crew noise).
    """
    extra_flags = crew + (2 if week % 2 == 0 else 0)
    return (
        *DEFAULT_PENALTIES,
        *(("False Start", 5.0) for _ in range(extra_flags)),
    )


def season_of(
    *,
    crews: int = 8,
    weeks: int = 6,
    season: int = SEASON,
    played_weeks: int | None = None,
) -> list[FakeGame]:
    """A synthetic season: `crews` crews each working one game a week.

    Eight crews rather than four, because the Fisher-z interval needs `n - 3`
    in a denominator and four crews cannot distinguish any correlation from
    zero. Six weeks so the odd/even split has three games a side.
    """
    games: list[FakeGame] = []
    for week in range(1, weeks + 1):
        for crew in range(1, crews + 1):
            games.append(
                FakeGame(
                    game_id=f"{season}_{week:02d}_C{crew}_H{crew}",
                    legacy_game_id=f"{season}{week:02d}{crew:03d}",
                    week=week,
                    # Numeric, like a real `official_id` — `crew_id`'s
                    # committed pattern is `^[0-9]{4}-ref[0-9]+$`, so a
                    # non-numeric fixture id would test a shape the schema
                    # forbids.
                    referee_id=str(700 + crew),
                    referee_name=f"Ref Number{crew}",
                    penalties=crew_penalties(crew, week),
                    offensive_plays=110 + crew + (5 if week % 2 else 0),
                )
            )
    return games


def games_csv(games: list[FakeGame], *, season: int = SEASON) -> str:
    """`games.csv` as nflverse serves it — the columns the adapter reads, plus
    two it does not, so the projection has something to drop."""
    header = (
        "game_id,season,game_type,week,gameday,away_team,home_team,"
        "old_game_id,referee,stadium\n"
    )
    rows = "".join(
        f"{g.game_id},{season},REG,{g.week},{season}-09-{g.week:02d},"
        f"C{g.week},H{g.week},{g.legacy_game_id},"
        f"{g.schedule_referee if g.schedule_referee is not None else g.referee_name},"
        f"Some Stadium\n"
        for g in games
    )
    return header + rows


def officials_csv(games: list[FakeGame], *, season: int = SEASON) -> str:
    """`officials.csv` as nflverse serves it, keyed by the LEGACY game id.

    A replay official is emitted for every game. It is not part of the on-field
    crew and the adapter must drop it — with it counted, every crew is eight
    people and continuity is computed over a member who throws no flags.
    """
    header = (
        "game_id,game_key,official_name,position,jersey_number,"
        "official_id,season,season_type,week\n"
    )
    lines: list[str] = []
    for game in games:
        lines.append(
            f"{game.legacy_game_id},9999,{game.referee_name},Referee,1,"
            f"{game.referee_id},{season},REG,{game.week}\n"
        )
        for official_id, name, position in game.crew():
            lines.append(
                f"{game.legacy_game_id},9999,{name},{position},2,"
                f"{official_id},{season},REG,{game.week}\n"
            )
        lines.append(
            f"{game.legacy_game_id},9999,Replay Person,Replay Official,3,"
            f"replay-{game.referee_id},{season},REG,{game.week}\n"
        )
    return header + "".join(lines)


def pbp_csv(games: list[FakeGame]) -> str:
    """`play_by_play_<season>.csv` as nflverse serves it, abridged.

    Seven columns rather than 372, but including `desc` — a wide column the
    adapter never reads — so the projection is exercised rather than assumed.
    Offensive snaps and penalties are emitted as separate rows, which is
    legitimate: a penalty on a `no_play` is exactly how the real feed records
    an enforced pre-snap foul.
    """
    header = (
        "play_id,game_id,season_type,week,play_type,penalty,penalty_type,"
        "penalty_yards,desc\n"
    )
    lines: list[str] = []
    play_id = 0
    for game in games:
        if not game.in_pbp:
            continue
        for _ in range(game.offensive_plays):
            play_id += 1
            lines.append(
                f"{play_id},{game.game_id},REG,{game.week},pass,0,,,"
                f"a fairly long play description\n"
            )
        for penalty_type, yards in game.penalties:
            play_id += 1
            lines.append(
                f"{play_id},{game.game_id},REG,{game.week},no_play,1,"
                f"{penalty_type},{yards},a fairly long play description\n"
            )
    return header + "".join(lines)


def gzipped(text: str) -> bytes:
    """A real gzip container, the way a release asset serves it."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return compressor.compress(text.encode("utf-8")) + compressor.flush()


def mock_upstreams(
    games: list[FakeGame],
    *,
    season: int = SEASON,
    games_response=None,
    officials_response=None,
    pbp_response=None,
) -> None:
    """Route all three upstreams at their real URLs. Call inside `respx.mock`.

    Each feed's response can be overridden outright, which is how the failure
    and degraded paths are driven: it is the difference between "play-by-play
    404s" and "play-by-play is empty", and those must not be the same test.
    """
    _register(
        respx,
        games,
        season=season,
        games_response=games_response,
        officials_response=officials_response,
        pbp_response=pbp_response,
    )


def upstream_router(games: list[FakeGame], *, season: int = SEASON, **overrides):
    """The same three upstreams, as a `with`-able router.

    `POST /refresh` returns 202 and fetches AFTERWARDS, in a background task,
    so a mock that closed at the POST would let the dispatched capture reach
    the real nflverse feeds on every test run. The route tests therefore hold
    this open across the polling loop, not just the request.
    """
    router = respx.mock(assert_all_called=False)
    _register(router, games, season=season, **overrides)
    return router


def _register(
    target,
    games: list[FakeGame],
    *,
    season: int,
    games_response=None,
    officials_response=None,
    pbp_response=None,
) -> None:
    import httpx

    target.get(games_adapter.UPSTREAM_URL).mock(
        return_value=games_response
        or httpx.Response(200, text=games_csv(games, season=season))
    )
    target.get(officials_adapter.UPSTREAM_URL).mock(
        return_value=officials_response
        or httpx.Response(200, text=officials_csv(games, season=season))
    )
    target.get(pbp_adapter.source_ref(season)).mock(
        return_value=pbp_response
        or httpx.Response(200, content=gzipped(pbp_csv(games)))
    )
