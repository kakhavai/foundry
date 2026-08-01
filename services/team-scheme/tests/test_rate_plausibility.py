"""Reporting a rate that is unlike the rest of its own pass — never refusing it.

**Why this file exists.** Run against live 2025, `no_huddle_rate` publishes
`0.6223` for WAS against a league median of `0.0748` and a second-highest of
`0.2264`. A 62% no-huddle rate is not a thing an NFL offense does. The
arithmetic is correct; nflfastR's `no_huddle` column for that team-season
almost certainly is not — and until this file the collector had **no
plausibility statement anywhere**, so coverage read 1.0, the schema validated,
the four-decimal contract held, and the number shipped as a fact.

It is also the one class of defect **no mutation can express**: "this number is
implausible" is not a code path that can be deleted. It was found by inspecting
live output, which is why the mechanism it produced has to be tested from both
ends — that a real outlier is reported, and that ordinary variation is not.

The rule asserts no league-average prior. See the reasoning block above
`flag_dispersion_outliers` in `rates.py`, including why a *refusing* bound was
rejected.
"""

import pytest

from team_scheme.capture import PROFILE, REASON_RATE_OUTLIER
from team_scheme.rates import (
    DISPERSION_FIELDS,
    MIN_DISPERSION_POPULATION,
    flag_dispersion_outliers,
)

from .conftest import Feeds, SpyLake, flat_proe, run_capture

# The measured live 2025 `no_huddle_rate` distribution, top five exact and the
# tail shaped to the reported 0.0748 median. Committed as the fixture because
# it is the finding: a synthetic outlier would prove the arithmetic works, and
# this proves it works on the number that motivated it.
WAS_2025_NO_HUDDLE = [
    0.6223,  # WAS — the outlier
    0.2264,  # NO
    0.2151,  # NYG
    0.1929,  # PHI
    0.1295,  # DEN
    0.1204,
    0.1151,
    0.1090,
    0.1042,
    0.0981,
    0.0944,
    0.0902,
    0.0871,
    0.0838,
    0.0802,
    0.0771,
    0.0748,
    0.0742,
    0.0715,
    0.0688,
    0.0651,
    0.0620,
    0.0587,
    0.0554,
    0.0521,
    0.0488,
    0.0442,
    0.0401,
    0.0355,
    0.0302,
    0.0244,
    0.0181,
]

TEAM_CODES = [f"T{index:02d}" for index in range(32)]


def rows_from(field: str, values: list[float]) -> list[dict]:
    """`[{team_id, <field>}]` — the shape `flag_dispersion_outliers` reads."""
    return [
        {"team_id": team, field: value}
        for team, value in zip(TEAM_CODES, values, strict=False)
    ]


# --------------------------------------------------------------------------
# The rule, against the distribution that motivated it
# --------------------------------------------------------------------------


def test_the_live_2025_no_huddle_outlier_is_reported():
    """WAS and nothing else, on the real distribution."""
    outliers = flag_dispersion_outliers(rows_from("no_huddle_rate", WAS_2025_NO_HUDDLE))
    assert [(o.team_id, o.value) for o in outliers] == [("T00", 0.6223)]
    assert outliers[0].field == "no_huddle_rate"


def test_the_second_highest_team_is_not_reported():
    """**The leave-one-out property doing its job, and the negative control
    the rule most needs.**

    NO at 0.2264 is 3x the median and is *not* reported, because WAS's own
    presence widens the pack every other team is measured against. A rule that
    computed one band for the whole league and applied it to every team would
    flag NO as well — and then NYG, and then PHI — which is how a plausibility
    check turns into noise.
    """
    outliers = flag_dispersion_outliers(rows_from("no_huddle_rate", WAS_2025_NO_HUDDLE))
    assert {o.team_id for o in outliers} == {"T00"}


