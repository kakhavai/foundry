"""The append-only revision design, and the two assertions the spec names.

These are the highest-value tests in this collector. The failure they guard is
completely silent: because venue data is nominally static, an adapter that
overwrites a record in place retroactively applies a mid-season surface
replacement or roof retrofit to the whole season, so a Week 2 game is
attributed a surface that was not installed until Week 11. Nothing errors,
nothing 500s, coverage stays at 1.0 — the season simply becomes internally
consistent with a fiction.

The spec gives two assertions, and both are here:

1. **No game may resolve to a venue revision whose `[effective_from,
   effective_to)` window excludes its kickoff date.** Checked as a join.
2. **A per-season count of venues with exactly one revision** — a venue known
   to have changed surfaces showing a single revision is the tell.

Every `all(...)` over a collection below is paired with a length assertion.
`all([])` is `True`, so an empty list satisfies every universal claim ever
made about it, and a filter that quietly matched nothing would turn the
strongest assertion in this file into a no-op.
"""

from dataclasses import replace
from datetime import date

import pytest

from venue import reference
from venue.capture import (
    ASSIGNMENT,
    REASON_REVISION_WINDOW_EXCLUDES_KICKOFF,
    STATIC,
)

from .conftest import (
    AFTER_CHANGE_WEEK,
    BEFORE_CHANGE_WEEK,
    SEASON,
    SEASON_GAMES,
    SURFACE_AFTER,
    SURFACE_BEFORE,
    SURFACE_CHANGE_ON,
    SURFACE_CHANGE_VENUE,
    SpyLake,
    game_row,
    round_robin,
    run_capture,
    season_rows,
    sunday_of,
    to_csv,
)

# ── the machinery: windows are derived, never stored ─────────────────────────


def _record(venue_id: str, effective_from: date, **overrides) -> reference.VenueRecord:
    base = reference.REVISIONS[venue_id][0].record
    return replace(base, effective_from=effective_from, **overrides)


def test_a_later_revision_closes_the_earlier_one():
    """`effective_to` is derived from the NEXT record, so there is no field for
    an adapter to overwrite in place."""
    history = reference.build_revisions(
        (
            _record("lambeau", date(2026, 1, 1)),
            _record("lambeau", date(2026, 10, 28), surface_class="synthetic_turf"),
        )
    )
    assert len(history) == 2
    assert history[0].effective_from == date(2026, 1, 1)
    assert history[0].effective_to == date(2026, 10, 28)
    assert history[1].effective_from == date(2026, 10, 28)
    assert history[1].effective_to is None


def test_the_window_is_half_open_so_the_install_date_belongs_to_exactly_one():
    """Closed-closed windows would make the install date match two revisions,
    and `revision_on` would silently take whichever came first."""
    history = reference.build_revisions(
        (_record("lambeau", date(2026, 1, 1)), _record("lambeau", SURFACE_CHANGE_ON))
    )
    matching = [rev for rev in history if rev.contains(SURFACE_CHANGE_ON)]
    assert len(matching) == 1
    assert matching[0].effective_from == SURFACE_CHANGE_ON

    day_before = date(2026, 10, 27)
    matching_before = [rev for rev in history if rev.contains(day_before)]
    assert len(matching_before) == 1
    assert matching_before[0].effective_from == date(2026, 1, 1)


def test_build_revisions_refuses_an_unordered_or_duplicated_effective_from():
    """Both break the property the read-time join relies on. A duplicate makes
    the join ambiguous; a reversed pair makes a negative-length window that
    contains no day at all, so the venue silently exists in no revision."""
    with pytest.raises(ValueError, match="strictly ordered"):
        reference.build_revisions(
            (
                _record("lambeau", date(2026, 10, 28)),
                _record("lambeau", date(2026, 1, 1)),
            )
        )
    with pytest.raises(ValueError, match="strictly ordered"):
        reference.build_revisions(
            (_record("lambeau", date(2026, 1, 1)), _record("lambeau", date(2026, 1, 1)))
        )


