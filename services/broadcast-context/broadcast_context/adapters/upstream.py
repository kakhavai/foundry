"""The upstream adapter — the only module that knows the wire format.

One feed, and it is the same one `schedule-context` reads: the nflverse game
table, every game since 1999 in a single CSV. **509 KB on the wire** (httpx
requests gzip; 2.07 MiB parsed), 272 regular-season rows for a scheduled
season. There is no second source and no play-by-play here — this is by a wide
margin the cheapest collector in the fleet.

**It is streamed and filtered as it is parsed, never materialised.**
`response.text` handed to `io.StringIO` holds the document three times over —
the shape that OOM-killed `roster-scope` at a 256Mi limit. `columns=` narrows
each row dict to the six columns this collector reads out of the feed's 46,
while `required_columns=` still validates the whole header, so a column
disappearing upstream is still schema drift rather than a silent null.

**Conditional GET is on.** `raw.githubusercontent.com` serves an ETag and
answers `If-None-Match` with a `304` carrying zero bytes, so a poll that lands
between two republications costs one round trip instead of 509 KB. A `304`
raises `UpstreamUnchanged`, which is a *successful* capture — `capture.py`
re-raises it above its generic handler, and the caveat in `docs/collectors.md`
applies unchanged: the store is keyed by URL and this URL does not vary by
season or week, so `POST /refresh {"season": 2025}` after a 2026 capture can
`304` into a no-op that still answers 202.

**Two columns that are NOT here.** The feed carries no `network` and no `tv`
column — checked, not assumed — so the spec's `network` field has no free
source. See the README's "Fields that are null by necessity".

**`gametime` is Eastern, not venue-local**, for every game including the
London 09:30 and the Los Angeles 20:20. That is the whole reason
`kickoff_local_time` is null on every row and `is_primetime` is computed from
the Eastern clock; the argument is in the README.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from collector_core.streaming import stream_csv_dicts

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake. Deliberately the same string `schedule-context` uses —
# it is the same artifact, and two names for one feed is drift a season later.
UPSTREAM_ADAPTER = "nflverse-games"

# Environment-overridable so a load test or a fixture server can stand in for
# the real feed without hammering a third party — the same reasoning that put
# `FORECAST_URL` and `SCHEDULE_URL` behind env vars during 8A, and the same
# variable name `schedule-context` reads.
UPSTREAM_URL = os.getenv(
    "SCHEDULE_URL",
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
)

# Schema-drift detection: an upstream that renames a field must fail the
# capture loudly with `reason=malformed` rather than map nulls into an
# append-only lake. Exactly the columns the mapping below reads — no more, so
# an unrelated column disappearing upstream does not fail a capture that never
# used it.
REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "season",
        "game_type",
        "week",
        "gameday",
        "gametime",
    }
)

# The feed publishes kickoff in this zone regardless of where the game is
# played — a London game at 14:30 local appears as 09:30. The season crosses
# the November DST transition, so a fixed -05:00 is wrong for exactly the
# late-season games; conversion goes through a real IANA zone.
FEED_TIMEZONE = ZoneInfo("America/New_York")

# Preseason games are excluded: they are not part of the competitive schedule
# and are not sold as broadcast windows.
EXCLUDED_GAME_TYPES = frozenset({"PRE"})


@dataclass(frozen=True)
class ScheduledGame:
    """One row of the feed, normalised.

    `kickoff_eastern` is the feed's own wall clock made explicit — aware, in
    `FEED_TIMEZONE`. `kickoff_at` is the same instant in UTC. Both are `None`
    for a game the feed lists without a usable kickoff time (a scheduled-but-
    unslotted late-season game, or a postponement whose replacement time has
    not been published). That is a real upstream ambiguity and it is
    represented rather than guessed: a guessed kickoff would assign a
    plausible-looking broadcast window to a game that does not have one yet,
    which is precisely the "defaults to `sun_early`" failure the spec names.
    """

    game_id: str
    season: int
    week: int
    game_type: str
    kickoff_at: datetime | None
    kickoff_eastern: datetime | None


def source_ref(season: int, week: int) -> str | None:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Also the conditional-GET cache key, deliberately: the ETag store is keyed
    by the same string the envelope records, so the two cannot drift.
    """
    return UPSTREAM_URL or None


def parse_kickoff(gameday: str, gametime: str) -> datetime | None:
    """`2026-09-13` + `13:00` in the feed's zone -> an aware Eastern datetime.

    Returns `None` for an absent or unparseable time rather than raising: one
    unslotted game must not fail a whole season's capture, and the caller
    already has two places to record it (a coverage miss for the game itself,
    and an incomplete-slate marker for every other game in its week).
    """
    gameday, gametime = gameday.strip(), gametime.strip()
    if not gameday or not gametime:
        return None
    try:
        naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    # `replace(tzinfo=FEED_TIMEZONE)`, because the feed's wall clock IS
    # Eastern and zoneinfo resolves the right DST offset for that date.
    # `replace(tzinfo=UTC)` would silently move every kickoff by four hours,
    # and nothing downstream could tell.
    return naive.replace(tzinfo=FEED_TIMEZONE)


def _to_game(row: dict) -> ScheduledGame:
    kickoff_eastern = parse_kickoff(row["gameday"], row["gametime"])
    return ScheduledGame(
        game_id=row["game_id"],
        season=int(row["season"]),
        week=int(row["week"]),
        game_type=row["game_type"].strip().upper(),
        kickoff_at=None if kickoff_eastern is None else kickoff_eastern.astimezone(UTC),
        kickoff_eastern=kickoff_eastern,
    )


def _in_scope(row: dict, season: int) -> bool:
    """Kept or discarded as the row goes past — never materialised first."""
    if row["season"] != str(season):
        return False
    return row["game_type"].strip().upper() not in EXCLUDED_GAME_TYPES


async def fetch_season_games(
    season: int,
    *,
    client: httpx.AsyncClient,
) -> list[ScheduledGame]:
    """Every competitive game of one season, in feed order.

    **A whole season, not a week**, for the same reason `venue` and
    `schedule-context` take one: `games_in_window` is a count over a slot's
    whole slate, and a week-scoped fetch cannot see whether the slate it holds
    is the whole one.

    Raises on an upstream failure rather than returning an empty list, so the
    caller can turn the exception into a `present: 0` envelope. An empty list
    would instead be recorded as a successful capture of nothing.
    """
    return [
        _to_game(row)
        async for row in stream_csv_dicts(
            client,
            UPSTREAM_URL,
            required_columns=REQUIRED_COLUMNS,
            columns=REQUIRED_COLUMNS,
            etag_key=UPSTREAM_URL,
        )
        if _in_scope(row, season)
    ]
