"""The injury-report upstream — the only module that knows the wire format.

It answers exactly one question: **which clubs filed, on which practice day,
and what did they file.** Who *owed* a filing is `schedule.py`'s answer, and
keeping the two apart is what stops this collector deriving its own coverage
denominator from the document it just fetched.

**The distinction this adapter must never lose.** A club that filed a report
listing nobody and a club that filed nothing are different facts, and
collapsing them into "no injuries" is the single most damaging normalization
this collector can make: the second one reads downstream as a fully healthy
roster. The wire carries the distinction structurally rather than by a flag —

    a filing with players      -> one row per player, `player_external_id` set
    a filing listing nobody    -> exactly one row, `player_external_id` empty
    no filing at all           -> no rows for that (team, practice_day)

so "did the club file" is `any rows` and "did the club list anybody" is
`any rows with a player`. A feed that cannot emit the empty-filing row cannot
express the difference, and the honest response is a gap in `coverage.missing`
rather than an invented one — see `report.py`.

**Vocabulary is mapped here, and an unrecognised value raises.** Designations,
practice participation, absence reason and roster status are small controlled
sets; a feed emitting `Probable` (retired from the league's report in 2016) is
describing something this collector has no word for, and passing it through
would put a value into an append-only lake that no consumer can interpret.
`report.py` catches per row, so one oddity costs one player rather than
thirty-one clubs' reports.

**Streamed and filtered as it is parsed.** A season-wide injury file carries
every club and every week; the scoped week is a small slice of it. See
`collector_core.streaming` and the roster-scope OOM write-up it carries — never
`response.text`, never a materialized list that is narrowed afterwards.
"""

import os
from datetime import UTC, datetime

import httpx
from collector_core.streaming import stream_csv_dicts

from ..vocabulary import (
    ABSENCE_REASONS,
    GAME_DESIGNATIONS,
    PARTICIPATIONS,
    PRACTICE_DAYS,
    ROSTER_STATUSES,
    UnknownVocabulary,
    body_part,
    side_of_ball,
)

# Names the upstream in every envelope's `upstream.adapter`. It is how a
# consumer tells two sources of the same signal apart in the lake a season
# later, so change it when the upstream changes.
UPSTREAM_ADAPTER = "league-injury-report"

# Empty selects the deterministic stub below — the collector deploys, captures
# and serves before its upstream exists, the same reasoning `player-projections`
# applies to `PROJECTIONS_SNAPSHOT_URL`. Pointing it at the real feed is a
# ConfigMap edit; `{season}` and `{week}` are substituted.
UPSTREAM_URL_ENV = "INJURY_REPORT_URL"

# Schema-drift detection, per the Phase 8 failure-handling contract: an upstream
# that renames one of these fails the capture loudly with `reason=schema`
# rather than mapping nulls into the lake.
REQUIRED_COLUMNS = frozenset(
    {
        "season",
        "week",
        "team",
        "practice_day",
        "report_published_at",
        "is_final_report",
        "is_estimated",
        "player_external_id",
        "player_name",
        "position",
        "practice_status",
        "report_status",
        "injury_primary",
        "roster_status",
        "absence_reason",
    }
)

# Wire spellings -> published vocabulary. The long forms are what the league's
# own report writes; the short ones are what most aggregators re-publish.
_PRACTICE_DAY_ALIASES = {
    **{day: day for day in PRACTICE_DAYS},
    "wed": "wednesday",
    "thu": "thursday",
    "thurs": "thursday",
    "fri": "friday",
}

_PARTICIPATION_ALIASES = {
    **{value: value for value in PARTICIPATIONS},
    "did not participate in practice": "dnp",
    "limited participation in practice": "limited",
    "full participation in practice": "full",
    "did not participate": "dnp",
    "limited participation": "limited",
    "full participation": "full",
}

# An empty `report_status` cell is deliberately NOT mapped here. It means "no
# designation published *yet*", and only the final report can turn it into the
# published-healthy `none`. `report.py` owns that promotion, because it needs
# the whole week to make it.
_DESIGNATION_ALIASES = {
    **{value: value for value in GAME_DESIGNATIONS},
    "no injury designation": "none",
}

_ROSTER_STATUS_ALIASES = {
    **{value: value for value in ROSTER_STATUSES},
    "": "active",
    "reserve/injured": "ir",
    "ir-designated to return": "ir_designated_return",
    "reserve/pup": "pup",
    "reserve/nfi": "nfi",
    "reserve/suspended": "suspended",
}

