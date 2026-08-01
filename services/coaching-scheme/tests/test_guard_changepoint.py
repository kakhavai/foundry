"""Guard 2: the estimator, its calibration, and the fact that it ships OFF.

`changepoint.py` carries the measurement. What is pinned here:

* the **estimator** (`max_t`) does what it claims — normalises by scatter,
  finds the split, is sign-agnostic;
* the **calibration** holds: `detect` fires on pure noise at approximately its
  own alpha, machine-checked rather than asserted in a docstring. That is the
  one property a permutation test must have, and the property the original
  fixed-threshold detector lacked;
* the **seed is stable across processes**, because a p-value that moved
  between passes would silently disable the digest gate;
* `explains` still has both arms; and
* the guard is **disabled**, the rows say so with a null rather than a
  `false`, and flipping the flag genuinely re-enables it — so the disabled
  path is a switch, not dead code.

**No test here computes its fixture from a constant it is trying to pin.** The
previous revision had two "boundary" tests that both derived their input from
`MIN_SHIFT`, so they pinned `>` versus `>=` and nothing about the value —
lowering the constant survived them both. `MIN_SHIFT` is gone entirely, and
`SIGNIFICANCE_ALPHA` is asserted as a literal below while the calibration
tests pass their alpha explicitly.
"""

import random
import statistics

import pytest

from coaching_scheme import changepoint as changepoint_module
from coaching_scheme.capture import PROFILE
from coaching_scheme.changepoint import (
    CHANGEPOINT_ENABLED,
    CHANGEPOINT_UNCALIBRATED,
    MAX_WINDOW,
    MIN_RUN,
    SIGNIFICANCE_ALPHA,
    detect,
    explains,
    max_t,
    permutation_seed,
)
from coaching_scheme.revisions import build_revisions

from .conftest import (
    SEASON,
    Feeds,
    SpyLake,
    coaches_with_change,
    proe_with_shift,
    run_capture,
    steady_coaches,
)

SEED = 20260801


def noisy(values, *, jitter=0.4, seed=7):
    """Real data is never perfectly flat, and `max_t` skips a zero-variance
    window on purpose. Fixtures therefore carry a little scatter, or they would
    exercise the degenerate branch instead of the statistic."""
    rng = random.Random(seed)
    return [v + rng.uniform(-jitter, jitter) for v in values]


def weeks(values):
    return list(enumerate(values, start=1))


# --------------------------------------------------------------------------
# The constants, as literals
# --------------------------------------------------------------------------


def test_the_constants_are_what_the_measurement_used():
    """Asserted as literals, never recomputed from themselves.

    The alpha is the one `changepoint.py`'s measured-FPR table was produced
    at; changing it silently would leave that table describing a different
    test.
    """
    assert SIGNIFICANCE_ALPHA == 0.01
    assert MIN_RUN == 3
    assert MAX_WINDOW == 4
    assert changepoint_module.PERMUTATIONS == 299


# --------------------------------------------------------------------------
# max_t — the estimator
# --------------------------------------------------------------------------


def test_max_t_finds_the_split_and_reports_the_shift():
    statistic, split, shift, before, after, n1, n2 = max_t(
        noisy([0, 0, 0, 0, 12, 12, 12, 12])
    )
    assert split == 4
    assert shift == pytest.approx(12.0, abs=0.6)
    assert statistic > 5
    assert (n1, n2) == (4, 4)
    assert before == pytest.approx(0.0, abs=0.4)
    assert after == pytest.approx(12.0, abs=0.4)


def test_max_t_is_sign_agnostic():
    """A detector comparing a signed statistic misses every offence that got
    more run-heavy, which is the commoner direction after a firing."""
    up = max_t(noisy([0, 0, 0, 0, 12, 12, 12, 12]))
    down = max_t(noisy([12, 12, 12, 12, 0, 0, 0, 0]))
    assert up[0] == pytest.approx(down[0], rel=0.4)
    assert up[2] > 0 > down[2]


