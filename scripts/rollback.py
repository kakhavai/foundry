#!/usr/bin/env python3
"""
Roll back a service to a previous image tag.

Usage:
  python scripts/rollback.py <service> <target-tag>

Example:
  python scripts/rollback.py weather abc1234

The script updates infra/gitops/envs/local/<service>/values.yaml with the
target tag, commits the change, and pushes. Argo CD picks up the commit
and reconciles the cluster back to the target image.
"""

import subprocess
import sys
from pathlib import Path

GITOPS_ROOT = Path(__file__).parent.parent / "infra" / "gitops"


def validate_service(service: str, gitops_root: Path = GITOPS_ROOT) -> Path:
    """Return the values.yaml path for the service, or exit if not found."""
    svc_dir = gitops_root / "envs" / "local" / service
    if not svc_dir.exists():
        available = sorted(
            d.name for d in (gitops_root / "envs" / "local").iterdir() if d.is_dir()
        )
        print(f"Error: unknown service '{service}'")
        print(f"Available: {', '.join(available)}")
        sys.exit(1)
    return svc_dir / "values.yaml"


def write_tag(values_file: Path, tag: str) -> None:
    """Write a new image tag to the values file."""
    values_file.write_text(f'image:\n  tag: "{tag}"\n')


def git_commit_and_push(values_file: Path, service: str, tag: str) -> None:
    """Commit the updated values file and push."""
    def run(cmd: list) -> None:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"Error running: {' '.join(cmd)}")
            sys.exit(result.returncode)

    run(["git", "add", str(values_file)])
    run([
        "git", "commit",
        "-m", f"revert({service}): roll back to {tag}",
    ])
    run(["git", "push"])


def print_verification(service: str, tag: str) -> None:
    """Print post-rollback verification steps."""
    print(f"""
Rollback committed. Next steps:

1. Check Argo CD UI: http://localhost:8080
   Application '{service}' should show OutOfSync -> Syncing -> Synced+Healthy

2. Verify the running image tag:
   kubectl get deployment {service} -o jsonpath='{{.spec.template.spec.containers[0].image}}'
   Expected: ...:{tag}

3. Confirm the service is healthy:
   curl http://localhost:{_port_for(service)}/health
   Expected: {{"status": "ok"}}

4. If the rollback itself is unhealthy, check git log for the last known-good tag:
   git log --oneline infra/gitops/envs/local/{service}/values.yaml
""")


def _port_for(service: str) -> int:
    ports = {"weather": 8000, "player-projections": 8001}
    return ports.get(service, 8080)


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scripts/rollback.py <service> <target-tag>")
        print("Example: python scripts/rollback.py weather abc1234")
        sys.exit(1)

    service, tag = sys.argv[1], sys.argv[2]
    values_file = validate_service(service)

    print(f"Rolling back {service} to {tag}...")
    write_tag(values_file, tag)
    git_commit_and_push(values_file, service, tag)
    print_verification(service, tag)


if __name__ == "__main__":
    main()
