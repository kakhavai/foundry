"""The `as_of` filter — the API half of the retroactive-certainty guard.

The spec's named failure mode is that `/signals` serves the current broadcast
state for a past week, so a game flexed into Sunday night in Week 12 appears
to have always been a Sunday night game. The lake is append-only and therefore
correct; the API is where the leak happens.

Every arm has its own fixture. `announced_at <= as_of` and
`announced_at > as_of` are the pair that a single fixture cannot distinguish,
and the null case is the one that fails **open** if it is got wrong — which is
the leak itself rather than a missing feature.
"""

import pytest
from fastapi import HTTPException

from broadcast_context.signals import ROW_FILTERS, SUPPORTED_FILTERS, signal_matches

from .conftest import NOW, SpyLake, by_game, feed_document, run_capture, week_rows

BEFORE = "2026-09-01T00:00:00Z"
AFTER = "2026-12-01T00:00:00Z"
OBSERVED = "2026-10-01T12:00:00Z"


def row(**overrides) -> dict:
    base = {
        "game_id": "2026_12_A_B",
        "window_id": "snf",
        "flex_status": "flexed_in",
        "announced_at": None,
        "first_observed_at": OBSERVED,
        "point_in_time_basis": "first_observed",
    }
    base.update(overrides)
    return base


def test_no_as_of_returns_the_row():
    """A bare `/signals` must keep working: `scripts/smoke-test.sh` asserts a
    200 with an `envelopes` array for every registered collector."""
    assert signal_matches(row(), {}) is True


def test_a_row_first_observed_before_as_of_is_returned():
    assert signal_matches(row(), {"as_of": AFTER}) is True


def test_a_row_first_observed_after_as_of_is_withheld():
    """The other arm, and the whole point: at 2026-09-01 this collector had no
    evidence the game was an SNF game, so a model fit as of then must not see
    one."""
    assert signal_matches(row(), {"as_of": BEFORE}) is False


def test_the_boundary_is_inclusive():
    """`announced_at <= as_of`, per the spec's own wording."""
    assert signal_matches(row(), {"as_of": OBSERVED}) is True


def test_announced_at_wins_when_it_is_populated():
    """A future upstream that publishes an announcement instant must be used
    in preference to our own observation — the observation is only ever a
    bound on it."""
    published = row(
        announced_at="2026-09-15T00:00:00Z",
        first_observed_at="2026-11-01T00:00:00Z",
        point_in_time_basis="announced",
    )
    assert signal_matches(published, {"as_of": "2026-10-01T00:00:00Z"}) is True
    # ...and the fallback would have said False, which is what makes this
    # assertion mean something.
    assert (
        signal_matches(
            row(first_observed_at="2026-11-01T00:00:00Z"),
            {"as_of": "2026-10-01T00:00:00Z"},
        )
        is False
    )


def test_a_row_with_no_usable_instant_is_excluded_not_admitted():
    """**The leak, closed.** A record that silently passes an `as_of` filter
    because its timestamp is null is exactly what the guard exists to stop, so
    the predicate fails closed."""
    assert (
        signal_matches(row(announced_at=None, first_observed_at=None), {"as_of": AFTER})
        is False
    )


def test_an_unparseable_instant_on_the_row_is_excluded():
    assert (
        signal_matches(row(first_observed_at="last Tuesday"), {"as_of": AFTER}) is False
    )


def test_a_non_string_instant_on_the_row_is_excluded():
    assert signal_matches(row(first_observed_at=12345), {"as_of": AFTER}) is False


@pytest.mark.parametrize(
    "value",
    [
        "not-a-date",
        "",
        "2026-13-01T00:00:00Z",
        # Naive: "some timezone the caller forgot to state" is not an instant,
        # and guessing UTC would move the boundary by hours for exactly the
        # rows near it.
        "2026-10-01T12:00:00",
        "2026-10-01",
    ],
)
def test_a_malformed_as_of_is_422_not_an_empty_list(value):
    """A silently empty response looks like a quiet week. The router turns
    this into a 422."""
    with pytest.raises(HTTPException) as raised:
        signal_matches(row(), {"as_of": value})
    assert raised.value.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"game_id": "no-such-game"},
        {"window_id": "no-such-window"},
        {"flex_status": "no-such-status"},
    ],
)
def test_a_malformed_as_of_is_422_even_when_a_row_filter_would_short_circuit(extra):
    """**R3.** Validation must precede the equality filters.

    With the `ROW_FILTERS` loop above the parse,
    `?game_id=no-such-game&as_of=garbage` returned **200 and an empty list**:
    the first row failed the `game_id` comparison and returned before the
    malformed instant was ever looked at. A consumer who believes they are
    getting point-in-time filtering silently is not — this collector's own
    failure mode arriving through a typo instead of through a flex.
    """
    with pytest.raises(HTTPException) as raised:
        signal_matches(row(), {**extra, "as_of": "definitely-not-a-date"})
    assert raised.value.status_code == 422


