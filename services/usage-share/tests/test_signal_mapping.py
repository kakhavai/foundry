"""The field mapping, and the refusals.

Two failure modes the spec names for this collector, both invisible per row:

- **A share divided by the wrong base.** 0.71 is plausible whether the base was
  62 snaps or 68. The tell is aggregate — a team's target shares must sum to
  1.0 — and it is checked against the *upstream's own* share column, because
  the shares this collector computes add to 1.0 by construction.
- **A share the arithmetic cannot support.** A numerator over a zero base, or
  anything outside [0, 1]. Those rows are refused and counted in
  `coverage.missing` with a reason, never emitted with a plausible value.
"""

import httpx
import pytest

from usage_share.adapters.upstream import (
    TeamDenominators,
    UsageRow,
    WeekUsage,
)
from usage_share.capture import (
    AmbiguousUsage,
    build_signal,
    capture_usage_share,
    team_sum_drift,
)

from .conftest import NOW, SAMPLE_RECORDS, SpyLake, to_csv


def _row(**overrides) -> UsageRow:
    values = dict(
        upstream_player_id="00-KC-WR1",
        game_id="2026_01_BUF_KC",
        team="KC",
        position="WR",
        targets=10,
        air_yards=120.0,
        carries=1,
        upstream_target_share=0.4,
    )
    values.update(overrides)
    return UsageRow(**values)


def _bases(**overrides) -> TeamDenominators:
    values = dict(team="KC", dropbacks=32, targets=25, air_yards=215.0, carries=22)
    values.update(overrides)
    return TeamDenominators(**values)


def test_a_row_without_its_bases_is_refused_not_stored():
    """The spec, verbatim: a share arriving without its base is rejected."""
    with pytest.raises(AmbiguousUsage) as caught:
        build_signal(_row(), None, now=NOW)
    assert caught.value.reason == "missing_denominators"


def test_a_nonzero_numerator_over_a_zero_base_is_refused():
    """The base is provably wrong. Dividing anyway yields an infinity, or a
    plausible number from a denominator nobody can defend."""
    with pytest.raises(AmbiguousUsage) as caught:
        build_signal(_row(targets=4), _bases(targets=0), now=NOW)
    assert caught.value.reason == "denominator_inconsistent"


def test_a_zero_over_zero_share_is_zero_not_a_refusal():
    """A player with no carries on a team with no carries has a zero share, not
    an unknown one — a bye-week-shaped fact, not a broken base."""
    signal = build_signal(_row(carries=0), _bases(carries=0), now=NOW)
    assert signal["carry_share"] == 0.0


def test_a_share_above_one_is_refused():
    """The spec's named impossible value. A base smaller than the numerator it
    divides means the denominator excluded plays it should have counted."""
    with pytest.raises(AmbiguousUsage) as caught:
        build_signal(_row(targets=30), _bases(targets=25), now=NOW)
    assert caught.value.reason == "share_out_of_range"


def test_wopr_is_the_declared_weighting():
    signal = build_signal(_row(), _bases(), now=NOW)
    assert signal["wopr"] == round(
        1.5 * signal["target_share"] + 0.7 * signal["air_yards_share"], 6
    )
    # Not incidentally equal to either share it is built from.
    assert signal["wopr"] != signal["target_share"]


def test_the_published_id_declares_where_it_came_from():
    """`player_id` is documented as canonical, and this collector cannot
    produce a canonical one yet. A consumer must be able to tell."""
    signal = build_signal(_row(), _bases(), now=NOW)
    assert signal["player_id"] == "00-KC-WR1"
    assert signal["player_id_source"] == "upstream_gsis"


def test_situational_splits_are_present_with_null_members():
    """Present rather than absent, so "this collector does not supply it" never
    has to be told apart from "the key is missing"."""
    signal = build_signal(_row(), _bases(), now=NOW)
    for block in ("redzone", "goal_line", "two_minute", "alignment"):
        assert signal[block], f"{block} is empty — nothing would be asserted"
        assert all(value is None for value in signal[block].values())