def test_max_t_normalises_by_the_teams_own_scatter():
    """**The estimator fix, as a test.**

    Two teams with the same six-point shift, one steady and one volatile. The
    raw mean difference the first implementation used scores them identically.
    A `t` does not, and should not — six points means different things for an
    offence that never moves and one swinging twenty a week.
    """
    steady = max_t(noisy([0, 0, 0, 0, 6, 6, 6, 6], jitter=0.5, seed=1))
    volatile = max_t(noisy([0, 0, 0, 0, 6, 6, 6, 6], jitter=6.0, seed=2))
    assert steady[2] == pytest.approx(volatile[2], abs=5.0)
    assert steady[0] > volatile[0] * 2


def test_max_t_refuses_a_degenerate_zero_variance_window():
    """`t` would be infinite. That is an artefact of a synthetic sample rather
    than evidence, and admitting it would make every flat fixture fire."""
    statistic, split = max_t([0.0, 0.0, 0.0, 5.0, 5.0, 5.0])[:2]
    assert split is None
    assert statistic == 0.0


def test_max_t_caps_both_windows():
    _, split, _, before, _, n1, n2 = max_t(
        noisy([7, 7, 7, 7, 0, 0, 0, 0, 12, 12, 12, 12])
    )
    assert n1 <= MAX_WINDOW and n2 <= MAX_WINDOW
    # Against the four weeks immediately before, not the 7s: an uncapped
    # baseline averages all eight priors to ~3.5, a level never played at.
    assert split == 8
    assert before == pytest.approx(0.0, abs=0.4)


# --------------------------------------------------------------------------
# Calibration — the property the original detector lacked
# --------------------------------------------------------------------------


def test_the_false_positive_rate_tracks_alpha_on_pure_noise():
    """**The headline property, machine-checked.**

    150 pure-Gaussian series of 17 weeks at the real sd. No changepoint exists
    in any of them, so every firing is a false positive. A permutation test
    delivers its alpha by construction; this checks the implementation does
    what the theory says.

    The original fixed-threshold detector fired on **55%** of week-shuffled
    real series. That is the number this test exists to make unreintroducible.

    Bounds are binomial slack, not a fudge: at alpha=0.05 over 150 series the
    expectation is 7.5 with sd ~2.7, so 25 is >6 sd out. The lower bound
    catches the opposite failure — a detector that never fires is also
    "calibrated" on a naive reading.
    """
    rng = random.Random(99)
    fires = 0
    trials = 150
    for index in range(trials):
        values = [rng.gauss(0.0, 8.0) for _ in range(17)]
        if detect(weeks(values), seed=index, alpha=0.05, permutations=99):
            fires += 1
    assert 0 < fires <= 25, f"{fires}/{trials} false positives at alpha=0.05"


def test_a_tighter_alpha_fires_strictly_less():
    """The alpha is a real dial. Without this it could be ignored entirely and
    the test above would still pass at its loose bound."""
    rng = random.Random(1234)
    corpus = [weeks([rng.gauss(0.0, 8.0) for _ in range(17)]) for _ in range(120)]

    def fires(alpha):
        return sum(
            1
            for i, s in enumerate(corpus)
            if detect(s, seed=i, alpha=alpha, permutations=99)
        )

    loose, tight = fires(0.20), fires(0.01)
    assert tight < loose


def test_an_overwhelming_shift_is_still_detected():
    """Calibration must not mean "never fires".

    **A minority shifted segment, deliberately.** A permutation test has
    surprisingly little power against a *balanced* step, because the null
    contains the reorderings that recreate it: an 8-and-8 split of the same
    30-point step measures p=0.03, not 0.003, since a shuffle can easily land
    four lows next to four highs. Four high weeks among seventeen is much
    harder to reassemble by chance, so this fires at the floor.

    That is not a fixture convenience — it is a real property of the test and
    a second reason (independent of the effect-size finding) that guard 2 is
    weak on a 17-week season.
    """
    found = detect(
        weeks(noisy([0] * 13 + [30] * 4, jitter=2.0, seed=3)),
        seed=SEED,
        alpha=0.05,
    )
    assert found is not None
    assert found.week == 14
    assert found.shift == pytest.approx(30.0, abs=2.5)


