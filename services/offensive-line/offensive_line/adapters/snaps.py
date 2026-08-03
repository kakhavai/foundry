"""Snap counts: who actually played, per team per game.

**This is the feed that decides `lineup_hash`**, and it is decided here rather
than from `depth_charts` because the spec says so outright:

> `lineup_hash` has to be computed from the actual snap-weighted starters in
> the sampled games, not from a published depth chart, or continuity becomes a
> description of the team's press releases.

So this adapter answers *who played*. `depth.py` answers *which slot the label
says they occupy*. Only the first is allowed to decide membership; the second
supplies the ordering the hash is taken in, frozen per week within one pass.

--------------------------------------------------------------------------
Format, and the join key problem
--------------------------------------------------------------------------

| asset | updated | size |
|---|---|---|
| `snap_counts_2025.csv` | 2026-02-09T13:39:51Z | 2.29 MiB |
| `snap_counts_2025.csv.gz` | 2026-02-09T13:39:50Z | **0.48 MiB** |
| `snap_counts_2025.parquet` | 2026-02-09T13:39:50Z | 0.23 MiB |

Every format shares a timestamp, so the fleet rule takes the `.csv.gz`.
Verified live for this build; `snap_counts_2026.*` does **not exist** — the
2026 season has not been played — which is one of three feeds that 404 for the
configured season and is why this collector ships `CAPTURE_ENABLED=false`.

**This feed is keyed by `pfr_player_id`, not `gsis_id`**, and `depth_charts`
is keyed by `gsis_id`. There is no shared key, so the two are bridged through
`players.csv`'s `pfr_id`/`gsis_id` pair — see `players.py`. Measured on the
real 2025 regular season: **4,195 of 4,212** offensive-line snap rows (99.6%)
crosswalk successfully. A name-based join was rejected: two linemen sharing a
surname on one roster is not hypothetical, and a wrong join here silently
attributes one man's snaps to another and changes the lineup hash.

`position` here is `T`/`G`/`C`/`OL` — no sidedness. That is the other half of
why `depth_charts` is read at all.
"""

import os
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-snap-counts"

UPSTREAM_URL_TEMPLATE = os.getenv(
    "SNAP_COUNTS_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/"
    "snap_counts_{season}.csv.gz",
)

REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "season",
        "game_type",
        "week",
        "pfr_player_id",
        "position",
        "team",
        "offense_snaps",
    }
)

SEASON_TYPE = "REG"

# The `position` codes this feed uses for offensive linemen. `OL` is the
# generic one PFR falls back to and it is the single most common of the four
# (1,457 rows against T 1,363, G 925, C 668 on 2025), so dropping it as
# "unspecific" would discard a third of the unit.
LINE_POSITIONS = frozenset({"T", "G", "C", "OL"})


@dataclass(frozen=True, slots=True)
class PlayerSnaps:
    """One player's offensive snaps in one game."""

    pfr_id: str
    team: str
    game_id: str
    week: int
    position: str
    offense_snaps: int


@dataclass
class SnapFold:
    """Everything this feed contributes, already narrowed.

    `line` is only offensive linemen — 4,212 rows of 26,612 on a real season,
    filtered as they are parsed. `team_offense_snaps` is built from **every**
    row, because the denominator of `starter_snap_share` is the team's own
    offensive snap total and the man who played all of them is not always a
    lineman.
    """

    line: list[PlayerSnaps]
    team_offense_snaps: dict[tuple[str, str], int]


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read. Also the ETag cache key."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


async def fetch_snaps(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> SnapFold:
    """Offensive-line snaps through `week`, plus each team-game's snap total.

    The team total is `max(offense_snaps)` over every player in that team-game
    — the man who never left the field, who is on the offensive line in
    practice. Derived rather than read because the feed publishes no team
    total, and derived as a max rather than from `offense_pct` because that
    column is a rounded two-decimal share: back-computing a denominator from
    `snaps / pct` inflates or deflates it by up to a percent for no reason,
    and the max is exact whenever any player took every snap.

    Raises on an upstream failure rather than returning a short fold.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    line: list[PlayerSnaps] = []
    team_totals: dict[tuple[str, str], int] = {}

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=url,
        **kwargs,
    ):
        if row["game_type"].strip().upper() != SEASON_TYPE:
            continue
        week_raw = row["week"].strip()
        if not week_raw.isdigit():
            continue
        row_week = int(week_raw)
        if row_week > week:
            continue

        team = row["team"].strip()
        game_id = row["game_id"].strip()
        if not team or not game_id:
            continue

        snaps_raw = row["offense_snaps"].strip()
        try:
            snaps = int(float(snaps_raw)) if snaps_raw else 0
        except ValueError:
            continue
        if snaps <= 0:
            # No offensive snaps: a defender or a special-teamer. Contributes
            # nothing to either output and is dropped as it is parsed.
            continue

        key = (team, game_id)
        if snaps > team_totals.get(key, 0):
            team_totals[key] = snaps

        if row["position"].strip().upper() not in LINE_POSITIONS:
            continue
        pfr_id = row["pfr_player_id"].strip()
        if not pfr_id:
            continue
        line.append(
            PlayerSnaps(
                pfr_id=pfr_id,
                team=team,
                game_id=game_id,
                week=row_week,
                position=row["position"].strip().upper(),
                offense_snaps=snaps,
            )
        )

    return SnapFold(line=line, team_offense_snaps=team_totals)