def test_drift_measures_the_upstreams_own_shares_not_ours():
    """A healthy feed sums to 1.0. This is the only independent check available:
    the shares this collector computes are divided by a base it summed itself."""
    usage = WeekUsage(
        denominators={
            "KC": TeamDenominators(team="KC", targets=25, upstream_target_share_sum=1.0),
            "BUF": TeamDenominators(
                team="BUF", targets=16, upstream_target_share_sum=0.9
            ),
        }
    )
    assert team_sum_drift(usage) == {"KC": 0.0, "BUF": 0.1}


def test_a_team_with_no_targets_is_not_reported_as_drift():
    """A team whose rows never arrived is a coverage fact, not a wrong base —
    reporting it as drift 1.0 would bury the teams that really are wrong."""
    usage = WeekUsage(
        denominators={
            "KC": TeamDenominators(team="KC", targets=0, upstream_target_share_sum=0.0)
        }
    )
    assert team_sum_drift(usage) == {}


async def _capture(body: str):
    import respx

    from .conftest import UPSTREAM_FOR_SEASON

    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM_FOR_SEASON).mock(
            return_value=httpx.Response(200, text=body)
        )
        async with httpx.AsyncClient() as client:
            return await capture_usage_share(
                2026, 1, client=client, lake=SpyLake(), now=NOW
            )


async def test_a_drifting_team_lands_in_the_envelopes_errors():
    """The alert is aggregate, so it has to be *recorded* somewhere a reader
    looks. An OTel gauge alone is invisible in the lake object."""
    records = [dict(record) for record in SAMPLE_RECORDS]
    for record in records:
        if record["player_id"] == "00-BUF-WR1":
            record["target_share"] = "0.1"

    envelopes = await _capture(to_csv(records))
    errors = envelopes["player_usage_weekly"].errors
    drift = [error for error in errors if error["reason"] == "team_sum_drift"]
    assert len(drift) == 1, errors
    assert "BUF" in drift[0]["detail"]


async def test_a_healthy_week_records_no_drift_error():
    """The counterpart: a check that fires on clean data is noise, and noise is
    how a real alert gets ignored."""
    envelopes = await _capture(to_csv(SAMPLE_RECORDS))
    errors = envelopes["player_usage_weekly"].errors
    assert [e for e in errors if e["reason"] == "team_sum_drift"] == []


async def test_a_refused_row_is_counted_missing_with_its_reason(monkeypatch):
    """End-to-end for the spec's rejection rule: a share without its base is
    not stored, AND the hole it leaves is explicit. Dropping the row silently
    would shrink the numerator and the denominator together, which reads as a
    perfectly healthy week that is quietly one player short.
    """
    usage = WeekUsage(
        rows=[
            _row(upstream_player_id="00-KC-WR1"),
            _row(upstream_player_id="00-DEN-WR1", team="DEN"),
        ],
        denominators={"KC": _bases()},
    )

    async def already_fetched(*args, **kwargs):
        return usage

    monkeypatch.setattr("usage_share.capture.fetch_week_usage", already_fetched)
    async with httpx.AsyncClient() as client:
        envelopes = await capture_usage_share(
            2026, 1, client=client, lake=SpyLake(), now=NOW
        )
    envelope = envelopes["player_usage_weekly"]

    assert [row["team"] for row in envelope.signals] == ["KC"]
    assert "player:00-DEN-WR1" in envelope.coverage.missing
    assert {"reason": "missing_denominators", "detail": "player:00-DEN-WR1"} in (
        envelope.errors
    )


async def test_a_team_whose_offense_never_appeared_fails_its_denominators_key():
    """"Complete but zero" and "absent" are different, and only one of them is
    a base a share can be taken against."""
    records = [
        record
        for record in SAMPLE_RECORDS
        if record["team"] == "KC" and record["week"] == "1"
    ]
    empty = dict(records[0])
    empty.update(
        player_id="00-NYJ-WR1",
        position="WR",
        team="NYJ",
        game_id="2026_01_NYJ_NE",
        attempts="",
        sacks_suffered="",
        targets="",
        receiving_air_yards="",
        carries="",
        target_share="",
    )

    envelopes = await _capture(to_csv([*records, empty]))
    envelope = envelopes["player_usage_weekly"]
    reasons = {error["reason"] for error in envelope.errors}
    assert "empty_denominators" in reasons, envelope.errors
    assert "denominators:NYJ" in envelope.coverage.missing
