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


import textwrap

# ── load_scenario ─────────────────────────────────────────────────────────────

def _write(tmp_path, text):
    path = tmp_path / "scenario.yaml"
    path.write_text(textwrap.dedent(text))
    return path


def test_load_scenario_splits_head_from_chaos_docs(tmp_path):
    path = _write(tmp_path, """
        apiVersion: foundry.chaos/v1
        kind: Scenario
        metadata:
          name: demo
        spec:
          hypothesis: something breaks
        ---
        apiVersion: chaos-mesh.org/v1alpha1
        kind: PodChaos
        metadata:
          name: demo-chaos
    """)
    head, chaos = rc.load_scenario(path)
    assert head["metadata"]["name"] == "demo"
    assert len(chaos) == 1
    assert chaos[0]["kind"] == "PodChaos"


def test_load_scenario_rejects_two_heads(tmp_path):
    path = _write(tmp_path, """
        apiVersion: foundry.chaos/v1
        kind: Scenario
        metadata:
          name: one
        ---
        apiVersion: foundry.chaos/v1
        kind: Scenario
        metadata:
          name: two
    """)
    with pytest.raises(ValueError, match="exactly one"):
        rc.load_scenario(path)


def test_load_scenario_rejects_missing_head(tmp_path):
    path = _write(tmp_path, """
        apiVersion: chaos-mesh.org/v1alpha1
        kind: PodChaos
        metadata:
          name: orphan
    """)
    with pytest.raises(ValueError, match="exactly one"):
        rc.load_scenario(path)


# ── validate_scenario ─────────────────────────────────────────────────────────

def _valid_head():
    return {
        "apiVersion": "foundry.chaos/v1",
        "kind": "Scenario",
        "metadata": {"name": "demo"},
        "spec": {
            "hypothesis": "the thing recovers",
            "steadyState": [
                {"description": "up", "query": 'up{job="weather"}', "expect": "== 1"}
            ],
            "criteria": [
                {"description": "recovered", "query": 'up{job="weather"}', "expect": "== 1"}
            ],
        },
    }


def _chaos_docs():
    return [{"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "PodChaos"}]


def test_valid_scenario_has_no_problems():
    assert rc.validate_scenario(_valid_head(), _chaos_docs()) == []


def test_missing_hypothesis_is_a_problem():
    head = _valid_head()
    del head["spec"]["hypothesis"]
    assert any("hypothesis" in p for p in rc.validate_scenario(head, _chaos_docs()))


def test_missing_steady_state_is_a_problem():
    head = _valid_head()
    head["spec"]["steadyState"] = []
    assert any("steadyState" in p for p in rc.validate_scenario(head, _chaos_docs()))


def test_missing_criteria_is_a_problem():
    head = _valid_head()
    head["spec"]["criteria"] = []
    assert any("criteria" in p for p in rc.validate_scenario(head, _chaos_docs()))


def test_scenario_that_injects_nothing_is_a_problem():
    assert any("inject" in p for p in rc.validate_scenario(_valid_head(), []))


def test_fault_commands_count_as_injection():
    """bad-deploy injects via kubectl, not via a Chaos Mesh resource."""
    head = _valid_head()
    head["spec"]["fault"] = [["kubectl", "set", "image", "deployment/weather", "weather=bad"]]
    assert rc.validate_scenario(head, []) == []


def test_trivially_true_criterion_is_rejected():
    head = _valid_head()
    head["spec"]["criteria"][0]["expect"] = ">= 0"
    problems = rc.validate_scenario(head, _chaos_docs())
    assert any("cannot fail" in p for p in problems)


def test_trivially_true_steady_state_is_allowed():
    """The guard targets criteria. A permissive steady state only makes the
    runner more willing to start, it does not manufacture false coverage."""
    head = _valid_head()
    head["spec"]["steadyState"][0]["expect"] = ">= 0"
    assert rc.validate_scenario(head, _chaos_docs()) == []


def test_check_missing_query_is_a_problem():
    head = _valid_head()
    del head["spec"]["criteria"][0]["query"]
    assert any("query" in p for p in rc.validate_scenario(head, _chaos_docs()))


def test_check_with_unparseable_expect_is_a_problem():
    head = _valid_head()
    head["spec"]["criteria"][0]["expect"] = "roughly one"
    assert any("expect" in p for p in rc.validate_scenario(head, _chaos_docs()))


# ── every real scenario file ──────────────────────────────────────────────────

SCENARIOS = sorted((ROOT / "chaos" / "scenarios").glob("*.yaml"))


def test_scenarios_directory_is_not_empty():
    assert SCENARIOS, "no scenario files found under chaos/scenarios/"


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario_file_is_valid(path):
    """Every committed scenario parses, declares a steady state, a hypothesis,
    and at least one criterion that some observation could falsify."""
    head, chaos_docs = rc.load_scenario(path)
    assert rc.validate_scenario(head, chaos_docs) == []


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario_name_matches_filename(path):
    head, _ = rc.load_scenario(path)
    assert head["metadata"]["name"] == path.stem


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario_declaring_traffic_has_a_manifest(path):
    head, _ = rc.load_scenario(path)
    traffic = head["spec"].get("traffic")
    if traffic:
        assert (ROOT / "chaos" / "traffic" / f"{traffic}.yaml").exists()


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_chaos_duration_matches_scenario_duration(path):
    """A scenario states `duration` twice: once in spec.duration (how long the
    runner holds the fault) and once in the Chaos Mesh resource's own
    spec.duration. If the Chaos Mesh value ever became shorter, the fault
    would end early while the criterion is still evaluated over a window
    partly containing it — a silent weakening rather than a failure.

    Skipped when a chaos document has no duration (pod-kill's PodChaos is
    instantaneous) or when there are no chaos documents at all (bad-deploy
    injects via spec.fault, not a Chaos Mesh resource).
    """
    head, chaos_docs = rc.load_scenario(path)
    scenario_duration = rc.parse_duration(head["spec"]["duration"])
    for doc in chaos_docs:
        chaos_duration = doc.get("spec", {}).get("duration")
        if chaos_duration is None:
            continue
        assert rc.parse_duration(chaos_duration) == scenario_duration, (
            f"{doc.get('kind')} duration {chaos_duration!r} does not match "
            f"spec.duration {head['spec']['duration']!r}"
        )
