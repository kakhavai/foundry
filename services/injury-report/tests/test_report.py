"""The aggregation layer: what a week of filings becomes, and what it refuses.

`test_coverage_floor.py` proves the denominator. This file proves the rows —
the week-long trajectory that is this collector's actual product, and the
places it declines to guess.
"""

import pytest
from collector_core.coverage import CoverageAccumulator

from injury_report.metrics import metrics
from injury_report.report import build_rows, practice_days_elapsed
from injury_report.vocabulary import NON_INJURY_REASONS, UnknownVocabulary, body_part

from .conftest import NOW, empty_filing, wire

DAYS = practice_days_elapsed(NOW)
SCHEDULE = {"KC": "2026_01_KC_BUF", "BUF": "2026_01_KC_BUF"}
# For the tests that are about one club's rows rather than about coverage: a
# second scheduled club would contribute its own `report_not_published` entries
# and drown the reason under test.
ONE_CLUB = {"KC": "2026_01_KC_BUF"}


def _build(rows, scheduled=None):
    acc = CoverageAccumulator(floor=0)
    aggregate = build_rows(
        dict(SCHEDULE if scheduled is None else scheduled),
        rows,
        now=NOW,
        acc=acc,
        metrics=metrics,
    )
    return aggregate, acc


def _week(team: str, **per_day) -> list[dict]:
    """One club's three filings, with per-day overrides keyed by day label."""
    filings = []
    for index, day in enumerate(DAYS):
        cells = {
            "team": team,
            "practice_day": day,
            "is_final_report": "true" if day == "friday" else "false",
            "report_published_at": f"2026-09-{16 + index:02d}T21:00:00Z",
        }
        cells.update(per_day.get(day, {}))
        filings.append(wire(**cells))
    return filings


def _every_day(**cells) -> dict[str, dict]:
    """The same overrides on all three of the week's filings."""
    return {day: dict(cells) for day in DAYS}


def _only_player(rows) -> dict:
    aggregate, _ = _build(rows)
    assert len(aggregate.player_rows) == 1, aggregate.player_rows
    return aggregate.player_rows[0]


def test_practice_participation_accumulates_across_the_week():
    """The trajectory is the signal. DNP/DNP/Limited means something different
    from Limited/Full/Full, and overwriting each day rather than appending
    would leave only the last one — the least informative of the three."""
    row = _only_player(
        _week(
            "KC",
            wednesday={"practice_status": "dnp"},
            thursday={"practice_status": "dnp"},
            friday={"practice_status": "limited", "report_status": "questionable"},
        )
    )
    trail = [entry["participation"] for entry in row["practice_participation"]]
    assert trail == ["dnp", "dnp", "limited"]
    assert [entry["day_label"] for entry in row["practice_participation"]] == list(DAYS)


def test_a_blank_designation_before_the_final_report_stays_null():
    """`null` and `none` are different facts. Nothing has been published for
    this player yet, and promoting that to the published-healthy `none` would
    assert something the club has not said."""
    row = _only_player(_week("KC", friday={"is_final_report": "false"}))
    assert row["game_designation"] is None
    assert row["designation_history"] == []


def test_a_blank_designation_on_the_final_report_becomes_published_healthy():
    """The other half. The club filed its final report and designated nobody —
    that IS a statement, and it is the one this collector is uniquely able to
    make. Leaving it null would lose it."""
    row = _only_player(_week("KC"))
    assert row["game_designation"] == "none"
    assert [e["designation"] for e in row["designation_history"]] == ["none"]


def test_designation_history_is_in_publication_order():
    row = _only_player(
        _week(
            "KC",
            wednesday={"report_status": "out"},
            thursday={"report_status": "doubtful"},
            friday={"report_status": "questionable"},
        )
    )
    assert [e["designation"] for e in row["designation_history"]] == [
        "out",
        "doubtful",
        "questionable",
    ]
    assert row["game_designation"] == "questionable"
    published = [e["published_at"] for e in row["designation_history"]]
    assert published == sorted(published)


def test_an_unrecognised_designation_emits_nothing_and_says_so():
    """`Probable` was retired from the league's report in 2016. Passing it
    through would put a value in an append-only lake that no consumer can
    interpret; guessing which of `questionable`/`none` it means is worse. The
    row is dropped and the reason recorded."""
    aggregate, acc = _build(
        _week("KC", **_every_day(report_status="Probable")), scheduled=ONE_CLUB
    )
    assert aggregate.player_rows == []
    assert {e["reason"] for e in acc.errors} == {"unknown_designation"}
    assert len(acc.errors) == len(DAYS)
    # The club still counts as having FILED: it did, and recording it as
    # unfiled would manufacture the missing report this collector detects.
    assert not any(key.startswith("KC:") for key in acc.result().missing)


