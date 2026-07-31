"""schedule-context's capture pass: fetch -> chain -> coverage -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

Three things here are correctness, not style.

**`coverage.expected` never derives from what succeeded.** `expected_floors`
below is computed from the shape of a season — 32 clubs, one game each per
week, a known bye window — and not from the document the fetch returned. A
truncated feed carrying four games therefore reports 8 present against an
expected 32, ratio 0.25, rather than `expected: 8, present: 8` and a
comfortable 1.0.

**A bye week and an outage must not look alike.** `Coverage.ratio` returns
1.0 when `expected` is 0, so a floor is what stands between "this week
legitimately has fewer games" and "we captured nothing". The floor is
week-aware for exactly that reason: a week inside the bye window floors lower
than a week outside it, so a real bye still reads 1.0 while a failed capture
in the same week reads 0.0.

**A failed capture still writes an envelope.** `fail_capture` writes one
`present: 0` envelope per signal type with a populated `errors` array, then
re-raises — the write makes the gap in the append-only lake explicit, and the
re-raise stops `CaptureState` installing an empty capture over the last good
one.

Every lake call goes off the event loop via `awrite`: `LakeWriter` is
synchronous boto3 and the lake handed to this function raises if it is called
from the loop thread. The pass's first statement is an `await` on the fetch,
so uvicorn finishes starting before any upstream latency is incurred.
"""

from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from .adapters.upstream import UPSTREAM_ADAPTER, fetch_season_games, source_ref
from .chain import SeasonChains, TeamGame, rest_context, team_games, travel_context
from .metrics import metrics

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "REST",
    "SIGNAL_TYPES",
    "SITUATIONAL",
    "capture_schedule_context",
    "expected_floors",
]

COLLECTOR_NAME = "schedule-context"
CADENCE_CLASS = CadenceClass.WEEKLY

SITUATIONAL = "game_situational_context"
REST = "team_rest_context"
SIGNAL_TYPES = (SITUATIONAL, REST)

# ── the expected universe ─────────────────────────────────────────────────────
#
# Derived from the league's structure, deliberately not from the fetch.
#
#   * 32 clubs, and a club plays at most one game in a week, so a week can
#     never owe more than 32 team-records — two per game, one per participant.
#   * Weeks 1-4 and 15-18 carry no byes in the 18-week format: every club
#     plays, so the floor is the whole league.
#   * Weeks 5-14 carry byes. The league has never scheduled more than six
#     clubs on bye in one week, so the floor for those weeks is 32 - 6 = 26.
#     A week with four clubs on bye observes 28 and reports 28/28 = 1.0,
#     because the floor raises a short count and never lowers a genuine one.
#     The same week captured during an outage observes 0 against a floor of
#     26 and reports 0.0 — which is exactly why `expected` must not fall out
#     of what the fetch returned.
#   * The postseason bracket is fixed: six wild-card games, four divisional,
#     two conference, one final — twelve, eight, four and two team-records.
#
# Both signal types share the floor because they describe the same universe:
# every participating club owes one record of each. A club whose venue cannot
# be resolved is short a *situational* record, and that shows up as coverage
# missing with a reason, which is where it belongs.
LEAGUE_CLUBS = 32
MAX_CLUBS_ON_BYE = 6
BYE_WINDOW = frozenset(range(5, 15))
POSTSEASON_RECORDS = {19: 12, 20: 8, 21: 4, 22: 2}


def expected_floor(week: int) -> int:
    """The smallest number of team-records the league is known to owe in a
    week. Never a count of anything fetched."""
    if week in POSTSEASON_RECORDS:
        return POSTSEASON_RECORDS[week]
    if week in BYE_WINDOW:
        return LEAGUE_CLUBS - MAX_CLUBS_ON_BYE
    return LEAGUE_CLUBS


def expected_floors(week: int) -> dict[str, int]:
    """The per-signal-type floor for one week, in the shape `fail_capture`
    and `CoverageAccumulator` want it."""
    floor = expected_floor(week)
    return {signal_type: floor for signal_type in SIGNAL_TYPES}


# Reasons recorded against a key that was owed but could not be produced.
# Named so a consumer can group them without matching prose.
KICKOFF_UNSCHEDULED = "kickoff_unscheduled"
VENUE_UNRESOLVED = "venue_unresolved"
REST_UNRESOLVED = "rest_unresolved"


