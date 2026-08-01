"""The upstream adapters — the only modules that know a wire format.

`durability-history` reads **five** nflverse feeds. They are different in kind,
different in size and different in how often they move, and every one of them
is read through `stream_csv_dicts` with conditional GET on, filtering as it
parses. Keeping them here means `capture.py` never sees a column name.

| Upstream | Size, measured 2026-08-01 | Shape | Moves |
|---|---|---|---|
| `games.csv` | 2.17 MB | one file, 1999-present | weekly, results settle |
| `players.csv` | 7.32 MB | one file | listed position/team, weekly-ish |
| `injuries_<n>.csv` | 0.70-0.82 MB *each* | one per season | weekly, in season |
| `snap_counts_<n>.csv` | 2.40 MB *each* | one per season | weekly, in season |
| `stats_player_week_<n>.csv` | 8.28 MB *each* | one per season | weekly, in season |

At the default three-season window that is **43.8 MB on a cold process** and
approximately **zero on every pass after it**: all five assets carry an `ETag`
and all five answer `If-None-Match` with a `304` carrying no body (verified
against the live endpoints on 2026-08-01, `injuries_2024.csv` included). A
season that has ended is immutable, so its three per-season files 304 forever.

**Why the window exists at all.** The spec says "career-to-date", and nflverse
publishes injuries back to 2009. A genuine career sweep is ~18 seasons x 11.4 MB
= ~205 MB per cold process, which is not a cost this collector may impose for a
field nobody has yet asked to be exact. So the window is bounded, and — this is
the half that matters — **every row says so**: `observation_window_first_season`
names where the count starts and `career_history_complete` is `false` whenever a
player's tenure predates it. `player-profile`'s `career_snaps_complete` is the
same decision for the same reason. A truncated total labelled complete is a
well-formed number that is silently wrong.

**Order matters, for memory and for cost.** `capture.py` resolves scope and
identity first, then reads `players.csv`, then resolves the ~380 scope slots to
their `gsis_id`/`pfr_id` join keys — and only then reads the three per-season
feeds, each filtered *as it streams* to those few hundred ids. The 8.28 MB
weekly-stats file is ~5,000 players wide and this collector keeps ~380 of them;
materialising the rest first is the `roster-scope` OOM in miniature.

**A `304` does not escape this module.** `depth-chart`'s route-1 pattern lets an
unchanged upstream abort the pass, which is right for a collector with one feed.
This one has five on four different republication cadences, and an unchanged
`players.csv` says nothing about whether this week's injury report moved. Each
reader falls back to this process's memo instead, exactly as `player-profile`
does, and `capture.py` keeps the "publish nothing when nothing changed" half
through a content digest.
"""

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import httpx
from collector_core.conditional import ETAGS, ETagStore, UpstreamUnchanged
from collector_core.streaming import stream_csv_dicts

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HISTORY_SEASONS",
    "GAMES_URL",
    "INJURIES_URL",
    "PLAYERS_URL",
    "RECURRENCE_RULE_VERSION",
    "RECURRENCE_WINDOW_DAYS",
    "SNAP_COUNTS_URL",
    "STATS_URL",
    "UPSTREAM_ADAPTER",
    "Designations",
    "DesignationRow",
    "GameRef",
    "Participation",
    "PlayerRow",
    "Production",
    "Schedule",
    "fetch_designations",
    "fetch_participation",
    "fetch_players",
    "fetch_production",
    "fetch_schedule",
    "history_seasons",
    "normalize_position",
    "reset_upstream_memo",
    "source_ref",
]

