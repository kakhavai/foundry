#!/usr/bin/env python3
"""
Run a k6 load shape against player-projections and report the result.

k6 runs in-cluster as a Job rather than on the host. player-projections is
deliberately not routed through the collector gateway, so there is no NodePort
path to it; an in-cluster Job also needs no k6 binary on the CI runner or on a
developer's machine, and keeps `kubectl port-forward` out of the measurement
path.

Usage:
  uv run --with pyyaml==6.0.3 python scripts/run-load.py --list
  uv run --with pyyaml==6.0.3 python scripts/run-load.py ramp
  uv run --with pyyaml==6.0.3 python scripts/run-load.py --all --soak-minutes 30

Exits non-zero if any shape failed.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "tests" / "load"
RESULTS_DIR = ROOT / "load-results"

# Verified against the image, not a changelog: the tag exists, --summary-export
# is still accepted (it was deprecated during k6's 0.5x line), --summary-mode
# full gives per-scenario breakdowns, and the image contains sh. 2.0.0 was
# rejected in favour of 2.1.0 because its image self-reports v2.0.0+dirty.
K6_IMAGE = "grafana/k6:2.1.0"

TARGET = "http://player-projections:8001"
CONFIGMAP = "k6-scripts"

# The summary JSON leaves the pod through its logs. There is no way to
# `kubectl cp` a file out of a container that has already terminated, and a
# shared volume would need a second pod to read it.
SUMMARY_MARKER = "---K6-SUMMARY-JSON---"

# k6 exits 99, specifically, when a threshold with abortOnFail is crossed.
# Verified by probe.
K6_EXIT_THRESHOLD_CROSSED = 99

# Declared rather than discovered from the directory: each shape carries what the
# runner must know about it, and a shape whose .js file is missing should be a
# loud error rather than a silently shorter --all run.
#
# `timeout` is the kubectl wait budget, sized well above each shape's own
# duration so a slow cluster does not read as a hung Job.
#
# `asserts_no_restart` is False only for breakpoint: its entire purpose is to
# drive the service past capacity until the error-rate threshold crosses, and a
# kubelet restart along the way is the expected result of that, not a defect.
# The other three shapes are meant to hold steady, so a restart during any of
# them is a real finding and keeps failing the run.
SHAPES: dict[str, dict] = {
    "ramp": {
        "script": "ramp.js",
        "expect_threshold_breach": False,
        "timeout": "10m",
        "asserts_no_restart": True,
    },
    "soak": {
        "script": "soak.js",
        "expect_threshold_breach": False,
        "timeout": "50m",
        "asserts_no_restart": True,
    },
    "spike": {
        "script": "spike.js",
        "expect_threshold_breach": False,
        "timeout": "10m",
        "asserts_no_restart": True,
    },
    "breakpoint": {
        "script": "breakpoint.js",
        "expect_threshold_breach": True,
        "timeout": "20m",
        "asserts_no_restart": False,
    },
}


def job_name(shape: str) -> str:
    return f"k6-{shape}"


def render_job(shape: str, soak_minutes: int) -> dict:
    """Build the Job manifest that runs one shape.

    The container runs under `sh` rather than k6's own entrypoint so it can print
    the marker and re-exit with k6's code — `sh -c` swallows the exit status
    otherwise, and the exit code is the entire verdict.
    """
    cfg = SHAPES[shape]
    # No --quiet: verified by probe that k6 still prints its periodic
    # non-interactive progress line (`running (Ns), NN/NN VUs, ...`) roughly
    # once a second when stdout is not a TTY, which is exactly the case under
    # `kubectl logs -f`. With --quiet, k6 prints nothing at all until the run
    # ends, which made route_log_lines's live-streaming pointless in practice
    # — an operator watching a 5-minute ramp saw nothing until t=300s.
    command = (
        f"k6 run --summary-mode full "
        f"--summary-trend-stats 'avg,min,med,p(95),p(99),max' "
        f"--summary-export /tmp/summary.json /scripts/{cfg['script']}; "
        f"code=$?; "
        f"echo '{SUMMARY_MARKER}'; "
        f"cat /tmp/summary.json 2>/dev/null || true; "
        f"exit $code"
    )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(shape),
            "labels": {"app.kubernetes.io/name": "k6-load"},
        },
        "spec": {
            # A crossed threshold is a result. Retrying it would hide the result
            # and double the load.
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": "k6-load"}},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "k6",
                            "image": K6_IMAGE,
                            "command": ["sh", "-c"],
                            "args": [command],
                            "env": [
                                {"name": "TARGET", "value": TARGET},
                                {"name": "SOAK_MINUTES", "value": str(soak_minutes)},
                            ],
                            "volumeMounts": [
                                {"name": "scripts", "mountPath": "/scripts"}
                            ],
                            # The load generator shares a single-node cluster
                            # with the service under test. Given room so k6 is
                            # not the bottleneck being measured; recorded as a
                            # caveat in docs/scale-baselines.md regardless.
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "256Mi"},
                                "limits": {"cpu": "2", "memory": "1Gi"},
                            },
                        }
                    ],
                    "volumes": [{"name": "scripts", "configMap": {"name": CONFIGMAP}}],
                },
            },
        },
    }


def split_summary(log_text: str) -> tuple[str, str]:
    """Split pod logs into k6's text summary and its exported JSON."""
    if SUMMARY_MARKER not in log_text:
        return log_text, ""
    text, _, payload = log_text.partition(SUMMARY_MARKER)
    return text, payload


