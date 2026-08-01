"""`games_in_window` end to end, and the partial-slate refusal.

The spec's second named trap:

> `games_in_window` is a derived count over the whole slate for that slot, so
> a partial fetch produces a wrong value for **every** game in the window, not
> just the missing one — the adapter must compute it only from a complete
> slate.

`is_standalone` is `games_in_window == 1` and `distribution` is derived from
`is_standalone`, so all three fall together. The decision taken here is to
**withhold the whole week** rather than publish plausible counts, because an
unslotted game could belong to any slot in its week.
"""

import pytest
from collector_core.conditional import UpstreamUnchanged

from broadcast_context.capture import SIGNAL

from .conftest import (
    NOW,
    SpyLake,
    by_game,
    feed_document,
    feed_row,
    run_capture,
    week_rows,
)


async def test_a_complete_week_publishes_counts_and_standalone():
    envelopes = await run_capture(feed_document(week_rows(1)), lake=SpyLake())
    rows = by_game(envelopes)

    early = rows["2026_01_A00_B00"]
    late = rows["2026_01_A12_B12"]
    snf = rows["2026_01_A14_B14"]
    mnf = rows["2026_01_A15_B15"]

    assert (early["games_in_window"], early["is_standalone"]) == (12, False)
    assert (late["games_in_window"], late["is_standalone"]) == (2, False)
    assert (snf["games_in_window"], snf["is_standalone"]) == (1, True)
    assert (mnf["games_in_window"], mnf["is_standalone"]) == (1, True)
    assert early["distribution"] == "regional"
    assert snf["distribution"] == "national"


async def test_one_unslotted_game_withholds_the_counts_for_its_whole_week():
    """The trap, exactly. Fifteen of the sixteen games have kickoff times and
    could each be given a plausible count; every one of those counts would be
    an undercount, and an undercount manufactures standalone primetime games
    that do not exist."""
    rows = [*week_rows(1, drop_kickoff_for=1), *week_rows(2)]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    by_id = by_game(envelopes)

    week_one = [row for row in by_id.values() if row["week"] == 1]
    week_two = [row for row in by_id.values() if row["week"] == 2]
    assert len(week_one) == 16 and len(week_two) == 16

    assert all(row["games_in_window"] is None for row in week_one)
    assert all(row["is_standalone"] is None for row in week_one)
    assert all(row["distribution"] is None for row in week_one)
    assert all(
        row["null_field_reasons"]["games_in_window"] == "incomplete_slate"
        for row in week_one
    )

    # The neighbouring week is untouched: the guard is week-scoped, so one
    # TBD game in December does not blank September.
    assert all(row["games_in_window"] is not None for row in week_two)


async def test_the_incomplete_week_is_named_with_its_reason():
    """The week AND why. Three findings need three different operator
    responses — wait for the league, investigate the feed, investigate the
    transport — and one shared `incomplete_slate` string erases that."""
    rows = [*week_rows(1, drop_kickoff_for=1), *week_rows(2)]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    entries = [
        error
        for error in envelopes[SIGNAL].errors
        if error["reason"] == "incomplete_slate"
    ]
    assert len(entries) == 1
    assert "week 1: unslotted_game" in entries[0]["detail"]


async def test_a_short_regular_season_week_withholds_its_counts_too():
    """A different arm, with its own fixture: every game here HAS a kickoff,
    so the unslotted-game check sees nothing. Twelve games cannot be a real
    regular-season week — six byes is the league maximum — so the document was
    truncated somewhere between the publisher and here."""
    rows = [*week_rows(1, games=12), *week_rows(2)]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    by_id = by_game(envelopes)

    week_one = [row for row in by_id.values() if row["week"] == 1]
    assert len(week_one) == 12
    assert all(row["kickoff_at"] is not None for row in week_one)
    assert all(row["games_in_window"] is None for row in week_one)
    # And it is still a fully COVERED week — every game has a window. The
    # withheld count is a null field with a reason, not a coverage miss.
    assert envelopes[SIGNAL].coverage.present == 28


async def test_a_week_that_shrank_since_the_last_snapshot_withholds_its_counts():
    """**The gap between the other two arms**, reproduced and closed.

    A stream truncated mid-document leaves the tail week with 13-15 games that
    **all have kickoffs**, so arm (a) sees nothing and arm (b) — a *floor*, not
    a completeness proof — sees nothing either. Before this arm existed, a
    14-game week that lost one of its two 16:25 games published
    `games_in_window: 1`, `is_standalone: true`, `distribution: national` for
    the survivor with no `incomplete_slate` anywhere: "the dangerous
    direction", manufacturing a standalone national game that does not exist.

    Two passes, because the guard is a comparison against the last snapshot.
    """
    lake = SpyLake()
    full = week_rows(1, games=14)
    await run_capture(feed_document(full), lake=lake)

    # Drop one of the two 16:25 games. Index 10 and 11 are the late pair in a
    # 14-game fixture week; every survivor still carries a kickoff.
    truncated = [row for index, row in enumerate(full) if index != 11]
    envelopes = await run_capture(
        feed_document(truncated), lake=lake, now=NOW.replace(day=16)
    )

    survivor = by_game(envelopes)["2026_01_A10_B10"]
    assert survivor["kickoff_eastern_time"] == "16:25"
    assert survivor["games_in_window"] is None
    assert survivor["is_standalone"] is None
    assert survivor["distribution"] is None
    assert survivor["null_field_reasons"]["games_in_window"] == "incomplete_slate"

    entries = [
        error
        for error in envelopes[SIGNAL].errors
        if error["reason"] == "incomplete_slate"
    ]
    assert len(entries) == 1
    assert "week 1: week_shrank" in entries[0]["detail"]