# ── the recurrence rule, and why it lives in `upstream.adapter` ──────────────
#
# The spec is explicit that deciding a hamstring strain 26 days after the last
# one is a re-aggravation rather than a novel injury "requires a documented,
# versioned rule ... and that rule must be emitted in `upstream.adapter` so a
# change in the rule is distinguishable from a change in the player."
#
# Without it, widening the window from 90 to 120 days rewrites
# `is_recurrence_of` on thousands of historical events at once, and a consumer
# diffing two lake objects sees every player's history change simultaneously
# with nothing in either object to say why. With it, the two objects disagree
# about `upstream.adapter` first, which is a one-line diff rather than an
# investigation.
#
# The rule: two events at the same **`injury_site`** are linked when the later
# one's `onset_date` falls within `RECURRENCE_WINDOW_DAYS` of the earlier one's
# RETURN — its `resolved_date`, falling back to its `onset_date` while it is
# still unresolved. Anchored on the return rather than the onset because a
# re-aggravation is measured from when the tissue was last asked to work again;
# anchoring on onset would make a long absence read as a recurrence of itself.
#
# **`injury_site`, not the spec's `body_part`, and the LABEL BELOW SAYS SO.**
# The spec's enum is ten values wide and has no member for a calf, a quad or an
# oblique, so all three collapse to `other` — and keying on it would link a Week
# 3 calf strain to a Week 8 quad strain as one re-aggravated tissue. `events.py`
# section 3 carries the full reasoning; `injury_site` is published on every
# event so `is_recurrence_of` stays reproducible from the row.
#
# The label has to describe the rule the code actually runs. The spec's whole
# reason for demanding the rule here is that a consumer can tell a rule change
# from a player change, and a consumer reproducing `is_recurrence_of` from a
# label that said `same_body_part` would compute different answers and conclude
# the players changed.
RECURRENCE_RULE_VERSION = "v1"
RECURRENCE_WINDOW_DAYS = int(os.getenv("RECURRENCE_WINDOW_DAYS", "90"))

# Names the upstream in every envelope's `upstream.adapter`. One publisher and
# one release train across all five feeds, so one label — carrying the
# recurrence rule as a machine-readable suffix, per the spec. `source_ref`
# records the exact artifacts a pass read, which is what a consumer joins on.
UPSTREAM_ADAPTER = (
    f"nflverse-injury-tables;recurrence={RECURRENCE_RULE_VERSION}:"
    f"same_injury_site_within_{RECURRENCE_WINDOW_DAYS}d_of_return"
)

# Deliberately the SAME variable name `venue` and `schedule-context` use: three
# collectors reading one feed should be redirectable together.
GAMES_URL = os.getenv(
    "SCHEDULE_URL",
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
)
PLAYERS_URL = os.getenv(
    "PLAYERS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv",
)
INJURIES_URL = os.getenv(
    "INJURIES_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/injuries/"
    "injuries_{season}.csv",
)
SNAP_COUNTS_URL = os.getenv(
    "SNAP_COUNTS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/"
    "snap_counts_{season}.csv",
)
# The same asset `usage-share` and `player-stats` read, under this collector's
# own variable name so it can be pointed elsewhere independently.
STATS_URL = os.getenv(
    "PLAYER_STATS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.csv",
)

# How many seasons back the history window reaches, INCLUDING the scoped one.
# Three is a cost decision, not a modelling one — see the module docstring.
DEFAULT_HISTORY_SEASONS = int(os.getenv("DURABILITY_HISTORY_SEASONS", "3"))

# nflverse publishes injury tables from 2009. Reaching below it produces 404s,
# which cost a request each and would be recorded as missing seasons forever.
FIRST_AVAILABLE_SEASON = int(os.getenv("DURABILITY_FIRST_SEASON", "2009"))

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly with `reason=malformed`
# rather than map nulls into an append-only lake nobody rewrites. Exactly the
# columns each mapping below reads and no more, so an unrelated column
# disappearing upstream does not fail a capture that never used it.
GAMES_COLUMNS = frozenset(
    {"game_id", "season", "game_type", "week", "gameday", "home_team", "away_team",
     "result"}
)  # fmt: skip
PLAYERS_COLUMNS = frozenset(
    {"gsis_id", "pfr_id", "display_name", "birth_date", "position", "latest_team",
     "jersey_number", "rookie_season", "last_season"}
)  # fmt: skip
INJURIES_COLUMNS = frozenset(
    {"season", "game_type", "team", "week", "gsis_id", "position",
     "report_primary_injury", "report_secondary_injury", "report_status",
     "practice_primary_injury", "practice_secondary_injury", "practice_status",
     "date_modified"}
)  # fmt: skip
SNAP_COUNTS_COLUMNS = frozenset(
    {"season", "game_type", "week", "pfr_player_id", "team", "offense_snaps",
     "offense_pct"}
)  # fmt: skip
STATS_COLUMNS = frozenset({"player_id", "season", "week", "fantasy_points_ppr"})

