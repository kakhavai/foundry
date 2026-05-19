"""Local deploy: docker build → kind load → helm upgrade for a given service."""

import subprocess
import sys

SERVICES = {
    "github-stats": {"port": 8000},
}


def run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


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

    run(["docker", "build", "-t", f"{service}:local", f"services/{service}/"])
    run(["kind", "load", "docker-image", f"{service}:local", "--name", "foundry"])
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
    print(f"  → http://localhost:{port}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
