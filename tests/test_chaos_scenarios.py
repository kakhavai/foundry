"""Tests for scripts/run-chaos.py and the scenario files it runs.

Lives in the repo-root platform suite because a chaos scenario is exactly the
kind of thing no per-service test can see. The structural assertions further
down are the mechanical defence against the failure mode the phase doc warns
about: a scenario that reports green because it cannot go red.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Import run-chaos.py (hyphenated) as a module — same pattern as
# tests/test_argocd_deploy.py.
spec = importlib.util.spec_from_file_location(
    "run_chaos", ROOT / "scripts" / "run-chaos.py"
)
rc = importlib.util.module_from_spec(spec)
sys.modules["run_chaos"] = rc
spec.loader.exec_module(rc)


# ── parse_expect ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,op,value",
    [
        ("== 1", "==", 1.0),
        ("!= 0", "!=", 0.0),
        (">= 2", ">=", 2.0),
        ("<= 1.05", "<=", 1.05),
        ("> 0", ">", 0.0),
        ("< 0.5", "<", 0.5),
        ("==1", "==", 1.0),
        ("  >  0.9  ", ">", 0.9),
    ],
)
def test_parse_expect_reads_operator_and_value(text, op, value):
    assert rc.parse_expect(text) == (op, value)


def test_parse_expect_prefers_two_char_operators():
    """'>= 2' must not parse as '>' with value '= 2'."""
    assert rc.parse_expect(">= 2")[0] == ">="
    assert rc.parse_expect("<= 2")[0] == "<="


@pytest.mark.parametrize("text", ["1", "~ 1", "== abc", "", ">="])
def test_parse_expect_rejects_junk(text):
    with pytest.raises(ValueError):
        rc.parse_expect(text)


# ── is_trivially_true ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("op,value", [(">=", 0.0), (">=", -1.0), (">", -1.0), ("!=", -1.0)])
def test_trivially_true_criteria_are_flagged(op, value):
    """Every metric these scenarios query is non-negative, so a comparison
    satisfied by every non-negative number can never go red."""
    assert rc.is_trivially_true(op, value) is True


@pytest.mark.parametrize(
    "op,value", [("==", 1.0), (">", 0.0), (">=", 2.0), ("<=", 1.05), ("<", 0.5), ("!=", 0.0)]
)
def test_falsifiable_criteria_are_not_flagged(op, value):
    assert rc.is_trivially_true(op, value) is False


# ── parse_duration ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,seconds",
    [("90s", 90.0), ("2m", 120.0), ("500ms", 0.5), ("1h", 3600.0), ("30", 30.0)],
)
def test_parse_duration(text, seconds):
    assert rc.parse_duration(text) == seconds


def test_parse_duration_reads_ms_before_s():
    """'500ms' also ends in 's'; the longer suffix must win."""
    assert rc.parse_duration("500ms") == 0.5


# ── extract_scalar ────────────────────────────────────────────────────────────

def _vector(value):
    return {"status": "success", "data": {"resultType": "vector",
            "result": [{"metric": {}, "value": [1785335007.657, str(value)]}]}}


def _empty():
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def test_extract_scalar_reads_the_value():
    assert rc.extract_scalar(_vector("0.217"), allow_empty=False) == 0.217


def test_empty_result_is_an_error_by_default():
    """Prometheus answers identically for a series that has never existed and
    for a typo'd metric name. Defaulting empty to 0 would let a typo satisfy
    every criterion silently."""
    assert rc.extract_scalar(_empty(), allow_empty=False) is None


def test_empty_result_is_zero_when_explicitly_allowed():
    assert rc.extract_scalar(_empty(), allow_empty=True) == 0.0


def test_multi_series_result_is_rejected():
    payload = {"status": "success", "data": {"result": [
        {"metric": {"pod": "a"}, "value": [1, "1"]},
        {"metric": {"pod": "b"}, "value": [1, "1"]},
    ]}}
    with pytest.raises(ValueError, match="reduce to one"):
        rc.extract_scalar(payload, allow_empty=False)


def test_failed_query_raises():
    with pytest.raises(ValueError, match="Prometheus query failed"):
        rc.extract_scalar({"status": "error", "error": "parse error"}, allow_empty=False)