# The canonical positions this collector carries. Deliberately the same
# offensive subset `roster_scope.rules.POSITION_ALIASES` mints its scope from
# and `player-profile` narrows with: a position this collector recognised and
# roster-scope did not could never be in scope anyway, and one roster-scope
# recognised and this did not would be a silent hole.
POSITION_ALIASES: dict[str, str] = {
    "QB": "QB", "RB": "RB", "HB": "RB", "TB": "RB",
    "WR": "WR", "SE": "WR", "FL": "WR", "SLOT": "WR", "X": "WR", "Z": "WR",
    "TE": "TE", "Y": "TE", "K": "K", "PK": "K",
}  # fmt: skip


def normalize_position(raw: str) -> str | None:
    """An upstream position label to the canonical one, or `None`.

    `None` rather than the raw string: a position this collector cannot place
    must not reach the same-position availability cohort, which would otherwise
    rank a defensive lineman against quarterbacks.
    """
    return POSITION_ALIASES.get((raw or "").strip().upper())


def history_seasons(season: int, *, span: int = DEFAULT_HISTORY_SEASONS) -> list[int]:
    """The seasons the window covers, oldest first.

    Clamped at `FIRST_AVAILABLE_SEASON` so a large `span` cannot spend requests
    on years nflverse has never published.
    """
    if span < 1:
        raise ValueError(f"span must be at least 1, got {span}")
    first = max(FIRST_AVAILABLE_SEASON, season - span + 1)
    return list(range(first, season + 1))


def _int(value: str | None) -> int | None:
    """A CSV cell to an int, or `None` for blank/unparseable. Never 0.

    A zero and a missing value are different facts throughout this collector: 0
    games missed is a measurement, a blank is not, and a fabricated 0 flows
    straight into `availability_rate` as a real reading.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _date(value: str | None) -> date | None:
    """The leading `YYYY-MM-DD` of a cell, or `None`.

    Sliced rather than parsed whole because `date_modified` on the injury feed
    is a full RFC 3339 instant while `gameday` on the schedule feed is a bare
    date, and both are wanted as the calendar day.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ── row shapes ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GameRef:
    """One completed team game, from the schedule feed."""

    game_id: str
    season: int
    week: int
    game_type: str
    gameday: date
    team: str


@dataclass(frozen=True)
class Schedule:
    """Every completed game, ordered per `(season, team)`.

    Ordered because "consecutive weeks" is meaningless across a bye: two injury
    designations in weeks 5 and 7 with a bye in week 6 are one uninterrupted
    absence, and comparing week numbers would split them into two events and
    then flag the second as a recurrence of the first. Event reconstruction
    walks this list's INDEX, never the week number.
    """

    by_team_season: dict[tuple[int, str], tuple[GameRef, ...]] = field(
        default_factory=dict
    )
    seasons_read: tuple[int, ...] = ()

    def games(self, season: int, team: str) -> tuple[GameRef, ...]:
        return self.by_team_season.get((season, team), ())


@dataclass(frozen=True)
class PlayerRow:
    """One `players.csv` row, narrowed to what a durability record needs."""

    gsis_id: str
    pfr_id: str | None
    display_name: str
    team: str | None
    position: str
    jersey_number: int | None
    birth_date: date | None
    rookie_season: int | None


@dataclass(frozen=True)
class DesignationRow:
    """One team's injury-report line for one player in one game week.

    **This is the only source `absence_reason` may come from.** The spec's named
    failure mode is a game missed for a suspension, a personal matter or a rest
    week being folded into `career_games_missed_injury`, and the guard is that
    the reason is *sourced from the designation* rather than inferred from the
    absence. `report_primary_injury` carries nflverse's verbatim text, including
    the "Not injury related - ..." variants, and `events.classify_absence` is
    the single place it is read.
    """

    season: int
    week: int
    team: str
    gsis_id: str
    game_type: str
    report_status: str
    report_primary_injury: str
    report_secondary_injury: str
    practice_primary_injury: str
    practice_secondary_injury: str
    practice_status: str
    reported_at: date | None


