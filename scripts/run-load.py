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
import subprocess
import sys
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
SHAPES: dict[str, dict] = {
    "ramp": {"script": "ramp.js", "expect_threshold_breach": False, "timeout": "10m"},
    "soak": {"script": "soak.js", "expect_threshold_breach": False, "timeout": "50m"},
    "spike": {"script": "spike.js", "expect_threshold_breach": False, "timeout": "10m"},
    "breakpoint": {
        "script": "breakpoint.js",
        "expect_threshold_breach": True,
        "timeout": "20m",
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
    command = (
        f"k6 run --quiet --summary-mode full "
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
                    "volumes": [
                        {"name": "scripts", "configMap": {"name": CONFIGMAP}}
                    ],
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
                "NO-BREAKPOINT — the top rung never crossed 1% errors, so this "
                "run measured nothing; raise the rungs in breakpoint.js"
            )
        return True, "PASS"
    if code == K6_EXIT_THRESHOLD_CROSSED:
        if expects_breach:
            return True, "MEASURED — threshold crossed as designed"
        return False, "FAIL — a threshold was crossed"
    return False, f"ERROR — k6 exited {code}"


# ── cluster interaction ───────────────────────────────────────────────────────

def kubectl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ kubectl {' '.join(args)}")
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True
    )
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


def pod_exit_code(shape: str) -> int:
    """Read the terminated container's exit code — the entire verdict."""
    result = kubectl([
        "get", "pod",
        "-l", f"job-name={job_name(shape)}",
        "-o", "jsonpath={.items[0].status.containerStatuses[0].state.terminated.exitCode}",
    ])
    text = result.stdout.strip()
    if not text:
        raise RuntimeError(f"{job_name(shape)}: no terminated container to read")
    return int(text)


def restart_count() -> int:
    """Total container restarts across player-projections pods.

    Read from the kubelet rather than Prometheus on purpose: a restart that
    lasts a second would fall between 60s scrapes, which is the lesson
    resource-pressure learned in #46 when a gauge criterion sat flat.
    """
    result = kubectl([
        "get", "pod",
        "-l", "app.kubernetes.io/name=player-projections",
        "-o", "jsonpath={.items[*].status.containerStatuses[*].restartCount}",
    ])
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
        kubectl(["delete", "job", job_name(shape), "--ignore-not-found"])
        apply_job(render_job(shape, soak_minutes))

        # Streams k6's own output as it runs. --follow returns when the pod
        # terminates, so this is also the wait.
        subprocess.run(
            ["kubectl", "logs", "-f", f"job/{job_name(shape)}", "--pod-running-timeout",
             cfg["timeout"]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        logs = kubectl(["logs", f"job/{job_name(shape)}"]).stdout
        text, payload = split_summary(logs)
        print(text)

        RESULTS_DIR.mkdir(exist_ok=True)
        (RESULTS_DIR / f"{shape}.txt").write_text(text)
        if payload.strip():
            (RESULTS_DIR / f"{shape}.json").write_text(payload)

        code = pod_exit_code(shape)
        passed, verdict = interpret_exit(shape, code)

        restarts_after = restart_count()
        if restarts_after != restarts_before:
            passed = False
            verdict += (
                f" | RESTARTED — player-projections restart count moved "
                f"{restarts_before} → {restarts_after}"
            )
        else:
            print(f"  restart count unchanged at {restarts_after}")

        print(f"\nresult: {verdict}")
        return passed
    finally:
        kubectl(["delete", "job", job_name(shape), "--ignore-not-found"], check=False)
        kubectl(["delete", "configmap", CONFIGMAP, "--ignore-not-found"], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run-load",
        description="Run a k6 load shape against player-projections.",
    )
    parser.add_argument("shape", nargs="?", help="shape name, e.g. ramp")
    parser.add_argument("--all", action="store_true", help="run every shape in order")
    parser.add_argument("--list", action="store_true", help="list available shapes")
    parser.add_argument(
        "--soak-minutes", type=int, default=5,
        help="soak duration in minutes (default 5; the phase doc specifies 30)",
    )
    args = parser.parse_args()

    if args.list:
        for shape in SHAPES:
            print(shape)
        return

    if args.all:
        # Order matters: ramp establishes the p(95) baseline that spike's
        # cooldown threshold is calibrated against.
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