def route_log_lines(lines: Iterable[str], show: Callable[[str], None]) -> str:
    """Route each log line as it streams in, and return everything captured.

    A callback rather than a buffer-then-return tuple: `lines` can be a live
    iterator over a subprocess's stdout, so calling `show` from inside this
    loop is what makes "streams k6's own output as it runs" literally true —
    the caller's print happens as each line is consumed, not after the whole
    Job has finished. Lines before the summary marker are shown immediately;
    the marker line and everything after (the exported JSON) are withheld from
    `show` — that JSON belongs in load-results/<shape>.json, not scrolling
    past the operator for up to 50 minutes of a soak.

    The full captured text is returned so the caller can pass it straight to
    split_summary — the same marker logic doing the same job in both places,
    not reimplemented here.
    """
    captured: list[str] = []
    seen_marker = False
    for line in lines:
        captured.append(line)
        if not seen_marker:
            if SUMMARY_MARKER in line:
                seen_marker = True
            else:
                show(line)
    return "".join(captured)


def needs_fallback(returncode: int, saw_marker: bool) -> bool:
    """True when the live-follow stream cannot be trusted as the complete log.

    A non-zero exit from `kubectl logs -f` means the stream itself failed —
    cut short by a network blip, a late attach, or anything else that made
    kubectl give up — and a zero exit with no marker seen means the stream
    ended before k6 printed its summary (e.g. --pod-running-timeout expired
    while k6 was still running). Either way the follow stream is not proof of
    a complete log, and the checked non-follow fetch — which only runs after
    the pod has fully terminated — is the text still worth trusting.
    """
    return returncode != 0 or not saw_marker


def phase_action(phase: str) -> str:
    """Decide what to do about a pod given its current `.status.phase`.

    Kept separate from the polling loop that calls it so the *decision* is
    unit-testable without a cluster — only the polling itself (which needs
    wall-clock time and a live API server) is not.

    "wait": not yet in a state that tells us anything useful — `Pending`
        (still `ContainerCreating`, the case that broke the very first live
        run of this tool: `kubectl logs -f` gives up in ~30-100ms rather than
        honouring `--pod-running-timeout` while the pod is still Pending), the
        empty string (no pod found yet), `Unknown` (node unreachable), or
        anything else unrecognised. The caller keeps polling.
    "follow": `Running` — attach the live log stream as normal.
    "fetch": `Succeeded` or `Failed` — the container already terminated. A
        short shape can finish before we ever look; a live follow would
        attach to nothing, so go straight to the checked non-follow fetch
        instead of trying to follow a stream that will never produce data.
    """
    if phase in ("Succeeded", "Failed"):
        return "fetch"
    if phase == "Running":
        return "follow"
    return "wait"


