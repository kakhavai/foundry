"""Contract-term arithmetic, checked against a table rather than a capture.

Three functions, three things that go wrong: an off-by-one on the inclusive
signing season, a `None` substituted with a default, and a clamp that hides a
stale source. All three produce well-formed numbers, which is why they get their
own file rather than being inferred from an end-to-end assertion.
"""

import pytest

from player_contract import derive

# ── contract_end_season: the inclusive-signing-season off-by-one ─────────────


@pytest.mark.parametrize(
    ("year_signed", "years", "expected"),
    [
        # A four-year deal signed in 2022 runs 2022, 2023, 2024, 2025.
        (2022, 4, 2025),
        # A one-year deal begins and ends in the same season. This is the case
        # `year_signed + years` gets wrong most visibly, and 855 of the 2,908
        # active rows are one-year deals.
        (2025, 1, 2025),
        (2020, 10, 2029),
    ],
)
def test_the_end_season_includes_the_signing_season(year_signed, years, expected):
    assert derive.contract_end_season(year_signed, years) == expected


def test_a_one_year_deal_does_not_end_the_season_after_it_was_signed():
    """Pinned separately from the table above, because `year_signed + years` is
    the natural way to write this and is wrong for EVERY row at once — which
    makes it invisible in a spot check of a single player."""
    assert derive.contract_end_season(2025, 1) != 2026


@pytest.mark.parametrize(
    ("year_signed", "years"),
    [(None, 4), (2022, None), (None, None)],
)
def test_an_unknown_term_propagates_as_none(year_signed, years):
    """`None`, not a substituted default. `int(None or 0)` would turn a row the
    upstream left blank into a deal that ended in 1999 — or, with the other
    obvious default, into a confident contract year."""
    assert derive.contract_end_season(year_signed, years) is None


# ── is_contract_year: null is not false ──────────────────────────────────────


def test_the_final_season_is_the_contract_year():
    assert derive.is_contract_year(2026, 2026) is True


def test_a_season_before_the_final_one_is_not_the_contract_year():
    assert derive.is_contract_year(2026, 2029) is False


def test_a_season_after_the_final_one_is_not_the_contract_year():
    """An expired deal is not a contract year either. Both arms of the
    comparison matter: `season >= end` would call every expired contract a
    contract year, which is the single loudest field this collector publishes."""
    assert derive.is_contract_year(2026, 2024) is False


def test_an_unknown_term_is_null_rather_than_false():
    """`False` is a claim that the player has time left. This collector does not
    know that, and a projection treating "unknown" as "not a contract year" loses
    the signal silently rather than visibly."""
    assert derive.is_contract_year(2026, None) is None


# ── seasons_remaining: deliberately unclamped ────────────────────────────────


def test_seasons_remaining_counts_the_seasons_after_this_one():
    assert derive.seasons_remaining(2026, 2029) == 3


def test_the_final_season_has_zero_remaining():
    assert derive.seasons_remaining(2026, 2026) == 0


def test_an_expired_deal_reports_a_NEGATIVE_number_rather_than_zero():
    """The stale-source alarm, and the reason this is not `max(0, ...)`.

    Clamped, an expired 2024 deal and a deal in its final season both read
    `seasons_remaining: 0` and differ only in `is_contract_year` — a plausible,
    well-formed record that quietly says the wrong thing. Unclamped, `-2` cannot
    be read as anything but "the source's record of this deal has expired".

    This matters concretely today: the upstream `.csv.gz` has not been
    regenerated since 2022-05-29.
    """
    assert derive.seasons_remaining(2026, 2024) == -2


def test_an_unknown_term_has_unknown_seasons_remaining():
    assert derive.seasons_remaining(2026, None) is None
