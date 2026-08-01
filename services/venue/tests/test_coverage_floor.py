"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned. These tests
fail if somebody "simplifies" `capture.py` by computing the expectation from
the rows it got back.

`venue` has a second, subtler version of the same hazard, and it is worth
naming because it looks legitimate: the set of venues owed IS derived from the
games. That is correct — the coverage rule says "every venue hosting at least
one game in the current season" — but only because it is derived from the games
the feed LISTED, never from the venue lookups that SUCCEEDED. The distinction
is exactly one line in `capture.py` and it is what
`test_a_venue_that_fails_to_resolve_stays_in_the_denominator` pins.
"""

from datetime import date

from venue.capture import (
    ASSIGNMENT,
    EXPECTED_FLOOR,
    REASON_VENUE_UNRESOLVED,
    SIGNAL_TYPES,
    STATIC,
)

from .conftest import (
    SEASON_GAMES,
    SpyLake,
    game_row,
    round_robin,
    run_capture,
    season_rows,
    to_csv,
)


async def test_a_truncated_upstream_does_not_report_full_coverage():
    """The failure this floor exists for: a feed returning one game of 272 must
    not yield `expected: 1, present: 1`, ratio 1.0."""
    rows = [game_row(week=1, away="CHI", home="GB")]
    envelopes = await run_capture(SpyLake(), csv=to_csv(rows))

    assignment = envelopes[ASSIGNMENT]
    assert assignment.coverage.expected == EXPECTED_FLOOR[ASSIGNMENT]
    assert assignment.coverage.present == 1
    assert assignment.coverage.ratio < 1.0
    reasons = {error["reason"] for error in assignment.errors}
    assert "below_expected_floor" in reasons, reasons

    # And the static side floors independently: one game reaches one venue, so
    # a static expectation derived from the fetch would read 1/1 = perfect.
    static = envelopes[STATIC]
    assert static.coverage.expected == EXPECTED_FLOOR[STATIC]
    assert static.coverage.present == 1
    assert static.coverage.ratio < 1.0


async def test_an_empty_upstream_reports_zero_not_one():
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a pass that captured nothing."""
    envelopes = await run_capture(SpyLake(), csv=to_csv([]))
    for envelope in envelopes.values():
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1
        assert envelope.coverage.ratio == 0.0


async def test_expansion_past_the_floor_still_reports_honestly():
    """The floor must not CAP a genuine count, only raise a short one.

    A 19-week season is 304 games, past the 272 floor, and must report 304.
    """
    rows = season_rows(weeks=19)
    assert len(rows) == 304
    envelopes = await run_capture(SpyLake(), csv=to_csv(rows))
    assignment = envelopes[ASSIGNMENT]
    assert assignment.coverage.expected == 304
    assert assignment.coverage.ratio == 1.0


async def test_a_venue_that_fails_to_resolve_stays_in_the_denominator():
    """A dropped row shrinks the numerator and the denominator together and
    reads as perfect coverage. An unresolvable game must be counted as owed.

    "Somewhere Unknown Stadium" is a neutral-site name this table does not
    carry. Carried over from `schedule_context.venues`: it resolves to NOTHING
    rather than to the designated home club's building.
    """
    good = [game_row(week=1, away=a, home=h) for a, h in round_robin(1)]
    bad = game_row(
        week=1,
        away="JAX",
        home="JAX",
        location="Neutral",
        stadium="Somewhere Unknown Stadium",
    )
    envelopes = await run_capture(SpyLake(), csv=to_csv([*good, bad]))

    assignment = envelopes[ASSIGNMENT]
    assert len(assignment.signals) == len(good)
    assert bad["game_id"] in assignment.coverage.missing
    reasons = {error["reason"] for error in assignment.errors}
    assert REASON_VENUE_UNRESOLVED in reasons, assignment.errors


async def test_a_game_with_no_kickoff_date_is_missing_not_guessed():
    """No date means no window to join against, and falling back to the most
    recent revision is exactly the retroactive attribution this collector
    exists to prevent."""
    rows = [game_row(week=1, away="CHI", home="GB", gameday="")]
    envelopes = await run_capture(SpyLake(), csv=to_csv(rows))

    assignment = envelopes[ASSIGNMENT]
    assert assignment.signals == []
    assert assignment.coverage.missing == [rows[0]["game_id"]]
    reasons = {error["reason"] for error in assignment.errors}
    assert "kickoff_date_missing" in reasons, assignment.errors


async def test_the_static_expectation_is_the_seasons_venues_not_the_whole_table():
    """`coverage.expected` for `venue_static` is what the spec says it is:
    venues hosting at least one game. A season played at one venue must not
    report 30 present."""
    rows = [game_row(week=week, away="CHI", home="GB") for week in range(1, 4)]
    envelopes = await run_capture(SpyLake(), csv=to_csv(rows))

    static = envelopes[STATIC]
    assert [row["venue_id"] for row in static.signals] == ["lambeau"]
    assert static.coverage.present == 1


async def test_a_capture_before_the_table_makes_any_claim_reports_no_static_rows():
    """`today` earlier than TABLE_COMPILED_ON means no revision contains it.

    Recorded as expected-and-missing with a reason, never resolved forward to
    the first revision — that fallback would assert a 2026 surface was true in
    2025.
    """
    from datetime import UTC, datetime

    envelopes = await run_capture(
        SpyLake(),
        csv=to_csv(season_rows(weeks=1)),
        now=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
    )
    static = envelopes[STATIC]
    assert static.signals == []
    assert static.coverage.present == 0
    reasons = {error["reason"] for error in static.errors}
    assert "no_revision_contains_today" in reasons, static.errors


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())
    assert len(EXPECTED_FLOOR) == 2
    # Pinned to the real numbers, not merely "at least one": a floor silently
    # lowered to 1 satisfies every assertion above while turning the whole
    # mechanism off.
    assert EXPECTED_FLOOR[STATIC] == 30
    assert EXPECTED_FLOOR[ASSIGNMENT] == SEASON_GAMES


def test_the_floors_match_what_a_real_season_produces():
    """A floor nothing cross-checks drifts. 30 buildings, 272 games."""
    from venue import reference

    assert len(set(reference._HOME_VENUE_IDS.values())) == EXPECTED_FLOOR[STATIC]
    assert len(season_rows()) == EXPECTED_FLOOR[ASSIGNMENT]
    assert date(2026, 9, 13).weekday() == 6, "week one's fixture day is a Sunday"
