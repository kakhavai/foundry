"""Shrinkage, the split-half stability check, and the refusal to serve.

The spec's named failure mode: *"rates that are pure sampling noise presented
as tendencies."* This file tests the three mechanisms that answer it, at the
unit level, with hand-built samples — because the arms that matter are the ones
real data does not currently produce.

**Two guards, four arms, a fixture per arm.** A guard whose two arms look alike
needs a fixture that can tell its sides apart:

    stability     a rate that IS stable, served raw          -> `_stable_samples`
                  a rate that is NOT stable and NOT shrunk   -> `_confident_noise`
    (continuity is the other guard, and lives in test_continuity_alarm.py)

The refusal arm is the awkward one and it is awkward for a real reason: it
needs a rate whose between-crew variance dwarfs its sampling error — so no
material shrinkage — while its odd/even halves correlate at zero. Those two
facts are estimated from different partitions of the same data, so they can and
do disagree; `_confident_noise` builds the disagreement deliberately.
"""

import math
import statistics

import pytest

from officiating.adapters.pbp import REGULATION_SECONDS, GamePenalties
from officiating.rates import (
    MIN_CREWS_FOR_STABILITY,
    RATE_NAMES,
    REASON_NO_SAMPLE,
    REASON_UNSTABLE_AND_UNSHRUNK,
    SHRINKAGE_MATERIAL_BELOW,
    CrewSample,
    build_rate,
    estimate_priors,
    fisher_ci_low,
    pearson,
    split_half,
)

RATE = "penalties_per_game"


def _game(penalties: int, *, plays: int = 120) -> GamePenalties:
    return GamePenalties(penalties=penalties, offensive_plays=plays)


def _sample(crew_id: str, per_week: dict[int, int]) -> CrewSample:
    weeks = tuple(sorted(per_week))
    return CrewSample(
        crew_id=crew_id,
        weeks=weeks,
        games=tuple(_game(per_week[week]) for week in weeks),
    )


def _stable_samples() -> list[CrewSample]:
    """Crews that genuinely differ, consistently, across the whole season.

    Crew i calls `i` penalties in every game, so its odd-week and even-week
    means are identical and the split-half correlation is 1.0 — real,
    reproducible between-crew variation. Sampling noise is zero, so the
    shrinkage weight is 1.0 and the raw mean is served untouched.
    """
    return [
        _sample(f"2026-ref{700 + crew}", {week: crew for week in range(1, 9)})
        for crew in range(1, 11)
    ]


# Odd-week and even-week levels, one pair per crew, deliberately uncorrelated
# with each other across crews (r = -0.139 as built).
_ODD_LEVELS = [0, 50, 100, 150, 200, 250, 300, 350, 400, 450]
_EVEN_LEVELS = [250, 400, 50, 450, 0, 300, 150, 100, 350, 200]

# See `_confident_noise` — the window size at which shrinkage stops being
# material for this fixture. Found by measurement, not chosen.
_CAREER_WINDOW_GAMES = 200


def _noisy_season() -> list[CrewSample]:
    """Ten crews, eight games each, with real game-to-game noise.

    A season-shaped window — the regime the collector actually runs in today —
    used where the assertion is about `n` itself rather than about the
    refusal. `_stable_samples` cannot serve here: it has zero within-crew
    variance, so every stderr is 0.0 and a broken stderr would still match.
    """
    return [
        _sample(
            f"2026-ref{700 + crew}",
            {week: 8 + crew + (4 if week % 3 else -4) for week in range(1, 9)},
        )
        for crew in range(10)
    ]


def _confident_noise() -> list[CrewSample]:
    """Crews whose estimates look confident and whose halves say nothing.

    This is the arm the spec's refusal exists for, and constructing it exposes
    something worth stating outright: **at a fixed window size, "confident" and
    "uncorrelated halves" fight each other.** `k = tau2 / (tau2 + sigma2/n)`,
    so a crew can only escape shrinkage by differing from the league more than
    its own game-to-game noise — and a crew that differs *consistently* is
    exactly a crew whose odd and even weeks agree. Every attempt to build this
    fixture at eight games a crew produced a heavily shrunk rate instead.

    What breaks the tie is `n`. `sigma2/n` goes to zero as the window grows, so
    `k` goes to 1 regardless of how noisy the crew is — shrinkage stops
    protecting anything — while the split-half correlation does not improve at
    all, because it is measured across crews. At 200 games (`rate_window:
    career`, roughly twelve seasons) this fixture reports k = 0.994 and a
    split-half r of -0.139 whose interval runs from -0.707.

    **That is the whole argument for having two guards rather than one.**
    Shrinkage protects the small-window case; the split-half refusal is the
    only thing still standing in the large-window one. Today's collector only
    computes `season_to_date`, so this regime is not reachable in production
    yet — it becomes reachable the moment `trailing_17` or `career` is
    implemented, which is precisely when a reviewer would no longer be looking.
    """
    samples = []
    for index in range(10):
        weeks = tuple(range(1, _CAREER_WINDOW_GAMES + 1))
        per_week = {
            week: (_ODD_LEVELS[index] if week % 2 == 1 else _EVEN_LEVELS[index])
            for week in weeks
        }
        samples.append(_sample(f"2026-ref{700 + index}", per_week))
    return samples


