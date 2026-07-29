#!/usr/bin/env python3
"""
Run a chaos scenario and check its hypothesis against Prometheus.

A scenario file is multi-document YAML: one `foundry.chaos/v1` Scenario head
carrying the steady state, hypothesis, and criteria, followed by the Chaos Mesh
resources that inject the fault. The criterion lives beside the fault it judges.

Usage:
  uv run --with pyyaml==6.0.3 python scripts/run-chaos.py <scenario>
  uv run --with pyyaml==6.0.3 python scripts/run-chaos.py --all
  uv run --with pyyaml==6.0.3 python scripts/run-chaos.py --list

Exits non-zero if any criterion failed.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "chaos" / "scenarios"
TRAFFIC_DIR = ROOT / "chaos" / "traffic"

SCENARIO_API = "foundry.chaos/v1"
PROM_PROXY = (
    "/api/v1/namespaces/monitoring/services/prometheus-server:80"
    "/proxy/api/v1/query"
)

# Two-character operators first: ">= 2" must not parse as ">" of "= 2".
_OPERATORS = ("==", "!=", ">=", "<=", ">", "<")

OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def parse_expect(expect: str) -> tuple[str, float]:
    """Split an `expect` string such as "== 1" into (operator, value)."""
    text = (expect or "").strip()
    for op in _OPERATORS:
        if text.startswith(op):
            rest = text[len(op):].strip()
            try:
                return op, float(rest)
            except ValueError:
                raise ValueError(f"expect value is not a number: {expect!r}")
    raise ValueError(f"expect must start with a comparison operator: {expect!r}")


def is_trivially_true(op: str, value: float) -> bool:
    """True when no observation could falsify the comparison.

    Every metric these scenarios query is non-negative — counters, byte gauges,
    ratios, and `up` — so a comparison satisfied by every non-negative number
    can never go red. Such a criterion reports coverage it does not have, which
    is worse than having no scenario at all.
    """
    if op == ">=" and value <= 0:
        return True
    if op == ">" and value < 0:
        return True
    if op == "!=" and value < 0:
        return True
    return False


def parse_duration(text) -> float:
    """Parse "90s", "2m", "500ms", "1h", or a bare number, into seconds."""
    text = str(text).strip()
    # "ms" before "s": "500ms" also ends in "s".
    for suffix, factor in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


def extract_scalar(payload: dict, *, allow_empty: bool) -> float | None:
    """Pull a single number out of a Prometheus query response.

    Returns None when the result is empty and the check did not opt in with
    `allowEmpty`. Prometheus answers `{"status":"success","result":[]}` both for
    a series that has never existed and for a typo'd metric name, so treating
    empty as 0 by default would let a typo satisfy every criterion silently.
    """
    if payload.get("status") != "success":
        raise ValueError(f"Prometheus query failed: {payload.get('error', payload)}")
    result = payload.get("data", {}).get("result", [])
    if not result:
        return 0.0 if allow_empty else None
    if len(result) > 1:
        raise ValueError(
            f"query returned {len(result)} series; a check must reduce to one "
            "(wrap it in an aggregation such as max() or count())"
        )
    return float(result[0]["value"][1])


def load_scenario(path: Path) -> tuple[dict, list[dict]]:
    """Split a scenario file into its Scenario head and its chaos resources."""
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    heads = [d for d in docs if d.get("apiVersion") == SCENARIO_API]
    others = [d for d in docs if d.get("apiVersion") != SCENARIO_API]
    if len(heads) != 1:
        raise ValueError(
            f"{path.name}: expected exactly one {SCENARIO_API} document, "
            f"found {len(heads)}"
        )
    return heads[0], others


def validate_scenario(head: dict, chaos_docs: list[dict]) -> list[str]:
    """Return a list of problems with a scenario. An empty list means valid."""
    problems: list[str] = []

    if not head.get("metadata", {}).get("name"):
        problems.append("metadata.name is required")

    spec = head.get("spec") or {}
    if not spec.get("hypothesis"):
        problems.append("spec.hypothesis is required")
    if not spec.get("steadyState"):
        problems.append("spec.steadyState must declare at least one check")
    if not spec.get("criteria"):
        problems.append("spec.criteria must declare at least one check")
    if not chaos_docs and not spec.get("fault"):
        problems.append(
            "a scenario must inject something: chaos resources, spec.fault, or both"
        )

    for label in ("steadyState", "criteria"):
        for i, check in enumerate(spec.get(label) or []):
            where = f"spec.{label}[{i}]"
            if not check.get("description"):
                problems.append(f"{where}.description is required")
            if not check.get("query"):
                problems.append(f"{where}.query is required")
            try:
                op, value = parse_expect(check.get("expect", ""))
            except ValueError as exc:
                problems.append(f"{where}.expect: {exc}")
                continue
            # Only criteria. A permissive steady state makes the runner more
            # willing to start; a permissive criterion manufactures coverage.
            if label == "criteria" and is_trivially_true(op, value):
                problems.append(
                    f"{where}.expect {check['expect']!r} cannot fail — a criterion "
                    "no observation can falsify reports coverage it does not have"
                )

    return problems


# ── cluster interaction ───────────────────────────────────────────────────────

def run_cmd(cmd: list[str], check: bool = True) -> None:
    """Run a command, printing it first. Optionally tolerate failure."""
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        if check:
            raise RuntimeError(f"command failed: {message}")
        print(f"    (ignored: {message})")


def promql(query: str) -> dict:
    """Query Prometheus through the Kubernetes API proxy.

    Deliberately not a port-forward. On Windows a stale `kubectl port-forward`
    survives `pkill -f port-forward` and silently holds the port, which has
    already cost this repo one confusing smoke-test failure. `kubectl get --raw`
    is a single invocation that behaves identically on Windows and in CI.
    """
    url = f"{PROM_PROXY}?query={urllib.parse.quote(query)}"
    result = subprocess.run(
        ["kubectl", "get", "--raw", url], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl get --raw failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def evaluate(checks: list[dict]) -> tuple[bool, list[str]]:
    """Run each check against Prometheus; return (all_passed, report lines)."""
    all_passed = True
    lines: list[str] = []
    for check in checks:
        op, expected = parse_expect(check["expect"])
        observed = extract_scalar(
            promql(check["query"]), allow_empty=check.get("allowEmpty", False)
        )
        if observed is None:
            passed, shown = False, "no data (set allowEmpty if this is expected)"
        else:
            passed, shown = OPS[op](observed, expected), f"{observed:g}"
        all_passed = all_passed and passed
        lines.append(
            f"  [{'PASS' if passed else 'FAIL'}] {check['description']}: "
            f"{shown} (want {op} {expected:g})"
        )
    return all_passed, lines


def kubectl_docs(action: str, docs: list[dict]) -> None:
    """Apply or delete a list of manifests by piping them to kubectl."""
    if not docs:
        return
    args = ["kubectl", action, "-f", "-"]
    if action == "delete":
        args.append("--ignore-not-found")
    print(f"  $ kubectl {action} -f - ({len(docs)} document(s))")
    result = subprocess.run(
        args, input=yaml.safe_dump_all(docs), capture_output=True, text=True
    )
    if result.returncode != 0 and action != "delete":
        raise RuntimeError(f"kubectl {action} failed: {result.stderr.strip()}")


def gateway_host() -> str:
    """Resolve the Envoy data-plane Service DNS name.

    The Service name carries a generated hash, so it is found by the label
    Envoy Gateway stamps on it rather than hardcoded — the same lookup
    scripts/smoke-test.sh does.
    """
    result = subprocess.run(
        ["kubectl", "get", "svc", "-n", "envoy-gateway-system",
         "-l", "gateway.envoyproxy.io/owning-gateway-name=foundry",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("could not resolve the Envoy data-plane Service")
    return f"{result.stdout.strip()}.envoy-gateway-system.svc.cluster.local"


def traffic_up(name: str) -> None:
    """Apply a traffic Deployment, substituting the resolved gateway host."""
    text = (TRAFFIC_DIR / f"{name}.yaml").read_text()
    text = text.replace("${GATEWAY_HOST}", gateway_host())
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"], input=text, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"applying traffic failed: {result.stderr.strip()}")
    # rollout status, never `wait --for=condition=ready pod -l`: the label
    # selector also matches Terminating pods, which never reach Ready.
    run_cmd(["kubectl", "rollout", "status",
             f"deployment/chaos-traffic-{name}", "--timeout=120s"])


def traffic_down(name: str) -> None:
    run_cmd(["kubectl", "delete", "deployment", f"chaos-traffic-{name}",
             "--ignore-not-found"], check=False)


# ── the scenario loop ─────────────────────────────────────────────────────────

def run_scenario(path: Path, *, skip_steady_state: bool = False) -> bool:
    head, chaos_docs = load_scenario(path)

    problems = validate_scenario(head, chaos_docs)
    if problems:
        print(f"\n{path.name} is invalid:")
        for problem in problems:
            print(f"  - {problem}")
        return False

    spec = head["spec"]
    name = head["metadata"]["name"]
    print(f"\n{'=' * 66}")
    print(f"scenario: {name}")
    print(f"{'=' * 66}")
    print(f"hypothesis: {spec['hypothesis'].strip()}\n")

    if not skip_steady_state:
        print("steady state:")
        ok, lines = evaluate(spec["steadyState"])
        print("\n".join(lines))
        if not ok:
            print(
                "\nABORT: the system is not in its steady state. Injecting now "
                "would make the result unattributable."
            )
            return False

    traffic_name = spec.get("traffic")
    hold = parse_duration(spec.get("duration", "60s"))
    settle = parse_duration(spec.get("settle", "30s"))

    try:
        if traffic_name:
            print(f"\ntraffic: {traffic_name}")
            traffic_up(traffic_name)

        print("\ninjecting fault")
        for cmd in spec.get("fault", []):
            run_cmd(list(cmd))
        kubectl_docs("apply", chaos_docs)

        print(f"holding for {hold:g}s")
        time.sleep(hold)

        print("removing fault")
        kubectl_docs("delete", chaos_docs)

        print(f"settling for {settle:g}s")
        time.sleep(settle)

        print("\ncriteria:")
        ok, lines = evaluate(spec["criteria"])
        print("\n".join(lines))
        print(f"\nresult: {'PASS' if ok else 'FAIL'}")
        return ok
    finally:
        # Idempotent — the happy path already deleted these.
        kubectl_docs("delete", chaos_docs)
        for cmd in spec.get("restore", []):
            run_cmd(list(cmd), check=False)
        if traffic_name:
            traffic_down(traffic_name)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run-chaos",
        description="Run a chaos scenario and check its hypothesis against Prometheus.",
    )
    parser.add_argument("scenario", nargs="?", help="scenario name, e.g. pod-kill")
    parser.add_argument("--all", action="store_true", help="run every scenario in order")
    parser.add_argument("--list", action="store_true", help="list available scenarios")
    parser.add_argument(
        "--skip-steady-state", action="store_true",
        help="inject without checking steady state first (debugging only)",
    )
    args = parser.parse_args()

    available = sorted(p.stem for p in SCENARIO_DIR.glob("*.yaml"))

    if args.list:
        for scenario in available:
            print(scenario)
        return

    if args.all:
        targets = available
    elif args.scenario:
        if args.scenario not in available:
            print(f"Unknown scenario: {args.scenario}")
            print(f"Available: {', '.join(available)}")
            sys.exit(1)
        targets = [args.scenario]
    else:
        parser.print_help()
        sys.exit(1)

    results = {}
    for scenario in targets:
        results[scenario] = run_scenario(
            SCENARIO_DIR / f"{scenario}.yaml",
            skip_steady_state=args.skip_steady_state,
        )

    print(f"\n{'=' * 66}")
    print("summary")
    print(f"{'=' * 66}")
    for scenario, passed in results.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {scenario}")

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
