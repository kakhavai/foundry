"""The spec's NAMED FAILURE MODE, guarded rather than noted.

> Games missed for non-injury reasons — a suspension, a personal-leave absence,
> a healthy scratch, or a late-season rest week for a team already seeded — get
> folded into `career_games_missed_injury`, and the player acquires a durability
> problem they do not have. It looks entirely plausible: the availability rate is
> a believable number, just wrong, and it biases every downstream projection for
> a player who has never been hurt.

The guard has two halves and both are tested here:

1. a **required `absence_reason` on every counted missed game, sourced from the
   designation rather than inferred from absence**; and
2. an assertion that injury-attributed missed games **never exceed** the games
   that carried a designation.

Charlie is the whole point of the fixture: he misses four of six games in the
scoped season and **not one of them is an injury**. A collector that inferred
injury from absence gives him `availability_rate: 0.78` instead of `1.00`, and
every other assertion about him still passes.
"""

from datetime import date

import httpx
import pytest
import respx

from durability_history import derive
from durability_history.adapters.upstream import DesignationRow
from durability_history.capture import (
    DURABILITY_PROFILE,
    INJURY_HISTORY,
    capture_durability_history,
)
from durability_history.events import COUNTED_ABSENCE_REASON, classify_absence

from .conftest import (
    CANONICAL_IDS,
    CHARLIE,
    NOW,
    SEASON,
    WEEK,
    mock_identity,
    mock_upstreams,
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def designation(
    primary: str = "", *, status: str = "", practice: str = ""
) -> DesignationRow:
    return DesignationRow(
        season=2026,
        week=1,
        team="SEA",
        gsis_id="00-0000001",
        game_type="REG",
        report_status=status,
        report_primary_injury=primary,
        report_secondary_injury="",
        practice_primary_injury="",
        practice_secondary_injury="",
        practice_status=practice,
        reported_at=date(2026, 9, 5),
    )


# ── half one: the reason is SOURCED, never inferred ──────────────────────────


def test_no_designation_is_undesignated_never_injury():
    """The single most important line in this collector. "Absent, therefore
    injured" is exactly the inference the guard forbids."""
    assert classify_absence(None) == "undesignated"
    assert classify_absence(None) != COUNTED_ABSENCE_REASON


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Not injury related - coach's decision", "discipline"),
        ("Coach's Decision", "discipline"),
        ("Coaching Decision", "discipline"),
        # Only the `not injury related - coach` prefix catches this one: no
        # apostrophe, no "coaching". Clubs are not consistent about the
        # possessive across seasons, and mutation testing showed that without a
        # case only that pattern matches, the pattern is dead weight nothing
        # would notice being deleted.
        ("Not injury related - coach decision", "discipline"),
        ("Suspended by the club", "discipline"),
        ("Not injury related - resting player", "rest"),
        ("Not injury related - personal matter", "personal"),
        ("Jury Duty", "personal"),
        ("Not injury related - other", "non_injury_other"),
        ("Illness", "illness"),
    ],
)
def test_a_non_injury_designation_is_never_counted_as_an_injury(text, expected):
    """Every one of these is a real string in `injuries_2024.csv`, and every one
    of them costs a player an availability point they should keep."""
    assert classify_absence(designation(text, status="Out")) == expected
    assert classify_absence(designation(text, status="Out")) != COUNTED_ABSENCE_REASON


def test_a_non_injury_phrase_beats_a_body_part_in_the_same_cell():
    """nflverse really publishes `Ankle [Not Injury Related - Personal, Thursday
    Only]`. Testing the body-part tokens first would classify that as a
    hamstring-class injury with an ankle on it, which is the failure mode wearing
    a body part."""
    row = designation("Ankle [Not Injury Related - Personal, Thursday Only]",
                      status="Out")  # fmt: skip
    assert classify_absence(row) == "personal"


@pytest.mark.parametrize(
    "text", ["Hamstring", "Left Knee", "Concussion", "Right Ankle", "Calf"]
)
def test_a_genuine_injury_designation_IS_counted(text):
    """The guard must not be so strict that it stops counting real injuries — an
    availability rate of 1.00 for everybody is exactly as useless as one that
    counts suspensions."""
    assert classify_absence(designation(text, status="Out")) == COUNTED_ABSENCE_REASON


def test_a_blank_practice_only_line_is_not_a_game_absence_reason():
    """Being limited in Wednesday practice is not a stated reason for missing
    Sunday's game. A row with no cause and no game status stays undesignated."""
    row = designation("", status="", practice="Limited Participation in Practice")
    assert classify_absence(row) == "undesignated"


