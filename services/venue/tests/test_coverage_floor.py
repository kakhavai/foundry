"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned. These tests
fail if somebody "simplifies" `capture.py` by computing the expectation from
the rows it got back.

`venue` has a second, subtler version of the same hazard, and it is worth naming
because it looks legitimate: the set of venues owed IS derived from the games.
That is correct — the coverage rule says "every venue hosting at least one game
in the current season" — but only because it is derived from the games the feed
LISTED, never from the venue lookups that SUCCEEDED.

**That distinction was a real bug and review found it.** The venue set was
collected at the bottom of the assignment loop, after the kickoff-presence check
and the window join had both passed, so a venue whose every game failed the
window join vanished from `venue_static` — taking its own coverage expectation
with it. The envelope reported ratio 1.0 with an empty `errors` array while two
venues the season uses were absent entirely. It is pinned by
`test_a_venue_whose_games_all_fail_the_window_join_is_still_owed` below, which
asserts the STATIC side; an earlier docstring claimed
`test_a_venue_that_fails_to_resolve_stays_in_the_denominator` covered this, and
it did not — that test only ever asserted the assignment envelope.
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

    This asserts the ASSIGNMENT envelope only, which is all it can: a game whose
    venue never resolved names no venue, so there is no venue for the static
    side to owe. The static-side counterpart — a venue that IS named and still
    fails — is the test below.
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


async def test_a_venue_whose_games_all_fail_the_window_join_is_still_published():
    """The bug review found: `venue_static`'s universe derived from success.

    Two venues host games this season, and every one of their games has a
    kickoff date the table makes no claim about — so no ASSIGNMENT row can be
    built for either. Their `venue_static` records are a separate question and
    the answer is yes: the table describes both perfectly well *today*.

    Before the fix, both vanished from `venue_static` entirely, because the
    venue set was collected only from games whose assignment row succeeded. The
    envelope then reported `expected 30 present 28` — and the shortfall showed
    up only as a generic `below_expected_floor`, purely because 28 happens to
    sit under the floor. See the next test for the version with no floor to
    save it.
    """
    rows = [r for r in season_rows() if r["home_team"] not in {"GB", "DET"}]
    rows.append(game_row(week=1, away="CHI", home="GB", gameday="2020-01-05"))
    rows.append(game_row(week=1, away="MIN", home="DET", gameday="2020-01-05"))
    envelopes = await run_capture(SpyLake(), csv=to_csv(rows))

    static = envelopes[STATIC]
    published = {row["venue_id"] for row in static.signals}
    assert "lambeau" in published, "a venue the season uses was dropped entirely"
    assert "ford-field" in published
    assert static.coverage.present == 30
    assert static.coverage.expected == 30

    # The failure belongs to the ASSIGNMENT side, and it is recorded there.
    assignment = envelopes[ASSIGNMENT]
    assert len(assignment.coverage.missing) == 2
    reasons = {error["reason"] for error in assignment.errors}
    assert "revision_window_excludes_kickoff" in reasons, assignment.errors


async def test_the_static_universe_is_not_capped_by_what_resolved():
    """The same bug with no floor to expose it — the reviewer's exact shape.

    Eight neutral-site venues the table knows, hosting games on dates it does
    not cover. Every league venue resolves normally, so a success-derived
    universe is 30 — over the floor — and reports `expected 30 present 30`,
    ratio **1.0 with an empty errors array**, while eight venues the season
    uses are absent from the document entirely.

    The assertion is therefore on the universe's SIZE and MEMBERSHIP, not on
    the ratio: the ratio is 1.0 both before and after the fix, which is exactly
    what made this invisible.
    """
    rows = list(season_rows())
    for index, stadium in enumerate(
        [
            "Wembley Stadium",
            "Allianz Arena",
            "Estadio Azteca",
            "Tottenham Hotspur Stadium",
            "Croke Park",
            "Stade de France",
            "Santiago Bernabéu",
            "Melbourne Cricket Ground",
        ]
    ):
        rows.append(
            game_row(
                week=1,
                away="NE",
                home=f"X{index}",
                location="Neutral",
                stadium=stadium,
                gameday="2020-01-05",
            )
        )

    envelopes = await run_capture(SpyLake(), csv=to_csv(rows))
    static = envelopes[STATIC]

    assert static.coverage.expected == 38, (
        "the season's venue universe is 30 league venues + 8 neutral sites; "
        "an expectation of 30 means it was derived from what succeeded"
    )
    published = {row["venue_id"] for row in static.signals}
    expected_neutral = {
        "wembley",
        "allianz",
        "azteca",
        "tottenham",
        "croke-park",
        "stade-de-france",
        "bernabeu",
        "mcg",
    }
    assert len(expected_neutral) == 8
    assert expected_neutral <= published, expected_neutral - published


async def test_a_capture_before_the_table_makes_any_claim_names_what_it_owes():
    """`today` earlier than TABLE_COMPILED_ON: nothing is describable, and every
    venue the season uses must be NAMED in `coverage.missing`.

    **What this does and does not pin, established by mutation rather than
    assumed — two drafts of this docstring were wrong before this one.**

    It is *not* a test of the season-venue-set fix above (M16 leaves it green):
    these games all kick off inside the table's window, so their assignment rows
    succeed either way and the venue set is identical before and after it.

    Nor is it a test of `acc.expect(venue_id)` alone. `_static_envelope`
    declares an undescribable venue owed **twice over** — `acc.expect` before
    the lookup, and `acc.fail` on the miss, which declares expectation itself
    ("a failure is evidence the key was owed"). Removing either one on its own
    changes nothing here, which is the point of having both.

    What it guards is that an undescribable venue is not **silently dropped**:
    remove both declarations and `missing` empties out, leaving `present: 0`
    against a bare floor with no indication of which venues were owed. That is
    mutation M20. "Zero of thirty, and here are the thirty" is an operator's
    starting point; "zero of thirty" alone is not.
    """
    from datetime import UTC, datetime

    envelopes = await run_capture(
        SpyLake(),
        # A full season, so all 30 venues are named and the count is exact.
        csv=to_csv(season_rows()),
        now=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
    )
    static = envelopes[STATIC]
    assert static.signals == []
    assert static.coverage.present == 0
    assert len(static.coverage.missing) == 30, static.coverage.missing
    assert "lambeau" in static.coverage.missing
    reasons = {error["reason"] for error in static.errors}
    assert "no_revision_contains_today" in reasons, static.errors


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
