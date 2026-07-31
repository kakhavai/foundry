"""Local deploy: docker build → kind load → helm upgrade for a given service.

The service list is not kept here. It is derived from
`contracts/collector-registry.yaml` plus each service's Helm values by
`scripts/collectors.py` — see that module's docstring for why the deployment
facts are read out of the artifact Kubernetes applies rather than copied into
a second list. Adding a collector means appending a registry entry; this file
does not change.

Needs PyYAML, via scripts/collectors.py. The CI runner image ships a bare
python3, so CI invokes it as:

    uv run --no-project --with pyyaml==6.0.3 python3 scripts/deploy-local.py <name>
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collectors as fleet  # noqa: E402  (needs the path insert above)

# Kind-only. A real token is created out of band and never enters Git; on EKS
# the Secret is backed by AWS Secrets Manager.
LOCAL_DEV_TOKEN = "local-dev-token"

# Matches infra/grafana-stack/values/minio.yaml. Kind-only, committed
# deliberately — real credentials are created out of band and never enter Git.
LOCAL_LAKE_ACCESS_KEY = "foundry"
LOCAL_LAKE_SECRET_KEY = "foundry-local-dev"


_BUILD_ENV = {**os.environ, "DOCKER_BUILDKIT": "1"}


def run(cmd: list[str], env: dict | None = None) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, env=env, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def deployment_exists(service: str) -> bool:
    result = subprocess.run(
        ["kubectl", "get", "deployment", service],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def run_piped(render_cmd: list[str], apply_cmd: list[str]) -> None:
    """Run `render_cmd`, piping its stdout into `apply_cmd`.

    Used for `kubectl create secret ... --dry-run=client -o yaml | kubectl
    apply -f -`, which is idempotent across every deploy after the first,
    unlike `kubectl create secret` alone.
    """
    rendered = subprocess.run(render_cmd, capture_output=True, text=True, check=False)
    if rendered.returncode != 0:
        print(rendered.stderr)
        sys.exit(rendered.returncode)

    applied = subprocess.run(apply_cmd, input=rendered.stdout, text=True, check=False)
    if applied.returncode != 0:
        sys.exit(applied.returncode)


def ensure_collector_secret(name: str) -> None:
    """Create or update the collector's bearer-token Secret."""
    print(f"\n$ kubectl create secret generic {name} | kubectl apply -f -")
    run_piped(
        [
            "kubectl",
            "create",
            "secret",
            "generic",
            name,
            f"--from-literal=token={LOCAL_DEV_TOKEN}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        ["kubectl", "apply", "-f", "-"],
    )


def ensure_lake_secret(name: str) -> None:
    """Create or update the collector's object-store credentials Secret."""
    print(f"\n$ kubectl create secret generic {name} | kubectl apply -f -")
    run_piped(
        [
            "kubectl",
            "create",
            "secret",
            "generic",
            name,
            f"--from-literal=access-key-id={LOCAL_LAKE_ACCESS_KEY}",
            f"--from-literal=secret-access-key={LOCAL_LAKE_SECRET_KEY}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        ["kubectl", "apply", "-f", "-"],
    )


def main() -> None:
    try:
        services = fleet.by_name()
    except fleet.FleetError as exc:
        print(f"FAIL: {exc}")
        sys.exit(2)

    if len(sys.argv) < 2:
        print("Usage: python scripts/deploy-local.py <service-name>")
        print(f"Available: {', '.join(services)}")
        sys.exit(1)

    name = sys.argv[1]
    if name not in services:
        print(f"Unknown service: {name}")
        print(f"Available: {', '.join(services)}")
        sys.exit(1)

    service = services[name]

    # Checked before the upgrade, because the upgrade is what creates it.
    already_deployed = deployment_exists(name)

    if service.build_context_root:
        # Collectors are uv workspace members depending on libs/collector-core/
        # by path, so the build needs the repo root in its context.
        run(
            [
                "docker",
                "build",
                "-f",
                f"services/{name}/Dockerfile",
                "-t",
                f"{name}:local",
                ".",
            ],
            env=_BUILD_ENV,
        )
    else:
        run(
            ["docker", "build", "-t", f"{name}:local", f"services/{name}/"],
            env=_BUILD_ENV,
        )
    run(["kind", "load", "docker-image", f"{name}:local", "--name", "foundry"])

    # Both Secret names come from the values file's own secretKeyRefs, so the
    # Secret created here is by construction the one the pod references.
    if service.token_secret:
        ensure_collector_secret(service.token_secret)
    if service.lake_secret:
        ensure_lake_secret(service.lake_secret)

    run(
        [
            "helm",
            "upgrade",
            "--install",
            name,
            "helm/charts/generic-service",
            "-f",
            f"helm/values/{name}/values.yaml",
            "--set",
            f"image.repository={name}",
            "--set",
            "image.tag=local",
            "--set",
            "image.pullPolicy=Never",
        ]
    )

    # image.tag is pinned to "local" on every deploy, so a rebuilt image (or a
    # just-updated token Secret) produces a byte-identical PodSpec and
    # Kubernetes never restarts the pod on its own.
    #
    # Only when a Deployment was already running, though. Restarting one the
    # upgrade just created starts a second ReplicaSet immediately, and the old
    # pod lingers Terminating — where `kubectl wait --for=condition=ready pod -l
    # <label>` matches it and waits for a readiness it will never reach. That is
    # a fresh-cluster CI failure, not a local-dev inconvenience.
    if already_deployed:
        run(["kubectl", "rollout", "restart", f"deployment/{name}"])
    run(["kubectl", "rollout", "status", f"deployment/{name}", "--timeout=180s"])

    print(f"\n{'=' * 50}")
    print(f"Deployed {name}. To access it:\n")
    print(f"  kubectl port-forward svc/{name} {service.port}:{service.port}")
    print(f"  -> http://localhost:{service.port}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
