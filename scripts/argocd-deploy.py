"""
Manage the Argo CD lifecycle: install, verify, promote, watch, and access the UI.

Usage:
  python scripts/argocd-deploy.py install  --env local [--context <ctx>]
  python scripts/argocd-deploy.py verify   --env local [--context <ctx>]
  python scripts/argocd-deploy.py promote  <service> --from <env> --to <env>
  python scripts/argocd-deploy.py watch    <service> --env local [--timeout 180]
  python scripts/argocd-deploy.py ui       [--port 8080] [--context <ctx>]
  python scripts/argocd-deploy.py help     [<command>]
"""

import argparse
import base64
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
GITOPS_ROOT = ROOT / "infra" / "gitops"
ARGO_DIR = ROOT / "infra" / "argo"
ARGO_MANIFESTS_DIR = ROOT / "infra" / "gitops" / "argo"


# ── pure helpers ──────────────────────────────────────────────────────────────

def discover_services(env: str, gitops_root: Path = GITOPS_ROOT) -> list[str]:
    """List service names found under infra/gitops/envs/<env>/."""
    env_dir = gitops_root / "envs" / env
    if not env_dir.exists():
        return []
    return sorted(d.name for d in env_dir.iterdir() if d.is_dir())


def app_name(service: str, env: str) -> str:
    """Return the ArgoCD Application name for a service+env combo."""
    return service if env == "local" else f"{service}-{env}"


def write_tag(values_file: Path, tag: str) -> None:
    """Write image tag to a gitops values file, creating it if needed.

    Gitops env values files hold only the live image tag (`image.tag`); all
    other config lives in helm/values/<service>/values.yaml. If the file is
    missing or has no quoted tag line, it is (re)written to the canonical
    single-key form. This matches scripts/rollback.py and is safe because
    these files never carry other keys.
    """
    if values_file.exists():
        text = values_file.read_text()
        patched = re.sub(r'(tag:\s*")[^"]*(")', rf'\g<1>{tag}\2', text)
        if patched != text:
            values_file.write_text(patched)
            return
    values_file.parent.mkdir(parents=True, exist_ok=True)
    values_file.write_text(f'image:\n  tag: "{tag}"\n')


def read_tag(values_file: Path) -> str:
    """Read the current image tag from a gitops values file."""
    if not values_file.exists():
        print(f"Error: values file not found: {values_file}")
        sys.exit(1)
    text = values_file.read_text()
    m = re.search(r'tag:\s*"([^"]+)"', text)
    if not m:
        print(f"Error: no image.tag found in {values_file}")
        sys.exit(1)
    return m.group(1)


def argo_values_file(env: str, argo_dir: Path = ARGO_DIR) -> Path:
    """Return the helmfile values file for the given env (env-specific or default)."""
    env_specific = argo_dir / f"values-{env}.yaml"
    return env_specific if env_specific.exists() else argo_dir / "values.yaml"


# ── subprocess helpers ────────────────────────────────────────────────────────

def run(cmd: list, cwd: Path | None = None) -> None:
    """Run a subprocess, print the command, exit on non-zero."""
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def _kubectl_cmd(args: tuple, context: str | None) -> list[str]:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    return cmd + list(args)


def kubectl_run(*args: str, context: str | None = None) -> None:
    """Run kubectl, exit on non-zero."""
    run(_kubectl_cmd(args, context))


def kubectl_capture(*args: str, context: str | None = None) -> tuple[int, str]:
    """Run kubectl, return (returncode, stdout). Never exits."""
    cmd = _kubectl_cmd(args, context)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip()


def helmfile_run(*args: str, context: str | None = None, cwd: Path | None = None) -> None:
    """Run helmfile, passing --kube-context if provided."""
    cmd = ["helmfile"]
    if context:
        cmd += ["--kube-context", context]
    run(cmd + list(args), cwd=cwd)


def argo_password(context: str | None = None) -> str:
    """Decode the ArgoCD initial admin password from the cluster secret."""
    _, out = kubectl_capture(
        "get", "secret", "argocd-initial-admin-secret",
        "-n", "argocd",
        "-o", "jsonpath={.data.password}",
        context=context,
    )
    if not out:
        return "<not found>"
    return base64.b64decode(out).decode().strip()


# ── polling ───────────────────────────────────────────────────────────────────

def poll_applications(
    services: list[str],
    env: str,
    context: str | None,
    timeout: int,
    poll_interval: int = 3,
) -> bool:
    """Poll Application sync+health until all Synced+Healthy or timeout."""
    names = [app_name(s, env) for s in services]
    deadline = time.time() + timeout
    while time.time() < deadline:
        all_healthy = True
        for name in names:
            _, out = kubectl_capture(
                "get", "application", name,
                "-n", "argocd",
                "-o", "jsonpath={.status.sync.status},{.status.health.status}",
                context=context,
            )
            sync, _, health = out.partition(",")
            if sync != "Synced" or health != "Healthy":
                all_healthy = False
                print(f"  {name}: {sync or '?'}/{health or '?'}")
        if all_healthy:
            return True
        time.sleep(poll_interval)
    return False


# ── git + manifest helpers ────────────────────────────────────────────────────

def git_commit_and_push(files: list[Path], message: str) -> None:
    """Stage the given files, commit with message, and push."""
    for f in files:
        run(["git", "add", str(f)])
    run(["git", "commit", "-m", message])
    run(["git", "push"])


def ensure_application_manifest(
    service: str,
    env: str,
    argo_manifests_dir: Path = ARGO_MANIFESTS_DIR,
) -> Path | None:
    """Create an env-specific Application manifest if it doesn't exist.

    Returns the new manifest path, or None if nothing was created.
    """
    if env == "local":
        return None

    manifest_path = argo_manifests_dir / f"{service}-{env}.yaml"
    if manifest_path.exists():
        return None

    source = argo_manifests_dir / f"{service}.yaml"
    if not source.exists():
        print(f"Error: source manifest not found: {source}")
        sys.exit(1)

    text = source.read_text()
    text = re.sub(
        r'^(  name:\s*)' + re.escape(service) + r'$',
        rf'\g<1>{service}-{env}',
        text,
        flags=re.MULTILINE,
    )
    text = text.replace("/infra/gitops/envs/local/", f"/infra/gitops/envs/{env}/")
    manifest_path.write_text(text)
    return manifest_path
