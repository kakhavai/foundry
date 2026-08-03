"""The spec's named failure mode, both arms.

> When a starter goes to IR mid-week, the unit's aggregate grades do not move
> ... the line keeps its elite `pressure_rate_allowed_adj` into the exact week
> it will be worst ... **Nothing about the row looks malformed.**

There is nothing downstream of a stale unit row to detect it with — every
field is populated, every value is in range, coverage is unaffected, the
schema validates and the differential against `defensive-front` computes
cleanly. So the guard has to be proved from both directions: that a
recomputed row publishes, and that a stale one **fails**.

The stale arm is reached by disabling the correction — the single function
`ratings.apply_lineup_adjustment` — rather than by hand-building a row. A
guard tested against a hand-built row proves the assertion's arithmetic and
nothing about whether it is wired into the path that publishes.
"""

import jsonschema
import pytest

from offensive_line import ratings
from offensive_line.capture import REASON_STALE_UNIT, STRENGTH
from offensive_line.guard import StaleUnitRow, assert_lineup_guard
from offensive_line.ratings import (
    RECORD_UNIT,
    UNCORRECTABLE_LINEUP_CHANGE,
    apply_lineup_adjustment,
)

from . import season as season_module
from .conftest import Feeds, SpyLake, run_capture, units

CHURNED = season_module.TEAMS[season_module.CHURN_FROM]
STABLE = season_module.TEAMS[0]


# --------------------------------------------------------------------------
# The arm that must PUBLISH
# --------------------------------------------------------------------------


async def test_a_changed_lineup_with_a_recomputed_row_publishes():
    """Both halves of the spec's requirement, on the row that reaches the lake.

    `continuity_games == 0`, and the adjusted rate moved by exactly the
    replacement correction rather than staying where the departed player's
    snaps put it.
    """
    row = units(await run_capture(Feeds(), lake=SpyLake()))[CHURNED]
    assert row["lineup_changed"] is True
    assert row["continuity_games"] == 0
    assert row["lineup_adjustment_pressure_rate"] not in (None, 0.0)
    assert row["pressure_rate_allowed_adj"] != row["pressure_rate_allowed_adj_observed"]
    assert row["pressure_rate_allowed_adj"] == pytest.approx(
        row["pressure_rate_allowed_adj_observed"]
        + row["lineup_adjustment_pressure_rate"],
        abs=1e-4,
    )


async def test_an_unchanged_lineup_is_corrected_by_exactly_nothing():
    """The negative arm. Without it, a collector that applied a correction to
    every row would pass every test above — and would be publishing a
    personnel adjustment for a line that never changed."""
    row = units(await run_capture(Feeds(), lake=SpyLake()))[STABLE]
    assert row["lineup_changed"] is False
    assert row["lineup_adjustment_pressure_rate"] == 0.0
    assert row["pressure_rate_allowed_adj"] == row["pressure_rate_allowed_adj_observed"]


# --------------------------------------------------------------------------
# The arm that must FAIL
# --------------------------------------------------------------------------


async def test_a_changed_lineup_with_an_unmoved_row_fails_the_pass(monkeypatch):
    """**The guard, proved.** The correction is disabled at its single call
    site, so every other line of the collector runs unchanged and the rows it
    builds are the rows it would have published. They must not reach the lake.
    """

    def uncorrected(observed_adj, delta):
        return observed_adj

    monkeypatch.setattr(ratings, "apply_lineup_adjustment", uncorrected)

    lake = SpyLake()
    with pytest.raises(StaleUnitRow):
        await run_capture(Feeds(), lake=lake)

    # `fail_capture`'s half: the gap in the append-only lake is explicit
    # rather than something a reader has to infer from absence.
    assert lake.writes, "a failed capture must still write an envelope"
    for envelope in lake.writes:
        assert envelope.coverage.present == 0
        assert {error["reason"] for error in envelope.errors} == {REASON_STALE_UNIT}


async def test_a_stale_row_is_not_installed_over_the_last_good_capture(monkeypatch):
    """The re-raise, which matters as much as the write. Returning the failure
    envelopes normally would hand them to `CaptureState.apply_capture`, and an
    upstream that is fine would have cost `/signals` its data."""

    monkeypatch.setattr(
        ratings, "apply_lineup_adjustment", lambda observed_adj, delta: observed_adj
    )
    with pytest.raises(StaleUnitRow):
        await run_capture(Feeds(), lake=SpyLake())


def test_the_guard_checks_the_streak_independently_of_the_correction():
    """Arm 1 in isolation. `continuity_games` is structurally zero on a change
    today, which makes this a defence of an invariant rather than of a live
    path — and exactly why it is a named check rather than an inlined one: the
    mutation that deletes it survives any suite that cannot construct the row
    distinguishing the two."""
    row = {
        "record_type": RECORD_UNIT,
        "team_id": "AAA",
        "lineup_changed": True,
        "continuity_games": 3,
        "lineup_adjustment_pressure_rate": 0.05,
        "pressure_rate_allowed_adj_observed": 0.30,
        "pressure_rate_allowed_adj": 0.35,
    }
    with pytest.raises(StaleUnitRow, match="continuity_games"):
        assert_lineup_guard([row])