def test_an_unresolvable_player_emits_nothing_and_says_so():
    """A confident id derived from nothing is worse than an absent row: it
    looks resolved to every downstream consumer."""
    rows = _week("KC", **_every_day(player_external_id="", player_name="Somebody"))
    aggregate, acc = _build(rows, scheduled=ONE_CLUB)
    assert aggregate.player_rows == []
    # Note it is NOT counted as an empty filing: the club listed somebody, this
    # collector simply could not say who. `has_player` keys on the id, so a
    # nameless line is dropped with a reason rather than read as "nobody hurt".
    assert {e["reason"] for e in acc.errors} == {"identity_missing_external_id"}


def test_a_rest_day_is_not_recorded_as_an_injury():
    """The phase doc's named failure mode: a veteran given a scheduled rest day
    is coded `dnp` identically to a player who could not practise, and by
    Friday the model has flagged a healthy starter as at-risk."""
    row = _only_player(
        _week("KC", **{day: {"absence_reason": "not injury related"} for day in DAYS})
    )
    assert row["absence_reason"] == "rest"
    assert row["is_non_injury"] is True


def test_a_blank_absence_reason_is_unspecified_never_injury():
    row = _only_player(_week("KC", **{day: {"absence_reason": ""} for day in DAYS}))
    assert row["absence_reason"] == "unspecified"
    # `unspecified` is not `is_non_injury`: "we do not know why" is not "we
    # know it was not an injury".
    assert row["is_non_injury"] is False
    assert "unspecified" not in NON_INJURY_REASONS


def test_a_questionable_player_with_no_practice_record_is_flagged():
    """The phase doc's own assertion. The row is kept — the designation is real
    — but the contradiction is stated rather than left to be noticed."""
    rows = [
        wire(
            team="KC",
            practice_day="friday",
            is_final_report="true",
            practice_status="",
            report_status="questionable",
        )
    ]
    aggregate, acc = _build(rows)
    assert len(aggregate.player_rows) == 1
    assert aggregate.player_rows[0]["game_designation"] == "questionable"
    assert "questionable_without_practice" in {e["reason"] for e in acc.errors}


def test_a_club_with_no_scheduled_game_is_reported_not_dropped():
    """A filing for a club with no game means the schedule upstream and the
    injury feed disagree about the week. Worth seeing long before it becomes a
    coverage number nobody can explain."""
    aggregate, acc = _build(_week("SEA"), scheduled=SCHEDULE)
    assert not [row for row in aggregate.team_rows if row["team"] == "SEA"]
    assert ("team_not_scheduled", "SEA") in {
        (e["reason"], e["detail"]) for e in acc.errors
    }


def test_an_unparseable_filing_timestamp_is_a_missing_report_not_a_silent_one():
    """`report_published_at` is what orders `designation_history`. A filing it
    cannot parse is unusable, and unusable must not read as received."""
    rows = [wire(team="KC", practice_day="wednesday", report_published_at="soon")]
    aggregate, acc = _build(rows)
    assert not aggregate.team_rows
    assert "unknown_report_published_at" in {e["reason"] for e in acc.errors}
    assert "KC:wednesday" in acc.result().missing


def test_the_per_day_filing_counts_are_recorded_for_every_elapsed_day():
    """`injury_report_teams_published / teams_with_games` per practice day is
    the phase doc's guard against a club's feed breaking on one day only. Both
    numbers must exist for every day, including days on which nothing arrived —
    an absent Prometheus series and a healthy one look identical."""
    aggregate, _ = _build([*_week("KC"), *[empty_filing(team="BUF")]])
    assert set(aggregate.owed_by_day) == set(DAYS)
    assert set(aggregate.filed_by_day) == set(DAYS)
    assert all(count == len(SCHEDULE) for count in aggregate.owed_by_day.values())
    assert aggregate.filed_by_day["wednesday"] == 2
    assert aggregate.filed_by_day["friday"] == 1


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("knee", "knee"),
        ("left knee (patellar tendinitis)", "knee"),
        ("Hamstring", "hamstring"),
        ("lower body", "unspecified"),
        ("", "unspecified"),
    ],
)
def test_body_part_degrades_rather_than_guessing(raw, normalized):
    """The one vocabulary that must NOT raise. Guessing a body part mislabels a
    row; declining to name one loses only specificity, and `body_part_raw`
    keeps the original either way."""
    assert body_part(raw) == normalized


def test_an_unknown_position_refuses_rather_than_defaulting():
    """A defender silently filed as offence is invisible to the exact query
    this collector exists to answer."""
    from injury_report.vocabulary import side_of_ball

    assert side_of_ball("CB") == "defense"
    with pytest.raises(UnknownVocabulary):
        side_of_ball("WIZARD")