_ABSENCE_REASON_ALIASES = {
    **{value: value for value in ABSENCE_REASONS},
    # Blank is `unspecified`, never `injury`. The phase doc's failure mode for
    # this collector is a veteran's scheduled rest day coded identically to a
    # player who could not practise; defaulting the reason to `injury` is
    # precisely how a healthy starter acquires a durability problem it does
    # not have.
    "": "unspecified",
    "not injury related": "rest",
    "not injury related - resting player": "rest",
    "resting player": "rest",
    "coach's decision": "personal",
}

_TRUE = frozenset({"1", "true", "yes", "y", "t"})
_FALSE = frozenset({"", "0", "false", "no", "n", "f"})

# The stub week. `NYJ` files every day and lists nobody — a club with nobody
# hurt genuinely reports nothing, and that must not read as an outage. `LV`
# files nothing at all — which must. Every other club files normally.
STUB_EMPTY_FILER = "NYJ"
STUB_SILENT_FILER = "LV"

# position, practice_status, report_status, injury_primary, roster_status,
# absence_reason. Three per filing club, deterministic, exercising both sides
# of the ball and both the injury and non-injury absence reasons.
_STUB_PLAYERS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("WR", "dnp", "out", "left hamstring strain", "active", "injury"),
    ("CB", "limited", "questionable", "knee", "active", "injury"),
    ("QB", "full", "", "not injury related", "active", "rest"),
)


def upstream_url() -> str:
    """Read at call time, not import time, so a test or a redeploy can change
    it without reimporting."""
    return os.getenv(UPSTREAM_URL_ENV, "").strip()


