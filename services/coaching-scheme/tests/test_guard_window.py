"""Guard 1: a rate window must not straddle a revision boundary.

The spec's named failure mode. **Two arms, and they need separate fixtures**
because they fail identically from the outside — a refused row and a coverage
entry — while catching different bugs:

* **Arm A, a sampled week outside the span.** The direct blend: week 3 data
  attached to a revision that starts in week 9.
* **Arm B, `games_sampled` exceeding the span.** A duplicate fold — the same
  game counted twice — leaves every sampled week correctly *inside* the span
  while doubling the sample. Arm A passes it.

A fixture that only exercised arm A would let `assert_window_within_revision`
lose its second `raise` entirely, and every test here would stay green.
"""

import pytest

from coaching_scheme import rates as rates_module
from coaching_scheme.rates import (
    REASON_GAMES_EXCEED_SPAN,
    REASON_WEEK_OUTSIDE_REVISION,
    WindowStraddlesRevision,
    assert_window_within_revision,
    weeks_in_revision,
)
from coaching_scheme.revisions import Revision

from .conftest import (
    LATER,
    SEASON,
    Feeds,
    SpyLake,
    coaches_with_change,
    run_capture,
)


def revision(
    *,
    from_week: int = 1,
    to_week: int | None = None,
    known_last_week: int = 12,
    team: str = "AAA",
) -> Revision:
    return Revision(
        revision_id=f"{team}-{SEASON}-rX",
        team_id=team,
        season=SEASON,
        effective_from_week=from_week,
        effective_to_week=to_week,
        head_coach_id="coach-x",
        head_coach_name="X",
        change_event="none",
        known_last_week=known_last_week,
    )


# --------------------------------------------------------------------------
# Arm A — a week outside the revision
# --------------------------------------------------------------------------


def test_a_window_inside_the_revision_is_accepted():
    """The negative control. Without it, a guard that raised unconditionally
    would pass every other test in this file."""
    assert (
        assert_window_within_revision(revision(from_week=9, to_week=12), [9, 10, 11], 3)
        is None
    )


def test_a_week_before_the_revision_starts_is_refused():
    """The spec's exact scenario: nine weeks of the fired coordinator blended
    into three of their replacement."""
    with pytest.raises(WindowStraddlesRevision) as caught:
        assert_window_within_revision(revision(from_week=9, to_week=12), [3, 10], 2)
    assert caught.value.reason == REASON_WEEK_OUTSIDE_REVISION
    assert "[3]" in caught.value.detail


def test_a_week_after_the_revision_ends_is_refused():
    """The mirror. A guard checking only the lower bound passes this."""
    with pytest.raises(WindowStraddlesRevision) as caught:
        assert_window_within_revision(revision(from_week=1, to_week=5), [4, 5, 6], 3)
    assert caught.value.reason == REASON_WEEK_OUTSIDE_REVISION
    assert "[6]" in caught.value.detail


def test_a_current_revision_has_no_upper_bound():
    """`effective_to_week is None` means current, so no week can be after it.
    A guard that treated `None` as 0 would refuse every current revision."""
    assert (
        assert_window_within_revision(
            revision(from_week=9, to_week=None, known_last_week=17), [9, 15, 17], 3
        )
        is None
    )


# --------------------------------------------------------------------------
# Arm B — games_sampled exceeding the span
# --------------------------------------------------------------------------


def test_more_games_than_weeks_is_refused_even_when_every_week_is_inside():
    """A duplicate fold. **Arm A cannot see this**: weeks 9-11 are all inside
    the revision, so the only thing wrong is that four games came out of three
    weeks — which is impossible, because a team plays at most one a week."""
    with pytest.raises(WindowStraddlesRevision) as caught:
        assert_window_within_revision(revision(from_week=9, to_week=11), [9, 10, 11], 4)
    assert caught.value.reason == REASON_GAMES_EXCEED_SPAN


def test_exactly_as_many_games_as_weeks_is_accepted():
    """The boundary. `>` rather than `>=` — off by one here would refuse every
    revision whose team had no bye inside it, which is most of them."""
    assert (
        assert_window_within_revision(revision(from_week=9, to_week=11), [9, 10, 11], 3)
        is None
    )


def test_a_current_revision_spans_to_the_last_known_week():
    """`week_span` on an open revision uses `known_last_week`, so arm B still
    has a ceiling. Treating an open revision as unbounded would disable arm B
    for the one revision every team always has."""
    open_revision = revision(from_week=10, to_week=None, known_last_week=12)
    assert open_revision.week_span == 3
    with pytest.raises(WindowStraddlesRevision) as caught:
        assert_window_within_revision(open_revision, [10, 11, 12], 4)
    assert caught.value.reason == REASON_GAMES_EXCEED_SPAN


