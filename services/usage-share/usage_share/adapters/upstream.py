"""The upstream adapter — the only module that knows the wire format.

Kept separate from `capture.py` so the orchestration (coverage accounting,
envelopes, the lake) can be tested against a fake upstream without mocking
HTTP, and so swapping the upstream touches one file.

**What this feed can and cannot supply, stated up front.** The candidate
upstream is nflverse's weekly player stats asset — one CSV per season, ~8.3 MB
for a completed season, carrying one row per player per game. It gives every
*opportunity* numerator this collector needs (targets, receiving air yards,
carries) plus the pass volume `team_dropbacks` is built from. It does **not**
carry offensive snap counts and it does not carry routes.

That shortfall is not papered over. The spec is explicit that *"a share
arriving without its base is rejected rather than stored"*, so `snap_share` and
`route_participation` are emitted as `null` with `denominators.team_offense_
snaps` null alongside them, and `usage_source` is `derived` — never `charted`.
A consumer can therefore tell "this collector cannot see snaps" apart from
"this player took no snaps", which a `0.0` would have hidden.

**Denominators are accumulated over EVERY row of a team**, before the position
filter narrows what is retained. Team targets, carries and air yards are
definitionally the sum over the whole roster, and a lineman who catches a
trick-play pass would otherwise shrink the denominator and inflate every other
receiver's share.

**Two memory rules, both learned the hard way.** roster-scope's first deploy was
OOMKilled at a 256Mi limit because a ~37 MB upstream document was buffered
three times over:

1. **Never hold an upstream response more than once.** `response.text` after
   `response.content`, or `json.loads(response.text)`, is two copies.
2. **Filter as you parse.** This module keeps one season-week's offensive-skill
   rows out of a whole season's several thousand, and the 32 denominator
   accumulators are fixed-size regardless of how long the season gets.

`collector_core.streaming.stream_csv_dicts` is rule 1 made reusable; the
`continue`s below are rule 2.
"""

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from collector_core.streaming import stream_csv_dicts

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake.
UPSTREAM_ADAPTER = "nflverse-participation"

# A `{season}` template, mirroring roster-scope's DEPTH_CHART_URL and
# player-projections' PROJECTIONS_SNAPSHOT_URL: the feed is published one asset
# per season, so a URL without the placeholder would silently pin every capture
# to whichever season was baked in. Env-overridable so a test, or a future
# vendor swap, needs no code change.
UPSTREAM_URL = os.getenv(
    "USAGE_UPSTREAM_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv",
)

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly rather than map nulls into
# an append-only lake that is never rewritten. `stream_csv_dicts` asserts these
# against the header BEFORE it yields a single row, so drift costs one request
# rather than a full parse. These are exactly the columns mapped below; the feed
# carries ~90 more this collector has no use for.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "player_id",
        "position",
        "season",
        "week",
        "game_id",
        "team",
        "attempts",
        "sacks_suffered",
        "carries",
        "targets",
        "receiving_air_yards",
        "target_share",
    }
)

# The positions that can record offensive usage. Kickers and team defenses are
# inside roster-scope's 416-slot universe and record none, which is why this
# collector's floor counts 11 slots per team rather than 13 — see
# capture.EXPECTED_FLOOR.
#
# Mapped rather than merely filtered: the feed writes `FB` and `HB`, and the
# platform's canonical position set is QB/RB/WR/TE/K/DST. Collapsing them here
# keeps a vendor's roster vocabulary out of every downstream consumer.
POSITION_MAP: dict[str, str] = {
    "QB": "QB",
    "RB": "RB",
    "HB": "RB",
    "FB": "RB",
    "WR": "WR",
    "TE": "TE",
}


class UpstreamRowError(ValueError):
    """One row could not be mapped. Carries a `reason` for `coverage.missing`.

    `ValueError` so `CollectorMetrics.reason_for` classifies it as `malformed`
    if it ever escapes to the pass level, but the normal path catches it per
    row: one unparseable row must not cost a whole week.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class UsageRow:
    """One player's raw opportunity counts for one game, straight off the wire.

    Deliberately counts, never shares. Every share this collector publishes is
    computed in `capture.build_signal` against an explicit denominator, so two
    adapters cannot disagree about what a target share is — the same separation
    `player-stats` applies to fantasy points.
    """

    upstream_player_id: str
    game_id: str
    team: str
    position: str
    targets: int
    air_yards: float
    carries: int
    # The feed's OWN target share, carried only as an independent cross-check.
    # Never published: it is computed against the vendor's denominator, and
    # reconciling the two is precisely how a wrong denominator is caught.
    upstream_target_share: float


@dataclass
class TeamDenominators:
    """The bases every share for this team is divided by.

    Mutable and accumulated in place while the document streams — one instance
    per team, so this is 32 small objects regardless of the feed's size.
    """

    team: str
    # This feed does not carry snap counts. `None` rather than 0: a zero base
    # would make every snap_share a division by zero or a confident 0.0, and
    # "we cannot see snaps" is not "nobody took any".
    offense_snaps: int | None = None
    dropbacks: int = 0
    targets: int = 0
    air_yards: float = 0.0
    carries: int = 0
    # Sum of the feed's own `target_share` column across the team's rows. A
    # healthy feed lands within [0.97, 1.03] of 1.0; anything else means its
    # rows and its own denominator disagree. See `capture`'s drift metric.
    upstream_target_share_sum: float = 0.0

    def to_dict(self) -> dict:
        return {
            "team_offense_snaps": self.offense_snaps,
            "team_dropbacks": self.dropbacks,
            "team_targets": self.targets,
            "team_air_yards": round(self.air_yards, 4),
            "team_carries": self.carries,
        }


@dataclass
class WeekUsage:
    """One capture's worth of upstream state: the retained rows, every team's
    denominators, and whether the stream was cut short."""

    rows: list[UsageRow] = field(default_factory=list)
    denominators: dict[str, TeamDenominators] = field(default_factory=dict)
    truncated: bool = False


def source_ref(season: int, week: int) -> str | None:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Not decorative: it is what makes a lake object reproducible, and what tells
    a reader which of two feeds a row came from a season later. `week` is not
    part of the URL — the asset is per-season and the week is a column — so it
    is accepted and ignored, keeping the signature every other adapter in the
    fleet has.
    """
    if not UPSTREAM_URL:
        return None
    return UPSTREAM_URL.format(season=season, week=week)


