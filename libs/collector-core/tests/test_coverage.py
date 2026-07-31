import pytest

from collector_core.coverage import (
    BELOW_EXPECTED_FLOOR,
    ERRORS_TRUNCATED,
    MAX_ERRORS,
    CoverageAccumulator,
    cap_errors,
)


def test_all_present_gives_full_coverage():
    acc = CoverageAccumulator(["a", "b", "c"])
    for key in ("a", "b", "c"):
        acc.record(key)

    result = acc.result()
    assert result.expected == 3
    assert result.present == 3
    assert result.missing == []
    assert result.ratio == 1.0


def test_missing_is_derived_not_declared():
    """Missing is expected-minus-present, so it cannot drift out of sync."""
    acc = CoverageAccumulator(["a", "b", "c"])
    acc.record("a")

    result = acc.result()
    assert result.present == 1
    assert result.missing == ["b", "c"]


def test_missing_is_sorted_for_stable_diffs():
    acc = CoverageAccumulator(["c", "a", "b"])
    result = acc.result()
    assert result.missing == ["a", "b", "c"]


def test_failure_records_reason_and_leaves_key_missing():
    acc = CoverageAccumulator(["a", "b"])
    acc.record("a")
    acc.fail("b", "no_venue_coordinates")

    result = acc.result()
    assert result.present == 1
    assert result.missing == ["b"]
    assert acc.errors == [{"reason": "no_venue_coordinates", "detail": "b"}]


def test_total_failure_still_produces_a_result():
    """A poll that fails entirely still writes — present 0, everything missing."""
    acc = CoverageAccumulator(["a", "b"])
    acc.fail("a", "timeout")
    acc.fail("b", "timeout")

    result = acc.result()
    assert result.expected == 2
    assert result.present == 0
    assert result.missing == ["a", "b"]
    assert result.ratio == 0.0


def test_empty_expectation_is_complete_not_broken():
    """A bye week expects nothing. 0/0 must read as 1.0, not as a failure."""
    result = CoverageAccumulator([]).result()
    assert result.expected == 0
    assert result.ratio == 1.0


def test_recording_an_unexpected_key_raises():
    """Recording a key nobody expected means the expectation set is wrong."""
    acc = CoverageAccumulator(["a"])
    with pytest.raises(KeyError, match="not in the expected set"):
        acc.record("z")


def test_recording_twice_is_idempotent():
    acc = CoverageAccumulator(["a"])
    acc.record("a")
    acc.record("a")
    assert acc.result().present == 1


# --- the floor: `expected` must never derive from what succeeded -------------


def test_a_truncated_upstream_does_not_report_perfect_coverage():
    """THE ratio-1.0 bug, stated as a test.

    A collector that builds its expectation from the document it just fetched
    reports expected=100, present=100, ratio 1.0 for a document truncated to
    100 of 2,900 records — perfectly healthy, while 96% of the league silently
    vanished. The floor is what makes that report honest.
    """
    acc = CoverageAccumulator(floor=2900)
    for index in range(100):
        key = f"player-{index}"
        acc.expect(key)
        acc.record(key)

    result = acc.result()
    assert result.expected == 2900
    assert result.present == 100
    assert result.ratio == pytest.approx(100 / 2900)
    assert result.ratio < 0.05


def test_without_the_floor_the_same_truncation_reads_as_perfect():
    """The control for the test above. If this ever stops reading 1.0, the
    floor is no longer the thing doing the work and the test above has
    stopped proving what it claims."""
    acc = CoverageAccumulator()
    for index in range(100):
        key = f"player-{index}"
        acc.expect(key)
        acc.record(key)

    assert acc.result().ratio == 1.0


def test_a_total_outage_against_a_floor_reports_zero_not_one():
    acc = CoverageAccumulator(floor=2900)
    result = acc.result()
    assert result.expected == 2900
    assert result.present == 0
    assert result.ratio == 0.0


def test_the_floor_never_lowers_a_genuinely_larger_universe():
    """A roster expansion past the floor must still report honestly."""
    acc = CoverageAccumulator(floor=3)
    for key in ("a", "b", "c", "d", "e"):
        acc.expect(key)
        acc.record(key)

    result = acc.result()
    assert result.expected == 5
    assert result.ratio == 1.0


def test_falling_short_of_the_floor_is_stated_as_an_error_not_left_to_arithmetic():
    acc = CoverageAccumulator(floor=2900)
    acc.expect("a")
    acc.record("a")

    assert acc.errors[0]["reason"] == BELOW_EXPECTED_FLOOR
    assert "2900" in acc.errors[0]["detail"]
    assert acc.observed == 1