def test_an_empty_window_is_clean():
    """A revision that has not played yet has nothing to blend. Refusing it
    would turn 'this regime has not played' into an error."""
    empty = assert_window_within_revision(revision(from_week=9, to_week=12), [], 0)
    assert empty is None


# --------------------------------------------------------------------------
# `weeks_in_revision` — the selection guard 1 checks
# --------------------------------------------------------------------------


def test_weeks_in_revision_selects_only_the_governed_weeks():
    assert weeks_in_revision(
        revision(from_week=6, to_week=9), [1, 5, 6, 7, 9, 10, 12]
    ) == (6, 7, 9)


def test_weeks_in_revision_is_ascending_regardless_of_input_order():
    """`aggregate` folds in this order and `sampled_weeks` is published from
    it, so an unsorted return would make the digest depend on dict iteration
    order and defeat the unchanged-snapshot gate."""
    assert weeks_in_revision(revision(from_week=1, to_week=5), [4, 1, 3]) == (1, 3, 4)


# --------------------------------------------------------------------------
# End to end — the guard as `capture` actually applies it
# --------------------------------------------------------------------------


async def test_a_real_two_revision_season_never_blends_its_windows(lake: SpyLake):
    """The whole point, through the real pipeline.

    AAA's coach changes at week 7, so it gets two revisions: weeks 1-6 and
    7-12. Every published week must lie inside its own revision, and the two
    windows must not intersect. An adapter computing season-to-date and
    attaching it to the current revision fails this on both counts.
    """
    envelopes = await run_capture(
        Feeds(coaches=coaches_with_change("AAA", at_week=7)), lake=lake
    )
    rows = [
        row
        for row in envelopes["team_scheme_profile"].signals
        if row["team_id"] == "AAA"
    ]
    assert len(rows) == 2, rows

    windows = {}
    for row in rows:
        # Paired with the length assertion above and the emptiness assertion
        # below: `all([])` is True, so a row set that silently became empty
        # would otherwise pass this.
        assert row["sampled_weeks"], row
        assert all(
            row["effective_from_week"] <= week
            and (row["effective_to_week"] is None or week <= row["effective_to_week"])
            for week in row["sampled_weeks"]
        ), row
        assert row["games_sampled"] <= len(row["sampled_weeks"])
        windows[row["revision_id"]] = set(row["sampled_weeks"])

    first, second = (windows[key] for key in sorted(windows))
    assert first == {1, 2, 3, 4, 5, 6}
    assert second == {7, 8, 9, 10, 11, 12}
    assert not (first & second)


async def test_the_two_revisions_report_the_rates_of_their_own_regime(lake: SpyLake):
    """The blend, made visible as a number.

    AAA's PROE is +1.0 through week 6 and +21.0 from week 7. Season-to-date
    would put both revisions at +11.0 — a figure describing neither regime,
    which is the spec's complaint word for word. Revision-scoped puts them at
    the two levels that actually happened.
    """
    from .conftest import proe_with_shift

    envelopes = await run_capture(
        Feeds(
            coaches=coaches_with_change("AAA", at_week=7),
            proe=proe_with_shift("AAA", at_week=7, shift=20.0),
        ),
        lake=lake,
    )
    rows = sorted(
        (
            row
            for row in envelopes["team_scheme_profile"].signals
            if row["team_id"] == "AAA"
        ),
        key=lambda r: r["effective_from_week"],
    )
    assert len(rows) == 2
    assert rows[0]["pass_rate_over_expected"] == pytest.approx(1.0)
    assert rows[1]["pass_rate_over_expected"] == pytest.approx(21.0)
    # The season-to-date number this design exists to avoid.
    assert 11.0 not in {row["pass_rate_over_expected"] for row in rows}


async def test_a_refused_window_costs_coverage_and_is_named(lake: SpyLake, monkeypatch):
    """A straddling window must not publish. Forced by making the guard refuse
    the first revision it sees — the collector's own structure makes a genuine
    straddle nearly unrepresentable, which is the design working, so the guard
    has to be provoked to prove it is wired in at all.
    """
    real = rates_module.assert_window_within_revision
    seen: list[str] = []

    def refuse_first(revision_, sampled_weeks, games_sampled):
        if not seen:
            seen.append(revision_.revision_id)
            raise WindowStraddlesRevision(
                REASON_WEEK_OUTSIDE_REVISION, "forced for this test"
            )
        return real(revision_, sampled_weeks, games_sampled)

    monkeypatch.setattr(rates_module, "assert_window_within_revision", refuse_first)

    envelopes = await run_capture(lake=lake, now=LATER)
    profile = envelopes["team_scheme_profile"]
    published = {row["revision_id"] for row in profile.signals}

    assert seen, "the guard was never called"
    assert seen[0] not in published
    assert seen[0] in profile.coverage.missing
    assert REASON_WEEK_OUTSIDE_REVISION in {error["reason"] for error in profile.errors}