def test_a_p_value_is_never_zero_and_never_exceeds_one():
    """The observed value is included in its own reference set — that is what
    keeps the test exact rather than anti-conservative, and why the schema
    types `changepoint_p_value` with `exclusiveMinimum: 0`."""
    found = detect(
        weeks(noisy([0] * 13 + [40] * 4, jitter=1.0, seed=5)),
        seed=SEED,
        alpha=0.5,
        permutations=99,
    )
    assert found is not None
    assert 0 < found.p_value <= 1.0
    # The floor is 1/(B+1), never 0 — that is the +1 in the numerator.
    assert found.p_value == pytest.approx(1 / 100)


def test_a_series_shorter_than_two_runs_returns_none():
    """A handoff in weeks 1-2 is undetectable — a known hole, stated rather
    than papered over with a one-week baseline."""
    assert detect(weeks([0, 0, 0, 30, 30]), seed=SEED) is None
    assert detect([], seed=SEED) is None


# --------------------------------------------------------------------------
# Determinism — the digest gate depends on it
# --------------------------------------------------------------------------


def test_the_seed_is_stable_across_processes():
    """**Not `hash()`.** Python salts `hash()` on `str` per process, so two
    pods would draw different nulls for one team and a single pod would
    disagree with itself across a restart. A p-value that moves between passes
    over identical upstream data makes every digest unique and silently
    disables the unchanged-snapshot gate.

    The literal is the point: recomputing it with `blake2b` here would pass
    against a `hash()` implementation too, on any single run.
    """
    value = permutation_seed("coaching-scheme", 2026, "KC")
    assert value == 16613222523932242960
    assert permutation_seed("coaching-scheme", 2026, "SF") != value
    assert permutation_seed("coaching-scheme", 2025, "KC") != value


def test_the_same_seed_gives_the_same_p_value():
    series = weeks(noisy([0] * 8 + [20] * 8, jitter=3.0, seed=11))
    first = detect(series, seed=SEED, alpha=0.5, permutations=99)
    second = detect(series, seed=SEED, alpha=0.5, permutations=99)
    assert first == second


# --------------------------------------------------------------------------
# explains — both arms
# --------------------------------------------------------------------------


def _revisions(grid):
    from coaching_scheme.adapters.games import TeamWeekCoach

    return build_revisions(
        [TeamWeekCoach("AAA", w, n) for w, n in sorted(grid.items())], season=SEASON
    )["AAA"]


def _changepoint(week):
    return changepoint_module.Changepoint(
        week=week,
        shift=20.0,
        p_value=0.005,
        statistic=6.0,
        before_mean=0.0,
        after_mean=20.0,
        weeks_before=4,
        weeks_after=4,
    )


def test_a_changepoint_matching_a_revision_is_explained():
    """**Arm A.** Alarming here would flag every genuine coaching change as a
    missed one."""
    revisions = _revisions(coaches_with_change("AAA", at_week=7)["AAA"])
    assert explains(_changepoint(7), revisions)


def test_a_changepoint_one_week_off_a_revision_is_still_explained():
    revisions = _revisions(coaches_with_change("AAA", at_week=7)["AAA"])
    assert explains(_changepoint(8), revisions)


def test_a_changepoint_two_weeks_off_a_revision_is_not_explained():
    """The neighbour that pins the tolerance at 1 rather than "some slack"."""
    revisions = _revisions(coaches_with_change("AAA", at_week=7)["AAA"])
    assert not explains(_changepoint(9), revisions)


