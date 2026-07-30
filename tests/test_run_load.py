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


def test_every_shape_has_a_script_file():
    """A shape whose .js file is missing must be a loud error, not a silently
    shorter --all run. Held back from Part A on purpose: there, it could only
    have passed against placeholder files."""
    for shape, cfg in rl.SHAPES.items():
        path = ROOT / "tests" / "load" / cfg["script"]
        assert path.exists(), f"{shape} names a missing script: {cfg['script']}"


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


# ── route_log_lines ───────────────────────────────────────────────────────────

def test_route_log_lines_shows_lines_before_the_marker():
    lines = ["line one\n", "line two\n", f"{rl.SUMMARY_MARKER}\n", '{"metrics": {}}\n']
    shown = []
    rl.route_log_lines(lines, shown.append)
    assert shown == ["line one\n", "line two\n"]


def test_route_log_lines_withholds_the_marker_and_everything_after():
    """The marker line itself, and the JSON that follows it, must not reach
    the console — that's what would turn a soak's summary export into a wall
    of JSON scrolling past the operator."""
    lines = ["line one\n", f"{rl.SUMMARY_MARKER}\n", '{"metrics": {}}\n', "trailer\n"]
    shown = []
    rl.route_log_lines(lines, shown.append)
    assert shown == ["line one\n"]


def test_route_log_lines_returns_the_full_captured_text():
    lines = ["line one\n", f"{rl.SUMMARY_MARKER}\n", '{"metrics": {}}\n']
    captured = rl.route_log_lines(lines, lambda _line: None)
    assert captured == "".join(lines)


def test_route_log_lines_tolerates_a_missing_marker():
    """A stream with no marker at all — k6 killed before it wrote a summary —
    still returns its text, the same case split_summary already tolerates."""
    lines = ["k6 crashed\n", "immediately\n"]
    shown = []
    captured = rl.route_log_lines(lines, shown.append)
    assert shown == lines
    assert captured == "".join(lines)


def test_route_log_lines_output_feeds_split_summary():
    """The captured text is meant to be handed straight to split_summary — the
    same marker logic doing the same job in both places."""
    lines = ["TOTAL RESULTS\n", f"{rl.SUMMARY_MARKER}\n", '{"metrics": {}}\n']
    captured = rl.route_log_lines(lines, lambda _line: None)
    text, payload = rl.split_summary(captured)
    assert "TOTAL RESULTS" in text
    assert payload.strip() == '{"metrics": {}}'


# ── needs_fallback ────────────────────────────────────────────────────────────

def test_needs_fallback_when_the_follow_stream_exited_nonzero():
    """A non-zero exit means kubectl itself gave up on the stream — a cut
    connection, a late attach — so the captured text cannot be trusted even
    if it happens to contain a marker."""
    assert rl.needs_fallback(returncode=1, saw_marker=True) is True


def test_needs_fallback_when_the_marker_was_never_seen():
    """A clean exit with no marker means the stream ended before k6 printed
    its summary, e.g. --pod-running-timeout expiring while k6 still runs."""
    assert rl.needs_fallback(returncode=0, saw_marker=False) is True


def test_no_fallback_for_a_clean_stream_with_a_marker():
    """The happy path: nothing to distrust, so the follow stream's own text
    stands — this is what keeps the fix from reintroducing an unconditional
    second fetch."""
    assert rl.needs_fallback(returncode=0, saw_marker=True) is False


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


# ── apply_restart_check ─────────────────────────────────────────────────────────

def test_only_breakpoint_is_exempt_from_the_no_restart_assertion():
    exempt = {s for s, c in rl.SHAPES.items() if not c["asserts_no_restart"]}
    assert exempt == {"breakpoint"}


def test_unchanged_restart_count_is_a_no_op_for_every_shape():
    for shape in rl.SHAPES:
        passed, verdict = rl.apply_restart_check(shape, True, "PASS", 2, 2)
        assert passed is True
        assert verdict == "PASS"


def test_breakpoint_still_passes_when_the_restart_count_moved():
    """breakpoint measures and asserts nothing about restarts -- provoking one
    is the point of the shape, not a defect it should fail on."""
    passed, verdict = rl.apply_restart_check(
        "breakpoint", True, "MEASURED -- threshold crossed as designed", 2, 3
    )
    assert passed is True


def test_breakpoint_still_reports_the_restart_as_an_observation():
    """The moved count is interesting data about where the service breaks down
    (Task 6 quotes it) and must still appear in the verdict text -- worded so it
    cannot be mistaken for an assertion that passed."""
    _, verdict = rl.apply_restart_check(
        "breakpoint", True, "MEASURED -- threshold crossed as designed", 2, 3
    )
    assert "restart count moved 2 -> 3" in verdict
    assert "(observed; not an assertion for this shape)" in verdict
    assert "RESTARTED" not in verdict


@pytest.mark.parametrize("shape", ["ramp", "soak", "spike"])
def test_a_moved_restart_count_fails_asserting_shapes(shape):
    """The guard against this change quietly disabling the check everywhere: a
    shape that was passing must go red the moment its restart count moves."""
    passed, verdict = rl.apply_restart_check(shape, True, "PASS", 2, 2 + 1)
    assert passed is False
    assert "RESTARTED" in verdict
    assert "restart count moved 2 -> 3" in verdict