# ---------------------------------------------------------------------------
# the primitives
# ---------------------------------------------------------------------------


def test_pearson_is_none_when_a_side_has_no_variance():
    """`None`, not 0.0. Zero variance means "this sample cannot answer the
    question"; a measured zero means "we looked and there is no relationship".
    Both end in not-stable, but only one is honest about why, and collapsing
    them reports an unexamined rate as PROVEN noise."""
    assert pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert pearson([1.0], [2.0]) is None
    assert pearson([], []) is None
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert pearson([1.0, 2.0, 3.0], [6.0, 4.0, 2.0]) == pytest.approx(-1.0)


def test_the_stability_interval_is_undefined_below_four_crews():
    """`n - 3` in the denominator. Three crews cannot distinguish any
    correlation from zero, and pretending otherwise would let a three-crew
    sample declare a rate stable on r = 0.99 that means nothing."""
    assert fisher_ci_low(0.99, MIN_CREWS_FOR_STABILITY - 1) is None
    assert fisher_ci_low(0.99, MIN_CREWS_FOR_STABILITY) is not None


def test_the_stability_interval_widens_as_the_sample_shrinks():
    """The property the refusal depends on: the same correlation is evidence
    at 17 crews and is not at 5."""
    assert fisher_ci_low(0.5, 5) < fisher_ci_low(0.5, 17)
    assert fisher_ci_low(0.5, 5) < 0 < fisher_ci_low(0.5, 17)


def test_a_perfect_correlation_does_not_blow_up():
    """`atanh` is undefined at 1.0, and a rate that correlates perfectly is
    exactly the case a naive implementation crashes on."""
    assert fisher_ci_low(1.0, 10) > 0.9
    assert fisher_ci_low(-1.0, 10) < -0.9


def test_split_half_ignores_a_crew_with_an_empty_half():
    """A crew with no even-week games is not a point on the scatter. Including
    it as a zero would drag the correlation toward a value nothing measured."""
    samples = [
        *_stable_samples(),
        _sample("2026-ref999", {1: 5, 3: 5, 5: 5}),  # odd weeks only
    ]

    r, ci_low = split_half(samples, RATE)

    assert r == pytest.approx(1.0)
    assert ci_low > 0


# ---------------------------------------------------------------------------
# shrinkage
# ---------------------------------------------------------------------------


def test_shrinkage_pulls_an_extreme_crew_toward_the_league_mean():
    """The core behaviour: a raw window mean is an estimate, not a parameter.

    Nine crews at 10 penalties a game with heavy per-game noise, plus one crew
    whose average happens to land at 20. With that much within-crew variance,
    almost none of the outlier's apparent deviation survives.
    """
    samples = [
        _sample(
            f"2026-ref{700 + crew}", {w: 10 + (5 if w % 2 else -5) for w in range(1, 9)}
        )
        for crew in range(9)
    ]
    samples.append(
        _sample("2026-ref799", {w: 20 + (5 if w % 2 else -5) for w in range(1, 9)})
    )
    priors = estimate_priors(samples, RATE)

    outlier = build_rate(samples[-1], RATE, priors)

    assert outlier["raw"] == pytest.approx(20.0)
    assert outlier["is_shrunk"] is True
    assert outlier["shrinkage_factor"] < 1.0
    # Strictly between the league mean and the raw observation, and strictly
    # nearer the mean than the raw value is. Two-sided on purpose: asserting
    # only `value < raw` passes for a value shrunk past the mean and out the
    # other side.
    assert priors.mu < outlier["value"] < outlier["raw"]


def test_a_rate_with_no_between_crew_signal_is_shrunk_all_the_way():
    """`tau2 = 0` — the observed spread of crew means is no larger than
    sampling noise alone would produce — means there is no evidence crews
    differ at all, and the honest published value is the league mean.

    This looks like a bug if you expected the raw number. It is the mechanism
    working: on the real 2025 season `defensive_holding_per_game` and
    `plays_per_game` both land here.
    """
    # Every crew drawn from the same alternating pattern, offset in phase so
    # the crew means are identical and only the within-crew noise differs.
    samples = [
        _sample(
            f"2026-ref{700 + crew}",
            {w: (12 if (w + crew) % 2 else 4) for w in range(1, 9)},
        )
        for crew in range(10)
    ]
    priors = estimate_priors(samples, RATE)

    assert priors.tau2 == 0.0
    built = build_rate(samples[0], RATE, priors)
    assert built["shrinkage_factor"] == 0.0
    assert built["is_shrunk"] is True
    assert built["value"] == pytest.approx(priors.mu)