def test_the_guard_rejects_a_correction_of_exactly_zero():
    """Arm 2. A changed lineup corrected by nothing is indistinguishable from
    one that was never corrected, which is the row this exists to catch."""
    row = {
        "record_type": RECORD_UNIT,
        "team_id": "AAA",
        "lineup_changed": True,
        "continuity_games": 0,
        "lineup_adjustment_pressure_rate": 0.0,
        "pressure_rate_allowed_adj_observed": 0.30,
        "pressure_rate_allowed_adj": 0.30,
    }
    with pytest.raises(StaleUnitRow, match="no replacement correction"):
        assert_lineup_guard([row])


def test_the_guard_rejects_a_correction_that_was_published_but_not_applied():
    """Arm 3, and the one that catches the real defect: a correction computed,
    published in its own column, and never added to the number a generator
    reads. Every field is populated and the row validates."""
    row = {
        "record_type": RECORD_UNIT,
        "team_id": "AAA",
        "lineup_changed": True,
        "continuity_games": 0,
        "lineup_adjustment_pressure_rate": 0.05,
        "pressure_rate_allowed_adj_observed": 0.30,
        "pressure_rate_allowed_adj": 0.30,
    }
    with pytest.raises(StaleUnitRow, match="not recomputed"):
        assert_lineup_guard([row])


def test_the_guard_ignores_rows_it_has_nothing_to_say_about():
    """A starter row and an unchanged unit row are not its business. A guard
    that raised on either would fail every ordinary pass."""
    assert_lineup_guard(
        [
            {"record_type": "starter", "team_id": "AAA", "starter_position": "LT"},
            {
                "record_type": RECORD_UNIT,
                "team_id": "BBB",
                "lineup_changed": False,
                "continuity_games": 4,
                "lineup_adjustment_pressure_rate": 0.0,
                "pressure_rate_allowed_adj_observed": 0.3,
                "pressure_rate_allowed_adj": 0.3,
            },
        ]
    )


def test_the_correction_is_a_no_op_when_there_is_nothing_to_correct():
    """`apply_lineup_adjustment` on a null observed rate or a null delta
    returns the observed value rather than raising or inventing one."""
    assert apply_lineup_adjustment(None, 0.05) is None
    assert apply_lineup_adjustment(0.3, None) == 0.3
    assert apply_lineup_adjustment(0.3, 0.05) == pytest.approx(0.35)


# --------------------------------------------------------------------------
# The case that is coverage rather than a crash
# --------------------------------------------------------------------------


async def test_an_uncorrectable_change_is_coverage_missing_not_a_raise():
    """ "The lineup changed and there is no delta to correct it with" is
    **missing input data**, not a defect — so the team is dropped into
    `coverage.missing` with a reason and the other seven still publish.
    Failing the whole league's capture over one team's absent backup sample
    would make a data gap look like a code fault.

    Reached by removing every measured delta, which is what a season with no
    mid-window substitution anywhere looks like — the prior is derived from
    the measurements, so emptying them empties it too.
    """
    envelopes = await _capture_with(lambda totals, strengths: {})
    envelope = envelopes[STRENGTH]

    published = {row["team_id"] for row in envelope.signals}
    for index, team in enumerate(season_module.TEAMS):
        if index >= season_module.CHURN_FROM:
            assert team not in published, f"{team} changed and cannot be corrected"
        else:
            assert team in published

    reasons = {error["reason"] for error in envelope.errors}
    assert UNCORRECTABLE_LINEUP_CHANGE in reasons, reasons
    assert f"{CHURNED}:{RECORD_UNIT}" in envelope.coverage.missing


async def _capture_with(replacement):
    """Run one capture with `ratings.replacement_deltas` replaced.

    A plain monkeypatch fixture cannot be used from an `async` helper that
    other tests call, so the swap is done and undone by hand.
    """
    from offensive_line import ratings as ratings_module

    original = ratings_module.replacement_deltas
    ratings_module.replacement_deltas = replacement
    try:
        return await run_capture(Feeds(), lake=SpyLake())
    finally:
        ratings_module.replacement_deltas = original


async def test_a_dropped_team_leaves_the_rest_of_the_envelope_valid():
    """The envelope still conforms with a team missing — a drop is a coverage
    fact, not a malformed document."""
    import json
    from pathlib import Path

    contracts = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
    envelope_schema = json.loads((contracts / "envelope.v1.schema.json").read_text())

    envelopes = await _capture_with(lambda totals, strengths: {})
    jsonschema.validate(envelopes[STRENGTH].to_dict(), envelope_schema)