def source_ref(season: int, week: int) -> str | None:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Not decorative: it is what makes a lake object reproducible, and what tells
    a reader which of two feeds a row came from a season later.
    """
    url = upstream_url()
    if not url:
        return None
    return url.format(season=season, week=week)


def _flag(raw: str, *, field: str) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise UnknownVocabulary(field, raw)


def _timestamp(raw: str, *, field: str) -> str:
    """Validate and re-emit as RFC 3339 UTC.

    Validated rather than passed through: `report_published_at` is what orders
    `designation_history`, so an unparseable value would silently scramble the
    week-long trajectory that is this collector's actual product.
    """
    text = (raw or "").strip()
    if not text:
        raise UnknownVocabulary(field, raw)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnknownVocabulary(field, raw) from exc
    if parsed.tzinfo is None:
        raise UnknownVocabulary(field, raw)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lookup(aliases: dict[str, str], raw: str, *, field: str) -> str:
    value = aliases.get((raw or "").strip().lower())
    if value is None:
        raise UnknownVocabulary(field, raw)
    return value


def practice_day(raw: str) -> str | None:
    """The wire's practice-day cell mapped onto `PRACTICE_DAYS`, or `None`.

    `None` rather than a raise, because this is also the pre-parse filter in
    `fetch_report_rows`: a row naming a day this pass is not capturing is not
    an error, it is simply not wanted.
    """
    return _PRACTICE_DAY_ALIASES.get((raw or "").strip().lower())


def normalize_filing(row: dict[str, str]) -> dict:
    """The club/day half of one wire row: who filed, when, and how.

    Raises `UnknownVocabulary` on anything it cannot map. Separated from
    `normalize_player` so a club's filing still counts as *received* when one
    of its player lines is unmappable — the club did file, and recording it as
    unfiled would manufacture exactly the missing report this collector is
    built to detect.
    """
    return {
        "team": (row["team"] or "").strip().upper(),
        "practice_day": _lookup(
            _PRACTICE_DAY_ALIASES, row["practice_day"], field="practice_day"
        ),
        "report_published_at": _timestamp(
            row["report_published_at"], field="report_published_at"
        ),
        "is_final_report": _flag(row["is_final_report"], field="is_final_report"),
        "is_estimated": _flag(row["is_estimated"], field="is_estimated"),
    }


def has_player(row: dict[str, str]) -> bool:
    """Whether this wire row lists a player, or is a club's empty filing.

    The empty filing is a real record, not a missing one: it is a club saying
    "we filed, and nobody is hurt". `report.py` counts it as coverage present.

    Keyed on the id **or** the name, deliberately. A line naming somebody this
    collector cannot resolve to a canonical `player_id` is still a line the
    club filed; treating it as an empty filing would turn "we could not
    identify this player" into "this club has nobody hurt", which is the same
    conflation this module exists to prevent, one level down.
    """
    return bool(
        (row.get("player_external_id") or "").strip()
        or (row.get("player_name") or "").strip()
    )


def normalize_player(row: dict[str, str]) -> dict:
    """The player half of one wire row, mapped onto the published vocabularies.

    Raises `UnknownVocabulary` for anything outside them. `designation` is
    `None` here when the cell was blank — "not published yet" — and only
    `report.py` can promote that to the published-healthy `none`, because only
    it knows whether the club's final report has landed.
    """
    designation_raw = (row["report_status"] or "").strip()
    participation_raw = (row["practice_status"] or "").strip()
    injury_raw = (row["injury_primary"] or "").strip()
    return {
        "external_id": (row["player_external_id"] or "").strip(),
        "player_name": (row["player_name"] or "").strip(),
        "position": (row["position"] or "").strip().upper(),
        "side_of_ball": side_of_ball(row["position"]),
        "designation": (
            _lookup(_DESIGNATION_ALIASES, designation_raw, field="designation")
            if designation_raw
            else None
        ),
        "participation": (
            _lookup(_PARTICIPATION_ALIASES, participation_raw, field="participation")
            if participation_raw
            else None
        ),
        "body_part": body_part(injury_raw),
        "body_part_raw": injury_raw,
        "roster_status": _lookup(
            _ROSTER_STATUS_ALIASES, row["roster_status"], field="roster_status"
        ),
        "absence_reason": _lookup(
            _ABSENCE_REASON_ALIASES, row["absence_reason"], field="absence_reason"
        ),
    }


def _stub_rows(season: int, week: int, teams: list[str], days: list[str]) -> list[dict]:
    """A deterministic week, shaped to exercise every path the real feed has."""
    rows: list[dict] = []
    for team in teams:
        if team == STUB_SILENT_FILER:
            continue
        for index, day in enumerate(days):
            base = {
                "season": str(season),
                "week": str(week),
                "team": team,
                "practice_day": day,
                "report_published_at": f"{season}-09-{16 + index:02d}T21:00:00Z",
                "is_final_report": "true" if day == "friday" else "false",
                "is_estimated": "false",
            }
            if team == STUB_EMPTY_FILER:
                rows.append(
                    {
                        **base,
                        "player_external_id": "",
                        "player_name": "",
                        "position": "",
                        "practice_status": "",
                        "report_status": "",
                        "injury_primary": "",
                        "roster_status": "",
                        "absence_reason": "",
                    }
                )
                continue
            for slot, player in enumerate(_STUB_PLAYERS):
                position, practice, status, injury, roster, reason = player
                rows.append(
                    {
                        **base,
                        "player_external_id": f"{team.lower()}-{slot:02d}",
                        "player_name": f"{team} Player {slot}",
                        "position": position,
                        "practice_status": practice,
                        # Designations land on the final report only, which is
                        # what makes the null-vs-`none` promotion real here.
                        "report_status": status if day == "friday" else "",
                        "injury_primary": injury,
                        "roster_status": roster,
                        "absence_reason": reason,
                    }
                )
    return rows


async def fetch_report_rows(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    teams: list[str],
    days: list[str],
) -> list[dict]:
    """Every wire row for the scoped week, filtered as the document is parsed.

    Raises on an upstream failure rather than returning an empty list. An empty
    list means "every club filed nothing", which is a catastrophic claim;
    `capture.py` turns the exception into a `present: 0` envelope with a
    populated `errors` array instead.

    `teams` and `days` are passed in so the filter runs *during* the parse.
    They narrow what is retained; they never widen what is expected — the
    coverage denominator is the schedule's, not this function's.
    """
    url = upstream_url()
    if not url:
        return _stub_rows(season, week, teams, days)

    wanted_teams = {team.upper() for team in teams}
    wanted_days = set(days)
    kept: list[dict] = []
    rows = stream_csv_dicts(
        client, url.format(season=season, week=week), required_columns=REQUIRED_COLUMNS
    )
    async for row in rows:
        if row["season"].strip() != str(season) or row["week"].strip() != str(week):
            continue
        if (row["team"] or "").strip().upper() not in wanted_teams:
            continue
        if practice_day(row["practice_day"]) not in wanted_days:
            continue
        kept.append(row)
    return kept