def test_tau2_is_net_of_sampling_noise_not_the_raw_spread_of_crew_means():
    """The correction that makes shrinkage mean anything.

    Crew means always differ a little, even when every crew is drawn from the
    same distribution — that is what sampling noise IS. Taking
    `Var(crew means)` as the between-crew variance therefore reports a
    difference that does not exist, `k` comes out far too high, and the raw
    means are published almost untouched. The subtraction of `sigma2 / mean_n`
    is what removes the part of the spread that noise alone explains.

    This fixture is the case that separates them: eight crews whose true rate
    is identical, differing only through a deterministic pattern that gives
    each a slightly different mean. `Var(crew means)` is comfortably positive;
    the corrected estimate clamps to zero.
    """
    samples = [
        _sample(
            f"2026-ref{700 + crew}",
            {
                week: (20 if (week * 3 + crew * 5) % 7 < 3 else 0)
                for week in range(1, 9)
            },
        )
        for crew in range(8)
    ]
    means = [statistics.fmean(sample.values(RATE)) for sample in samples]
    assert statistics.variance(means) > 1.0, "the crew means must actually differ"

    priors = estimate_priors(samples, RATE)

    assert priors.tau2 == 0.0, (
        "the spread of crew means here is entirely sampling noise; an "
        "uncorrected tau2 would report between-crew signal that is not there"
    )
    built = build_rate(samples[0], RATE, priors)
    assert built["shrinkage_factor"] == 0.0
    assert built["value"] == pytest.approx(priors.mu)


def test_a_rate_is_only_stable_when_its_interval_excludes_zero():
    """`stable` is the lower bound of the interval, not the sign of `r`.

    A positive correlation at a small number of crews is not evidence. This
    fixture correlates at r = +0.36 — the same order as
    `penalty_yards_per_game` on the real 2025 season, the best of the seven —
    and its 95% interval still runs below zero. Judging on `r > 0` would serve
    it as a stable, unshrunk tendency.
    """
    samples = [
        _sample(
            f"2026-ref{700 + crew}",
            {week: _WEAKLY_CORRELATED[crew][week % 2] for week in range(1, 9)},
        )
        for crew in range(len(_WEAKLY_CORRELATED))
    ]
    priors = estimate_priors(samples, RATE)

    assert priors.split_half_r > 0.2, "the fixture must correlate POSITIVELY"
    assert priors.split_half_ci_low < 0.0, "and still not exclude zero"
    assert priors.stable is False


# Odd/even level pairs per crew, chosen so the correlation across crews is
# positive but weak — the regime where `r > 0` and "distinguishable from zero"
# disagree, and the only one in which that distinction is observable.
_WEAKLY_CORRELATED = [
    (10, 12),
    (20, 70),
    (30, 30),
    (40, 50),
    (50, 10),
    (60, 40),
    (12, 20),
    (70, 60),
]


def test_rate_stderr_is_the_standard_error_at_games_sampled():
    """The spec's definition, exactly: sqrt(within-crew variance / n).

    Computed independently here rather than read back out of the same helper —
    two independent declarations of one expectation mean neither is
    load-bearing.
    """
    samples = _noisy_season()
    priors = estimate_priors(samples, RATE)

    built = build_rate(samples[0], RATE, priors)

    expected = math.sqrt(priors.sigma2 / len(samples[0].games))
    assert built["stderr"] == pytest.approx(expected, rel=1e-4)
    assert built["stderr"] > 0, "a zero stderr would match a broken formula too"
    assert built["games_sampled"] == 8


def test_stderr_shrinks_as_the_window_grows():
    """A rate over 4 games must not claim the precision of one over 16. If
    stderr ignored `n`, every crew would report the same confidence regardless
    of how much was behind it — which is the spec's failure mode with extra
    steps."""
    short = _sample("2026-ref701", {1: 5, 2: 15, 3: 5, 4: 15})
    long = _sample("2026-ref702", {w: (5 if w % 2 else 15) for w in range(1, 17)})
    priors = estimate_priors([short, long, *_noisy_season()], RATE)

    assert (
        build_rate(short, RATE, priors)["stderr"]
        > (build_rate(long, RATE, priors)["stderr"])
    )


def test_every_rate_ships_with_games_sampled_and_stderr():
    """ "Every rate must ship with `games_sampled` and `rate_stderr` — no
    exceptions." Asserted over all eight, with a length check, because
    `all(...)` over an empty iterable is True."""
    samples = _noisy_season()
    priors = {rate: estimate_priors(samples, rate) for rate in RATE_NAMES}

    built = {rate: build_rate(samples[0], rate, priors[rate]) for rate in RATE_NAMES}

    assert len(built) == 8
    for rate, row in built.items():
        assert row["games_sampled"] == 8, rate
        assert row["stderr"] is not None, rate
        assert row["stderr"] >= 0, rate


