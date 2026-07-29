"""Local deploy: docker build → kind load → helm upgrade for a given service."""

import os
import subprocess
import sys

SERVICES = {
    "weather": {"port": 8000, "secret": "weather-collector-token"},
    "player-projections": {"port": 8001},
}

# Kind-only. A real token is created out of band and never enters Git; on EKS
# the Secret is backed by AWS Secrets Manager.
LOCAL_DEV_TOKEN = "local-dev-token"


_BUILD_ENV = {**os.environ, "DOCKER_BUILDKIT": "1"}


def run(cmd: list[str], env: dict | None = None) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        sys.exit(result.returncode)


def ensure_collector_secret(name: str) -> None:
    """Create or update the collector's bearer-token Secret.

    Rendered then applied rather than `kubectl create secret` alone, which
    fails on every deploy after the first.
    """
    print(f"\n$ kubectl create secret generic {name} | kubectl apply -f -")
    rendered = subprocess.run(
        [
            "kubectl", "create", "secret", "generic", name,
            f"--from-literal=token={LOCAL_DEV_TOKEN}",
            "--dry-run=client", "-o", "yaml",
        ],
        capture_output=True,
        text=True,
    )
    if rendered.returncode != 0:
        print(rendered.stderr)
        sys.exit(rendered.returncode)

    applied = subprocess.run(
        ["kubectl", "apply", "-f", "-"], input=rendered.stdout, text=True
    )
    if applied.returncode != 0:
        sys.exit(applied.returncode)


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

    run(["docker", "build", "-t", f"{service}:local", f"services/{service}/"], env=_BUILD_ENV)
    run(["kind", "load", "docker-image", f"{service}:local", "--name", "foundry"])

    secret = SERVICES[service].get("secret")
    if secret:
        ensure_collector_secret(secret)

    run([
        "helm", "upgrade", "--install", service,
        "helm/charts/generic-service",
        "-f", f"helm/values/{service}/values.yaml",
        "--set", f"image.repository={service}",
        "--set", "image.tag=local",
        "--set", "image.pullPolicy=Never",
    ])

    print(f"\n{'=' * 50}")
    print(f"Deployed {service}. To access it:\n")
    print(f"  kubectl port-forward svc/{service} {port}:{port}")
    print(f"  -> http://localhost:{port}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