def test_build_revisions_refuses_a_list_that_mixes_venues():
    """A silent mix would attach one venue's history to another's id."""
    with pytest.raises(ValueError, match="mixes venue_ids"):
        reference.build_revisions(
            (
                _record("lambeau", date(2026, 1, 1)),
                _record("metlife", date(2026, 6, 1)),
            )
        )


def test_build_revisions_of_nothing_is_nothing():
    assert reference.build_revisions(()) == ()


def test_overlapping_windows_resolve_to_no_revision_rather_than_the_first():
    """`revision_on` returns `None` when more than one revision matches.

    Overlap cannot happen through `build_revisions`, which is why this builds
    the pair by hand — but a caller written against an Optional would silently
    take whichever came first, and "silently took one of two contradictory
    records" is the same class of failure as overwriting in place.
    """
    base = reference.REVISIONS["lambeau"][0]
    overlapping = (
        reference.VenueRevision(
            record=_record("lambeau", date(2026, 1, 1)),
            effective_to=date(2026, 12, 1),
            home_team_ids=base.home_team_ids,
        ),
        reference.VenueRevision(
            record=_record("lambeau", date(2026, 6, 1)),
            effective_to=None,
            home_team_ids=base.home_team_ids,
        ),
    )
    assert sum(rev.contains(date(2026, 9, 1)) for rev in overlapping) == 2

    patched = dict(reference.REVISIONS)
    patched["lambeau"] = overlapping
    original = reference.REVISIONS
    reference.REVISIONS = patched
    try:
        assert len(reference.revisions_containing("lambeau", date(2026, 9, 1))) == 2
        assert reference.revision_on("lambeau", date(2026, 9, 1)) is None
    finally:
        reference.REVISIONS = original


def test_the_committed_table_never_makes_a_claim_before_it_was_compiled():
    """A back-dated record would assert today's surface was true years ago,
    which is the retroactive fiction this collector exists to prevent."""
    assert reference.REVISIONS, "the committed table is empty"
    earliest = [history[0].effective_from for history in reference.REVISIONS.values()]
    assert len(earliest) == len(reference.REVISIONS)
    assert all(day >= reference.TABLE_COMPILED_ON for day in earliest), earliest


def test_a_kickoff_before_the_table_was_compiled_resolves_to_nothing():
    """`None`, never "the closest revision". The fallback IS the failure mode."""
    day_before = reference.TABLE_COMPILED_ON - (date(2026, 7, 31) - date(2026, 7, 30))
    assert reference.revision_on("lambeau", day_before) is None
    assert reference.revisions_containing("lambeau", day_before) == ()


# ── SPEC ASSERTION 1: the kickoff-inside-window join ─────────────────────────


async def test_no_game_resolves_to_a_revision_whose_window_excludes_its_kickoff():
    """The spec's first assertion, checked as a read-time join over real output.

    **What this test can and cannot catch, stated honestly** — review noted the
    docstring used to imply more than the test does. The capture already
    enforces the same rule at WRITE time (`revision_on` cannot return a window
    excluding the day), so this is close to tautological as a check on the join
    itself. What it genuinely guards is `build_assignment_row` writing a
    DIFFERENT revision's window onto the row than the one the join selected —
    a transcription bug that write-time enforcement would not notice and that
    would make every downstream read-time join silently wrong.

    The substantive coverage of the spec's assertion is mutation M15 (the
    capture ignoring the window and taking the newest revision) together with
    `test_a_mid_season_surface_change_is_not_applied_retroactively`, which uses
    a two-revision fixture. This test is the cheap consistency check over the
    full real season alongside them, not the assertion's main proof.

    The length assertion is not decoration: `all([])` is `True`, so without it a
    capture that published nothing would satisfy this perfectly.
    """
    envelopes = await run_capture(SpyLake())
    rows = envelopes[ASSIGNMENT].signals

    assert len(rows) == SEASON_GAMES, f"expected a full season, got {len(rows)}"

    def inside(row: dict) -> bool:
        kickoff = date.fromisoformat(row["kickoff_on"])
        start = date.fromisoformat(row["venue_effective_from"])
        end = row["venue_effective_to"]
        if kickoff < start:
            return False
        return end is None or kickoff < date.fromisoformat(end)

    offenders = [row for row in rows if not inside(row)]
    assert offenders == [], offenders


