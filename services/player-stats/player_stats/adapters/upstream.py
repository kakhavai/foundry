"""The weekly box-score upstream — the only module that knows the wire format.

Kept separate from `capture.py` so the orchestration (coverage accounting,
revision derivation, envelopes, the lake) can be tested against a fake upstream
without mocking HTTP, and so swapping the feed touches one file.

**The response is streamed and filtered as it is parsed, never materialised.**
The candidate feed is ~8.3 MB for one season (measured: `Content-Length:
8279410` on `stats_player_week_2024.csv`) and carries every week of the season
at 145 columns per row. Reading it with `response.text` would hold it three
times over — httpx's buffered bytes, the decoded `str`, and a `StringIO` copy
handed to `csv` — which is exactly what OOM-killed `roster-scope`'s pod at a
256Mi limit on its first deploy. `collector_core.streaming.stream_csv_dicts`
hands rows out one at a time, and `_qualifies` below drops the rows belonging
to other weeks or to defensive-only players before any of them is retained.

**This adapter maps counting stats and nothing else.** `rates` and
`fantasy_points` are computed downstream of normalisation, in `scoring.py`, so
that two adapters can never disagree on what a PPR point is. That is the
Phase 8 spec's adapter note, and it is the reason this file has no scoring
table in it.

Three things the feed simply does not carry, each stated rather than invented:

- **No finality marker.** The spec is explicit that an adapter must expose the
  upstream's *own* notion of finality onto `stat_state` "rather than inferring
  it from elapsed time". nflverse publishes none, so `stat_state` is `None`.
  Do not replace that with a Monday-to-Wednesday heuristic — the point of the
  field is that it reports what the upstream certified, and an inference
  dressed up as a certification is worse than an honest null.
- **No revision marker.** The spec asks an adapter to "surface a stable
  upstream revision marker if one exists". None does, so `revision` is derived
  by `revisions.py` from the append-only lake instead — see that module.
- **No snap count.** Offensive snaps live in the participation feed, which is
  `usage-share`'s upstream, not this one. `offense_snaps` is therefore `None`
  rather than `0`: a fabricated zero would read as "dressed but never played",
  which is a claim this feed cannot support.
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import httpx
from collector_core.streaming import UpstreamSchemaError, stream_csv_dicts

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake.
UPSTREAM_ADAPTER = "nflverse-player-stats"

# A `{season}` template, mirroring roster-scope's DEPTH_CHART_URL and
# player-projections' PROJECTIONS_SNAPSHOT_URL: the feed is published one asset
# per season, so a URL without the placeholder would silently pin every capture
# to whichever season was baked in. Env-overridable so a load test can point at
# a fake upstream rather than hammering a third party.
PLAYER_STATS_URL = os.getenv(
    "PLAYER_STATS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv",
)

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly with `reason=malformed`
# rather than map nulls into an append-only lake nobody rewrites.
# `UpstreamSchemaError` subclasses `ValueError`, which is what classifies it.
#
# Exactly the columns `_box_row` and `_qualifies` read — the feed carries 145
# and this collector has no use for the other 112.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
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
        "passing_yards",
        "passing_tds",
        "passing_interceptions",
        "sacks_suffered",
        "passing_air_yards",
        "passing_2pt_conversions",
        "carries",
        "rushing_yards",
        "rushing_tds",
        "rushing_first_downs",
        "rushing_2pt_conversions",
        "targets",
        "receptions",
        "receiving_yards",
        "receiving_tds",
        "receiving_air_yards",
        "receiving_yards_after_catch",
        "receiving_first_downs",
        "receiving_2pt_conversions",
        "fumbles_lost_total",
        "special_teams_tds",
        "fg_att",
        "pat_att",
    }
)

# The positions the scope's rules can ever demand — roster-scope's
# POSITION_RULES plus its kicker rule, with `FB` added because this feed lists
# some fullbacks separately while a depth chart collapses them into RB. A row
# outside this set is kept only if it shows offensive involvement, which is how
# a defensive lineman who returned a fumble for a touchdown still lands.
SCOPE_POSITIONS: frozenset[str] = frozenset({"QB", "RB", "FB", "WR", "TE", "K"})

# The columns whose sum decides whether a non-scope-position row is worth
# keeping. A deliberate *under*-approximation of the spec's "recorded at least
# one offensive snap": a player who was on the field but never involved is not
# counted, because this feed cannot see them. Widening it needs the
# participation feed, which is `usage-share`'s upstream — not a heuristic here.
INVOLVEMENT_COLUMNS: tuple[str, ...] = ("attempts", "carries", "targets")

# Kicking attempts qualify a row on their own: a kicker takes no offensive
# snap, and K is a scope position, so an offence-only test would drop a kicker
# whose `position` cell the feed left blank.
KICKING_COLUMNS: tuple[str, ...] = ("fg_att", "pat_att")


class UpstreamRowError(ValueError):
    """One row could not be mapped. Fatal to the row, never to the pass."""


@dataclass(frozen=True)
class BoxRow:
    """One upstream box score, in this collector's own counting-stat shape.

    Counting stats only. `rates` and `fantasy_points` are `scoring.py`'s, and
    `revision` is `revisions.py`'s — neither belongs to an adapter.
    """

    upstream_player_id: str
    player_name: str
    game_id: str
    team: str
    opponent: str
    position: str
    passing: dict
    rushing: dict
    receiving: dict
    misc: dict


def stats_url(season: int) -> str:
    return PLAYER_STATS_URL.format(season=season)


def source_ref(season: int, week: int) -> str | None:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Not decorative: it is what makes a lake object reproducible, and what tells
    a reader which of two feeds a row came from a season later. `week` is not
    part of it — the asset is per season, and the week is already in the
    envelope's own `scope`.
    """
    if not PLAYER_STATS_URL:
        return None
    return stats_url(season)


