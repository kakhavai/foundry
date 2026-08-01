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


def test_every_declared_filter_is_actually_implemented():
    """A filter declared but not implemented is worse than one that is
    missing: the router accepts it, the predicate ignores it, and the response
    returns everything — which looks exactly like a working filter."""
    collector_owned = set(SUPPORTED_FILTERS) - {"season", "week", "signal_type"}
    assert collector_owned == set(ROW_FILTERS) | {"as_of"}

    for name in ROW_FILTERS:
        assert signal_matches(row(), {name: "definitely-not-this-value"}) is False
