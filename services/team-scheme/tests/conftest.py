"""Fixtures for team-scheme's suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.

**The three document builders produce the real wire format**, header and all —
including the columns the adapters project *away*. A fixture carrying only the
read columns cannot catch a projection bug, and `required_columns` validates
the full header, so a narrow fixture would also make a schema-drift test
vacuous. The play-by-play builder gzips, because that is what the adapter asks
for and the gzip framing is what raises `UpstreamTruncated`.

Every test that goes through `capture_team_scheme` therefore exercises the real
streaming parser, the real conditional-GET path, the real window guard and the
real coverage accounting. Only the socket is fake.
"""

import gzip
from collections.abc import Iterable, Mapping, Sequence
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

from team_scheme.adapters import ftn as ftn_adapter
from team_scheme.adapters import participation as participation_adapter
from team_scheme.adapters import pbp as pbp_adapter
from team_scheme.capture import capture_team_scheme, reset_published_digests
from team_scheme.main import app

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

SEASON = 2026

# Four teams rather than 32. The declared floor is 32 either way, so every
# fixture below deliberately reports a coverage shortfall — which is the honest
# reading of a four-team feed, and is what a floor is FOR. A fixture sized to
# the floor would make `below_expected_floor` unreachable.
TEAMS: tuple[str, ...] = ("AAA", "BBB", "CCC", "DDD")


# --------------------------------------------------------------------------
# play_by_play.csv.gz
# --------------------------------------------------------------------------

# The columns the adapter requires, plus four it does not read. The extras are
# what make the `columns=` projection observable: an adapter that stopped
# projecting would still pass every test, but one whose header check silently
# narrowed would not.
PBP_COLUMNS: tuple[str, ...] = (
    "game_id",
    "play_id",
    "season_type",
    "week",
    "posteam",
    "defteam",
    "desc",
    "epa",
    "play_type",
    "pass",
    "pass_oe",
    "shotgun",
    "no_huddle",
    "down",
    "qtr",
    "wp",
    "game_seconds_remaining",
    "drive",
    "aborted_play",
    "two_point_attempt",
    "punt_attempt",
    "field_goal_attempt",
    "fourth_down_converted",
    "fourth_down_failed",
)


def pbp_document(plays: Iterable[Mapping[str, object]]) -> bytes:
    """Rows -> a gzipped play-by-play body, as the release asset is served."""
    lines = [",".join(PBP_COLUMNS)]
    for play in plays:
        values = dict.fromkeys(PBP_COLUMNS, "")
        values.update({key: str(value) for key, value in play.items()})
        lines.append(",".join(str(values[column]) for column in PBP_COLUMNS))
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))


def team_week_plays(
    team: str,
    week: int,
    *,
    season: int = SEASON,
    plays: int = 10,
    pass_oe: float = 0.0,
    passes: int | None = None,
    shotgun: int = 0,
    no_huddle: int = 0,
    neutral: bool = True,
    start_play_id: int = 1,
    opponent: str = "OPP",
) -> list[dict]:
    """One team's snaps for one week, with a controllable PROE level.

    `pass_oe` is stamped on every play, so the week's mean PROE is exactly
    that number — which is what lets a fixture state a season's PROE as an
    exact figure rather than approximate one.
    """
    if passes is None:
        passes = plays // 2
    game_id = f"{season}_{week:02d}_{team}_{opponent}"
    rows: list[dict] = []
    for index in range(plays):
        rows.append(
            {
                "game_id": game_id,
                "play_id": start_play_id + index,
                "season_type": "REG",
                "week": week,
                "posteam": team,
                "defteam": opponent,
                "play_type": "pass" if index < passes else "run",
                "pass": 1 if index < passes else 0,
                "pass_oe": pass_oe,
                "shotgun": 1 if index < shotgun else 0,
                "no_huddle": 1 if index < no_huddle else 0,
                "down": 1,
                "qtr": 1 if neutral else 4,
                "wp": 0.5 if neutral else 0.02,
                # Descending by 25s inside one drive, so `sec_per_play_neutral`
                # has real inter-snap deltas inside the adapter's bounds.
                "game_seconds_remaining": 3600 - 25 * index,
                "drive": 1,
                "aborted_play": 0,
                "two_point_attempt": 0,
                "punt_attempt": 0,
                "field_goal_attempt": 0,
                "fourth_down_converted": 0,
                "fourth_down_failed": 0,
            }
        )
    return rows


