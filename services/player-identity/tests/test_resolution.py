"""Tier 3's arithmetic, the three refusal rules, and the miss queue.

The two worked cases at the top are the Phase 8 spec's own examples, pinned
to their exact scores. They are the reason the denominator is "every
attribute the record carries" rather than "the intersection of record and
query" — see `score_row`'s docstring.
"""

from datetime import UTC, datetime, timedelta

from conftest import STAMP, canonical_row

from player_identity.identity import normalized_key
from player_identity.resolution import (
    MARGIN,
    MIN_AGREEING_ATTRIBUTES,
    STALENESS_FLOOR,
    THRESHOLD,
    WEIGHTS,
    MissQueue,
    ResolutionIndex,
    ResolveQuery,
    score_row,
    staleness_discount,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def query(name=None, **kwargs) -> ResolveQuery:
    position = kwargs.pop("position", None)
    group = {"WR": "offense_skill", "S": "defense", "K": "special_teams"}.get(position)
    return ResolveQuery(
        raw_name=name,
        normalized_key=normalized_key(name) if name else None,
        position=position,
        position_group=group,
        **kwargs,
    )


def test_weights_sum_to_one():
    assert round(sum(WEIGHTS.values()), 6) == 1.0


def test_traded_tuesday_row_scores_0_765_and_resolves():
    """The spec's first worked case. A player traded on Tuesday agrees with a
    book's Wednesday row on name, position, and number, and disagrees on
    team. An equality-based key drops a row it should have resolved."""
    row = canonical_row()  # LV, #17, WR, "davante adams"
    scored = score_row(
        row,
        query("Davante Adams", team="NYJ", position="WR", jersey_number=17),
        reference=NOW,
    )

    assert round(scored.score, 3) == 0.765
    assert scored.disagreeing == ("team",)
    assert scored.score >= THRESHOLD
    assert len(scored.agreeing) >= MIN_AGREEING_ATTRIBUTES


def test_no_name_book_string_scores_0_647_and_resolves():
    """The spec's second worked case. With team, position, and number
    present a row resolves *without the name matching at all* — which is how
    "Hollywood Brown" and "CMC" resolve with no fuzzy string handling."""
    row = canonical_row()
    scored = score_row(
        row, query(team="LV", position="WR", jersey_number=17), reference=NOW
    )

    assert round(scored.score, 3) == 0.647
    assert scored.absent == ("normalized_key",)
    assert scored.score >= THRESHOLD


def test_absent_attributes_are_not_reported_as_disagreements():
    """`identity_resolution_failures_total{attribute=...}` is unreadable if
    "the caller did not supply it" and "it disagreed" share a label."""
    scored = score_row(
        canonical_row(),
        query(team="LV", position="WR", jersey_number=17),
        reference=NOW,
    )
    assert scored.disagreeing == ()


def test_name_only_query_against_a_sparse_record_is_refused():
    """The N-of-M rule, and why the score alone is not enough.

    A free agent with no team and no jersey number has a denominator of
    0.30 + 0.15 = 0.45, so a name-only query scores 0.667 on ONE agreeing
    attribute — comfortably over the 0.60 threshold and worth nothing.
    """
    index = ResolutionIndex()
    index.replace(
        [canonical_row(team=None, jersey_number=None, normalized_key="john smith")]
    )

    resolution = index.resolve(query("John Smith"), now=NOW)

    best = resolution.candidates[0]
    assert best.score > THRESHOLD, "precondition: the score alone would pass"
    assert len(best.agreeing) < MIN_AGREEING_ATTRIBUTES
    assert not resolution.resolved
    assert resolution.reason == "insufficient_agreeing_attributes"
    assert resolution.player_id is None


def test_a_tie_inside_the_margin_goes_to_the_queue_not_the_higher_score():
    """Two same-name players on the same team wearing different numbers, and
    a query that supplies no number. Both score identically, so the margin
    is 0.0 and the row is a tie — and a tie goes to the miss queue, never to
    whichever candidate happened to sort first."""
    index = ResolutionIndex()
    index.replace(
        [
            canonical_row("fdy-00000000000a", jersey_number=17, position="WR"),
            canonical_row("fdy-00000000000b", jersey_number=88, position="WR"),
        ]
    )

    resolution = index.resolve(
        query("Davante Adams", team="LV", position="WR"), now=NOW
    )

    assert resolution.candidates[0].score >= THRESHOLD
    assert resolution.margin is not None and resolution.margin < MARGIN
    assert not resolution.resolved
    assert resolution.reason == "ambiguous"
    assert resolution.near_miss is True


def test_a_clear_winner_outside_the_margin_resolves():
    """The inverse of the tie above: the same two candidates, but the query
    now carries the number, so the margin opens and the link is made."""
    index = ResolutionIndex()
    index.replace(
        [
            canonical_row("fdy-00000000000a", jersey_number=17, position="WR"),
            canonical_row("fdy-00000000000b", jersey_number=88, position="WR"),
        ]
    )

    resolution = index.resolve(
        query("Davante Adams", team="LV", position="WR", jersey_number=17), now=NOW
    )

    assert resolution.resolved
    assert resolution.player_id == "fdy-00000000000a"
    assert resolution.link_method == "attribute_score"
    assert resolution.margin >= MARGIN


def test_staleness_discount_decays_linearly_to_a_floor():
    fresh = NOW
    assert staleness_discount(fresh, NOW) == 1.0
    half = staleness_discount(NOW - timedelta(days=15), NOW)
    assert 0.6 < half < 0.65
    assert staleness_discount(NOW - timedelta(days=30), NOW) == STALENESS_FLOOR
    assert staleness_discount(NOW - timedelta(days=300), NOW) == STALENESS_FLOOR
    # An absent as_of is fully stale, not fully fresh.
    assert staleness_discount(None, NOW) == STALENESS_FLOOR


def test_a_stale_team_carries_less_weight_in_both_directions():
    """The discount lands on the denominator as well as the numerator.

    Same query, same disagreement on `team`; the only difference is how old
    the record says its team value is. A month-stale team must cost the
    record less than a fresh one — otherwise a trade the upstream has not
    caught up with silently converts a resolvable row into a miss, which is
    the failure mode the spec names by name.
    """
    fresh = canonical_row(team_as_of=STAMP)
    stale = canonical_row(
        team_as_of=(NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    asked = query("Davante Adams", team="NYJ", position="WR", jersey_number=17)

    fresh_score = score_row(fresh, asked, reference=NOW).score
    stale_score = score_row(stale, asked, reference=NOW).score

    assert stale_score > fresh_score
    assert round(fresh_score, 3) == 0.765
    # 0.65 / (0.30 + 0.20*0.25 + 0.20 + 0.15) = 0.65 / 0.70 = 0.9286
    assert round(stale_score, 3) == 0.929


def test_a_stale_agreeing_attribute_also_counts_for_less():
    """The other direction of the same rule: when the stale attribute is the
    one that *agrees*, discounting only the numerator would be a free win."""
    fresh = canonical_row(team_as_of=STAMP)
    stale = canonical_row(
        team_as_of=(NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    asked = query(team="LV", position="WR", jersey_number=17)

    assert (
        score_row(stale, asked, reference=NOW).score
        < score_row(fresh, asked, reference=NOW).score
    )


def test_tier1_adopts_a_published_crosswalk_id_without_scoring():
    index = ResolutionIndex()
    index.replace(
        [
            canonical_row(
                external_ids={
                    "gsis": {
                        "id": "00-0031381",
                        "linked_at": STAMP,
                        "link_method": "crosswalk",
                        "match_score": None,
                        "match_margin": None,
                    }
                }
            )
        ]
    )

    resolution = index.resolve(
        ResolveQuery(source="gsis", source_id="00-0031381"), now=NOW
    )

    assert resolution.resolved
    assert resolution.link_method == "crosswalk"
    assert resolution.confidence == 1.0


def test_tier2_matches_an_upstreams_own_id_already_seen():
    """Distinct from Tier 1, and it must stay distinct: a non-crosswalk
    source is served out of a different map, so it can never be reported as
    an adopted published link."""
    index = ResolutionIndex()
    index.replace(
        [
            canonical_row(
                external_ids={
                    "sleeper": {
                        "id": "2133",
                        "linked_at": STAMP,
                        "link_method": "exact_id",
                        "match_score": None,
                        "match_margin": None,
                    }
                }
            )
        ]
    )

    resolution = index.resolve(
        ResolveQuery(source="sleeper", source_id="2133"), now=NOW
    )

    assert resolution.resolved
    assert resolution.link_method == "exact_id"


def test_a_crosswalk_source_never_resolves_out_of_the_upstream_map():
    """The tiers are separated in the data structure, not just in a comment:
    an id filed under a non-crosswalk source cannot be claimed as Tier 1."""
    index = ResolutionIndex()
    index.replace(
        [
            canonical_row(
                external_ids={
                    "sleeper": {
                        "id": "2133",
                        "linked_at": STAMP,
                        "link_method": "exact_id",
                        "match_score": None,
                        "match_margin": None,
                    }
                }
            )
        ]
    )

    assert not index.resolve(
        ResolveQuery(source="gsis", source_id="2133"), now=NOW
    ).resolved


def test_no_candidate_is_refused_rather_than_guessed():
    index = ResolutionIndex()
    index.replace([canonical_row()])

    resolution = index.resolve(query("Nobody At All"), now=NOW)

    assert not resolution.resolved
    assert resolution.reason == "no_candidate"
    assert resolution.candidates == ()


def test_injectivity_violations_are_reported_not_silently_merged():
    """Each external_ids.<source>.id maps to exactly one player_id."""
    shared = {
        "gsis": {
            "id": "00-0031381",
            "linked_at": STAMP,
            "link_method": "crosswalk",
            "match_score": None,
            "match_margin": None,
        }
    }
    index = ResolutionIndex()
    conflicts = index.replace(
        [
            canonical_row("fdy-00000000000a", external_ids=dict(shared)),
            canonical_row("fdy-00000000000b", external_ids=dict(shared)),
        ]
    )

    assert len(conflicts) == 1
    assert conflicts[0]["source"] == "gsis"
    assert conflicts[0]["player_ids"] == ["fdy-00000000000a", "fdy-00000000000b"]


def test_miss_queue_counts_occurrences_and_orders_by_them():
    index = ResolutionIndex()
    index.replace([canonical_row()])
    queue = MissQueue()

    rare = query("Nobody At All", team="LV")
    common = query("Also Nobody", team="LV")
    queue.record(rare, index.resolve(rare, now=NOW), now=NOW)
    for _ in range(3):
        queue.record(common, index.resolve(common, now=NOW), now=NOW)

    rows = queue.rows()
    assert [r["occurrence_count"] for r in rows] == [3, 1]
    assert rows[0]["raw_name"] == "Also Nobody"
    assert len(queue) == 2


def test_miss_rows_carry_the_disagreeing_attributes():
    """The label that separates a staleness problem from a genuine unknown."""
    index = ResolutionIndex()
    index.replace([canonical_row(entry_year=2014, birth_date="1992-12-24")])
    queue = MissQueue()

    asked = query("Davante Adams", team="NYJ", position="S", jersey_number=99)
    row = queue.record(asked, index.resolve(asked, now=NOW), now=NOW).to_signal()

    assert set(row["disagreeing_attributes"]) >= {"team", "position_group"}
    assert row["best_candidate_player_id"] == "fdy-000000000001"
    assert 0.0 <= row["match_score"] <= 1.0
