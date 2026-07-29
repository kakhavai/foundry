"""Tests for scripts/run-load.py.

Lives in the repo-root platform suite for the same reason the chaos tests do: a
load harness is a platform concern no per-service suite can see. Everything here
is a pure function — no cluster, no k6, no network.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Import run-load.py (hyphenated) as a module — same pattern as
# tests/test_chaos_scenarios.py and tests/test_argocd_deploy.py.
spec = importlib.util.spec_from_file_location(
    "run_load", ROOT / "scripts" / "run-load.py"
)
rl = importlib.util.module_from_spec(spec)
sys.modules["run_load"] = rl
spec.loader.exec_module(rl)


# ── the shape registry ────────────────────────────────────────────────────────
#
# test_every_shape_has_a_script_file lives in Part B, not here: it can only be
# meaningful once the .js files exist, and adding it now would mean committing
# placeholder scripts for a test to pass against.

def test_the_four_specified_shapes_are_present():
    assert set(rl.SHAPES) == {"ramp", "soak", "spike", "breakpoint"}


def test_only_breakpoint_expects_a_threshold_breach():
    breaching = {s for s, c in rl.SHAPES.items() if c["expect_threshold_breach"]}
    assert breaching == {"breakpoint"}


# ── render_job ────────────────────────────────────────────────────────────────

def test_render_job_runs_the_shape_s_own_script():
    job = rl.render_job("ramp", soak_minutes=5)
    command = job["spec"]["template"]["spec"]["containers"][0]["args"][-1]
    assert "/scripts/ramp.js" in command


def test_render_job_pins_the_verified_image():
    job = rl.render_job("ramp", soak_minutes=5)
    assert job["spec"]["template"]["spec"]["containers"][0]["image"] == rl.K6_IMAGE
    assert rl.K6_IMAGE == "grafana/k6:2.1.0"


def test_render_job_never_retries():
    """backoffLimit 0 and restartPolicy Never: a crossed threshold is a result,
    and re-running the shape would both hide it and double the load."""
    job = rl.render_job("soak", soak_minutes=5)
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_render_job_passes_soak_minutes_as_env():
    job = rl.render_job("soak", soak_minutes=30)
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "SOAK_MINUTES", "value": "30"} in env


def test_render_job_passes_the_target_as_env():
    job = rl.render_job("ramp", soak_minutes=5)
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "TARGET", "value": rl.TARGET} in env
    assert "player-projections:8001" in rl.TARGET


def test_render_job_mounts_the_script_configmap():
    job = rl.render_job("ramp", soak_minutes=5)
    pod = job["spec"]["template"]["spec"]
    assert pod["volumes"][0]["configMap"]["name"] == rl.CONFIGMAP
    mount = pod["containers"][0]["volumeMounts"][0]
    assert mount["mountPath"] == "/scripts"


def test_render_job_emits_the_summary_marker_then_preserves_the_exit_code():
    """The summary JSON leaves the pod through its logs — there is no way to
    kubectl cp a file out of a terminated container — so the container prints a
    marker, cats the JSON, and re-exits with k6's own code."""
    job = rl.render_job("breakpoint", soak_minutes=5)
    command = job["spec"]["template"]["spec"]["containers"][0]["args"][-1]
    assert rl.SUMMARY_MARKER in command
    assert "exit $code" in command


def test_render_job_names_are_distinct_per_shape():
    names = {rl.job_name(shape) for shape in rl.SHAPES}
    assert len(names) == len(rl.SHAPES)


# ── split_summary ─────────────────────────────────────────────────────────────

def test_split_summary_separates_text_from_json():
    log = f"TOTAL RESULTS\n  http_reqs: 400\n{rl.SUMMARY_MARKER}\n{{\"metrics\": {{}}}}\n"
    text, payload = rl.split_summary(log)
    assert "http_reqs: 400" in text
    assert rl.SUMMARY_MARKER not in text
    assert payload.strip() == '{"metrics": {}}'


def test_split_summary_tolerates_a_missing_marker():
    """k6 killed before it wrote a summary still has readable text output."""
    text, payload = rl.split_summary("k6 crashed immediately\n")
    assert "crashed" in text
    assert payload == ""


# ── interpret_exit ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("shape", ["ramp", "soak", "spike"])
def test_zero_passes_for_asserting_shapes(shape):
    passed, verdict = rl.interpret_exit(shape, 0)
    assert passed is True
    assert verdict == "PASS"


@pytest.mark.parametrize("shape", ["ramp", "soak", "spike"])
def test_crossed_threshold_fails_asserting_shapes(shape):
    passed, verdict = rl.interpret_exit(shape, 99)
    assert passed is False
    assert "threshold" in verdict.lower()


def test_crossed_threshold_is_the_expected_result_for_breakpoint():
    passed, verdict = rl.interpret_exit("breakpoint", 99)
    assert passed is True
    assert "MEASURED" in verdict


def test_breakpoint_that_never_breaks_is_not_a_pass():
    """Exit 0 means the top rung never crossed 1% errors, so the run found no
    breakpoint. Reporting that as PASS would file a non-measurement as coverage."""
    passed, verdict = rl.interpret_exit("breakpoint", 0)
    assert passed is False
    assert "NO-BREAKPOINT" in verdict


@pytest.mark.parametrize("code", [1, 104, 107, 255])
def test_other_nonzero_codes_fail_even_for_breakpoint(code):
    """The one shape that cannot fail must still be able to report a broken
    script or an unreachable target. 'Any non-zero is expected here' would have
    swallowed exactly that."""
    passed, verdict = rl.interpret_exit("breakpoint", code)
    assert passed is False
    assert "ERROR" in verdict