async def test_a_persistent_truncation_stays_flagged_on_every_later_pass():
    """**R2.** Three passes: 16, then 14, then 14 again.

    Against the previous pass's count, pass 3 compares 14 to 14, goes quiet,
    and republishes `games_in_window: 1 / is_standalone: true /
    distribution: national` for the survivor with no `incomplete_slate`
    anywhere — the same "manufactures standalone primetime games that do not
    exist" outcome the arm was added to close, arriving one pass later and
    then persisting. On a 24-hour cadence that is one day of warning for a
    permanent wrongness, and `below_expected_floor` is envelope-level so a row
    consumer sees nothing.

    Against the high-water mark the third pass is byte-identical to the
    second, so the digest gate reports it unchanged and nothing new is
    appended — which is the strongest possible form of "still flagged".
    """
    lake = SpyLake()
    full = week_rows(1, games=16)
    await run_capture(feed_document(full), lake=lake)

    truncated = [row for index, row in enumerate(full) if index != 13]
    second = await run_capture(
        feed_document(truncated), lake=lake, now=NOW.replace(day=16)
    )
    survivor = by_game(second)["2026_01_A12_B12"]
    assert survivor["games_in_window"] is None
    assert len(lake.writes) == 2

    with pytest.raises(UpstreamUnchanged):
        await run_capture(feed_document(truncated), lake=lake, now=NOW.replace(day=17))
    assert len(lake.writes) == 2, "pass 3 must not republish a corrected count"

    # And the served rows are still the withheld ones, not a recomputed 1.
    still_withheld = by_game(second)["2026_01_A12_B12"]
    assert still_withheld["is_standalone"] is None
    assert still_withheld["distribution"] is None
    assert still_withheld["null_field_reasons"]["games_in_window"] == "incomplete_slate"


async def test_a_week_that_grew_since_the_last_snapshot_is_not_flagged():
    """The other arm. A guard that fired on any change would blank the counts
    for every week the moment a postponed game is rescheduled back in — and
    would pass the test above just as happily."""
    lake = SpyLake()
    await run_capture(feed_document(week_rows(1, games=14)), lake=lake)

    envelopes = await run_capture(
        feed_document(week_rows(1, games=16)), lake=lake, now=NOW.replace(day=16)
    )
    reasons = {error["reason"] for error in envelopes[SIGNAL].errors}
    assert "incomplete_slate" not in reasons, reasons
    signals = envelopes[SIGNAL].signals
    assert len(signals) == 14 or len(signals) == 16
    assert all(row["games_in_window"] is not None for row in signals)


async def test_a_first_capture_has_no_baseline_and_does_not_invent_one():
    """`week_counts` is empty on a first capture, which must make the shrink
    arm inert rather than a guess. A `.get(week, 0)` inverted to a truthy
    default would flag every week of every first pass."""
    envelopes = await run_capture(feed_document(week_rows(1, games=14)), lake=SpyLake())
    reasons = {error["reason"] for error in envelopes[SIGNAL].errors}
    assert "incomplete_slate" not in reasons, reasons
    signals = envelopes[SIGNAL].signals
    assert len(signals) == 14 or len(signals) == 16
    assert all(row["games_in_window"] is not None for row in signals)


async def test_the_count_is_by_kickoff_instant_not_by_window_id():
    """A four-game Wild Card Saturday/Sunday: all `window_id: playoff`, each
    the only football on television when it kicks off. Counting by window
    would report 4 for every one of them and `is_standalone: false` for four
    standalone games."""
    rows = [
        feed_row(
            "2026_19_A_B",
            game_type="WC",
            week=19,
            gameday="2027-01-09",
            gametime="16:30",
        ),
        feed_row(
            "2026_19_C_D",
            game_type="WC",
            week=19,
            gameday="2027-01-09",
            gametime="20:00",
        ),
        feed_row(
            "2026_19_E_F",
            game_type="WC",
            week=19,
            gameday="2027-01-10",
            gametime="13:00",
        ),
        feed_row(
            "2026_19_G_H",
            game_type="WC",
            week=19,
            gameday="2027-01-10",
            gametime="16:30",
        ),
    ]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    signals = envelopes[SIGNAL].signals
    assert len(signals) == 4
    assert {row["window_id"] for row in signals} == {"playoff"}
    assert [row["games_in_window"] for row in signals] == [1, 1, 1, 1]
    assert all(row["is_standalone"] for row in signals)