def season_plays(
    proe_by_team_week: Mapping[str, Mapping[int, float]],
    *,
    season: int = SEASON,
    plays: int = 10,
) -> list[dict]:
    """`{team: {week: pass_oe}}` -> every play of a synthetic season."""
    rows: list[dict] = []
    play_id = 1
    for team in sorted(proe_by_team_week):
        for week in sorted(proe_by_team_week[team]):
            rows.extend(
                team_week_plays(
                    team,
                    week,
                    season=season,
                    plays=plays,
                    pass_oe=proe_by_team_week[team][week],
                    shotgun=plays // 2,
                    no_huddle=1,
                    start_play_id=play_id,
                )
            )
            play_id += plays
    return rows


def flat_proe(
    teams: Sequence[str] = TEAMS,
    *,
    weeks: int = 12,
    level: float = 1.0,
) -> dict[str, dict[int, float]]:
    """A season in which no team's PROE moves."""
    return {team: {week: level for week in range(1, weeks + 1)} for team in teams}


def proe_with_shift(
    team: str,
    *,
    at_week: int,
    shift: float,
    teams: Sequence[str] = TEAMS,
    weeks: int = 12,
    level: float = 1.0,
) -> dict[str, dict[int, float]]:
    """One team's PROE steps by `shift` at `at_week` and stays there.

    Kept from the `coaching-scheme` fixtures because it is what makes the
    **blend** visible as a number — see `test_rates_window.py`. There is no
    changepoint detector here to feed: the phase doc records that experiment's
    measured negative result and it is deliberately not rebuilt.
    """
    grid = flat_proe(teams, weeks=weeks, level=level)
    for week in range(at_week, weeks + 1):
        grid[team][week] = grid[team][week] + shift
    return grid


# --------------------------------------------------------------------------
# ftn_charting.csv and pbp_participation.csv
# --------------------------------------------------------------------------

FTN_COLUMNS: tuple[str, ...] = (
    "ftn_game_id",
    "nflverse_game_id",
    "season",
    "week",
    "ftn_play_id",
    "nflverse_play_id",
    "is_no_huddle",
    "is_motion",
    "is_play_action",
    "is_screen_pass",
    "date_pulled",
)

PARTICIPATION_COLUMNS: tuple[str, ...] = (
    "nflverse_game_id",
    "old_game_id",
    "play_id",
    "possession_team",
    "offense_formation",
    "offense_personnel",
    "defense_personnel",
    "offense_players",
    "defense_players",
)


def ftn_document(
    plays: Iterable[Mapping[str, object]],
    *,
    motion_every: int = 2,
    play_action_every: int = 5,
) -> str:
    """A charting document covering the given plays.

    Built FROM the play-by-play rows rather than independently, because the
    adapter attributes charted rows through the offensive-play index — a
    fixture whose ids did not match play-by-play's would silently produce zero
    charted plays and every rate would read null for the wrong reason.
    """
    lines = [",".join(FTN_COLUMNS)]
    for index, play in enumerate(plays):
        values = dict.fromkeys(FTN_COLUMNS, "")
        values.update(
            ftn_game_id=str(index),
            nflverse_game_id=str(play["game_id"]),
            season=str(SEASON),
            week=str(play["week"]),
            ftn_play_id=str(index),
            nflverse_play_id=str(play["play_id"]),
            is_no_huddle="FALSE",
            is_motion="TRUE" if index % motion_every == 0 else "FALSE",
            is_play_action="TRUE" if index % play_action_every == 0 else "FALSE",
            is_screen_pass="FALSE",
            date_pulled="2026-09-15T00:00:00Z",
        )
        lines.append(",".join(values[column] for column in FTN_COLUMNS))
    return "\n".join(lines) + "\n"