@dataclass
class SeasonFeed:
    """A per-season sweep's result, plus what it could not read.

    `seasons_missing` travels to the envelope's `errors` and makes every
    affected row report `career_history_complete: false`. A history silently
    missing a season is a well-formed number that is simply wrong.
    """

    seasons_read: tuple[int, ...] = ()
    seasons_missing: tuple[int, ...] = ()


@dataclass
class Designations(SeasonFeed):
    by_player: dict[str, list[DesignationRow]] = field(default_factory=dict)


@dataclass
class Participation(SeasonFeed):
    """`(pfr_id, season, week) -> offensive snap share`.

    A key being PRESENT is what "this player was on the field for this game"
    means. Absence from the map is not "zero snaps" — it is "no snap-count row",
    which for a game inside the player's tenure span is the absence a
    designation then has to explain, or not.

    `team_of` carries the club each of those games was played for. It is the
    half that lets a HEALTHY player's tenure be enumerated at all: a player who
    never reaches an injury report has no designation row to read a club off,
    and without this his whole season would contribute zero games possible —
    which is `availability_rate` going null for exactly the players who have
    nothing wrong with them.
    """

    snap_pct: dict[tuple[str, int, int], float] = field(default_factory=dict)
    team_of: dict[tuple[str, int, int], str] = field(default_factory=dict)


@dataclass
class Production(SeasonFeed):
    """`(gsis_id, season, week) -> PPR fantasy points`."""

    points: dict[tuple[str, int, int], float] = field(default_factory=dict)


# ── process-lifetime memos ───────────────────────────────────────────────────
#
# Keyed the same way the `ETagStore` is, and for the same reason: a `304` says
# "byte-identical to what you already read", which is only useful to a process
# that still holds what it read. A restart costs exactly one full download per
# key. `player-profile`'s adapter carries the full reasoning.
#
# **The three per-season memos carry the KEEP-SET they were built with, and that
# is not bookkeeping — it is a correctness requirement this collector creates for
# itself.** `player-profile` memoises the *unfiltered* table and narrows
# afterwards, so a season-keyed memo is complete by construction there. This
# adapter pushes the scope filter into the parse (which is what keeps the 8.28 MB
# weekly-stats file from materialising ~4,600 unwanted players), so a memo is
# only ever complete *for the scope that built it*.
#
# Season-keyed alone, the failure is silent and permanent:
#
#   * prior-season files are immutable and 304 forever — this module's own
#     docstring says so;
#   * `roster-scope` membership changes weekly;
#   * so every player who enters the scope AFTER process start would get a 304,
#     be served a memo built without him, and publish a history containing only
#     the current season — while `career_history_complete` reported **true** and
#     `observation_window_first_season` still named the window start.
#
# That is a believable availability rate that is simply wrong, published under a
# flag that promises it is not, which is the exact failure the whole collector
# exists to prevent. `_memo_covers` is the guard: a memo whose keep-set does not
# cover the request is treated as ABSENT, and `_read_rows` then self-heals by
# dropping the stored ETag and re-reading unconditionally.
#
# The stored keep-set GROWS by union on every re-read, so a scope that gains one
# player costs one re-download and then covers both scopes forever. The bound is
# the league, and only for the columns this collector keeps.

_SCHEDULE_MEMO: dict[str, tuple[GameRef, ...]] = {}
_PLAYERS_MEMO: dict[str, tuple[PlayerRow, ...]] = {}
_INJURY_MEMO: dict[int, tuple[frozenset[str], tuple[DesignationRow, ...]]] = {}
_SNAP_MEMO: dict[int, tuple[frozenset[str], dict[str, tuple[float, str]]]] = {}
_STATS_MEMO: dict[int, tuple[frozenset[str], dict[str, float]]] = {}


def _memo_covers(memo: dict, season: int, keep: frozenset[str]) -> bool:
    """Whether `memo[season]` was built with a keep-set that covers `keep`.

    A superset is usable — the stored rows contain everything `keep` asks for
    plus some it does not, and the caller filters on emit. A memo that does not
    cover is treated as absent rather than as stale, because "stale" would imply
    the served answer is merely old; it is *wrong*, and silently so.
    """
    entry = memo.get(season)
    return entry is not None and keep <= entry[0]


