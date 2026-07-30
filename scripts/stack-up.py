"""
Bring up the full local stack: cluster, observability, services, port-forwards.

Usage:
  python scripts/stack-up.py                  # all services
  python scripts/stack-up.py <name> [<name>]  # specific services only

The service list is derived from `contracts/collector-registry.yaml` plus each
service's Helm values by `scripts/collectors.py` — adding a collector means
appending a registry entry, not editing this file.

Ctrl+C stops all port-forwards. The cluster and Helm releases are left running.
Use `kind delete cluster --name foundry` to tear everything down.
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collectors as fleet  # noqa: E402  (needs the path insert above)

ROOT = Path(__file__).parent.parent

# Every service is port-forwarded from the `default` namespace on its own
# service port; nothing per-service is configured here.
SERVICE_NAMESPACE = "default"

OBS_FORWARDS = [
    {
        "name": "Grafana",
        "svc": "grafana",
        "namespace": "monitoring",
        "local": 3000,
        "remote": 80,
        "url": "http://localhost:3000  (admin / admin)",
    },
    {
        "name": "Prometheus",
        "svc": "prometheus-server",
        "namespace": "monitoring",
        "local": 9090,
        "remote": 80,
        "url": "http://localhost:9090",
    },
    {
        "name": "Loki",
        "svc": "loki",
        "namespace": "monitoring",
        "local": 3100,
        "remote": 3100,
        "url": "http://localhost:3100/ready",
    },
    {
        "name": "Tempo",
        "svc": "tempo",
        "namespace": "monitoring",
        "local": 3200,
        "remote": 3200,
        "url": "http://localhost:3200/ready",
    },
]


def run(cmd: list, cwd: Path | None = None) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def cluster_running() -> bool:
    result = subprocess.run(
        ["kind", "get", "clusters"], capture_output=True, text=True
    )
    return "foundry" in result.stdout.splitlines()


def main() -> None:
    try:
        services = fleet.by_name()
    except fleet.FleetError as exc:
        print(f"FAIL: {exc}")
        sys.exit(2)

    args = sys.argv[1:]
    forward_only = "--forward-only" in args
    requested_raw = [a for a in args if not a.startswith("--")]
    requested = requested_raw or list(services)

    unknown = [s for s in requested if s not in services]
    if unknown:
        print(f"Unknown service(s): {', '.join(unknown)}")
        print(f"Available: {', '.join(services)}")
        sys.exit(1)

    if not forward_only:
        # 1. Cluster
        if cluster_running():
            print("\nKind cluster 'foundry' already running — skipping create.")
        else:
            run([
                "kind", "create", "cluster",
                "--config", ROOT / "infra/kind/cluster.yaml",
            ])

        # 2. Observability stack
        grafana_stack = ROOT / "infra/grafana-stack"
        run(["helmfile", "repos"], cwd=grafana_stack)
        run(["helmfile", "apply"], cwd=grafana_stack)

        # 3. Collector gateway
        gateway = ROOT / "infra/gateway"
        run(["helmfile", "apply"], cwd=gateway)
        run(["kubectl", "apply", "-f", str(gateway / "manifests")])
        run([
            "kubectl", "wait", "--for=condition=Programmed",
            "gateway/foundry", "-n", "envoy-gateway-system", "--timeout=180s",
        ])

        # 4. Services
        for service in requested:
            run([sys.executable, ROOT / "scripts/deploy-local.py", service])

        # 5. Wait for pods to be ready
        print("\nWaiting for pods to be ready...")
        run([
            "kubectl", "wait", "--for=condition=ready", "pod",
            "--all", "-n", "monitoring", "--timeout=180s",
        ])
        # `rollout status`, not `wait --for=condition=ready pod -l <label>`: the
        # label selector also matches pods left Terminating by a rollout, which
        # never reach Ready, so the wait times out on a healthy Deployment.
        # deploy-local.py already waits per service; this re-checks cheaply
        # after all of them are deployed.
        for service in requested:
            run([
                "kubectl", "rollout", "status", f"deployment/{service}",
                "--timeout=120s",
            ])
    else:
        print("\nSkipping deploy — starting port-forwards only.")

    # 6. Start port-forwards in background
    print("\nStarting port-forwards...")
    procs = []

    for fwd in OBS_FORWARDS:
        proc = subprocess.Popen([
            "kubectl", "port-forward",
            "-n", fwd["namespace"],
            f"svc/{fwd['svc']}",
            f"{fwd['local']}:{fwd['remote']}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)

    for service in requested:
        port = services[service].port
        proc = subprocess.Popen([
            "kubectl", "port-forward",
            "-n", SERVICE_NAMESPACE,
            f"svc/{service}",
            f"{port}:{port}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)

    # Give forwards a moment to bind
    time.sleep(2)

    # 7. Print access URLs
    print("\n" + "=" * 50)
    print("Stack is up. Access your services at:\n")
    for service in requested:
        print(f"  {service:<20} http://localhost:{services[service].port}")
    for fwd in OBS_FORWARDS:
        print(f"  {fwd['name']:<20} {fwd['url']}")
    print(f"  {'Collector gateway':<20} http://localhost:8080/collectors/<name>/")
    print("\n" + "=" * 50)
    print("Press Ctrl+C to stop port-forwards (cluster stays running).")
    print("To tear everything down: kind delete cluster --name foundry")

    # 8. Wait for Ctrl+C
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping port-forwards...")
        for proc in procs:
            proc.terminate()
        print("Done.")


if __name__ == "__main__":
    main()