def test_a_changepoint_with_no_revision_at_all_is_unexplained():
    """**Arm B**, and the normal case on a live season — see adapters/games.py."""
    revisions = _revisions(steady_coaches()["AAA"])
    assert len(revisions) == 1
    assert not explains(_changepoint(7), revisions)


def test_the_first_revision_never_explains_anything():
    """`revisions[1:]` is load-bearing. Asserted with a wide tolerance so the
    arithmetic cannot hide it."""
    revisions = _revisions(steady_coaches()["AAA"])
    assert revisions[0].effective_from_week == 1
    assert not explains(_changepoint(4), revisions, tolerance=99)


# --------------------------------------------------------------------------
# The guard is OFF, and the rows say so
# --------------------------------------------------------------------------


def test_the_guard_ships_disabled():
    """Asserted, so re-enabling it reds this test and forces whoever does it to
    read `changepoint.py`'s measurement first."""
    assert CHANGEPOINT_ENABLED is False


async def test_every_profile_row_reports_not_checked_rather_than_clean(
    lake: SpyLake,
):
    """**Null, not False.** `false` asserts "we checked and this row is
    clean". The collector has not checked, and a consumer filtering on
    `changepoint_unexplained == false` would silently treat unchecked rows as
    verified ones."""
    envelopes = await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=25.0)), lake=lake
    )
    rows = envelopes[PROFILE].signals
    assert rows
    for row in rows:
        assert row["changepoint_unexplained"] is None
        assert row["changepoint_week"] is None
        assert row["changepoint_shift"] is None
        assert row["changepoint_p_value"] is None
        assert (
            row["null_field_reason"]["changepoint_unexplained"]
            == CHANGEPOINT_UNCALIBRATED
        )


async def test_a_disabled_guard_raises_no_priority_error(lake: SpyLake):
    """The twenty-errors-a-pass pathology. A disabled detector must be silent,
    not quietly firing into the errors array."""
    envelopes = await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=25.0)), lake=lake
    )
    reasons = {e["reason"] for e in envelopes[PROFILE].errors}
    assert changepoint_module.REASON_UNEXPLAINED_CHANGEPOINT not in reasons


async def test_flipping_the_flag_re_enables_the_wiring(lake: SpyLake, monkeypatch):
    """**The disabled path is a switch, not dead code.**

    Without this, `capture.py` could stop calling `detect` altogether and every
    test above would still pass — so re-enabling the guard later would silently
    do nothing.
    """
    monkeypatch.setattr(changepoint_module, "CHANGEPOINT_ENABLED", True)
    envelopes = await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=40.0, jitter=2.0)),
        lake=lake,
    )
    flagged = [r for r in envelopes[PROFILE].signals if r["changepoint_unexplained"]]
    assert flagged, "the flag no longer reaches the detector"
    assert {r["team_id"] for r in flagged} == {"AAA"}
    assert flagged[0]["changepoint_week"] == 7
    assert flagged[0]["changepoint_p_value"] is not None
    assert "changepoint_unexplained" not in flagged[0]["null_field_reason"]
    assert changepoint_module.REASON_UNEXPLAINED_CHANGEPOINT in {
        e["reason"] for e in envelopes[PROFILE].errors
    }


def test_the_fixture_grid_is_what_it_claims():
    """A guard on the fixtures the assertions above rest on."""
    from .conftest import games_document

    assert "Interim AAA" not in games_document(steady_coaches())
    assert "Interim AAA" in games_document(coaches_with_change("AAA", at_week=7))


def test_the_fixtures_are_far_cleaner_than_real_data():
    """A reader's guard rail, not a behaviour test.

    Every fixture here is near-noiseless by construction, so `detect` looks far
    more powerful than it is. Real weekly PROE has sd ~6.9 points *within* a
    team-season, which is the whole reason the guard ships off. Stated in-suite
    so nobody concludes from a green run that guard 2 works.
    """
    assert statistics.stdev(noisy([0] * 8)) < 1.0