def parse_kube_duration(spec: str) -> int:
    """Parse a Kubernetes-style duration ('10m', '90s', '1h') into seconds.

    Only the single-unit form the SHAPES table actually uses; not a general
    Go-duration parser.
    """
    match = re.fullmatch(r"(\d+)([smh])", spec)
    if not match:
        raise ValueError(f"unrecognized duration: {spec!r}")
    value, unit = match.groups()
    return int(value) * {"s": 1, "m": 60, "h": 3600}[unit]


def newest_pod(pods: list[dict]) -> dict | None:
    """Pick the most recently created pod from a list of pod objects.

    Kubernetes has no "Terminating" phase: a pod with a `deletionTimestamp`
    set still reports `.status.phase == "Running"` until it actually
    disappears. `-l job-name=<name>` matches on the label alone, so a rerun
    of the same shape can briefly have two pods behind that selector — the
    previous run's dying pod and the freshly created one — and the API gives
    no ordering guarantee for which comes back first. Picking `.items[0]`
    (the old approach) could silently read the dying pod instead of the one
    that was just created.

    Sorting by `.metadata.creationTimestamp` (RFC3339, lexicographically
    sortable) and taking the last removes that ambiguity regardless of
    list order — this is the part of the fix that makes selection correct;
    `--cascade=foreground` on the Job delete (in run_shape) is the other
    part, removing the race at its source instead of only reading around it.
    """
    if not pods:
        return None
    return sorted(pods, key=lambda pod: pod["metadata"]["creationTimestamp"])[-1]


def interpret_exit(shape: str, code: int) -> tuple[bool, str]:
    """Turn a k6 exit code into (passed, verdict).

    Only `breakpoint` treats 99 as success, and it treats *only* 99 as success.
    "Any non-zero is expected here" would swallow a script error or an
    unreachable target, turning the one shape that cannot fail into the one shape
    that cannot report a problem either.
    """
    expects_breach = SHAPES[shape]["expect_threshold_breach"]
    if code == 0:
        if expects_breach:
            return False, (
                "NO-BREAKPOINT -- the top rung never crossed 1% errors, so this "
                "run measured nothing; raise the rungs in breakpoint.js"
            )
        return True, "PASS"
    if code == K6_EXIT_THRESHOLD_CROSSED:
        if expects_breach:
            return True, "MEASURED -- threshold crossed as designed"
        return False, "FAIL -- a threshold was crossed"
    return False, f"ERROR -- k6 exited {code}"


def apply_restart_check(
    shape: str, passed: bool, verdict: str, before: int, after: int
) -> tuple[bool, str]:
    """Fold a restart-count observation into a shape's (passed, verdict).

    A no-op when the count did not move. When it did, every shape gets the
    fact appended to the verdict text -- it is useful data regardless of
    outcome -- but only a shape with asserts_no_restart=True has it flip
    `passed`. breakpoint is the one exception: it exists to drive the service
    past capacity until it breaks, so a restart along the way is the expected
    result, not a defect, and the line is worded so it cannot be mistaken for
    an assertion that passed.
    """
    if before == after:
        return passed, verdict
    moved = f"player-projections restart count moved {before} -> {after}"
    if SHAPES[shape]["asserts_no_restart"]:
        return False, f"{verdict} | RESTARTED -- {moved}"
    return passed, f"{verdict} | {moved} (observed; not an assertion for this shape)"


def first_breached_threshold(summary: dict) -> str | None:
    """Name the first metric whose threshold was crossed.

    Polarity confirmed empirically against both a passing and a crossed run —
    see docs/scale-baselines.md. k6's exported schema is
    metrics.<name>.thresholds = {"<expression>": <bool>}, and true means the
    threshold was crossed: a real ramp run's held http_req_failed threshold
    exported `{"rate<0.01": false}`, and a deliberately impossible
    http_req_duration threshold that actually aborted the run exported
    `{"p(95)<0.001": true}`.

    A summary that isn't a dict (valid JSON but not an object -- `null`, a
    bare number or string, a list) has no thresholds to name; returning None
    is the honest answer rather than raising AttributeError on `.get`.
    """
    if not isinstance(summary, dict):
        return None
    for name, metric in sorted((summary.get("metrics") or {}).items()):
        for expression, crossed in (metric.get("thresholds") or {}).items():
            if crossed:
                return f"{name} ({expression})"
    return None


# ── cluster interaction ───────────────────────────────────────────────────────