def _int(row: dict, column: str) -> int:
    """A counting stat as an int. Blank means zero; anything else raises.

    The feed writes an empty cell for a stat that does not apply to a position
    (a kicker has no `targets` value), which is genuinely zero. A cell carrying
    text is per-row schema drift and must not silently become 0 —
    `UpstreamRowError` turns it into one `coverage.missing` entry with a reason.
    """
    raw = (row.get(column) or "").strip()
    if not raw:
        return 0
    try:
        # `float` first: the feed writes some integral columns as `13.0`.
        return int(float(raw))
    except ValueError as exc:
        raise UpstreamRowError(f"{column}={raw!r} is not numeric") from exc


def _qualifies(row: dict, season: int, week: int) -> bool:
    """Whether a streamed row is worth retaining. Applied *during* the parse.

    Filtering afterwards would mean holding the whole season — every week, and
    every defensive-only row — in memory first, which is precisely the waste
    that OOM-killed `roster-scope`.
    """
    if (row.get("season") or "").strip() != str(season):
        return False
    if (row.get("week") or "").strip() != str(week):
        return False
    if (row.get("position") or "").strip().upper() in SCOPE_POSITIONS:
        return True
    try:
        involvement = sum(
            _int(row, column) for column in (*INVOLVEMENT_COLUMNS, *KICKING_COLUMNS)
        )
    except UpstreamRowError:
        # A row that cannot even be tested for involvement is kept, so the
        # mapping below fails it explicitly rather than dropping it silently.
        return True
    return involvement >= 1


def _box_row(row: dict) -> BoxRow:
    """One feed row -> one `BoxRow`. Raises `UpstreamRowError` on a bad row."""
    player_id = (row.get("player_id") or "").strip()
    game_id = (row.get("game_id") or "").strip()
    if not player_id or not game_id:
        raise UpstreamRowError("row carries no player_id or no game_id")

    return BoxRow(
        upstream_player_id=player_id,
        player_name=(row.get("player_display_name") or "").strip(),
        game_id=game_id,
        team=(row.get("team") or "").strip().upper(),
        opponent=(row.get("opponent_team") or "").strip().upper(),
        position=(row.get("position") or "").strip().upper(),
        passing={
            "attempts": _int(row, "attempts"),
            "completions": _int(row, "completions"),
            "yards": _int(row, "passing_yards"),
            "touchdowns": _int(row, "passing_tds"),
            "interceptions": _int(row, "passing_interceptions"),
            "sacks_taken": _int(row, "sacks_suffered"),
            "air_yards": _int(row, "passing_air_yards"),
        },
        rushing={
            "attempts": _int(row, "carries"),
            "yards": _int(row, "rushing_yards"),
            "touchdowns": _int(row, "rushing_tds"),
            "first_downs": _int(row, "rushing_first_downs"),
        },
        receiving={
            "targets": _int(row, "targets"),
            "receptions": _int(row, "receptions"),
            "yards": _int(row, "receiving_yards"),
            "touchdowns": _int(row, "receiving_tds"),
            "air_yards": _int(row, "receiving_air_yards"),
            "yards_after_catch": _int(row, "receiving_yards_after_catch"),
            "first_downs": _int(row, "receiving_first_downs"),
        },
        misc={
            "fumbles_lost": _int(row, "fumbles_lost_total"),
            "two_point_conversions": sum(
                _int(row, column)
                for column in (
                    "passing_2pt_conversions",
                    "rushing_2pt_conversions",
                    "receiving_2pt_conversions",
                )
            ),
            "return_touchdowns": _int(row, "special_teams_tds"),
        },
    )


def parse_box_rows(
    rows: Iterable[dict], season: int, week: int
) -> tuple[list[BoxRow], list[dict]]:
    """Fold already-parsed feed rows into `BoxRow`s. Convenience for tests.

    Returns the rows that mapped and one error entry per row that did not, so
    a single malformed row costs one `coverage.missing` entry rather than the
    whole pass.
    """
    kept: list[BoxRow] = []
    errors: list[dict] = []
    for row in rows:
        if not _qualifies(row, season, week):
            continue
        try:
            kept.append(_box_row(row))
        except UpstreamRowError as exc:
            errors.append(_row_error(row, exc))
    return kept, errors


def _row_error(row: dict, exc: Exception) -> dict:
    return {
        "reason": "malformed_row",
        "detail": f"{(row.get('player_id') or '?').strip()}: {exc}",
    }


async def fetch_box_rows(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    now: datetime,
) -> tuple[list[BoxRow], list[dict]]:
    """Stream the feed and fold one week out of it.

    Raises on an upstream failure rather than returning an empty list. That is
    deliberate: `capture` turns the exception into a `present: 0` envelope with
    a populated `errors` array, and an empty list would instead be recorded as
    a successful capture of nothing.

    `now` is unused — the feed is a per-season archive keyed by `week`, not a
    snapshot carrying its own capture instant — and is kept in the signature so
    this adapter matches the shape every collector's fetch has.
    """
    url = stats_url(season)
    if not url:
        raise UpstreamSchemaError("PLAYER_STATS_URL is empty")

    kept: list[BoxRow] = []
    errors: list[dict] = []
    async for row in stream_csv_dicts(client, url, required_columns=REQUIRED_COLUMNS):
        # Filtered here, one row at a time, so nothing this collector does not
        # keep is ever retained. See the module docstring.
        if not _qualifies(row, season, week):
            continue
        try:
            kept.append(_box_row(row))
        except UpstreamRowError as exc:
            errors.append(_row_error(row, exc))
    return kept, errors
