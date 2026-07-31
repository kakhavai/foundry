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

import re
import subprocess
import sys
from pathlib import Path

GITOPS_ROOT = Path(__file__).parent.parent / "infra" / "gitops"


def validate_service(service: str, gitops_root: Path = GITOPS_ROOT) -> Path:
    """Return the values.yaml path for the service, or exit if not found."""
    local_dir = gitops_root / "envs" / "local"
    svc_dir = local_dir / service
    if not svc_dir.exists():
        available = (
            sorted(d.name for d in local_dir.iterdir() if d.is_dir())
            if local_dir.exists()
            else []
        )
        print(f"Error: unknown service '{service}'")
        if available:
            print(f"Available: {', '.join(available)}")
        else:
            print(f"GitOps root not found: {local_dir}")
        sys.exit(1)
    return svc_dir / "values.yaml"


def write_tag(values_file: Path, tag: str) -> None:
    """Write a new image tag to the values file, preserving other keys."""
    if values_file.exists():
        text = values_file.read_text()
        patched = re.sub(r'(tag:\s*")[^"]*(")', rf"\g<1>{tag}\2", text)
        if patched != text:
            values_file.write_text(patched)
            return
    # Fallback: file does not exist or has no tag line yet
    values_file.write_text(f'image:\n  tag: "{tag}"\n')


def git_commit_and_push(values_file: Path, service: str, tag: str) -> None:
    """Commit the updated values file and push."""

    def run(cmd: list) -> None:
        # check=False: the failure is reported with the command that caused it
        # and the child's own exit code is propagated, which a raised
        # CalledProcessError would replace with a traceback and exit 1.
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(f"Error running: {' '.join(cmd)}")
            sys.exit(result.returncode)

    run(["git", "add", str(values_file)])
    run(
        [
            "git",
            "commit",
            "-m",
            f"revert({service}): roll back to {tag}",
        ]
    )
    run(["git", "push"])


def print_verification(service: str, tag: str) -> None:
    """Print post-rollback verification steps."""
    # Held in a name only so the source line stays inside the line limit; the
    # printed text is unchanged, and it is meant to be copy-pasteable.
    image_jsonpath = "{.spec.template.spec.containers[0].image}"
    print(f"""
Rollback committed. Next steps:

1. Check Argo CD UI: http://localhost:8080
   Application '{service}' should show OutOfSync -> Syncing -> Synced+Healthy

2. Verify the running image tag:
   kubectl get deployment {service} -o jsonpath='{image_jsonpath}'
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
