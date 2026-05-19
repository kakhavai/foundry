"""Local deploy: docker build → kind load → helm upgrade for a given service."""

import subprocess
import sys


def run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/deploy-local.py <service-name>")
        print("Example: python scripts/deploy-local.py github-stats")
        sys.exit(1)

    service = sys.argv[1]

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

    print(f"\nDone. Run: kubectl port-forward svc/{service} 8000:8000")


if __name__ == "__main__":
    main()
