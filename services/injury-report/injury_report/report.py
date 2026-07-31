"""Turning a week of filings into the two signals, and counting what is owed.

This is where `injury-report`'s one hard requirement lives: **"no injuries
reported" and "no report arrived" must never become the same fact.** A club
with nobody hurt genuinely files a report listing nobody, and conflating that
with a club whose feed broke reads downstream as a fully healthy roster — the
precise hole this collector exists to close.

The two are separated structurally rather than by a flag, so nothing has to
remember to check:

| upstream                       | `team_injury_report` row      | coverage        |
|--------------------------------|-------------------------------|-----------------|
| club filed, players listed     | `filing_status: "published"`  | `record`        |
| club filed, listed nobody      | `filing_status: "empty"`      | `record`        |
| club filed nothing             | *no row at all*               | `fail(...)`     |

A club that filed appears in `signals` either way, so a consumer reading only
the rows sees the difference. A club that did not file appears in
`coverage.missing` with reason `report_not_published`, so a consumer reading
only coverage sees it too. Neither channel can express "healthy" for a club
that said nothing.

**`coverage.expected` is one filing per scheduled club per practice day
elapsed**, exactly as the phase doc states it. Both halves come from outside
the injury feed: the clubs from the schedule upstream, the days from the clock.
Nothing here is derived from what the fetch happened to return.

**The report week runs Wednesday to Tuesday.** Wednesday's practice report is
the first thing a club owes and Friday's is its final one; Saturday through
Tuesday are the tail of the same week, when all three have been filed. That is
why `practice_days_elapsed` never returns an empty tuple — an `expected` of 0
would make `Coverage.ratio` read 1.0, and a pass that captured nothing would
report perfect coverage.
"""

from dataclasses import dataclass, field
from datetime import datetime

from .adapters.identity import UnresolvablePlayer, resolve_player_id
from .adapters.upstream import (
    has_player,
    normalize_filing,
    normalize_player,
    practice_day,
)
from .vocabulary import NON_INJURY_REASONS, PRACTICE_DAYS, UnknownVocabulary

# Monday=0 .. Sunday=6, per `datetime.weekday()`. Wednesday sees only its own
# practice; Thursday sees two; every other day of the report week sees all
# three, because Friday's final report closes it.
_WEDNESDAY = 2
_THURSDAY = 3


def practice_days_elapsed(now: datetime) -> tuple[str, ...]:
    """Which of the week's practice days a capture at `now` can expect to see.

    Derived from the clock alone. It is deliberately not derived from the
    filings — "how many days have elapsed" is the denominator, and reading it
    off the documents that arrived would make a Friday on which nobody filed
    look like a Wednesday.
    """
    weekday = now.weekday()
    if weekday == _WEDNESDAY:
        return PRACTICE_DAYS[:1]
    if weekday == _THURSDAY:
        return PRACTICE_DAYS[:2]
    return PRACTICE_DAYS


def coverage_key(team: str, day: str) -> str:
    """The key that appears in `coverage.missing`.

    Stable across passes and unique within one: a key that changed between
    captures would make every club look newly missing.
    """
    return f"{team}:{day}"


@dataclass
class Aggregate:
    """What one pass produced, plus the numbers the per-day metrics need."""

    team_rows: list[dict] = field(default_factory=list)
    player_rows: list[dict] = field(default_factory=list)
    # Clubs that filed, and clubs that owed a filing, per practice day. The
    # phase doc asks for `injury_report_teams_published / teams_with_games`
    # tracked per day; recorded as two gauges so PromQL can divide them and a
    # reader can still see the counts.
    filed_by_day: dict[str, int] = field(default_factory=dict)
    owed_by_day: dict[str, int] = field(default_factory=dict)