def _rfc3339(value: datetime | None) -> str | None:
    """RFC 3339 UTC with a `Z` suffix, or `None`.

    Rejects naive datetimes rather than assuming UTC. Kickoff times are the
    most timezone-sensitive data in this repo, and guessing writes a wrong
    instant into an append-only lake nobody rewrites.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"timezone-aware datetime required, got naive: {value!r}")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_rest_signal(season: SeasonChains, record: TeamGame) -> dict:
    """One team-game -> one `team_rest_context` row.

    Everything here comes off the clock or off the weeks a club is scheduled
    in; none of it needs a venue, which is why a club playing at an
    unrecognised neutral site still gets a rest record.
    """
    rest = rest_context(season, record)
    return {
        "game_id": record.game_id,
        "team_id": record.team,
        "opponent_id": record.opponent,
        "season": record.season,
        "week": record.week,
        "home_away": record.home_away,
        "kickoff_at": _rfc3339(record.kickoff_at),
        "previous_kickoff_at": _rfc3339(rest.previous_kickoff_at),
        "days_rest": None if rest.days_rest is None else round(rest.days_rest, 4),
        "is_short_week": rest.is_short_week,
        "is_post_bye": rest.is_post_bye,
        "is_pre_bye": rest.is_pre_bye,
    }


def build_situational_signal(season: SeasonChains, record: TeamGame) -> dict:
    """One team-game -> one `game_situational_context` row.

    `schedule_revision_count` is `null` and stays that way until something
    supplies a revision history: the upstream is a single snapshot with no
    memory of where a kickoff used to be, so any number here would be
    invented. Deriving it from this collector's own append-only lake is a real
    option and a deliberate follow-up — it needs a bounded prefix read on every
    pass, which is not free and is not this PR.
    """
    travel = travel_context(season, record)
    assert record.venue is not None  # guarded by `_blocking_reason`
    return {
        "game_id": record.game_id,
        "team_id": record.team,
        "opponent_id": record.opponent,
        "home_away": record.home_away,
        "kickoff_at": _rfc3339(record.kickoff_at),
        "kickoff_local_time": travel.kickoff_local_time,
        "venue_id": record.venue.venue_id,
        "is_international": travel.is_international,
        "travel_distance_mi": (
            None
            if travel.travel_distance_mi is None
            else round(travel.travel_distance_mi, 1)
        ),
        "travel_direction": travel.travel_direction,
        "timezone_shift_hours": travel.timezone_shift_hours,
        "body_clock_offset_hours": (
            None
            if travel.body_clock_offset_hours is None
            else round(travel.body_clock_offset_hours, 2)
        ),
        "consecutive_road_games": travel.consecutive_road_games,
        "days_since_timezone_change": (
            None
            if travel.days_since_timezone_change is None
            else round(travel.days_since_timezone_change, 4)
        ),
        "schedule_revision_count": None,
    }


def blocking_reason(
    signal_type: str, season: SeasonChains, record: TeamGame
) -> str | None:
    """Why this team-game cannot produce a row of this signal type, or `None`.

    Emitting nothing and naming the reason is the contract for an ambiguous
    upstream: a row built on a guessed kickoff or a guessed venue is
    schema-valid, plausible and wrong, and nothing downstream could tell.
    """
    if record.kickoff_at is None:
        return KICKOFF_UNSCHEDULED
    if signal_type == SITUATIONAL and record.venue is None:
        return VENUE_UNRESOLVED
    if signal_type == REST and record.week > 1:
        # The spec's own definition of complete: "`days_rest` non-null for
        # every team past Week 1". A null there means the club's earlier games
        # did not reach us, which is a coverage hole and not a legitimate
        # value.
        if rest_context(season, record).days_rest is None:
            return REST_UNRESOLVED
    return None


BUILDERS = {SITUATIONAL: build_situational_signal, REST: build_rest_signal}


async def capture_schedule_context(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one envelope per signal type."""
    scope = {"season": season, "week": week}
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=source_ref(season, week),
    )
    floors = expected_floors(week)

    metrics.capture_attempt()
    try:
        games = await fetch_season_games(season, client=client)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Writes a `present: 0` envelope per signal type, then re-raises `exc`.
        # Never returns — do not add code after this call.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            expected=floors,
            source_ref=source_ref(season, week),
        )

    # The whole season is chained; only the scoped week is emitted. Rest, road
    # stretch and acclimatisation are facts about the games before this one.
    all_records = team_games(games)
    chains = SeasonChains(all_records)
    scoped = [record for record in all_records if record.week == week]
    metrics.games_in_scope(len({record.game_id for record in scoped}))
    metrics.unresolved_venues(sum(1 for record in scoped if record.venue is None))

    envelopes: dict[str, Envelope] = {}
    for signal_type in SIGNAL_TYPES:
        acc = CoverageAccumulator(floor=floors[signal_type])
        signals: list[dict] = []
        for record in scoped:
            key = record.key
            # Declared because the club PLAYS this week and is therefore owed
            # a record — never because building one below succeeded.
            acc.expect(key)
            if deadline is not None and datetime.now(tz=UTC) >= deadline:
                # Over budget. Record the rest as missing rather than throwing
                # away what already resolved: a truncated pass that reports
                # itself truncated is useful; one that reports itself complete
                # is not.
                acc.fail(key, "deadline_exceeded")
                continue
            blocked = blocking_reason(signal_type, chains, record)
            if blocked is not None:
                acc.fail(key, blocked)
                continue
            try:
                signals.append(BUILDERS[signal_type](chains, record))
            except Exception as exc:  # noqa: BLE001 — one bad row is not a pass
                acc.fail(key, metrics.reason_for(exc))
                continue
            acc.record(key)
        envelopes[signal_type] = Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=signal_type,
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=acc.result(),
            errors=acc.errors,
            signals=signals,
        )

    return await publish_capture(envelopes, lake=lake, metrics=metrics)