def _int(raw: str, column: str) -> int:
    """A count column. Empty means zero — the feed leaves a stat a player never
    recorded blank rather than writing 0."""
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError as exc:
        raise UpstreamRowError("malformed", f"{column}={raw!r}") from exc


def _float(raw: str, column: str) -> float:
    text = (raw or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError as exc:
        raise UpstreamRowError("malformed", f"{column}={raw!r}") from exc


def parse_row(record: dict[str, str]) -> UsageRow:
    """One header-keyed CSV record -> one `UsageRow`.

    Validates before it maps. A renamed column is already fatal at the header
    (`REQUIRED_COLUMNS`); this catches the per-row residue — a blank player id,
    a position the platform has no canonical form for — and refuses rather than
    writing a row keyed on nothing.
    """
    player_id = (record.get("player_id") or "").strip()
    if not player_id:
        raise UpstreamRowError("schema", "row carries no player_id")

    position = POSITION_MAP.get((record.get("position") or "").strip().upper())
    if position is None:
        raise UpstreamRowError("out_of_scope", record.get("position", ""))

    team = (record.get("team") or "").strip().upper()
    game_id = (record.get("game_id") or "").strip()
    if not team or not game_id:
        raise UpstreamRowError("schema", f"team={team!r} game_id={game_id!r}")

    return UsageRow(
        upstream_player_id=player_id,
        game_id=game_id,
        team=team,
        position=position,
        targets=_int(record.get("targets", ""), "targets"),
        air_yards=_float(record.get("receiving_air_yards", ""), "receiving_air_yards"),
        carries=_int(record.get("carries", ""), "carries"),
        upstream_target_share=_float(record.get("target_share", ""), "target_share"),
    )


def accumulate_denominators(usage: WeekUsage, record: dict[str, str]) -> None:
    """Fold one record into its team's bases.

    Called for **every** row of the scoped week, including the ones the position
    filter then discards. Team targets, carries and air yards are the sum over
    the whole roster by definition; narrowing first would shrink the denominator
    and inflate every retained player's share.
    """
    team = (record.get("team") or "").strip().upper()
    if not team:
        return
    denominators = usage.denominators.setdefault(team, TeamDenominators(team=team))
    denominators.targets += _int(record.get("targets", ""), "targets")
    denominators.air_yards += _float(
        record.get("receiving_air_yards", ""), "receiving_air_yards"
    )
    denominators.carries += _int(record.get("carries", ""), "carries")
    # A dropback is a pass attempt or a sack taken. The feed carries both per
    # passer, so summing over the team is exact rather than estimated.
    denominators.dropbacks += _int(record.get("attempts", ""), "attempts") + _int(
        record.get("sacks_suffered", ""), "sacks_suffered"
    )
    denominators.upstream_target_share_sum += _float(
        record.get("target_share", ""), "target_share"
    )


async def fetch_week_usage(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    now: datetime,
    deadline: datetime | None = None,
) -> WeekUsage:
    """Stream one season's feed, keeping only the scoped week.

    Raises on an upstream failure rather than returning an empty `WeekUsage`.
    That is deliberate: `capture` turns the exception into a `present: 0`
    envelope with a populated `errors` array, and an empty result would instead
    be recorded as a successful capture of nothing.

    A per-row mapping failure is *not* raised — it is skipped here and shows up
    as a shortfall against the declared floor. Rows the position filter rejects
    are equally not errors: this feed carries every defender and lineman in the
    league, and none of them are owed a usage row.

    `deadline` truncates rather than raising: a pass that reports itself
    truncated is useful, and one that throws away what it already parsed is
    not. `usage.truncated` is what `capture` turns into an explicit error entry.
    """
    url = source_ref(season, week)
    usage = WeekUsage()
    season_text, week_text = str(season), str(week)

    async for record in stream_csv_dicts(
        client, url, required_columns=REQUIRED_COLUMNS
    ):
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            usage.truncated = True
            break
        # Filter to the scoped week FIRST — the asset is a whole season, and
        # this is the discard that keeps the retained set at a few hundred rows
        # rather than several thousand.
        if record.get("season", "").strip() != season_text:
            continue
        if record.get("week", "").strip() != week_text:
            continue

        accumulate_denominators(usage, record)
        try:
            usage.rows.append(parse_row(record))
        except UpstreamRowError:
            # Out-of-scope positions and unmappable rows alike. Neither is a
            # missing player row: a player this collector never expected cannot
            # be missing from it.
            continue

    return usage