def _group_filings(rows: list[dict], acc, metrics) -> dict[tuple[str, str], list[dict]]:
    """Wire rows grouped by `(team, practice_day)`.

    Grouped on the *wire* team and day rather than a normalized filing, because
    a filing whose metadata will not parse still has to be attributable to the
    club that sent it — otherwise an unparseable timestamp silently becomes a
    missing report.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        team = (row.get("team") or "").strip().upper()
        day = practice_day(row.get("practice_day", ""))
        if not team or day is None:
            acc.add_error("unattributable_filing", f"{team or '?'}:{day or '?'}")
            metrics.unmapped_row("unattributable_filing")
            continue
        grouped.setdefault((team, day), []).append(row)
    return grouped


def _build_team_row(filing: dict, rows: list[dict], game_id: str) -> dict:
    """One `team_injury_report` row: the record that a club filed at all.

    `player_count` of 0 with `filing_status: "empty"` is a complete, healthy
    observation. There is no row at all for a club that filed nothing.
    """
    listed = [row for row in rows if has_player(row)]
    return {
        "team": filing["team"],
        "practice_day": filing["practice_day"],
        "game_id": game_id,
        "report_published_at": filing["report_published_at"],
        "is_final_report": filing["is_final_report"],
        "is_estimated": filing["is_estimated"],
        "filing_status": "published" if listed else "empty",
        "player_count": len(listed),
    }


def _new_player_row(player: dict, filing: dict, game_id: str, player_id: str) -> dict:
    return {
        "player_id": player_id,
        "team": filing["team"],
        "side_of_ball": player["side_of_ball"],
        # Stays `None` until something is actually published. `None` and
        # `"none"` are different facts and the whole collector turns on it.
        "game_designation": None,
        "designation_history": [],
        "practice_participation": [],
        "body_part": player["body_part"],
        "body_part_raw": player["body_part_raw"],
        "is_non_injury": player["absence_reason"] in NON_INJURY_REASONS,
        "absence_reason": player["absence_reason"],
        "roster_status": player["roster_status"],
        "report_published_at": filing["report_published_at"],
        "is_final_report": filing["is_final_report"],
        "game_id": game_id,
    }


def _apply_filing(row: dict, player: dict, filing: dict) -> None:
    """Fold one day's line for a player into that player's week.

    Appends rather than overwrites, per the phase doc: practice participation
    and the game designation arrive from different places on different days, so
    the trajectory (DNP/DNP/Limited versus Limited/Full/Full) only exists if
    each day is kept. The scalar fields take the latest filing's value.
    """
    published_at = filing["report_published_at"]

    designation = player["designation"]
    # The promotion that only this layer can make: a blank designation on a
    # club's FINAL report means "published, and this player carries no
    # designation" — the healthy `none`. A blank on any earlier day means "not
    # published yet", which stays absent from the history.
    if designation is None and filing["is_final_report"]:
        designation = "none"
    if designation is not None:
        row["designation_history"].append(
            {"published_at": published_at, "designation": designation}
        )
        row["game_designation"] = designation

    if player["participation"] is not None:
        row["practice_participation"].append(
            {
                "practice_date": published_at[:10],
                "day_label": filing["practice_day"],
                "participation": player["participation"],
                "is_estimated": filing["is_estimated"],
            }
        )

    row["body_part"] = player["body_part"]
    row["body_part_raw"] = player["body_part_raw"]
    row["absence_reason"] = player["absence_reason"]
    row["is_non_injury"] = player["absence_reason"] in NON_INJURY_REASONS
    row["roster_status"] = player["roster_status"]
    row["report_published_at"] = published_at
    row["is_final_report"] = filing["is_final_report"]


def build_rows(
    scheduled: dict[str, str],
    rows: list[dict],
    *,
    now: datetime,
    acc,
    metrics,
) -> Aggregate:
    """Fold a week of wire rows into both signals, declaring coverage as it goes.

    `acc` is a `collector_core.coverage.CoverageAccumulator`; every key is
    declared with `expect` because the *schedule* says the club owes a filing,
    and recorded only once a filing has actually been parsed. `metrics` is this
    collector's `InjuryReportMetrics`.
    """
    days = practice_days_elapsed(now)
    grouped = _group_filings(rows, acc, metrics)
    aggregate = Aggregate()

    # Clubs that filed but have no game this week. Not silently dropped: it
    # means the schedule and the injury feed disagree about the week, which is
    # worth seeing long before it becomes a coverage number nobody can explain.
    for team in sorted({team for team, _ in grouped} - set(scheduled)):
        acc.add_error("team_not_scheduled", team)
        metrics.unmapped_row("team_not_scheduled")

    players: dict[tuple[str, str], dict] = {}
    for day in days:
        aggregate.owed_by_day[day] = len(scheduled)
        aggregate.filed_by_day[day] = 0
        for team in sorted(scheduled):
            key = coverage_key(team, day)
            # Declared because the schedule says this club owes a filing today
            # — never because one arrived.
            acc.expect(key)
            filing_rows = grouped.get((team, day))
            if not filing_rows:
                # The whole point. A club that published nothing is a missing
                # report, never a roster of healthy players.
                acc.fail(key, "report_not_published")
                continue
            try:
                filing = normalize_filing(filing_rows[0])
            except UnknownVocabulary as exc:
                acc.fail(key, exc.reason)
                metrics.unmapped_row(exc.reason)
                continue

            aggregate.filed_by_day[day] += 1
            aggregate.team_rows.append(
                _build_team_row(filing, filing_rows, scheduled[team])
            )
            acc.record(key)

            for wire_row in filing_rows:
                if not has_player(wire_row):
                    continue
                _fold_player(
                    wire_row, filing, scheduled[team], players, acc=acc, metrics=metrics
                )

    aggregate.player_rows = [players[key] for key in sorted(players)]
    for row in aggregate.player_rows:
        # The phase doc's own assertion. The row is kept rather than dropped:
        # the designation is real information, and losing it would be a worse
        # error than publishing it with a gap that is stated in `errors`.
        if (
            row["game_designation"] == "questionable"
            and not row["practice_participation"]
        ):
            acc.add_error("questionable_without_practice", row["player_id"])
    return aggregate


def _fold_player(
    wire_row: dict,
    filing: dict,
    game_id: str,
    players: dict[tuple[str, str], dict],
    *,
    acc,
    metrics,
) -> None:
    """One player line, mapped and folded — or declined and recorded.

    A line this collector cannot map emits nothing. A guessed designation is
    worse than a declared gap: it is indistinguishable from a real one
    downstream, where this error entry is not.
    """
    try:
        player = normalize_player(wire_row)
        player_id = resolve_player_id(player["external_id"])
    except (UnknownVocabulary, UnresolvablePlayer) as exc:
        detail = f"{filing['team']}:{(wire_row.get('player_external_id') or '?')}"
        acc.add_error(exc.reason, detail)
        metrics.unmapped_row(exc.reason)
        return

    key = (filing["team"], player_id)
    if key not in players:
        players[key] = _new_player_row(player, filing, game_id, player_id)
    _apply_filing(players[key], player, filing)
