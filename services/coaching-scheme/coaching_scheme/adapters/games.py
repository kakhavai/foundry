"""The schedule feed — the season/week grid and the head coach of record.

`games.csv` is nfldata's master schedule. It is the only free feed in this
collector's set that names a coach at all, and it names exactly one: the head
coach, per game, per side.

**Measured: 0.49 MiB on the wire** (2.07 MiB parsed —
`raw.githubusercontent.com` transfer-encodes it, so httpx's `Accept-Encoding:
gzip` gets the compressed body and inflates it). Cheapest of the four upstreams
by a factor of forty.

--------------------------------------------------------------------------
The finding that shapes this whole collector
--------------------------------------------------------------------------

`away_coach`/`home_coach` **do** record mid-season head-coach changes — but
only for seasons nfldata has back-filled. Measured live on 2026-08-01 against
the current file:

| season | teams with a mid-season head-coach change in the feed |
|---|---|
| 2022 | 3 — CAR wk6 Rhule->Wilks, IND wk10 Reich->Saturday, DEN wk17 |
| 2023 | 3 — LV wk9 McDaniels->Pierce, CAR wk13, LAC wk16 |
| 2024 | **0** |
| 2025 | **0** |

2024 and 2025 each had at least three real ones. NYJ fired Robert Saleh after
week 5 of 2024; the feed carries `Robert Saleh` for all seventeen NYJ games.
Same for NO (Dennis Allen, fired week 9), CHI (Matt Eberflus, week 13) and TEN
in 2025 (Brian Callahan, week 6). The column is populated forward from the
season opener and reconciled later, if at all.

So on a **live** season this feed reports one revision per team and will miss
the change the collector exists to detect. That is not a reason to distrust the
collector; it is the reason `changepoint.py` exists and is not optional. The
staff feed is a *hypothesis* about when the regime changed, and the PROE series
is an independent test of it. Where they disagree on a live season, the feed is
the one that is usually wrong.

`metrics.CoachingSchemeMetrics.staff_revisions` makes the consequence legible:
a season sitting at exactly 32 revisions is the feed's un-backfilled state, not
a quiet year.
"""

import os
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nfldata-games"

# Not season-templated: one file holds every season, which is why its ETag key
# is the bare URL and why `ETagStore` stays at one entry for this feed forever.
UPSTREAM_URL = os.getenv(
    "GAMES_URL",
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
)

REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "season",
        "game_type",
        "week",
        "away_team",
        "home_team",
        "away_coach",
        "home_coach",
    }
)

# Regular season only. A postseason game's coach is the same person, and
# admitting it would extend a revision's week span past the regular-season
# grid that `effective_from_week` is defined on.
SEASON_TYPE = "REG"


@dataclass(frozen=True)
class TeamWeekCoach:
    """One team's head coach of record for one week, as the feed states it."""

    team_id: str
    week: int
    head_coach_name: str


def source_ref() -> str:
    """The exact upstream artifact this pass read, recorded in the envelope."""
    return UPSTREAM_URL


async def fetch_team_week_coaches(
    season: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> list[TeamWeekCoach]:
    """Every (team, week, head coach) the feed states for one season.

    Filtered as the rows stream past — the file carries every season back to
    1999, and materialising it to select one is the memory rule this library
    exists to enforce.

    Raises on an upstream failure rather than returning an empty list. An empty
    list is a legitimate answer for a season the feed has not published yet,
    and it must not be how a 404 is reported: without the grid there is no team
    universe, no revision and no week span, so `capture` ends the pass.

    `etag_store` is the escape hatch `capture` uses to force an unconditional
    re-fetch when *another* feed changed and this one 304'd — see
    `capture._refetch_unconditionally`. Left `None` it uses the shared store.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    rows: list[TeamWeekCoach] = []
    season_str = str(season)

    async for row in stream_csv_dicts(
        client,
        UPSTREAM_URL,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        etag_key=UPSTREAM_URL,
        **kwargs,
    ):
        if row["season"] != season_str or row["game_type"].strip() != SEASON_TYPE:
            continue
        week_raw = row["week"].strip()
        if not week_raw.isdigit():
            continue
        week = int(week_raw)
        for team_key, coach_key in (
            ("home_team", "home_coach"),
            ("away_team", "away_coach"),
        ):
            team = row[team_key].strip()
            coach = row[coach_key].strip()
            if not team:
                continue
            rows.append(TeamWeekCoach(team, week, coach))
    return rows
