"""The curated play-caller register: resolution, expiry, and what it refuses.

`play_callers.py`'s docstring carries the full argument. What is pinned here:

* the expiry actually expires — the answer to "what happens in week 10 when
  the entry is three revisions out of date";
* the three refusal reasons stay distinct, because they imply different work;
* a malformed entry is fatal at import rather than degrading quietly;
* **`play_caller_id` is never the head coach.** That is the one thing this
  collector must not do, and it is asserted against the real capture output
  rather than by reading the source.
"""

import pytest

from coaching_scheme.play_callers import (
    ASSERTIONS,
    REASON_EXPIRED,
    REASON_NOT_YET_EFFECTIVE,
    REASON_SPLIT_WITHIN_REVISION,
    REASON_UNKNOWN,
    PlayCallerAssertion,
    resolve,
    resolve_for_span,
    validate,
)

from .conftest import SpyLake, run_capture


def entry(**overrides) -> PlayCallerAssertion:
    base = dict(
        team_id="AAA",
        season=2026,
        play_caller_id="coach-someone",
        play_caller_role="offensive_coordinator",
        effective_from_week=1,
        asserted_through_week=6,
        source="https://example.test/report",
    )
    base.update(overrides)
    return PlayCallerAssertion(**base)


# --------------------------------------------------------------------------
# The register as shipped
# --------------------------------------------------------------------------


def test_the_register_ships_empty():
    """Deliberate, and the reason `staff_assignment` coverage starts at 0/32.

    An entry added without evidence would be exactly the fabrication this
    module exists to prevent, so an empty register is asserted rather than
    merely happening to be true.
    """
    assert ASSERTIONS == ()


def test_the_shipped_register_validates():
    """`validate()` runs at import; this states the invariant for a future
    curator who adds entries and wonders whether anything checks them."""
    assert validate() is None


# --------------------------------------------------------------------------
# resolve — three distinct refusals
# --------------------------------------------------------------------------


def test_an_unknown_team_is_unknown_not_expired():
    assert resolve("ZZZ", 2026, 1, assertions=(entry(),)) == (None, REASON_UNKNOWN)


def test_an_in_force_assertion_resolves():
    found, reason = resolve("AAA", 2026, 3, assertions=(entry(),))
    assert reason is None
    assert found is not None
    assert found.play_caller_id == "coach-someone"


def test_the_boundary_weeks_are_inclusive():
    """`<=` on both ends. Off by one here silently drops the first and last
    week of every assertion, which is invisible against a season-long one."""
    assertions = (entry(effective_from_week=3, asserted_through_week=5),)
    assert resolve("AAA", 2026, 3, assertions=assertions)[0] is not None
    assert resolve("AAA", 2026, 5, assertions=assertions)[0] is not None
    assert resolve("AAA", 2026, 2, assertions=assertions)[0] is None
    assert resolve("AAA", 2026, 6, assertions=assertions)[0] is None


def test_a_stale_assertion_expires_rather_than_persisting():
    """**The staleness story, and the whole reason this is not just a file.**

    Week 10 against an entry sourced through week 6. A register without
    expiry answers `coach-someone` here — an unevidenced claim outliving its
    own justification, which is a defaulted head coach with extra steps.
    """
    found, reason = resolve("AAA", 2026, 10, assertions=(entry(),))
    assert found is None
    assert reason == REASON_EXPIRED


def test_a_future_assertion_is_not_yet_effective_rather_than_expired():
    """The two refusals must not collapse. 'Expired' says re-check a claim
    that was once evidenced; 'not yet effective' says the curator recorded a
    change ahead of the week being captured. Different work."""
    future = (entry(effective_from_week=5, asserted_through_week=9),)
    found, reason = resolve("AAA", 2026, 2, assertions=future)
    assert found is None
    assert reason == REASON_NOT_YET_EFFECTIVE


def test_a_season_mismatch_is_unknown():
    """Assertions are season-scoped: last year's staff is not evidence about
    this year's, and silently reusing it is the staleness failure at a
    year's scale."""
    assert resolve("AAA", 2027, 1, assertions=(entry(),))[1] == REASON_UNKNOWN


def test_two_consecutive_assertions_both_resolve():
    """A team whose play-caller genuinely changed mid-season. Both windows
    must answer, or the register cannot represent the event it exists for."""
    assertions = (
        entry(effective_from_week=1, asserted_through_week=6),
        entry(
            effective_from_week=7,
            asserted_through_week=12,
            play_caller_id="coach-other",
            play_caller_role="position_coach",
        ),
    )
    assert resolve("AAA", 2026, 3, assertions=assertions)[0].play_caller_id == (
        "coach-someone"
    )
    assert resolve("AAA", 2026, 9, assertions=assertions)[0].play_caller_id == (
        "coach-other"
    )


