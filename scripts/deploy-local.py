"""Local deploy: docker build → kind load → helm upgrade for a given service."""

import os
import subprocess
import sys

SERVICES = {
    "weather": {
        "port": 8000,
        "secret": "weather-collector-token",
        "lake_secret": "weather-lake-credentials",
        # weather is a uv workspace member depending on libs/collector-core/
        # by path, so the build needs the repo root in its context. Future
        # collectors that consume collector-core inherit this same value.
        "build_context_root": True,
    },
    "player-projections": {"port": 8001},
    "player-identity": {
        "port": 8002,
        "secret": "player-identity-collector-token",
        "lake_secret": "player-identity-lake-credentials",
        "build_context_root": True,
    },
}

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
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        sys.exit(result.returncode)


def deployment_exists(service: str) -> bool:
    result = subprocess.run(
        ["kubectl", "get", "deployment", service],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def run_piped(render_cmd: list[str], apply_cmd: list[str]) -> None:
    """Run `render_cmd`, piping its stdout into `apply_cmd`.

    Used for `kubectl create secret ... --dry-run=client -o yaml | kubectl
    apply -f -`, which is idempotent across every deploy after the first,
    unlike `kubectl create secret` alone.
    """
    rendered = subprocess.run(render_cmd, capture_output=True, text=True)
    if rendered.returncode != 0:
        print(rendered.stderr)
        sys.exit(rendered.returncode)

    applied = subprocess.run(apply_cmd, input=rendered.stdout, text=True)
    if applied.returncode != 0:
        sys.exit(applied.returncode)


def ensure_collector_secret(name: str) -> None:
    """Create or update the collector's bearer-token Secret."""
    print(f"\n$ kubectl create secret generic {name} | kubectl apply -f -")
    run_piped(
        [
            "kubectl", "create", "secret", "generic", name,
            f"--from-literal=token={LOCAL_DEV_TOKEN}",
            "--dry-run=client", "-o", "yaml",
        ],
        ["kubectl", "apply", "-f", "-"],
    )


def ensure_lake_secret(name: str) -> None:
    """Create or update the collector's object-store credentials Secret."""
    print(f"\n$ kubectl create secret generic {name} | kubectl apply -f -")
    run_piped(
        [
            "kubectl", "create", "secret", "generic", name,
            f"--from-literal=access-key-id={LOCAL_LAKE_ACCESS_KEY}",
            f"--from-literal=secret-access-key={LOCAL_LAKE_SECRET_KEY}",
            "--dry-run=client", "-o", "yaml",
        ],
        ["kubectl", "apply", "-f", "-"],
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/deploy-local.py <service-name>")
        print(f"Available: {', '.join(SERVICES)}")
        sys.exit(1)

    service = sys.argv[1]
    if service not in SERVICES:
        print(f"Unknown service: {service}")
        print(f"Available: {', '.join(SERVICES)}")
        sys.exit(1)

    port = SERVICES[service]["port"]

    # Checked before the upgrade, because the upgrade is what creates it.
    already_deployed = deployment_exists(service)

    if SERVICES[service].get("build_context_root"):
        run(
            ["docker", "build", "-f", f"services/{service}/Dockerfile",
             "-t", f"{service}:local", "."],
            env=_BUILD_ENV,
        )
    else:
        run(
            ["docker", "build", "-t", f"{service}:local", f"services/{service}/"],
            env=_BUILD_ENV,
        )
    run(["kind", "load", "docker-image", f"{service}:local", "--name", "foundry"])

    secret = SERVICES[service].get("secret")
    if secret:
        ensure_collector_secret(secret)

    lake_secret = SERVICES[service].get("lake_secret")
    if lake_secret:
        ensure_lake_secret(lake_secret)

    run([
        "helm", "upgrade", "--install", service,
        "helm/charts/generic-service",
        "-f", f"helm/values/{service}/values.yaml",
        "--set", f"image.repository={service}",
        "--set", "image.tag=local",
        "--set", "image.pullPolicy=Never",
    ])

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
        run(["kubectl", "rollout", "restart", f"deployment/{service}"])
    run(["kubectl", "rollout", "status", f"deployment/{service}", "--timeout=180s"])

    print(f"\n{'=' * 50}")
    print(f"Deployed {service}. To access it:\n")
    print(f"  kubectl port-forward svc/{service} {port}:{port}")
    print(f"  -> http://localhost:{port}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