def test_the_reported_band_is_evidence_not_a_verdict():
    """The error message quotes the band, so a reader can disagree with the
    rule and still see the numbers. A bare "implausible" would be the
    collector asserting a prior it cannot source."""
    (outlier,) = flag_dispersion_outliers(
        rows_from("no_huddle_rate", WAS_2025_NO_HUDDLE)
    )
    assert outlier.high == pytest.approx(0.2264 + (0.2264 - 0.0181))
    assert outlier.low == pytest.approx(0.0181 - (0.2264 - 0.0181))
    assert outlier.value > outlier.high


def test_an_ordinary_league_reports_nothing():
    """The other side. Without it the rule could report everything and every
    test above would still pass. These are the real measured 2025 ranges for
    the other bounded shares."""
    for field, values in (
        ("neutral_pass_rate", [0.521 + 0.0043 * i for i in range(32)]),
        ("shotgun_rate", [0.410 + 0.0148 * i for i in range(32)]),
        ("play_action_rate", [0.101 + 0.0033 * i for i in range(32)]),
        ("sec_per_play_neutral", [29.91 + 0.1497 * i for i in range(32)]),
    ):
        assert flag_dispersion_outliers(rows_from(field, values)) == [], field


def test_a_low_side_outlier_is_reported_too():
    """Both directions. A rule checking only the ceiling would miss a team
    whose feed silently stopped populating the column — which reads as 0.0,
    not as a null, and so is not caught by the coverage predicate either."""
    values = [0.0] + [0.30 + 0.004 * index for index in range(31)]
    (outlier,) = flag_dispersion_outliers(rows_from("no_huddle_rate", values))
    assert outlier.team_id == "T00"
    assert outlier.value == 0.0
    assert outlier.value < outlier.low


# --------------------------------------------------------------------------
# Where the rule deliberately says nothing
# --------------------------------------------------------------------------


def test_a_pack_too_small_to_have_a_shape_is_not_judged():
    """Below `MIN_DISPERSION_POPULATION` there is no pack to be outside of.
    Seven teams with a 10x spread report nothing; eight report the outlier."""
    assert MIN_DISPERSION_POPULATION == 8
    extreme = [0.9] + [0.01 * index for index in range(1, 8)]
    assert flag_dispersion_outliers(rows_from("no_huddle_rate", extreme[:7])) == []
    assert flag_dispersion_outliers(rows_from("no_huddle_rate", extreme)) != []


def test_a_pack_of_zero_width_is_not_judged():
    """Thirty-one identical values and one a hundredth higher. A
    gap-against-nothing comparison fires here; the spread guard makes it
    silent, because a pack with no width has no shape to be outside of."""
    values = [0.11] + [0.10] * 31
    assert flag_dispersion_outliers(rows_from("no_huddle_rate", values)) == []


def test_a_null_rate_is_not_a_value_and_does_not_count_toward_the_pack():
    """A degraded charting feed nulls three fields on every row. Reading those
    nulls as zeroes would invent a 32-team pack of zeroes and report every
    populated team as an outlier — turning a field-level degradation into
    thirty-two spurious errors."""
    rows = rows_from("play_action_rate", [0.15] * 32)
    for row in rows[:30]:
        row["play_action_rate"] = None
    assert flag_dispersion_outliers(rows) == []


def test_pass_rate_over_expected_is_not_checked():
    """Signed and centred near zero, so "outside the pack" says nothing about
    it: a pack straddling zero makes the comparison meaningless rather than
    merely noisy. Named in the field list rather than left to chance, and
    pinned so a later 'why not this one too?' has to read the reason first."""
    assert "pass_rate_over_expected" not in DISPERSION_FIELDS
    assert "personnel_rates" not in DISPERSION_FIELDS
    extreme = [-40.0] + [0.1 * index for index in range(31)]
    assert flag_dispersion_outliers(rows_from("pass_rate_over_expected", extreme)) == []