# --------------------------------------------------------------------------
# validate — a curation defect is fatal
# --------------------------------------------------------------------------


def test_an_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="is not one of"):
        validate((entry(play_caller_role="head-coach"),))


def test_an_unsourced_assertion_is_rejected():
    """An assertion with no evidence is indistinguishable from a guess."""
    with pytest.raises(ValueError, match="must cite a source"):
        validate((entry(source="   "),))


def test_an_empty_play_caller_id_is_rejected():
    with pytest.raises(ValueError, match="empty play_caller_id"):
        validate((entry(play_caller_id=""),))


def test_an_inverted_week_range_is_rejected():
    with pytest.raises(ValueError, match="precedes effective_from_week"):
        validate((entry(effective_from_week=9, asserted_through_week=4),))


def test_overlapping_assertions_are_rejected():
    """Two claims covering week 5 would make `resolve` return whichever came
    first in the tuple — an ordering-dependent answer to a factual question."""
    with pytest.raises(ValueError, match="overlap at week"):
        validate(
            (
                entry(effective_from_week=1, asserted_through_week=6),
                entry(effective_from_week=5, asserted_through_week=9),
            )
        )


def test_two_teams_with_the_same_weeks_do_not_overlap():
    """The negative control for the overlap check: it must be scoped by team
    and season, or the register cannot hold more than one team."""
    assert validate((entry(team_id="AAA"), entry(team_id="BBB"))) is None


def test_unknown_is_a_legitimate_role():
    """The spec is explicit: `unknown` counts as present beside a real id. A
    validator rejecting it would make the spec's own example unrepresentable."""
    assert validate((entry(play_caller_role="unknown"),)) is None


# --------------------------------------------------------------------------
# The thing this collector must never do
# --------------------------------------------------------------------------


async def test_play_caller_id_is_never_defaulted_to_the_head_coach(lake: SpyLake):
    """**The single worst thing this collector could ship.**

    The head coach is right often enough to look correct and wrong exactly
    when a play-calling handoff happens — the event the collector exists to
    detect. Asserted against real capture output: every row here has a
    populated `head_coach_id` and an empty register, so a default would show
    up as `play_caller_id == head_coach_id`.
    """
    envelopes = await run_capture(lake=lake)
    rows = envelopes["staff_assignment"].signals
    assert rows, "no rows — the fixture is broken, not the guard"
    assert all(row["head_coach_id"] is not None for row in rows)
    assert all(row["play_caller_id"] is None for row in rows)
    assert all(row["play_caller_role"] is None for row in rows)
    assert all(row["play_caller_missing_reason"] == REASON_UNKNOWN for row in rows)


async def test_a_resolved_assertion_carries_its_provenance_onto_the_wire(
    lake: SpyLake, monkeypatch
):
    """A populated `play_caller_id` always ships with the evidence behind it,
    so a consumer never has to take the claim on trust.

    `asserted_through_week=12` covers the whole revision. Anything shorter is
    refused by `resolve_for_span` — see the F2 tests below.
    """
    from coaching_scheme import play_callers

    monkeypatch.setattr(play_callers, "ASSERTIONS", (entry(asserted_through_week=12),))
    envelopes = await run_capture(lake=lake)
    resolved = [
        row
        for row in envelopes["staff_assignment"].signals
        if row["play_caller_id"] is not None
    ]
    assert resolved
    for row in resolved:
        assert row["team_id"] == "AAA"
        assert row["play_caller_source"] == "https://example.test/report"
        assert row["play_caller_missing_reason"] is None
        assert row["play_caller_id"] != row["head_coach_id"]


# --------------------------------------------------------------------------
# resolve_for_span — F2. The play-caller belongs to a REVISION, not a query.
# --------------------------------------------------------------------------


def test_an_assertion_covering_the_whole_span_resolves():
    assertions = (entry(effective_from_week=1, asserted_through_week=12),)
    found, reason = resolve_for_span("AAA", 2026, 1, 12, assertions=assertions)
    assert reason is None
    assert found is not None and found.play_caller_id == "coach-someone"


def test_an_assertion_that_starts_mid_span_does_not_govern_the_span():
    """**The F2 defect, as a unit test.**

    A register asserting weeks 9-12, against the weeks 1-8 regime. Resolving
    at the *query* week (say 10) returns the assertion and stamps it — and its
    source — onto a regime that ended in week 8. Over the span it correctly
    refuses.
    """
    assertions = (entry(effective_from_week=9, asserted_through_week=12),)
    # The old behaviour, still available and still correct for a single week:
    assert resolve("AAA", 2026, 10, assertions=assertions)[0] is not None
    # The span-scoped question has a different, correct answer.
    found, reason = resolve_for_span("AAA", 2026, 1, 8, assertions=assertions)
    assert found is None
    assert reason == REASON_NOT_YET_EFFECTIVE