def reset_upstream_memo(etag_store: ETagStore = ETAGS) -> None:
    """Forget every memo and every stored ETag. For tests only.

    Both halves, together: clearing one without the other produces exactly the
    inconsistent state `_read_rows` has to defend against, and a test that
    creates it is testing the defence rather than the behaviour it meant to.
    """
    _SCHEDULE_MEMO.clear()
    _PLAYERS_MEMO.clear()
    _INJURY_MEMO.clear()
    _SNAP_MEMO.clear()
    _STATS_MEMO.clear()
    etag_store.clear()


def source_ref(season: int, week: int, *, span: int = DEFAULT_HISTORY_SEASONS) -> str:
    """The exact upstream artifacts this pass read, recorded in the envelope.

    All five, comma-joined, because a durability row is a join across all of
    them and a consumer tracing a wrong `days_to_return` a season later needs to
    know which documents produced it. The per-season feeds are named once per
    season in the window, so the window itself is recoverable from a lake object
    without a separate field.

    `week` is accepted and unused: no feed here varies by week. It is in the
    signature because every collector's `source_ref` has it and a caller passing
    the scope through should not have to remember which ones care.
    """
    del week
    refs = [GAMES_URL, PLAYERS_URL]
    for one_season in history_seasons(season, span=span):
        refs.append(INJURIES_URL.format(season=one_season))
        refs.append(SNAP_COUNTS_URL.format(season=one_season))
        refs.append(STATS_URL.format(season=one_season))
    return ",".join(refs)


async def _read_rows(
    client: httpx.AsyncClient,
    url: str,
    *,
    required_columns: frozenset[str],
    memo_present: bool,
    etag_store: ETagStore,
) -> list[dict] | None:
    """Stream `url` conditionally. Rows, or `None` meaning "use the memo".

    **`UpstreamUnchanged` is caught here rather than re-raised**, which is a
    departure from `depth-chart`'s route-1 pattern on purpose: this collector
    reads five feeds on four cadences, and an unchanged `players.csv` says
    nothing about whether this week's injury report moved. `capture.py` keeps
    the "publish nothing when nothing changed" half through a content digest,
    which is strictly better here because it also catches a republication that
    changed no field this collector reads.

    The one inconsistent state worth defending: an ETag stored with no memo
    behind it. It cannot arise from this module — every successful read writes
    its memo — but it can arise from a test that clears one and not the other.
    Rather than serve nothing, the stored ETag is dropped and the document is
    re-requested unconditionally, which costs one download and self-heals.
    """
    stream = stream_csv_dicts(
        client,
        url,
        required_columns=required_columns,
        etag_key=url,
        etag_store=etag_store,
    )
    try:
        return [row async for row in stream]
    except UpstreamUnchanged:
        if memo_present:
            return None
        logger.warning("%s returned 304 with no memo behind it; re-reading", url)
        etag_store.set(url, None)
        return [
            row
            async for row in stream_csv_dicts(
                client,
                url,
                required_columns=required_columns,
                etag_key=url,
                etag_store=etag_store,
            )
        ]


# ── the schedule ─────────────────────────────────────────────────────────────


def _game_refs(row: dict) -> list[GameRef]:
    """One schedule row as up to two `GameRef`s — one per club.

    A game is a row per matchup upstream and a row per TEAM here, because
    tenure is a property of a player's club: "team games during the player's
    tenure" needs the home side's list and the away side's list separately.

    **A game with no `result` is not returned at all.** A scheduled future game
    is not a game the player could have missed, and counting it would inflate
    `career_games_possible` — which lowers `availability_rate` and manufactures
    exactly the durability problem the named failure mode is about. The bias
    direction is why this is a hard filter rather than a flag.
    """
    season = _int(row.get("season"))
    week = _int(row.get("week"))
    gameday = _date(row.get("gameday"))
    game_id = _text(row.get("game_id"))
    if season is None or week is None or gameday is None or game_id is None:
        return []
    if _text(row.get("result")) is None:
        return []
    game_type = _text(row.get("game_type")) or "REG"
    refs = []
    for side in ("home_team", "away_team"):
        team = _text(row.get(side))
        if team:
            refs.append(
                GameRef(
                    game_id=game_id,
                    season=season,
                    week=week,
                    game_type=game_type,
                    gameday=gameday,
                    team=team,
                )
            )
    return refs