# Cycled across plays so `personnel_rates` has a known, non-degenerate shape:
# one snap of each of 11, 12, 21 and 13 personnel per four plays.
PERSONNEL_CYCLE: tuple[str, ...] = (
    "1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR",
    "1 C, 2 G, 1 QB, 1 RB, 2 T, 2 TE, 2 WR",
    "1 C, 2 G, 1 QB, 2 RB, 2 T, 1 TE, 2 WR",
    "1 C, 2 G, 1 QB, 1 RB, 2 T, 3 TE, 1 WR",
)


def participation_document(plays: Iterable[Mapping[str, object]]) -> str:
    """A participation document covering the given plays, personnel cycled."""
    lines = [",".join(PARTICIPATION_COLUMNS)]
    for index, play in enumerate(plays):
        values = dict.fromkeys(PARTICIPATION_COLUMNS, "")
        values.update(
            nflverse_game_id=str(play["game_id"]),
            play_id=str(play["play_id"]),
            possession_team=str(play["posteam"]),
            offense_formation="SHOTGUN",
            offense_personnel=PERSONNEL_CYCLE[index % len(PERSONNEL_CYCLE)],
            defense_personnel="4 DL, 3 LB, 4 DB",
        )
        # Quoted where the value contains a comma, exactly as upstream does.
        lines.append(
            ",".join(
                f'"{values[column]}"' if "," in values[column] else values[column]
                for column in PARTICIPATION_COLUMNS
            )
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# App-level fixtures
# --------------------------------------------------------------------------


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


class Feeds:
    """One pass's three upstream bodies, and how each responds.

    A record rather than a dozen keyword arguments on `run_capture`: every
    test that degrades one feed leaves the other two alone, and the call site
    should say only what it changed.

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
        proe: Mapping[str, Mapping[int, float]] | None = None,
        season: int = SEASON,
        plays_per_week: int = 10,
        pbp_status: int = 200,
        ftn_status: int = 200,
        participation_status: int = 200,
        etags: Mapping[str, str] | None = None,
    ) -> None:
        self.season = season
        self.proe = proe if proe is not None else flat_proe()
        self.plays = season_plays(self.proe, season=season, plays=plays_per_week)
        self.status = {
            "pbp": pbp_status,
            "ftn": ftn_status,
            "participation": participation_status,
        }
        self.etags = dict(etags or {})
        # Every request the mock saw, as `(feed, If-None-Match or None)`, so a
        # test can assert the header was actually sent rather than infer it
        # from a status code.
        self.requests: list[tuple[str, str | None]] = []

    def _responder(self, name: str, body: bytes):
        etag = self.etags.get(name)
        status = self.status[name]

        def respond(request: httpx.Request) -> httpx.Response:
            sent = request.headers.get("If-None-Match")
            self.requests.append((name, sent))
            if etag is not None and sent == etag:
                return httpx.Response(304, headers={"ETag": etag})
            headers = {"ETag": etag} if etag is not None else {}
            return httpx.Response(status, content=body, headers=headers)

        return respond

    def install(self, router) -> None:
        router.get(pbp_adapter.source_ref(self.season)).mock(
            side_effect=self._responder("pbp", pbp_document(self.plays))
        )
        router.get(ftn_adapter.source_ref(self.season)).mock(
            side_effect=self._responder("ftn", ftn_document(self.plays).encode())
        )
        router.get(participation_adapter.source_ref(self.season)).mock(
            side_effect=self._responder(
                "participation", participation_document(self.plays).encode()
            )
        )


async def run_capture(
    feeds: Feeds | None = None,
    *,
    lake: SpyLake,
    now: datetime = NOW,
    season: int = SEASON,
    week: int = 1,
    deadline: datetime | None = None,
):
    """One real capture pass against three mocked feeds.

    Goes through `stream_csv_dicts`, `conditional_stream`, the gzip inflater,
    the window guard and the digest gate. Only the socket is fake.
    """
    feeds = feeds if feeds is not None else Feeds(season=season)
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            return await capture_team_scheme(
                season,
                week,
                client=client,
                lake=lake,
                now=now,
                deadline=deadline,
            )


def rows_of(envelopes, signal_type: str) -> list[dict]:
    return list(envelopes[signal_type].signals)


def by_team(envelopes, signal_type: str) -> dict[str, dict]:
    return {row["team_id"]: row for row in rows_of(envelopes, signal_type)}