def test_a_bare_out_with_no_cause_text_is_an_injury():
    """A club listing a player Out on the INJURY REPORT has designated them out
    for injury-report reasons, and the non-injury cases carry explicit text. 3 of
    6,215 rows in `injuries_2024.csv` look like this."""
    assert classify_absence(designation("", status="Out")) == COUNTED_ABSENCE_REASON
    assert (
        classify_absence(designation("", status="Doubtful")) == COUNTED_ABSENCE_REASON
    )


@respx.mock
async def test_a_player_who_missed_four_games_for_no_injury_keeps_a_perfect_rate(lake):
    """The named failure mode, end to end.

    Charlie missed two games to a coach's decision, one to a personal matter, and
    one with no designation at all. `career_games_missed_injury` must be 0 and
    `availability_rate` must be 1.0 — the durability problem he does not have.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    row = next(
        r
        for r in envelopes[DURABILITY_PROFILE].signals
        if r["player_id"] == CANONICAL_IDS[CHARLIE]
    )
    assert row["career_games_possible"] == 18
    assert row["career_games_missed_injury"] == 0
    assert row["availability_rate"] == 1.0
    assert row["games_missed_by_reason"] == {
        "injury": 0,
        "illness": 0,
        "personal": 1,
        "discipline": 2,
        "rest": 0,
        "non_injury_other": 0,
        "undesignated": 1,
    }


@respx.mock
async def test_an_undesignated_absence_is_published_rather_than_hidden(lake):
    """Not counting it is only half the job. An absence nobody explained is a
    fact about the feed, and burying it would make a quiet injury report
    indistinguishable from a healthy league."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    row = next(
        r
        for r in envelopes[INJURY_HISTORY].signals
        if r["player_id"] == CANONICAL_IDS[CHARLIE]
    )
    undesignated = [a for a in row["absences"] if a["absence_reason"] == "undesignated"]
    assert len(undesignated) == 1
    assert undesignated[0]["week"] == 4
    assert undesignated[0]["body_part"] is None


@respx.mock
async def test_every_absence_row_carries_a_non_null_reason(lake):
    """The spec's "required `absence_reason` on every counted missed game", made
    structural: a null would be indistinguishable from "we never looked"."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    absences = [a for r in envelopes[INJURY_HISTORY].signals for a in r["absences"]]
    assert len(absences) >= 8, absences
    assert all(a["absence_reason"] is not None for a in absences)
    assert all(a["absence_reason"] for a in absences)


# ── half two: the assertion ──────────────────────────────────────────────────


@respx.mock
async def test_injury_misses_never_exceed_designated_games(lake):
    """The spec's assertion, checked against the PUBLISHED numbers rather than
    against an internal counter — both halves are on the profile row precisely so
    a consumer can run this check itself."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    rows = envelopes[DURABILITY_PROFILE].signals
    assert rows, "nothing to assert over"
    for row in rows:
        assert row["career_games_missed_injury"] <= row["designated_games"], row


@respx.mock
async def test_the_published_reason_counts_add_up_to_the_injury_total(lake):
    """A consumer summing `absence_reason == "injury"` over `injury_history` must
    get the same number `durability_profile` reports. Two published totals that
    disagree are worse than one."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    by_player = {r["player_id"]: r for r in envelopes[INJURY_HISTORY].signals}
    for row in envelopes[DURABILITY_PROFILE].signals:
        from_absences = sum(
            1
            for a in by_player[row["player_id"]]["absences"]
            if a["absence_reason"] == "injury"
        )
        assert from_absences == row["career_games_missed_injury"], row["player_id"]


async def test_a_violation_is_raised_as_a_priority_error(lake, monkeypatch):
    """The assertion holds by construction, which is exactly why it is checked:
    it is the property that breaks first if anybody ever makes absence itself
    imply an injury. Forced here so the reporting path is proven rather than
    assumed dead code."""
    monkeypatch.setattr(derive, "injury_absence_invariant_holds", lambda history: False)
    with respx.mock:
        mock_upstreams(respx.mock)
        mock_identity(respx.mock)
        envelopes = await capture(lake)

    for envelope in envelopes.values():
        reasons = [error["reason"] for error in envelope.errors]
        assert "injury_attribution_exceeds_designations" in reasons, reasons
        # Priority, so a few hundred routine per-slot failures cannot push the
        # one entry that says the collector fabricated data past the 50-cap.
        assert reasons.index("injury_attribution_exceeds_designations") <= 1, reasons
