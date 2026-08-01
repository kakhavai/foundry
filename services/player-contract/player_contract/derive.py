"""Contract-term arithmetic: the four fields derived rather than read.

Separated from `capture.py` so each can be tested against a table of terms
without a scope, an identity seam or an HTTP mock — these are the functions a
reviewer should be able to check by eye.

**Everything here propagates `None` rather than substituting a default.** A
contract whose `years` the feed left blank has an *unknown* end season, and the
difference between "unknown" and "this season" is the whole signal this
collector exists to publish. `int(None or 0)` would turn every such row into a
confident contract year.
"""

__all__ = [
    "contract_end_season",
    "is_contract_year",
    "seasons_remaining",
]


def contract_end_season(year_signed: int | None, years: int | None) -> int | None:
    """The final season of the deal, or `None` when the term is unknown.

    **Inclusive of the signing season**, which is the whole of the off-by-one:
    a four-year deal signed in 2022 runs 2022, 2023, 2024, 2025 and ends in
    **2025**, not 2026. `year_signed + years` would put every player's free
    agency one season late — a well-formed number that is wrong for everyone at
    once, and therefore invisible in a spot check.

    Void years are excluded by construction rather than by subtraction: the feed
    carries no void-year column at all, so `years` is the stated term. See
    `void_years_count` in the service README.
    """
    if year_signed is None or years is None:
        return None
    return year_signed + years - 1


def is_contract_year(season: int, end_season: int | None) -> bool | None:
    """Whether `season` is the final season of the deal.

    `None`, never `False`, when the end season is unknown. `False` is a claim
    that the player has time left, and this collector does not know that — the
    two cases have different consequences for a projection and must stay
    distinguishable.
    """
    if end_season is None:
        return None
    return season == end_season


def seasons_remaining(season: int, end_season: int | None) -> int | None:
    """Contract seasons AFTER `season`, or `None` when the term is unknown.

    **Deliberately not clamped at zero.** A negative value means the source's
    record of the deal ended before the season being captured. Clamping it to
    `0` would render an expired 2021 contract identical to a deal in its final
    season, differing only in `is_contract_year` — exactly the plausible-but-
    wrong record the phase doc's failure mode describes.

    Against the live parquet this fires on 33 of 2,930 rows: deals the upstream
    still flags active whose term has run out. It earns its keep as a **backstop**
    rather than as a routine signal, and it earned it once already — while this
    collector read the release's abandoned `.csv.gz`, it fired on 2,869 of
    2,887, which is how that artifact's four-year staleness became visible. See
    `adapters/upstream.py`.
    """
    if end_season is None:
        return None
    return end_season - season
