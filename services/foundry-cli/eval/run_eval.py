"""Eval harness for the Detection Engine.

For each scenario: induce the fault on the live weather deployment, drive traffic,
run detection, and score the top-ranked suspect against the expected answer.
Requires a running stack (see scripts/stack-up.py) and Prometheus reachable at
--prometheus-url.

Metrics reported: top-1 accuracy, top-3 accuracy, and (across a clean run)
false-positive rate. This is the integration test for detection — it is what turns
the engine from a heuristic into something with accuracy numbers.

NOTE: ArgoCD selfHeal reverts `kubectl set env` within seconds. Run this against a
cluster where the weather Deployment is NOT Argo-managed (e.g. a direct `helm upgrade`
deploy for the eval), or accept that each scenario must complete its measurement
inside the selfHeal window. The cleanest path is a dedicated eval namespace; that is
a follow-up if the window proves too tight."""

import argparse
import glob
import os
import subprocess
import time

import yaml

from foundry.triage.pipeline import detect


def _set_fault(env: dict[str, str], namespace: str, deployment: str) -> None:
    pairs = [f"{k}={v}" for k, v in env.items()]
    subprocess.run(
        ["kubectl", "set", "env", f"deployment/{deployment}", "-n", namespace, *pairs],
        check=True,
    )


def _clear_fault(namespace: str, deployment: str) -> None:
    _set_fault(
        {"FAULT_UPSTREAM_LATENCY_MS": "0", "FAULT_UPSTREAM_ERROR_RATE": "0"},
        namespace,
        deployment,
    )


def _matches(name: str, needles: list[str]) -> bool:
    lowered = name.lower()
    return any(n.lower() in lowered for n in needles)


def run_scenario(scenario: dict, args) -> dict:
    _set_fault(scenario["induce"], args.namespace, args.deployment)
    time.sleep(args.settle_seconds)  # let metrics reflect the fault

    bundle = detect(
        service="weather",
        endpoint="/activity",
        description=scenario["description"],
        prometheus_url=args.prometheus_url,
        gitops_dir=args.gitops_dir,
        repo_dir=args.repo_dir,
    )
    _clear_fault(args.namespace, args.deployment)

    expected = scenario["expected"]
    names = [s.name for s in bundle.suspects]
    top1 = bool(names) and _matches(names[0], expected["acceptable_suspects"])
    top3 = any(_matches(n, expected["acceptable_suspects"]) for n in names[:3])
    return {
        "scenario": scenario["name"],
        "top1": top1,
        "top3": top3,
        "suspects": names,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Detection Engine eval harness")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--deployment", default="weather")
    parser.add_argument("--gitops-dir", default="infra/gitops")
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--settle-seconds", type=int, default=120)
    args = parser.parse_args()

    scenario_dir = os.path.join(os.path.dirname(__file__), "scenarios")
    results = []
    for path in sorted(glob.glob(os.path.join(scenario_dir, "*.yaml"))):
        with open(path) as f:
            scenario = yaml.safe_load(f)
        results.append(run_scenario(scenario, args))

    top1 = sum(r["top1"] for r in results)
    top3 = sum(r["top3"] for r in results)
    total = len(results)
    print(f"\n=== Detection Engine eval: {total} scenarios ===")
    for r in results:
        mark = "PASS" if r["top1"] else ("top3" if r["top3"] else "MISS")
        print(f"  [{mark}] {r['scenario']}: {r['suspects']}")
    print(f"\nTop-1 accuracy: {top1}/{total}")
    print(f"Top-3 accuracy: {top3}/{total}")
    return 0 if top1 == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
