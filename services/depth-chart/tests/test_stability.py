"""The stability half: hashing, `weeks_at_rank`, `rank_changes_4w`, freshness.

Pure functions over `PositionHistory`, so these are built directly rather than
fetched — the adapter's own suite proves the fold, and mixing the two would make
a hashing bug look like a parsing bug.
"""

from datetime import UTC, datetime, timedelta

from depth_chart.adapters.upstream import ChartEntry, PositionHistory, WeekOrdering
from depth_chart.stability import (
    STALE_AFTER_DAYS,
    chart_hash,
    comparisons,
    freshness,
    rank_changes,
    stability_score,
    weeks_at_rank,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def ordering(*names, week: int, at: datetime | None = None) -> WeekOrdering:
    """Build one week's ordering, ranked in the order the names are given.

    The crosswalk id is derived from the **name**, never from the rank. Deriving
    it from the rank would make every ordering of the same size hash
    identically, and `test_the_hash_changes_when_the_ordering_changes` would
    pass vacuously — which is exactly how it failed the first time this file
    ran.
    """
    return WeekOrdering(
        iso_year=2026,
        iso_week=week,
        captured_at=at or NOW,
        entries=tuple(
            ChartEntry(rank=rank, player_name=name, gsis_id=f"00-{name}")
            for rank, name in enumerate(names, start=1)
        ),
    )


def history(*weeks: WeekOrdering) -> PositionHistory:
    return PositionHistory(team="KC", position="WR", weeks=weeks)


def test_the_hash_is_stable_across_a_reordered_serialization():
    """The spec's named hard part: several upstreams re-serialize an unchanged
    chart with a different tie-break order, so hashing the response bytes would
    report a promotion that never happened."""
    forward = ordering("A", "B", "C", week=37)
    shuffled = WeekOrdering(
        iso_year=37,
        iso_week=37,
        captured_at=forward.captured_at,
        entries=tuple(reversed(forward.entries)),
    )
    assert chart_hash(forward) == chart_hash(shuffled)


def test_the_hash_changes_when_the_ordering_changes():
    assert chart_hash(ordering("A", "B", week=37)) != chart_hash(
        ordering("B", "A", week=37)
    )


def test_weeks_at_rank_counts_consecutive_weeks_and_stops_at_the_first_change():
    subject = history(
        ordering("A", "B", week=37),
        ordering("A", "B", week=36),
        ordering("B", "A", week=35),
        ordering("A", "B", week=34),
    )
    # `A` held rank 1 in weeks 37 and 36, then lost it in 35 — the run stops
    # there and does not resume at week 34.
    assert weeks_at_rank(subject, "00-A", "A") == 2


def test_weeks_at_rank_is_zero_for_a_player_absent_from_the_current_chart():
    subject = history(ordering("A", week=37), ordering("B", week=36))
    assert weeks_at_rank(subject, "00-B", "B") == 0


def test_weeks_at_rank_falls_back_to_the_name_when_no_crosswalk_id_exists():
    nameless = WeekOrdering(
        iso_year=2026,
        iso_week=37,
        captured_at=NOW,
        entries=(ChartEntry(rank=1, player_name="A", gsis_id=None),),
    )
    subject = PositionHistory(team="KC", position="WR", weeks=(nameless, nameless))
    assert weeks_at_rank(subject, None, "A") == 2


def test_rank_changes_counts_week_to_week_transitions_not_republishes():
    subject = history(
        ordering("B", "A", week=37),
        ordering("A", "B", week=36),
        ordering("A", "B", week=35),
        ordering("A", "B", week=34),
    )
    assert comparisons(subject) == 3
    assert rank_changes(subject) == 1


def test_rank_changes_ignores_weeks_outside_the_window():
    subject = history(
        ordering("A", "B", week=37),
        ordering("A", "B", week=36),
        ordering("A", "B", week=35),
        ordering("A", "B", week=34),
        ordering("B", "A", week=33),
    )
    # The week-34-to-33 change is outside the four-week window.
    assert rank_changes(subject) == 0


def test_a_settled_ordering_scores_one():
    subject = history(*(ordering("A", "B", week=w) for w in (37, 36, 35, 34)))
    assert stability_score(subject) == 1.0


def test_a_churning_ordering_scores_zero():
    subject = history(
        ordering("A", "B", week=37),
        ordering("B", "A", week=36),
        ordering("A", "B", week=35),
        ordering("B", "A", week=34),
    )
    assert stability_score(subject) == 0.0


def test_a_single_observed_week_scores_none_not_one():
    """`0/0` reading as a perfect score is the same arithmetic trap
    `Coverage.ratio` guards against with its floor: a first-ever capture has
    produced no evidence of stability at all."""
    subject = history(ordering("A", "B", week=37))
    assert comparisons(subject) == 0
    assert stability_score(subject) is None


def test_freshness_is_measured_from_the_publication_instant_not_the_fetch():
    """A vendor re-serving a frozen chart refreshes the fetch every 15 minutes
    while `dt` stands still. Anchoring on the fetch would report a dead chart
    as perpetually fresh — the collector's headline failure mode."""
    frozen = NOW - timedelta(days=STALE_AFTER_DAYS + 5)
    subject = history(ordering("A", week=30, at=frozen))
    result = freshness(subject, NOW)
    assert result.chart_published_at == frozen
    assert result.is_stale is True
    assert result.age_days == round(STALE_AFTER_DAYS + 5, 3)


def test_a_chart_inside_the_threshold_is_not_stale():
    recent = NOW - timedelta(days=STALE_AFTER_DAYS - 1)
    subject = history(ordering("A", week=36, at=recent))
    assert freshness(subject, NOW).is_stale is False


def test_a_frozen_chart_still_scores_one_which_is_why_freshness_is_separate():
    """Stated as a test because it is the trap: `stability_score` alone cannot
    distinguish a settled room from a dead feed. Both read 1.0; only
    `is_stale` separates them."""
    frozen = NOW - timedelta(days=60)
    subject = history(
        *(ordering("A", "B", week=w, at=frozen) for w in (37, 36, 35, 34))
    )
    assert stability_score(subject) == 1.0
    assert freshness(subject, NOW).is_stale is True