async def fetch_schedule(
    seasons: list[int],
    *,
    client: httpx.AsyncClient,
    etag_store: ETagStore = ETAGS,
) -> Schedule:
    """Every completed game in `seasons`, ordered per `(season, team)`.

    One file for every season nflverse has ever published, so the window is
    applied here rather than by fetching less: filtering as the 2.17 MB document
    streams is what keeps 27 seasons of games out of memory.

    Raises on an upstream failure rather than returning an empty schedule. A
    schedule this collector cannot read means it cannot enumerate a single
    player's tenure, so there is nothing to publish and the caller turns the
    exception into a `present: 0` envelope.
    """
    wanted = set(seasons)
    memo_key = ",".join(str(s) for s in sorted(wanted))
    rows = await _read_rows(
        client,
        GAMES_URL,
        required_columns=GAMES_COLUMNS,
        memo_present=memo_key in _SCHEDULE_MEMO,
        etag_store=etag_store,
    )
    if rows is None:
        refs = _SCHEDULE_MEMO[memo_key]
    else:
        refs = tuple(
            ref
            for row in rows
            if _int(row.get("season")) in wanted
            for ref in _game_refs(row)
        )
        _SCHEDULE_MEMO[memo_key] = refs

    by_team_season: dict[tuple[int, str], list[GameRef]] = defaultdict(list)
    for ref in refs:
        by_team_season[(ref.season, ref.team)].append(ref)
    return Schedule(
        by_team_season={
            key: tuple(sorted(games, key=lambda g: (g.gameday, g.week)))
            for key, games in by_team_season.items()
        },
        seasons_read=tuple(sorted(wanted)),
    )


# ── the player table ─────────────────────────────────────────────────────────


def _to_player(row: dict, season: int, span: int) -> PlayerRow | None:
    gsis_id = _text(row.get("gsis_id"))
    position = normalize_position(row.get("position", ""))
    if not gsis_id or position is None:
        # No join key to `player-identity`, or a position this collector cannot
        # place in an availability cohort. Dropped rather than emitted with a
        # guessed id; the shortfall shows up against `EXPECTED_FLOOR`.
        return None
    last_season = _int(row.get("last_season"))
    if last_season is None or last_season < season - span:
        # Outside the window entirely — nothing this pass reads could carry a
        # game for them. ~25,000 rows in, ~1,400 out.
        return None
    return PlayerRow(
        gsis_id=gsis_id,
        pfr_id=_text(row.get("pfr_id")),
        display_name=_text(row.get("display_name")) or gsis_id,
        team=_text(row.get("latest_team")),
        position=position,
        jersey_number=_int(row.get("jersey_number")),
        birth_date=_date(row.get("birth_date")),
        rookie_season=_int(row.get("rookie_season")),
    )


async def fetch_players(
    season: int,
    *,
    client: httpx.AsyncClient,
    span: int = DEFAULT_HISTORY_SEASONS,
    etag_store: ETagStore = ETAGS,
) -> tuple[PlayerRow, ...]:
    """Every recently-active player at a position this collector carries.

    Three things are joined off this table and only this table: the `gsis_id`
    that resolves through `player-identity`, the `pfr_id` that joins the
    snap-count feed (which carries no GSIS id at all), and the `birth_date`
    behind `age_adjusted_availability_rate`.

    Raises on an upstream failure rather than returning an empty tuple, so the
    caller can turn the exception into a `present: 0` envelope. An empty tuple
    would be recorded as a successful capture of nothing.
    """
    memo_key = f"{season}:{span}"
    rows = await _read_rows(
        client,
        PLAYERS_URL,
        required_columns=PLAYERS_COLUMNS,
        memo_present=memo_key in _PLAYERS_MEMO,
        etag_store=etag_store,
    )
    if rows is None:
        return _PLAYERS_MEMO[memo_key]

    players = tuple(
        player
        for player in (_to_player(row, season, span) for row in rows)
        if player is not None
    )
    _PLAYERS_MEMO[memo_key] = players
    return players


# ── the per-season sweeps ────────────────────────────────────────────────────