def test_no_shortfall_error_when_the_floor_is_met():
    acc = CoverageAccumulator(["a", "b"], floor=2)
    assert acc.errors == []


def test_a_negative_floor_is_rejected():
    with pytest.raises(ValueError, match="must not be negative"):
        CoverageAccumulator(floor=-1)


# --- `expected` cannot grow because something succeeded ----------------------


def test_record_still_refuses_a_key_that_was_never_expected():
    """`expect` exists so a collector can declare keys as an upstream
    document is read. It must not soften `record`: if `record` declared keys
    itself, `expected` would grow on success and the whole coverage block
    would be self-certifying."""
    acc = CoverageAccumulator(floor=10)
    with pytest.raises(KeyError, match="not in the expected set"):
        acc.record("never-declared")


def test_failing_a_key_declares_it_expected():
    """A failure is evidence the key was owed — the opposite of deriving the
    expectation from a success."""
    acc = CoverageAccumulator()
    acc.fail("a", "timeout")

    result = acc.result()
    assert result.expected == 1
    assert result.present == 0
    assert result.missing == ["a"]
    assert result.ratio == 0.0


def test_expect_is_idempotent():
    acc = CoverageAccumulator()
    acc.expect("a")
    acc.expect("a")
    assert acc.result().expected == 1


# --- pass-level errors -------------------------------------------------------


def test_add_error_records_a_problem_not_tied_to_a_missing_key():
    acc = CoverageAccumulator(["a"])
    acc.record("a")
    acc.add_error("merge_conflict", "sleeper:99 claimed by fdy-x, fdy-y")

    assert acc.errors == [
        {"reason": "merge_conflict", "detail": "sleeper:99 claimed by fdy-x, fdy-y"}
    ]
    # A pass-level error must not invent a missing key.
    assert acc.result().present == 1
    assert acc.result().missing == []


# --- the errors cap ----------------------------------------------------------


def test_errors_are_capped_and_the_truncation_is_visible():
    """A total schema break produced 2,900 near-identical entries in an 8A
    prototype. The cap bounds it; the marker is what keeps the drop from
    being silent."""
    acc = CoverageAccumulator()
    for index in range(2900):
        acc.fail(f"player-{index}", "schema")

    errors = acc.errors
    assert len(errors) == MAX_ERRORS + 1
    marker = errors[-1]
    assert marker["reason"] == ERRORS_TRUNCATED
    assert marker["omitted"] == 2900 - MAX_ERRORS
    assert marker["total"] == 2900
    # Every retained entry is a real error, not padding.
    assert all(e["reason"] == "schema" for e in errors[:MAX_ERRORS])
    assert len(errors[:MAX_ERRORS]) == MAX_ERRORS


def test_an_uncapped_list_carries_no_marker():
    acc = CoverageAccumulator()
    for index in range(3):
        acc.fail(f"k{index}", "timeout")

    errors = acc.errors
    assert len(errors) == 3
    assert [e["reason"] for e in errors] == ["timeout"] * 3


def test_exactly_the_cap_is_not_truncated():
    """Off-by-one guard: the marker must appear only once entries are
    actually dropped."""
    acc = CoverageAccumulator()
    for index in range(MAX_ERRORS):
        acc.fail(f"k{index}", "timeout")

    errors = acc.errors
    assert len(errors) == MAX_ERRORS
    assert ERRORS_TRUNCATED not in {e["reason"] for e in errors}


def test_the_floor_shortfall_survives_truncation():
    """It is first, not last, precisely so the cap cannot drop the single
    most important entry in the list."""
    acc = CoverageAccumulator(floor=5000)
    for index in range(2900):
        acc.fail(f"player-{index}", "schema")

    errors = acc.errors
    assert errors[0]["reason"] == BELOW_EXPECTED_FLOOR
    assert errors[-1]["reason"] == ERRORS_TRUNCATED


def test_cap_errors_is_idempotent():
    """Applying the cap twice must not truncate an already-truncated list and
    report a wrong omitted count."""
    once = cap_errors([{"reason": "x", "detail": str(i)} for i in range(500)])
    twice = cap_errors(once)

    assert twice == once
    assert twice[-1]["omitted"] == 500 - MAX_ERRORS
    assert twice[-1]["total"] == 500


def test_cap_errors_rejects_a_nonsense_cap():
    with pytest.raises(ValueError, match="at least 1"):
        cap_errors([{"reason": "x"}], max_errors=0)


def test_the_cap_is_configurable_per_accumulator():
    acc = CoverageAccumulator(max_errors=2)
    for index in range(10):
        acc.fail(f"k{index}", "timeout")

    errors = acc.errors
    assert len(errors) == 3
    assert errors[-1]["omitted"] == 8