def kubectl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ kubectl {' '.join(args)}")
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"kubectl {args[0]} failed: {message}")
    return result


def configmap_up() -> None:
    """Rebuild the script ConfigMap from tests/load/*.js.

    Rendered then applied rather than `kubectl create` alone, which fails on
    every run after the first — the same pattern as
    scripts/deploy-local.py's ensure_collector_secret.
    """
    args = ["create", "configmap", CONFIGMAP]
    for path in sorted(SCRIPT_DIR.glob("*.js")):
        args += [f"--from-file={path.name}={path}"]
    args += ["--dry-run=client", "-o", "yaml"]
    rendered = kubectl(args)
    applied = subprocess.run(
        ["kubectl", "apply", "-f", "-"], input=rendered.stdout, text=True
    )
    if applied.returncode != 0:
        raise RuntimeError("applying the script ConfigMap failed")


def apply_job(job: dict) -> None:
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml.safe_dump(job),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"applying the Job failed: {result.stderr.strip()}")


def get_shape_pods(shape: str) -> list[dict]:
    """Fetch every pod matching the shape's Job, as parsed pod objects.

    Full `-o json` rather than a `.items[0]` jsonpath: the jsonpath form
    picks whichever pod the API happens to list first, which is not
    guaranteed to be the newest — see newest_pod(). `check=False` because
    "no pods yet" is a normal, expected state right after a Job is applied,
    not an error to raise on.
    """
    result = kubectl(
        ["get", "pod", "-l", f"job-name={job_name(shape)}", "-o", "json"],
        check=False,
    )
    if not result.stdout.strip():
        return []
    return json.loads(result.stdout).get("items", [])


def pod_exit_code(shape: str) -> int:
    """Read the newest matching pod's terminated container's exit code —
    the entire verdict."""
    pod = newest_pod(get_shape_pods(shape))
    if pod is None:
        raise RuntimeError(f"{job_name(shape)}: no pod found to read")
    try:
        exit_code = pod["status"]["containerStatuses"][0]["state"]["terminated"][
            "exitCode"
        ]
    except (KeyError, IndexError):
        raise RuntimeError(f"{job_name(shape)}: no terminated container to read")
    return int(exit_code)


def pod_phase(shape: str) -> str:
    """Read the newest matching pod's current `.status.phase` ('' if no pod
    exists yet at all)."""
    pod = newest_pod(get_shape_pods(shape))
    if pod is None:
        return ""
    return pod.get("status", {}).get("phase", "")