# ── first_breached_threshold ──────────────────────────────────────────────────
#
# Both fixtures are trimmed from real k6 2.1.0 --summary-export output captured
# against the live cluster in Task 2: PASSING from a 5-minute ramp run
# (14999/14999 requests succeeded, http_req_failed's rate<0.01 held), CROSSED
# from a throwaway probe deliberately given an impossible threshold
# (p(95)<0.001, i.e. 1 microsecond) so it would abort with a real crossed
# result. The boolean's polarity is the whole point of this function, so it is
# pinned against real output in both directions rather than a hand-written
# guess: true means crossed, false means held.

CROSSED = {
    "metrics": {
        "http_req_duration": {
            "p(95)": 1.6454691999999997,
            "p(99)": 1.7809906399999997,
            "max": 1.814871,
            "avg": 1.0058854,
            "min": 0.688533,
            "med": 0.836026,
            "thresholds": {"p(95)<0.001": True},
        }
    }
}

PASSING = {
    "metrics": {
        "http_req_failed": {
            "passes": 0,
            "fails": 14999,
            "thresholds": {"rate<0.01": False},
            "value": 0,
        }
    }
}


def test_names_the_crossed_threshold():
    assert rl.first_breached_threshold(CROSSED) == "http_req_duration (p(95)<0.001)"


def test_returns_none_when_every_threshold_held():
    assert rl.first_breached_threshold(PASSING) is None


def test_tolerates_a_metric_with_no_thresholds():
    assert rl.first_breached_threshold({"metrics": {"http_reqs": {"value": 10}}}) is None


def test_tolerates_an_empty_summary():
    assert rl.first_breached_threshold({}) is None


# ── phase_action ──────────────────────────────────────────────────────────────
#
# The very first live run of this tool found `kubectl logs -f` giving up in
# ~30-100ms — not honouring --pod-running-timeout at all — while the pod was
# still ContainerCreating (phase "Pending"), and the fallback fetch racing the
# identical window and failing the same way. phase_action is the decision that
# drives waiting through that window instead of racing it; the polling loop
# that calls it needs a live cluster and is not tested here.

def test_pending_means_wait():
    assert rl.phase_action("Pending") == "wait"


def test_running_means_follow():
    assert rl.phase_action("Running") == "follow"


def test_succeeded_means_fetch():
    """A short shape can finish before we ever look — that's the normal path
    for a quick run, not a failure, so it gets the checked fetch directly
    rather than a follow attempt with nothing left to stream."""
    assert rl.phase_action("Succeeded") == "fetch"


def test_failed_means_fetch():
    assert rl.phase_action("Failed") == "fetch"


def test_unknown_phase_means_wait():
    """Node-unreachable ('Unknown') is not a state we know how to act on —
    keep polling rather than guessing."""
    assert rl.phase_action("Unknown") == "wait"


def test_empty_phase_means_wait():
    """No pod found yet (selector hasn't matched anything) reads the same as
    still waiting, not an error."""
    assert rl.phase_action("") == "wait"


def test_unrecognized_phase_means_wait():
    """An unexpected string is treated the same as not-yet-actionable rather
    than raising — the caller's timeout is what bounds this, not an exception
    from a phase string kubectl might add in some future version."""
    assert rl.phase_action("SomeFuturePhase") == "wait"


# ── parse_kube_duration ──────────────────────────────────────────────────────

def test_parses_minutes():
    assert rl.parse_kube_duration("10m") == 600


def test_parses_seconds():
    assert rl.parse_kube_duration("90s") == 90


def test_parses_hours():
    assert rl.parse_kube_duration("1h") == 3600


def test_rejects_an_unrecognized_duration():
    with pytest.raises(ValueError):
        rl.parse_kube_duration("10")


# ── newest_pod ────────────────────────────────────────────────────────────────
#
# Kubernetes has no "Terminating" phase: a pod with a deletionTimestamp set
# still reports .status.phase == "Running" until it actually disappears, so a
# rerun of the same shape can briefly have two pods behind the same
# `-l job-name=...` selector — the previous run's dying pod and the freshly
# created one — with no guarantee the API lists them in creation order.
# Picking `.items[0]` (the old approach, flagged in Task 2's review) could
# silently read the wrong one. These fixtures are shaped like real
# `kubectl get pod -o json` output, trimmed to the fields newest_pod reads.

def _pod(name: str, created: str, phase: str = "Running") -> dict:
    return {
        "metadata": {"name": name, "creationTimestamp": created},
        "status": {"phase": phase},
    }


def test_newest_pod_of_an_empty_list_is_none():
    assert rl.newest_pod([]) is None


def test_newest_pod_with_a_single_pod_returns_it():
    pod = _pod("k6-ramp-abc", "2026-07-29T23:10:00Z")
    assert rl.newest_pod([pod]) == pod


def test_newest_pod_picks_the_later_timestamp_when_it_is_listed_first():
    newer = _pod("k6-ramp-new", "2026-07-29T23:15:00Z")
    older = _pod("k6-ramp-old", "2026-07-29T23:10:00Z")
    assert rl.newest_pod([newer, older]) == newer


def test_newest_pod_picks_the_later_timestamp_when_it_is_listed_last():
    """This is the exact failure the review flagged: the API has no ordering
    guarantee, and `.items[0]` would have silently returned the dying pod
    from a previous run instead of the one just created."""
    older = _pod("k6-ramp-old", "2026-07-29T23:10:00Z", phase="Running")
    newer = _pod("k6-ramp-new", "2026-07-29T23:15:00Z", phase="Pending")
    assert rl.newest_pod([older, newer]) == newer
