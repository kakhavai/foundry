"""Depth-chart adapter — ordered player references per team, with a capture
timestamp.

A pure upstream-to-dataclass mapper. It validates the feed's columns and
normalizes the *team* label, and stops there: collapsing position labels,
breaking co-listings, and assigning ranks are resolution decisions and live
in `scope.py`, so that all three read from one place rather than being split
across a parser and a resolver that must stay in step.

Worth stating plainly, because it is the collector's central caveat: depth
ordering is not a league-published fact. Teams publish charts for media
obligations, several sort alphabetically below the starter, and the ordering
that actually predicts snap share is closer to last week's usage than to any
published chart. `depth_rank` here means "where the chart put them", not
"how good they are".
"""

import csv
import io
import os
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from ..rules import canonical_team

# A `{season}` template, mirroring `player-projections`' PROJECTIONS_SNAPSHOT_URL:
# the feed is published one file per season, so a URL without the placeholder
# would silently pin every capture to whichever season was baked in.
DEPTH_CHART_URL = os.getenv(
    "DEPTH_CHART_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "depth_charts/depth_charts_{season}.csv",
)

# Schema-drift detection, per the Phase 8 failure-handling section: an
# upstream that renames a field must fail the capture loudly rather than map
# nulls into an append-only lake. `jersey_number` and `last_updated` are
# deliberately not required — both are genuinely optional facts.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {"season", "week", "club_code", "depth_position", "depth_team", "full_name"}
)


class DepthChartSchemaError(ValueError):
    """The feed did not carry the columns the mapping depends on."""


@dataclass(frozen=True)
class DepthChartRow:
    """One row exactly as the chart published it.

    `name_raw` may still be a co-listing (`A OR B`) and `position_raw` may
    still be a media label (`SE`, `Z`). Both are resolved in `scope.py`.
    """

    team: str
    position_raw: str
    depth_order: int
    name_raw: str
    jersey_number: int | None


@dataclass(frozen=True)
class DepthChart:
    team: str
    # Freshness of *this team's* chart. Per-team rather than one number for
    # the fetch, because one team's frozen chart is invisible in an aggregate
    # freshness average — which is the failure mode the spec names.
    captured_at: datetime
    rows: tuple[DepthChartRow, ...]


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_last_updated(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_depth_chart_csv(
    text: str, *, season: int, week: int, fetched_at: datetime
) -> dict[str, DepthChart]:
    """Group the feed into one `DepthChart` per canonical team.

    A team the config does not know is dropped rather than passed through:
    its slots then read as missing, which is the correct accounting for "we
    have no chart for this team".

    `captured_at` per team is the newest `last_updated` among that team's own
    rows, falling back to `fetched_at`. Falling back to the *fetch* instant
    rather than to a sentinel is deliberate — a feed without the column is
    as fresh as the fetch, and inventing an old timestamp would make every
    team permanently stale.
    """
    reader = csv.DictReader(io.StringIO(text))
    columns = set(reader.fieldnames or ())
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise DepthChartSchemaError(
            f"depth chart feed is missing column(s): {', '.join(sorted(missing))}"
        )

    grouped: dict[str, list[DepthChartRow]] = {}
    freshest: dict[str, datetime] = {}

    for row in reader:
        if str(row.get("season", "")).strip() != str(season):
            continue
        if str(row.get("week", "")).strip() != str(week):
            continue

        team = canonical_team(row.get("club_code", ""))
        if team is None:
            continue

        depth_order = _int_or_none(row.get("depth_team"))
        if depth_order is None:
            # An unordered row cannot occupy a ranked slot. Dropping it leaves
            # the slot missing, which is visible; guessing an order would not be.
            continue

        name = (row.get("full_name") or "").strip()
        if not name:
            continue

        grouped.setdefault(team, []).append(
            DepthChartRow(
                team=team,
                position_raw=(row.get("depth_position") or "").strip(),
                depth_order=depth_order,
                name_raw=name,
                jersey_number=_int_or_none(row.get("jersey_number")),
            )
        )

        updated = _parse_last_updated(row.get("last_updated"))
        if updated is not None:
            current = freshest.get(team)
            if current is None or updated > current:
                freshest[team] = updated

    return {
        team: DepthChart(
            team=team,
            captured_at=freshest.get(team, fetched_at),
            # Sorted by published order here so `scope.py` receives a
            # deterministic sequence regardless of how the feed happened to
            # emit its rows.
            rows=tuple(sorted(rows, key=lambda r: (r.position_raw, r.depth_order))),
        )
        for team, rows in grouped.items()
    }


def depth_chart_url(season: int) -> str:
    return DEPTH_CHART_URL.format(season=season)


async def fetch_depth_charts(
    season: int, week: int, client: httpx.AsyncClient, *, now: datetime
) -> dict[str, DepthChart]:
    url = depth_chart_url(season)
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return parse_depth_chart_csv(resp.text, season=season, week=week, fetched_at=now)