# --------------------------------------------------------------------------
# The arm that used to be missing: "could not tell" is not "did not change"
# --------------------------------------------------------------------------


def _blind_one_slot(team: str, week: int):
    """Remove one man from `team`'s lineup in `week`, as a live feed can.

    Not a synthetic condition. On the real 2025 season exactly one offensive
    lineman cannot be crosswalked from `snap_counts` to a `gsis_id` — Alec
    Anderson (BUF), blank `pfr_id` in `players.csv` — and he played 74 of 74
    offensive snaps in week 13 and 75 of 75 in week 18. Either start leaves
    Buffalo's five for that game unidentifiable, which is exactly this.
    """
    from offensive_line import ratings as ratings_module

    original = ratings_module.build_rows

    def blinded(totals, **kwargs):
        for (found, game), slots in list(totals.lineups.items()):
            if found == team and totals.game_week.get(game) == week:
                totals.lineups[(found, game)] = slots[:-1]
        return original(totals, **kwargs)

    return blinded


async def _capture_blinding(team: str, week: int):
    from offensive_line import capture as capture_module

    original = capture_module.build_rows
    capture_module.build_rows = _blind_one_slot(team, week)
    try:
        return await run_capture(Feeds(), lake=SpyLake())
    finally:
        capture_module.build_rows = original


async def test_an_unknown_prior_lineup_does_not_publish_as_verified_stable():
    """**The blind spot, closed.**

    Blinding one slot of the PRIOR game makes the naive `changed` false while
    the five genuinely moved. Before this arm existed the row published
    `pressure_rate_allowed_adj = 0.3537` where the corrected value is 0.2342 —
    **51% high** — with coverage, the row's own `lineup_hash`, its five
    starter rows and its schema validity all identical to a healthy pass. The
    row's own hash is populated; only the prior one is missing, and the prior
    hash is not on the row, so the schema's "read it together with
    lineup_hash" advice could not have saved a consumer.
    """
    envelopes = await _capture_blinding(CHURNED, season_module.WEEKS - 1)
    row = units(envelopes)[CHURNED]

    assert row["lineup_change_known"] is False
    assert row["pressure_rate_allowed_adj"] is None
    assert row["lineup_adjustment_pressure_rate"] is None
    assert row["null_field_reason"]["pressure_rate_allowed_adj"]
    # Everything the uncertainty does not touch still publishes — which is why
    # this is a null rather than a dropped team.
    assert row["pressure_rate_allowed_adj_observed"] is not None
    assert row["pressure_rate_allowed"] is not None
    assert row["adjusted_line_yards"] is not None


async def test_an_unknown_current_lineup_does_not_publish_a_corrected_rate():
    """The other half. A null `lineup_hash` is at least visible, but a
    consumer differencing `pressure_rate_allowed_adj` against
    `defensive-front` never looks at the hash."""
    envelopes = await _capture_blinding(CHURNED, season_module.WEEKS)
    row = units(envelopes)[CHURNED]
    assert row["lineup_hash"] is None
    assert row["lineup_change_known"] is False
    assert row["pressure_rate_allowed_adj"] is None


async def test_a_verified_stable_line_still_publishes_its_corrected_rate():
    """The negative arm, and it is what stops the fix from being "null it
    always". A team whose five were identified in both games has a known
    status, a zero correction, and a published rate."""
    row = units(await run_capture(Feeds(), lake=SpyLake()))[STABLE]
    assert row["lineup_change_known"] is True
    assert row["lineup_adjustment_pressure_rate"] == 0.0
    assert row["pressure_rate_allowed_adj"] is not None


async def test_a_team_with_one_game_has_a_known_status():
    """With no prior game nothing could have changed, so week 1 is not
    "unknown" — and treating it as unknown would null the adjusted rate for
    the entire league in week 1, every season."""
    envelopes = await run_capture(Feeds(), lake=SpyLake(), week=1)
    for team, row in units(envelopes).items():
        assert row["lineup_change_known"] is True, team
        assert row["lineup_changed"] is False, team


def test_the_guard_rejects_a_corrected_rate_it_could_not_have_computed():
    """Arm 0 in isolation, so the mutation that deletes it is killable
    independently of the fixture reaching the state."""
    row = {
        "record_type": RECORD_UNIT,
        "team_id": "AAA",
        "lineup_change_known": False,
        "lineup_changed": False,
        "continuity_games": 0,
        "lineup_adjustment_pressure_rate": None,
        "pressure_rate_allowed_adj_observed": 0.30,
        "pressure_rate_allowed_adj": 0.30,
    }
    with pytest.raises(StaleUnitRow, match="cannot tell"):
        assert_lineup_guard([row])

    row["pressure_rate_allowed_adj"] = None
    assert_lineup_guard([row])