async def test_a_game_outside_every_window_is_recorded_missing_not_published():
    """The refusal, not just the absence of a violation.

    A game whose kickoff predates the table's claim window must be counted as
    expected-and-missing with a reason, never dropped — a dropped row shrinks
    the numerator and the denominator together and reads as perfect coverage.
    """
    # A season whose games are all played BEFORE the table makes any claim.
    rows = [
        game_row(week=1, away=away, home=home, gameday="2026-01-11")
        for away, home in round_robin(1)
    ]
    envelopes = await run_capture(SpyLake(), csv=to_csv(rows))

    assignment = envelopes[ASSIGNMENT]
    assert assignment.signals == []
    assert len(assignment.coverage.missing) == len(rows)
    reasons = {error["reason"] for error in assignment.errors}
    assert REASON_REVISION_WINDOW_EXCLUDES_KICKOFF in reasons, assignment.errors
    assert assignment.coverage.ratio == 0.0


async def test_a_mid_season_surface_change_is_not_applied_retroactively(
    two_revision_venue,
):
    """THE test. An adapter that overwrote in place would kill this one.

    Two games at the same venue, one either side of a surface installed on
    2026-10-28. The earlier game must resolve to the earlier revision and carry
    the OLD surface; the later game must carry the new one. Collapse the two
    revisions into one current record — which is exactly what overwriting in
    place does — and both games report the new surface, coverage stays 1.0, and
    nothing else in this suite notices.
    """
    rows = [
        game_row(week=BEFORE_CHANGE_WEEK, away="CHI", home="GB"),
        game_row(week=AFTER_CHANGE_WEEK, away="MIN", home="GB"),
    ]
    envelopes = await run_capture(SpyLake(), csv=to_csv(rows))

    by_week = {row["week"]: row for row in envelopes[ASSIGNMENT].signals}
    assert set(by_week) == {BEFORE_CHANGE_WEEK, AFTER_CHANGE_WEEK}, by_week

    early, late = by_week[BEFORE_CHANGE_WEEK], by_week[AFTER_CHANGE_WEEK]
    assert early["venue_id"] == SURFACE_CHANGE_VENUE
    assert late["venue_id"] == SURFACE_CHANGE_VENUE

    # The windows the two games resolved against are DIFFERENT. This is the
    # assertion an in-place overwrite cannot satisfy: one record has one window.
    assert early["venue_effective_to"] == SURFACE_CHANGE_ON.isoformat()
    assert late["venue_effective_from"] == SURFACE_CHANGE_ON.isoformat()
    assert late["venue_effective_to"] is None

    # And the surface each game is attributed follows from that window, which
    # is the fact the generator actually consumes.
    early_revision = reference.revision_on(
        SURFACE_CHANGE_VENUE, date.fromisoformat(early["kickoff_on"])
    )
    late_revision = reference.revision_on(
        SURFACE_CHANGE_VENUE, date.fromisoformat(late["kickoff_on"])
    )
    assert early_revision is not None and late_revision is not None
    assert early_revision.record.surface_class == SURFACE_BEFORE
    assert late_revision.record.surface_class == SURFACE_AFTER


# ── SPEC ASSERTION 2: the per-season single-revision count ───────────────────


def _single_revision_count(venue_ids) -> int:
    ids = list(venue_ids)
    assert ids, "no venues to count — the assertion below would be vacuous"
    return sum(1 for venue_id in ids if len(reference.revisions_for(venue_id)) == 1)


async def test_every_venue_in_the_committed_table_has_exactly_one_revision():
    """The honest state of the table today, asserted rather than assumed.

    No surface change in this table has a sourced install date (see
    `venue/reference.py`'s docstring), so every venue has one revision. That is
    the spec's own tell — "a venue known to have changed surfaces showing a
    single revision" — and pinning it here means the day somebody adds a second
    revision, this test is what tells them the count moved on purpose.
    """
    venue_ids = sorted(reference.REVISIONS)
    assert len(venue_ids) >= 30, venue_ids
    assert _single_revision_count(venue_ids) == len(venue_ids)