def test_a_matching_row_filter_does_not_hide_a_malformed_as_of_either():
    """The other arm: the bug only showed when the filter MISMATCHED, so a
    fixture that happens to match proves nothing about the ordering."""
    with pytest.raises(HTTPException) as raised:
        signal_matches(
            row(), {"game_id": "2026_12_A_B", "as_of": "definitely-not-a-date"}
        )
    assert raised.value.status_code == 422


def test_validation_does_not_override_a_mismatched_row_filter():
    """Validating first must not mean *applying* first. A well-formed `as_of`
    that would admit the row still loses to a `game_id` that does not match,
    or the fix would have turned a filter conjunction into a disjunction."""
    assert signal_matches(row(), {"game_id": "someone-else", "as_of": AFTER}) is False


def test_a_non_utc_offset_is_accepted_and_compared_as_an_instant():
    """`2026-10-01T09:00:00-04:00` is 13:00Z, which is after the row's
    12:00Z observation. An implementation that string-compared, or that
    dropped the offset, would answer the other way."""
    assert signal_matches(row(), {"as_of": "2026-10-01T09:00:00-04:00"}) is True
    assert signal_matches(row(), {"as_of": "2026-10-01T07:00:00-04:00"}) is False


def test_as_of_composes_with_the_equality_filters():
    """A filter pair must AND, not short-circuit. `as_of` is evaluated last,
    so a mismatched `game_id` has to win regardless."""
    assert signal_matches(row(), {"game_id": "someone-else", "as_of": AFTER}) is False
    assert signal_matches(row(), {"game_id": "2026_12_A_B", "as_of": BEFORE}) is False
    assert signal_matches(row(), {"game_id": "2026_12_A_B", "as_of": AFTER}) is True


# --------------------------------------------------------------------------
# The slate-fact leak, end to end through two real captures
# --------------------------------------------------------------------------


async def test_a_game_whose_slot_mate_flexed_is_withheld_not_admitted():
    """**The leak the predicate alone could not close.**

    `games_in_window`, `is_standalone` and `distribution` are recomputed from
    today's slate, so before `games_in_window` joined the observed state a game
    whose OWN window never moved kept its early `first_observed_at`, passed
    this filter, and arrived carrying a post-cutoff slot count. Reproduced
    exactly: two 16:25 games in week 1, one flexed to Sunday night on the
    second pass, and the survivor read `first_observed_at: 2026-09-15`,
    `flex_status: original`, `games_in_window: 1`, `is_standalone: true`,
    `distribution: national`.

    An `as_of` before the flex must now return the survivor NOT AT ALL, rather
    than returning it with facts from after the cutoff. Withhold, never admit.
    """
    lake = SpyLake()
    before = week_rows(1)
    await run_capture(feed_document(before), lake=lake)

    # Index 13 is the second of the fixture week's two 16:25 games; index 12 is
    # its slot-mate, which does not move.
    after = list(before)
    after[13] = after[13].replace(",16:25,", ",20:20,")
    envelopes = await run_capture(
        feed_document(after), lake=lake, now=NOW.replace(day=16)
    )

    survivor = by_game(envelopes)["2026_01_A12_B12"]
    assert survivor["flex_status"] == "original", "this game itself never moved"
    assert survivor["games_in_window"] == 1
    assert survivor["is_standalone"] is True
    assert survivor["distribution"] == "national"
    # The instant advanced with the slot fact, so the row describes a state
    # that only became true on the 16th.
    assert survivor["first_observed_at"] == "2026-09-16T12:00:00Z"

    cutoff = "2026-09-15T18:00:00Z"
    assert signal_matches(survivor, {"as_of": cutoff}) is False

    # And a game in an untouched slot, captured at the same two instants, is
    # still admitted — otherwise this test would pass against a filter that
    # withholds everything.
    untouched = by_game(envelopes)["2026_01_A00_B00"]
    assert untouched["first_observed_at"] == "2026-09-15T12:00:00Z"
    assert signal_matches(untouched, {"as_of": cutoff}) is True


def test_every_declared_filter_is_actually_implemented():
    """A filter declared but not implemented is worse than one that is
    missing: the router accepts it, the predicate ignores it, and the response
    returns everything — which looks exactly like a working filter."""
    collector_owned = set(SUPPORTED_FILTERS) - {"season", "week", "signal_type"}
    assert collector_owned == set(ROW_FILTERS) | {"as_of"}

    for name in ROW_FILTERS:
        assert signal_matches(row(), {name: "definitely-not-this-value"}) is False