# ---------------------------------------------------------------------------
# the refusal — both arms
# ---------------------------------------------------------------------------


def test_arm_one_a_stable_rate_is_served_unshrunk():
    """A rate whose halves agree across crews is real signal, and is published
    as the crew's own number."""
    samples = _stable_samples()
    priors = estimate_priors(samples, RATE)

    assert priors.stable is True
    assert priors.split_half_r == pytest.approx(1.0)

    built = build_rate(samples[3], RATE, priors)
    assert built["served"] is True
    assert built["stable"] is True
    assert built["is_shrunk"] is False
    assert built["shrinkage_factor"] >= SHRINKAGE_MATERIAL_BELOW
    assert built["value"] == pytest.approx(built["raw"])
    assert built["refusal_reason"] is None


def test_arm_two_a_confident_noise_rate_is_refused():
    """THE refusal. Not stable and not materially shrunk, so `value` is null.

    `raw` survives — a consumer doing its own statistics is entitled to the
    observation. It is `value`, the field a naive consumer reads and formats to
    three decimal places, that goes away.
    """
    samples = _confident_noise()
    priors = estimate_priors(samples, RATE)

    assert priors.stable is False
    assert priors.split_half_ci_low is not None
    assert priors.split_half_ci_low <= 0

    built = build_rate(samples[0], RATE, priors)
    assert built["is_shrunk"] is False
    assert built["shrinkage_factor"] >= SHRINKAGE_MATERIAL_BELOW
    assert built["served"] is False
    assert built["value"] is None
    assert built["refusal_reason"] == REASON_UNSTABLE_AND_UNSHRUNK
    assert built["raw"] is not None, "the observation survives the refusal"
    assert built["stderr"] is not None


def test_an_unstable_but_shrunk_rate_is_served():
    """The third combination, and the one the real season lands in.

    "Refuse to serve any rate whose split-half correlation is not
    distinguishable from zero WITHOUT is_shrunk = true" — so with it, the rate
    is served. Measured against the 2025 regular season, all seven rates are
    here: none stable, all materially shrunk.
    """
    # Ten crews whose means differ only as much as noise would explain, so
    # k is small — but whose halves still correlate at nothing.
    samples = [
        _sample(
            f"2026-ref{700 + crew}",
            {w: (0 if (w * 7 + crew * 3) % 5 else 20) for w in range(1, 9)},
        )
        for crew in range(10)
    ]
    priors = estimate_priors(samples, RATE)

    built = build_rate(samples[0], RATE, priors)

    assert priors.stable is False
    assert built["is_shrunk"] is True
    assert built["served"] is True
    assert built["value"] is not None
    assert built["refusal_reason"] is None


def test_a_crew_with_no_games_is_refused_for_a_different_reason():
    """An empty window and a noisy one are different facts. Collapsing them
    into one refusal reason would send an operator looking at the statistics
    when the actual problem is that play-by-play carried nothing."""
    empty = CrewSample(crew_id="2026-ref799", weeks=(), games=())
    priors = estimate_priors(_stable_samples(), RATE)

    built = build_rate(empty, RATE, priors)

    assert built["served"] is False
    assert built["value"] is None
    assert built["raw"] is None
    assert built["games_sampled"] == 0
    assert built["refusal_reason"] == REASON_NO_SAMPLE


def test_seconds_per_play_is_derived_from_snaps_not_stored():
    """A pace proxy, and the one rate that is not a simple count. A crew whose
    games run 100 snaps is slower per play than one whose games run 150."""
    slow = CrewSample("2026-ref701", (1, 2), (_game(10, plays=100),) * 2)
    fast = CrewSample("2026-ref702", (1, 2), (_game(10, plays=150),) * 2)
    priors = estimate_priors([slow, fast, *_noisy_season()], "seconds_per_play")

    assert build_rate(slow, "seconds_per_play", priors)["raw"] == pytest.approx(
        REGULATION_SECONDS / 100
    )
    assert build_rate(fast, "seconds_per_play", priors)["raw"] == pytest.approx(
        REGULATION_SECONDS / 150
    )


def test_the_eight_spec_rates_are_all_present():
    """The spec's field table, pinned. A rate quietly dropped from
    RATE_EXTRACTORS would simply stop appearing in the signal, and every other
    test here would still pass."""
    assert RATE_NAMES == (
        "penalties_per_game",
        "penalty_yards_per_game",
        "dpi_per_game",
        "dpi_yards_per_game",
        "offensive_holding_per_game",
        "defensive_holding_per_game",
        "plays_per_game",
        "seconds_per_play",
    )