def wait_for_pod_action(
    shape: str, timeout_s: float, poll_interval: float = 2.0
) -> str:
    """Poll the shape's pod until phase_action has a real decision, up to timeout_s.

    Deliberately not `kubectl wait --for=condition=ready` — this repo has
    been bitten repeatedly by that selector also matching a still-`Terminating`
    pod from a previous run (Kubernetes has no "Terminating" phase; a pod with
    a `deletionTimestamp` set still reports `.status.phase == "Running"` until
    it actually disappears), which never reaches Ready and hangs the wait
    indefinitely. Polling `.status.phase` directly and asking phase_action for
    a bounded decision avoids that specific hang.

    This function does not by itself guarantee the phase it reads came from
    the *right* pod when more than one matches the job-name selector — that
    is pod_phase()'s job, via newest_pod(), combined with `--cascade=foreground`
    on the Job delete in run_shape so a stale pod from a previous run is gone
    before this one starts polling.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        phase = pod_phase(shape)
        action = phase_action(phase)
        if action != "wait":
            return action
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{job_name(shape)}: pod still {phase or '(not found)'} "
                f"after {timeout_s:.0f}s"
            )
        time.sleep(poll_interval)


def restart_count() -> int:
    """Total container restarts across player-projections pods.

    Read from the kubelet rather than Prometheus on purpose: a restart that
    lasts a second would fall between 60s scrapes, which is the lesson
    resource-pressure learned in #46 when a gauge criterion sat flat.
    """
    result = kubectl(
        [
            "get",
            "pod",
            "-l",
            "app.kubernetes.io/name=player-projections",
            "-o",
            "jsonpath={.items[*].status.containerStatuses[*].restartCount}",
        ]
    )
    return sum(int(n) for n in result.stdout.split())


def run_shape(shape: str, soak_minutes: int) -> bool:
    cfg = SHAPES[shape]
    script = SCRIPT_DIR / cfg["script"]
    if not script.exists():
        raise RuntimeError(f"{shape}: missing script {script}")

    print(f"\n{'=' * 66}")
    print(f"shape: {shape}   script: {cfg['script']}")
    print(f"{'=' * 66}")

    restarts_before = restart_count()

    try:
        configmap_up()
        # --cascade=foreground: the default (background) cascade returns as
        # soon as the Job object itself is gone, before its pod has actually
        # terminated — a rerun of the same shape could then apply a new Job
        # while the old pod was still Running (Kubernetes has no
        # "Terminating" phase), leaving two pods behind the same job-name
        # selector. Waiting here removes that race at its source; newest_pod()
        # in pod_phase/pod_exit_code is the second, independent line of
        # defence if a pod from outside this tool's own lifecycle ever
        # matches anyway.
        kubectl(
            [
                "delete",
                "job",
                job_name(shape),
                "--ignore-not-found",
                "--cascade=foreground",
            ]
        )
        apply_job(render_job(shape, soak_minutes))

        # The very first live run of this tool found kubectl logs -f giving up
        # in ~30-100ms — not honouring --pod-running-timeout at all — while the
        # pod was still ContainerCreating, and the (then-immediate) fallback
        # fetch racing the exact same window and failing identically. Wait for
        # the pod to leave that window first, budgeted by the shape's own
        # timeout, and let phase_action decide what to do with what's observed.
        action = wait_for_pod_action(shape, parse_kube_duration(cfg["timeout"]))

        if action == "follow":
            # Streams k6's own output as it runs, live — route_log_lines prints
            # each line as it arrives instead of buffering the whole run. --follow
            # exits when the pod terminates, and reading its stdout to EOF is the
            # wait; the summary JSON is withheld from the console and captured
            # for the results file instead.
            follow_cmd = [
                "kubectl",
                "logs",
                "-f",
                f"job/{job_name(shape)}",
                "--pod-running-timeout",
                cfg["timeout"],
            ]
            print(f"  $ {' '.join(follow_cmd)}")
            proc = subprocess.Popen(
                follow_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            logs = route_log_lines(proc.stdout, lambda line: print(line, end=""))
            proc.wait()

            # The follow stream is not proof of a complete log: a non-zero exit
            # means kubectl itself gave up, and a clean exit with no marker means
            # it ended before k6 printed its summary. Either way, fall back to the
            # checked, non-follow fetch — it only runs after the pod has fully
            # terminated, and kubectl()'s check=True makes a failure here loud
            # rather than quietly leaving `logs` as whatever the stream managed.
            if needs_fallback(proc.returncode, SUMMARY_MARKER in logs):
                print(
                    f"  follow stream looked incomplete (exit {proc.returncode}, "
                    f"marker seen: {SUMMARY_MARKER in logs}) -- re-fetching settled logs"
                )
                logs = kubectl(["logs", f"job/{job_name(shape)}"]).stdout
        else:
            # action == "fetch": the pod already reached Succeeded/Failed
            # before wait_for_pod_action's poll ever saw it Running — a fast
            # shape can finish inside one poll interval. A live follow would
            # attach to a stream with nothing left to send, so this is the
            # normal path for a quick run, not a failure; go straight to the
            # checked fetch.
            print(
                "  pod already terminated before follow would have attached -- fetching settled logs directly"
            )
            logs = kubectl(["logs", f"job/{job_name(shape)}"]).stdout

        text, payload = split_summary(logs)
        if SUMMARY_MARKER not in logs:
            print(
                f"  WARNING: no summary marker in logs for {shape} -- "
                f"{shape}.json will not be written"
            )

        RESULTS_DIR.mkdir(exist_ok=True)
        # encoding="utf-8" explicitly: k6's own startup banner contains U+203E
        # (the "/‾‾/" ascii-art), and on Windows `uv run python` resolves
        # write_text's default encoding to the locale codepage (cp1252) rather
        # than UTF-8, so a 5-minute ramp completed and then died writing its
        # own results. Every capture has that banner, so this is not an edge
        # case -- it broke every local run on this platform.
        (RESULTS_DIR / f"{shape}.txt").write_text(text, encoding="utf-8")
        if payload.strip():
            (RESULTS_DIR / f"{shape}.json").write_text(payload, encoding="utf-8")

        code = pod_exit_code(shape)
        passed, verdict = interpret_exit(shape, code)

        # Name the threshold that crossed, if k6 exited 99. Gated on the exit
        # code rather than `not passed`: breakpoint's whole output is "which
        # rung broke", and it exits 99 on a *successful* MEASURED run (passed
        # is True there), not a failing one. Gating on `not passed` would
        # silently skip the one shape whose entire purpose is this data. A
        # malformed or missing summary must not crash a run that is already
        # reporting a failure -- json.JSONDecodeError degrades to "no name",
        # not a traceback.
        if code == K6_EXIT_THRESHOLD_CROSSED and payload.strip():
            try:
                breached = first_breached_threshold(json.loads(payload))
            except json.JSONDecodeError:
                breached = None
            if breached:
                verdict = f"{verdict}: {breached}"

        restarts_after = restart_count()
        passed, verdict = apply_restart_check(
            shape, passed, verdict, restarts_before, restarts_after
        )
        if restarts_after == restarts_before:
            print(f"  restart count unchanged at {restarts_after}")

        print(f"\nresult: {verdict}")
        return passed
    finally:
        # Same --cascade=foreground reasoning as the pre-run delete above:
        # leaving this one on the default background cascade would mean the
        # *next* invocation's pre-run delete is what ends up waiting out the
        # old pod instead, which still works but makes this cleanup's own
        # "done" signal a lie in the meantime.
        kubectl(
            [
                "delete",
                "job",
                job_name(shape),
                "--ignore-not-found",
                "--cascade=foreground",
            ],
            check=False,
        )
        kubectl(["delete", "configmap", CONFIGMAP, "--ignore-not-found"], check=False)


def main() -> None:
    # Same U+203E exposure as the results-file write, but for the streaming
    # path: when stdout is a pipe (CI, or a captured local run) rather than a
    # console, Python still resolves its encoding from the locale codepage on
    # Windows unless UTF-8 mode is on. route_log_lines' `show` callback prints
    # every line k6 emits live, banner included. Guarded for interpreters
    # where stdout has no reconfigure (e.g. already replaced by a test
    # harness's capture object).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="run-load",
        description="Run a k6 load shape against player-projections.",
    )
    parser.add_argument("shape", nargs="?", help="shape name, e.g. ramp")
    parser.add_argument("--all", action="store_true", help="run every shape in order")
    parser.add_argument("--list", action="store_true", help="list available shapes")
    parser.add_argument(
        "--soak-minutes",
        type=int,
        default=5,
        help="soak duration in minutes (default 5; the phase doc specifies 30)",
    )
    args = parser.parse_args()

    if args.list:
        for shape in SHAPES:
            print(shape)
        return

    if args.all:
        # This is the phase doc's specified order, not a runtime dependency:
        # spike's cooldown threshold (p(95)<10 in spike.js) is a static
        # literal calibrated ahead of time against captured runs (see
        # docs/scale-baselines.md), not a value ramp produces or that this
        # tool feeds forward at run time.
        targets = list(SHAPES)
    elif args.shape:
        if args.shape not in SHAPES:
            print(f"Unknown shape: {args.shape}")
            print(f"Available: {', '.join(SHAPES)}")
            sys.exit(1)
        targets = [args.shape]
    else:
        parser.print_help()
        sys.exit(1)

    # A str result, not a bool: "ERROR" (the shape raised) must stay
    # distinguishable from "FAIL" (it ran and an assertion was not met), and one
    # raise must not kill the rest of an --all run the way it would leave CI's
    # artifact a bare traceback.
    results: dict[str, str] = {}
    for shape in targets:
        try:
            results[shape] = "PASS" if run_shape(shape, args.soak_minutes) else "FAIL"
        except Exception as exc:
            print(f"\nERROR running {shape}: {exc}")
            results[shape] = "ERROR"

    print(f"\n{'=' * 66}")
    print("summary")
    print(f"{'=' * 66}")
    for shape, status in results.items():
        print(f"  {status}  {shape}")

    if any(status != "PASS" for status in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
