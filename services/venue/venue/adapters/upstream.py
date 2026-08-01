"""The upstream adapters — the only modules that know a wire format.

`venue` has two upstreams, and they are deliberately different in kind.

**`venue_static` reads a committed reference table** (`venue.reference`) and
reaches no third party at all. That is the primary source the phase-8 spec
names, and it is what makes this signal type fully deterministic: the
conformance tests assert real captured output rather than a mock's, and a CI
run costs nobody's rate limit. There is nothing to fetch, so there is nothing
here for it beyond `REFERENCE_SOURCE_REF`.

**`venue_game_assignment` cannot be static.** It answers "which building is
THIS game played in", and the game list moves — flex scheduling, postponements,
neutral-site relocations announced mid-season. It reads the nflverse game
table, the same feed `schedule-context` already reads, through the same
`stream_csv_dicts` path for the same reason: the document is ~2.1 MB of every
game since 1999 and a capture keeps one season of it.

**Memory rules, both learned the hard way.** `roster-scope`'s first deploy was
OOMKilled at a 256Mi limit because a ~37 MB upstream document was buffered
three times over (`response.text` plus a decode plus an `io.StringIO`).
`stream_csv_dicts` hands rows out one at a time, so `_in_scope` below filters
as the document is parsed and the other twenty-six seasons are never
materialised.

**The two signal types fail independently, on purpose.** A `venue_static` pass
needs no network, so a dead nflverse feed must not stop this collector
publishing the venue records it already holds. `capture.py` is where that split
is made; this module only supplies the pieces.
"""

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from collector_core.streaming import stream_csv_dicts

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake.
UPSTREAM_ADAPTER = "venue-reference-table"

# The reference table is a module, not a URL. Recorded as the `source_ref` of
# every `venue_static` envelope so a lake object a season old still says which
# artifact produced it.
REFERENCE_SOURCE_REF = "venue.reference:VENUE_RECORDS"

# Environment-overridable so a load test or a fixture server can stand in for
# the real feed without hammering a third party — the same reasoning that put
# `FORECAST_URL` and `SCHEDULE_URL` behind env vars during 8A. Deliberately the
# SAME variable name `schedule-context` uses: two collectors reading one feed
# should be redirectable together.
SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
)

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly with `reason=malformed`
# rather than map nulls into an append-only lake. Exactly the columns the
# mapping below reads and no more, so an unrelated column disappearing upstream
# does not fail a capture that never used it.
REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "season",
        "game_type",
        "week",
        "gameday",
        "home_team",
        "location",
        "stadium",
    }
)

# NOTE on the feed's kickoff zone, which this module deliberately does NOT
# convert. `schedule-context` reads the same feed and needs the kickoff
# *instant*, so it combines `gameday` + `gametime` and converts from the feed's
# Eastern wall clock. This collector needs only the calendar DATE the game is
# played on, to join against a revision window that is itself a date range —
# and `gameday` is already that date in the feed's own zone. Converting it
# through UTC would move a 20:20 Eastern Sunday-night game onto Monday, which
# is why `parse_kickoff_date` reads `gameday` and ignores `gametime` entirely.
#
# A `FEED_TIMEZONE = ZoneInfo(...)` constant used to sit here, copied from
# `schedule-context`, referenced by nothing. Ruff could not see it was dead
# because it kept the `zoneinfo` import "used", and its comment described
# conversion behaviour this module does not have.

# Preseason is not part of the competitive schedule. Excluded for the reason
# `schedule-context` excludes it: those games inflate the assignment count with
# fixtures no projection is made for.
EXCLUDED_GAME_TYPES = frozenset({"PRE"})


@dataclass(frozen=True)
class ScheduledGame:
    """One feed row, narrowed to what a venue assignment needs.

    `kickoff_on` is `None` for a game the feed lists without a usable date — a
    scheduled-but-unslotted late-season game, or a postponement whose
    replacement date has not been published. That is a real upstream ambiguity
    and it is represented rather than guessed: without a date there is no
    revision window to join against, and falling back to the most recent
    revision is exactly the retroactive attribution this collector exists to
    prevent.
    """

    game_id: str
    season: int
    week: int
    game_type: str
    kickoff_on: date | None
    home_team: str
    is_neutral_site: bool
    stadium_name: str


def schedule_source_ref() -> str | None:
    """The `venue_game_assignment` envelope's source_ref: the game feed.

    **No `season`/`week` parameters**, unlike the scaffolder's `source_ref` and
    unlike most collectors': neither of this collector's upstreams varies by
    scope. The game feed is one URL carrying every season, filtered as it
    streams, and the reference table is a module. Accepting arguments and
    ignoring them would advertise a per-week artifact that does not exist, and
    would make a caller passing the wrong week look harmless.

    `venue_static`'s counterpart is the plain `REFERENCE_SOURCE_REF` constant —
    there is no function at all, because there is nothing to compute.
    """
    return SCHEDULE_URL or None


def parse_kickoff_date(gameday: str) -> date | None:
    """`2026-09-13` -> a date, or `None` for an absent or unparseable value.

    Returns `None` rather than raising: one unslotted game must not fail a
    whole season's capture, and the caller already has a place to record it.
    """
    gameday = gameday.strip()
    if not gameday:
        return None
    try:
        return datetime.strptime(gameday, "%Y-%m-%d").date()
    except ValueError:
        return None


def _to_game(row: dict) -> ScheduledGame:
    return ScheduledGame(
        game_id=row["game_id"].strip(),
        season=int(row["season"]),
        week=int(row["week"]),
        game_type=row["game_type"].strip().upper(),
        kickoff_on=parse_kickoff_date(row["gameday"]),
        home_team=row["home_team"].strip(),
        # The feed's own neutral-site marker. Carried over from
        # `schedule_context`: this flag, not the stadium id, is what says the
        # `stadium_id` column describes the home CLUB rather than the venue.
        is_neutral_site=row["location"].strip().lower() == "neutral",
        stadium_name=row["stadium"].strip(),
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

    Raises on an upstream failure rather than returning an empty list, so the
    caller can turn the exception into a `present: 0` envelope for the
    assignment signal type. An empty list would instead be recorded as a
    successful capture of nothing.
    """
    return [
        _to_game(row)
        async for row in stream_csv_dicts(
            client, SCHEDULE_URL, required_columns=REQUIRED_COLUMNS
        )
        if _in_scope(row, season)
    ]


def utc_today(now: datetime) -> date:
    """The day the coverage rule means by "today".

    Taken from the capture's own `now` rather than `date.today()`, so a test
    that freezes the clock freezes this too and a capture stays reproducible
    from its own envelope.
    """
    return now.astimezone(UTC).date()