async def _sweep(
    seasons: list[int],
    *,
    read_one,
    deadline: datetime | None,
    label: str,
) -> tuple[list[int], list[int]]:
    """Run `read_one(season)` over `seasons`, newest first, absorbing failures.

    Newest first so that a deadline truncates the OLDEST seasons — the ones
    contributing least to a currently-scoped player's history — rather than the
    current one, which is the only season that still moves.

    A season that cannot be read is **recorded, not skipped**. Every affected
    row then reports `career_history_complete: false` rather than publishing a
    total built over a hole and calling it career-to-date.
    """
    read: list[int] = []
    missing: list[int] = []
    for one_season in sorted(seasons, reverse=True):
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            missing.extend(s for s in seasons if s not in read and s not in missing)
            break
        try:
            await read_one(one_season)
        except Exception as exc:  # noqa: BLE001 — one season is not the pass
            logger.warning("%s for %s unavailable: %s", label, one_season, exc)
            missing.append(one_season)
            continue
        read.append(one_season)
    return sorted(read), sorted(set(missing))


# Every game class the injury report covers. Preseason carries no report and no
# `game_type` outside this set appears in the feed; naming them is schema-drift
# detection rather than a filter with an opinion.
INJURY_GAME_TYPES = frozenset({"REG", "WC", "DIV", "CON", "SB"})


def _to_designation(row: dict) -> DesignationRow | None:
    season = _int(row.get("season"))
    week = _int(row.get("week"))
    gsis_id = _text(row.get("gsis_id"))
    team = _text(row.get("team"))
    if season is None or week is None or not gsis_id or not team:
        return None
    game_type = _text(row.get("game_type")) or "REG"
    if game_type not in INJURY_GAME_TYPES:
        return None
    return DesignationRow(
        season=season,
        week=week,
        team=team,
        gsis_id=gsis_id,
        game_type=game_type,
        report_status=(row.get("report_status") or "").strip(),
        report_primary_injury=(row.get("report_primary_injury") or "").strip(),
        report_secondary_injury=(row.get("report_secondary_injury") or "").strip(),
        practice_primary_injury=(row.get("practice_primary_injury") or "").strip(),
        practice_secondary_injury=(row.get("practice_secondary_injury") or "").strip(),
        practice_status=(row.get("practice_status") or "").strip(),
        reported_at=_date(row.get("date_modified")),
    )


async def fetch_designations(
    seasons: list[int],
    *,
    client: httpx.AsyncClient,
    keep_gsis: frozenset[str],
    deadline: datetime | None = None,
    etag_store: ETagStore = ETAGS,
) -> Designations:
    """Every injury-report line for `keep_gsis`, across `seasons`.

    **The one feed this collector cannot do without.** `absence_reason` has no
    other source, and inventing one from absence is the named failure mode.
    `capture.py` therefore treats "no season read at all" as fatal, while a
    single missing season is degraded.

    ~6,200 rows per season in, a few hundred out: `keep_gsis` is the resolved
    scope, so the filter runs as the document streams.
    """
    result = Designations(by_player=defaultdict(list))

    async def read_one(one_season: int) -> None:
        url = INJURIES_URL.format(season=one_season)
        cached = _INJURY_MEMO.get(one_season)
        rows = await _read_rows(
            client,
            url,
            required_columns=INJURIES_COLUMNS,
            memo_present=_memo_covers(_INJURY_MEMO, one_season, keep_gsis),
            etag_store=etag_store,
        )
        if rows is None:
            kept = cached[1]
        else:
            # The UNION, so a scope that gained one player costs one re-read and
            # then covers both scopes for the rest of the process's life.
            wanted = keep_gsis | (cached[0] if cached else frozenset())
            kept = tuple(
                designation
                for designation in (
                    _to_designation(row)
                    for row in rows
                    if _text(row.get("gsis_id")) in wanted
                )
                if designation is not None
            )
            _INJURY_MEMO[one_season] = (wanted, kept)
        for designation in kept:
            # Filtered on EMIT as well as on parse: the memo may legitimately
            # hold a superset of this pass's scope.
            if designation.gsis_id in keep_gsis:
                result.by_player[designation.gsis_id].append(designation)

    read, missing = await _sweep(
        seasons, read_one=read_one, deadline=deadline, label="injury designations"
    )
    result.seasons_read = tuple(read)
    result.seasons_missing = tuple(missing)
    for rows in result.by_player.values():
        rows.sort(key=lambda d: (d.season, d.week))
    return result


