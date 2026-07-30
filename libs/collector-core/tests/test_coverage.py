import pytest

from collector_core.coverage import CoverageAccumulator


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