def test_every_checked_field_is_a_real_row_field():
    """A typo in `DISPERSION_FIELDS` is silent: `row.get(field)` returns None
    for every row, the field is skipped, and the check quietly covers one
    fewer rate than it claims to."""
    from team_scheme.capture import build_profile_row
    from team_scheme.rates import TeamSeasonRates

    row = build_profile_row(
        "AAA",
        2026,
        TeamSeasonRates(
            games_sampled=1,
            sampled_weeks=(1,),
            folded_teams=frozenset({"AAA"}),
            neutral_pass_rate=0.5,
            pass_rate_over_expected=1.0,
            sec_per_play_neutral=25.0,
            no_huddle_rate=0.1,
            shotgun_rate=0.5,
            play_action_rate=0.2,
            pre_snap_motion_rate=0.5,
            personnel_rates=None,
            fourth_down_go_rate=0.0,
        ),
        degraded=(),
    )
    assert set(DISPERSION_FIELDS) <= set(row)


# --------------------------------------------------------------------------
# End to end — reported, published, and coverage untouched
# --------------------------------------------------------------------------


def _no_huddle_by_team(feeds: Feeds, counts: dict[str, int]) -> None:
    """Stamp `no_huddle` on the first `counts[team]` plays of each team-week."""
    seen: dict[tuple[str, int], int] = {}
    for play in feeds.plays:
        key = (play["posteam"], play["week"])
        index = seen.get(key, 0)
        seen[key] = index + 1
        play["no_huddle"] = 1 if index < counts.get(play["posteam"], 0) else 0


TEN_TEAMS = tuple(f"T{index:02d}" for index in range(10))


async def test_an_outlier_is_reported_and_its_row_still_publishes(lake: SpyLake):
    """**The whole judgement call, as behaviour.**

    T00 runs no-huddle on 8 of 10 snaps; the other nine teams sit between 0.0
    and 0.2. The row publishes unchanged, the team stays present, coverage does
    not move, and the pass says so in `errors`.

    A refusing bound would drop the row and record T00 missing. That is the
    design this collector rejected: it asserts the upstream is wrong on the
    strength of one source, and a threshold tuned to today's distribution
    becomes a filter on tomorrow's signal.
    """
    feeds = Feeds(proe=flat_proe(TEN_TEAMS))
    _no_huddle_by_team(
        feeds,
        {"T00": 8, **{team: index % 3 for index, team in enumerate(TEN_TEAMS[1:])}},
    )

    envelopes = await run_capture(feeds, lake=lake)
    profile = envelopes[PROFILE]

    row = next(r for r in profile.signals if r["team_id"] == "T00")
    assert row["no_huddle_rate"] == 0.8

    # Published, present, and coverage untouched.
    assert len(profile.signals) == len(TEN_TEAMS)
    assert "T00" not in profile.coverage.missing
    assert profile.coverage.present == len(TEN_TEAMS)

    # And reported, with the evidence in the message.
    reported = [e for e in profile.errors if e["reason"] == REASON_RATE_OUTLIER]
    assert len(reported) == 1
    assert "T00.no_huddle_rate" in reported[0]["detail"]
    assert "0.8" in reported[0]["detail"]


async def test_an_ordinary_pass_reports_no_outlier(lake: SpyLake):
    """The negative control end to end. Without it the error could be
    unconditional and the test above would still pass."""
    feeds = Feeds(proe=flat_proe(TEN_TEAMS))
    _no_huddle_by_team(feeds, {team: index % 3 for index, team in enumerate(TEN_TEAMS)})
    envelopes = await run_capture(feeds, lake=lake)
    assert REASON_RATE_OUTLIER not in {
        error["reason"] for error in envelopes[PROFILE].errors
    }


async def test_the_four_team_fixture_is_below_the_population_floor(lake: SpyLake):
    """Every other test in this suite runs a four-team fixture, so none of
    them can trip the rule by accident. Asserted rather than assumed — a
    lowered `MIN_DISPERSION_POPULATION` would start decorating unrelated
    tests' envelopes with errors, and the failure would look like those tests
    breaking."""
    envelopes = await run_capture(lake=lake)
    assert REASON_RATE_OUTLIER not in {
        error["reason"] for error in envelopes[PROFILE].errors
    }