def test_an_assertion_that_expires_mid_span_does_not_govern_the_span():
    """The other direction, and the reason resolving at the revision's FIRST
    week is not good enough either.

    An assertion sourced through week 12, against a revision running to week
    17. Resolving at week 9 would return it and stamp an unevidenced claim on
    weeks 13-17 — the staleness bug, moved rather than fixed.
    """
    assertions = (entry(effective_from_week=9, asserted_through_week=12),)
    assert resolve("AAA", 2026, 9, assertions=assertions)[0] is not None
    found, reason = resolve_for_span("AAA", 2026, 9, 17, assertions=assertions)
    assert found is None
    assert reason == REASON_EXPIRED


def test_two_assertions_inside_one_revision_are_refused_not_chosen_between():
    """A play-calling handoff with no staff change. One row cannot state two
    play-callers, and picking either attaches a fact about one regime to
    another — the same error again."""
    assertions = (
        entry(effective_from_week=1, asserted_through_week=6),
        entry(
            effective_from_week=7,
            asserted_through_week=12,
            play_caller_id="coach-other",
        ),
    )
    found, reason = resolve_for_span("AAA", 2026, 1, 12, assertions=assertions)
    assert found is None
    assert reason == REASON_SPLIT_WITHIN_REVISION


def test_an_empty_span_is_unknown_rather_than_silently_resolved():
    found, reason = resolve_for_span("AAA", 2026, 5, 4, assertions=(entry(),))
    assert found is None
    assert reason == REASON_UNKNOWN


async def test_a_partial_assertion_is_not_stamped_on_the_earlier_regime(
    lake: SpyLake, monkeypatch
):
    """**F2 end to end, on the rows a consumer actually reads.**

    AAA's coach changes at week 7, so it has two revisions: weeks 1-6 and
    7-12. The register asserts a play-caller for weeks 7-12 only. The weeks
    1-6 row must carry `None` — and, critically, must NOT carry the week-7
    source as evidence for a regime that ended in week 6.

    This is served on `/signals` and on `GET /teams/{id}/revisions`, which is
    the timeline route where a consumer reads revisions side by side.
    """
    from coaching_scheme import play_callers

    from .conftest import Feeds, coaches_with_change

    monkeypatch.setattr(
        play_callers,
        "ASSERTIONS",
        (entry(effective_from_week=7, asserted_through_week=12),),
    )
    envelopes = await run_capture(
        Feeds(coaches=coaches_with_change("AAA", at_week=7)), lake=lake
    )
    rows = {
        row["revision_id"]: row
        for row in envelopes["staff_assignment"].signals
        if row["team_id"] == "AAA"
    }
    assert len(rows) == 2

    earlier = rows["AAA-2026-r1"]
    later = rows["AAA-2026-r2"]
    assert earlier["effective_to_week"] == 6
    assert earlier["play_caller_id"] is None
    assert earlier["play_caller_source"] is None
    assert earlier["play_caller_missing_reason"] == REASON_NOT_YET_EFFECTIVE
    assert later["play_caller_id"] == "coach-someone"
    assert later["play_caller_source"] == "https://example.test/report"


async def test_an_assertion_that_expires_before_the_regime_ends_is_refused(
    lake: SpyLake, monkeypatch
):
    """**The other half of F2, and the half a unit test cannot reach.**

    `capture` must resolve over the revision's WHOLE span, not just its first
    week. One steady revision runs weeks 1-12; the register asserts weeks 1-6.
    Resolving at the revision's start alone returns the assertion and stamps
    an unevidenced claim on weeks 7-12 — the staleness bug relocated rather
    than fixed, and invisible to `test_an_assertion_that_expires_mid_span_...`
    because that one calls `resolve_for_span` with explicit weeks.
    """
    from coaching_scheme import play_callers

    monkeypatch.setattr(play_callers, "ASSERTIONS", (entry(asserted_through_week=6),))
    envelopes = await run_capture(lake=lake)
    rows = [
        row for row in envelopes["staff_assignment"].signals if row["team_id"] == "AAA"
    ]
    assert len(rows) == 1
    assert rows[0]["effective_to_week"] is None  # runs to the end of the grid
    assert rows[0]["play_caller_id"] is None
    assert rows[0]["play_caller_source"] is None
    assert rows[0]["play_caller_missing_reason"] == REASON_EXPIRED