async def test_the_single_revision_count_falls_when_a_venue_gains_a_revision(
    two_revision_venue,
):
    """The counter is the alarm, so it has to actually move.

    A count that stayed at "every venue" while a venue genuinely had two
    revisions would be a gauge that can never fire — which is worse than no
    gauge, because it reads as a passing check.
    """
    venue_ids = sorted(reference.REVISIONS)
    assert len(reference.revisions_for(SURFACE_CHANGE_VENUE)) == 2
    assert _single_revision_count(venue_ids) == len(venue_ids) - 1


async def test_the_capture_publishes_one_static_row_per_venue_not_one_per_revision(
    two_revision_venue,
):
    """`venue_static` publishes the revision true TODAY, one row per venue.

    A capture that published the whole history would double-count the changed
    venue in `coverage.present` and make the ratio depend on how many revisions
    a venue happens to have — a collector that reports better coverage because
    a stadium was resurfaced.
    """
    envelopes = await run_capture(SpyLake())
    rows = envelopes[STATIC].signals
    venue_ids = [row["venue_id"] for row in rows]

    assert len(venue_ids) >= 30
    assert len(venue_ids) == len(set(venue_ids)), "a venue appeared twice"
    assert envelopes[STATIC].coverage.present == len(rows)


async def test_a_capture_today_resolves_the_pre_change_revision(two_revision_venue):
    """ "Today" is 2026-09-15, before the fixture's 2026-10-28 install.

    So `venue_static` must publish the OLD surface. A capture that published
    the newest revision regardless of the window would pass every coverage
    check and hand the generator a surface that does not exist yet.
    """
    envelopes = await run_capture(SpyLake())
    changed = [
        row
        for row in envelopes[STATIC].signals
        if row["venue_id"] == SURFACE_CHANGE_VENUE
    ]
    assert len(changed) == 1, changed
    assert changed[0]["surface_class"] == SURFACE_BEFORE
    assert changed[0]["effective_to"] == SURFACE_CHANGE_ON.isoformat()


# ── the content hash, which is what a `static reference` cadence appends on ──


def test_the_content_hash_ignores_the_validity_window():
    """Two revisions carrying identical facts under different dates hash the
    same. That is what "this venue's record did not change" means, and it is
    what lets a daily re-read skip an identical snapshot."""
    first = reference.build_revisions((_record("lambeau", date(2026, 1, 1)),))[0]
    later = reference.build_revisions((_record("lambeau", date(2026, 6, 1)),))[0]
    assert reference.content_hash(first) == reference.content_hash(later)


def test_the_content_hash_changes_when_a_content_field_changes():
    """The other half. A hash that never changed would silently pin the lake to
    the first snapshot this process ever wrote."""
    before = reference.build_revisions((_record("lambeau", date(2026, 1, 1)),))[0]
    after = reference.build_revisions(
        (_record("lambeau", date(2026, 1, 1), surface_class="synthetic_turf"),)
    )[0]
    assert reference.content_hash(before) != reference.content_hash(after)


async def test_every_published_static_row_carries_its_content_hash():
    envelopes = await run_capture(SpyLake())
    rows = envelopes[STATIC].signals
    assert len(rows) >= 30
    assert all(len(row["content_hash"]) == 64 for row in rows)
    # Distinct venues must not collide: a hash that ignored too much would make
    # the whole table one value and the cadence gate meaningless.
    assert len({row["content_hash"] for row in rows}) == len(rows)


def test_the_fixture_season_spans_the_whole_league():
    """Guards every length assertion above from a fixture that quietly shrank.

    `season_rows` is what makes `>= 30 venues` and `== 272 games` meaningful.
    If the round-robin ever stopped covering the league — or stopped swapping
    home and away — those bounds would start passing on a subset and nobody
    would notice, because every one of them would still be satisfiable.
    """
    rows = season_rows(season=SEASON)
    assert len(rows) == SEASON_GAMES
    assert {row["home_team"] for row in rows} == set(reference._HOME_VENUE_IDS), (
        "not every club hosts, so the fixture cannot reach all 30 venues"
    )
    assert sunday_of(1) == "2026-09-13"