async def fetch_participation(
    seasons: list[int],
    *,
    client: httpx.AsyncClient,
    keep_pfr: frozenset[str],
    deadline: datetime | None = None,
    etag_store: ETagStore = ETAGS,
) -> Participation:
    """Offensive snap share per `(pfr_id, season, week)` for `keep_pfr`.

    Keyed by `pfr_id` because `snap_counts_<n>.csv` carries no GSIS id — the hop
    is `gsis -> pfr` via `players.csv`, which is why a player with no `pfr_id`
    has no participation record and is reported as such rather than as absent.

    The memo holds one entry per season, which is what keeps a three-season
    sweep flat: the ~26,600 per-game rows are never materialised.
    """
    result = Participation()

    async def read_one(one_season: int) -> None:
        url = SNAP_COUNTS_URL.format(season=one_season)
        cached = _SNAP_MEMO.get(one_season)
        rows = await _read_rows(
            client,
            url,
            required_columns=SNAP_COUNTS_COLUMNS,
            memo_present=_memo_covers(_SNAP_MEMO, one_season, keep_pfr),
            etag_store=etag_store,
        )
        if rows is None:
            table = cached[1]
        else:
            wanted = keep_pfr | (cached[0] if cached else frozenset())
            table = {}
            for row in rows:
                pfr_id = _text(row.get("pfr_player_id"))
                week = _int(row.get("week"))
                if not pfr_id or pfr_id not in wanted or week is None:
                    continue
                pct = _float(row.get("offense_pct"))
                team = _text(row.get("team")) or ""
                table[f"{pfr_id}|{week}"] = (0.0 if pct is None else pct, team)
            _SNAP_MEMO[one_season] = (wanted, table)
        for key, (pct, team) in table.items():
            pfr_id, _, week_text = key.partition("|")
            if pfr_id not in keep_pfr:
                continue
            week_key = (pfr_id, one_season, int(week_text))
            result.snap_pct[week_key] = pct
            if team:
                result.team_of[week_key] = team

    read, missing = await _sweep(
        seasons, read_one=read_one, deadline=deadline, label="snap counts"
    )
    result.seasons_read = tuple(read)
    result.seasons_missing = tuple(missing)
    return result


async def fetch_production(
    seasons: list[int],
    *,
    client: httpx.AsyncClient,
    keep_gsis: frozenset[str],
    deadline: datetime | None = None,
    etag_store: ETagStore = ETAGS,
) -> Production:
    """PPR fantasy points per `(gsis_id, season, week)` for `keep_gsis`.

    The single most expensive feed here — 8.28 MB a season, ~24.8 MB of the
    43.8 MB cold window — and it backs exactly one field,
    `post_return_production_delta`. It is fetched anyway rather than stubbed
    because it CAN be sourced, and a plausible invented number is worse than an
    honest null. A season that fails is degraded, not fatal: the delta goes null
    for the events that needed it and `seasons_missing` says why.
    """
    result = Production()

    async def read_one(one_season: int) -> None:
        url = STATS_URL.format(season=one_season)
        cached = _STATS_MEMO.get(one_season)
        rows = await _read_rows(
            client,
            url,
            required_columns=STATS_COLUMNS,
            memo_present=_memo_covers(_STATS_MEMO, one_season, keep_gsis),
            etag_store=etag_store,
        )
        if rows is None:
            table = cached[1]
        else:
            wanted = keep_gsis | (cached[0] if cached else frozenset())
            table = {}
            for row in rows:
                gsis_id = _text(row.get("player_id"))
                week = _int(row.get("week"))
                if not gsis_id or gsis_id not in wanted or week is None:
                    continue
                points = _float(row.get("fantasy_points_ppr"))
                if points is None:
                    continue
                table[f"{gsis_id}|{week}"] = points
            _STATS_MEMO[one_season] = (wanted, table)
        for key, points in table.items():
            gsis_id, _, week_text = key.partition("|")
            if gsis_id not in keep_gsis:
                continue
            result.points[(gsis_id, one_season, int(week_text))] = points

    read, missing = await _sweep(
        seasons, read_one=read_one, deadline=deadline, label="weekly player stats"
    )
    result.seasons_read = tuple(read)
    result.seasons_missing = tuple(missing)
    return result
