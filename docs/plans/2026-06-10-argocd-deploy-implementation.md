# ArgoCD Deploy Script — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/argocd-deploy.py` — a standalone sub-command CLI that owns the full Argo CD lifecycle: install, verify, promote, watch, and UI access; plus update the README.

**Architecture:** Single Python file using only stdlib + subprocess. Pure helper functions are tested with `tmp_path`; subprocess-touching functions are tested with `unittest.mock.patch`. Each sub-command (`install`, `verify`, `promote`, `watch`, `ui`, `help`) is a standalone function wired via argparse.

**Tech Stack:** Python 3.12+, argparse, subprocess, unittest.mock, pytest; kubectl, helmfile (called as subprocesses).

**Spec:** `docs/plans/2026-06-10-argocd-deploy-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/argocd-deploy.py` | Create | Full CLI: helpers + sub-commands + argparse wiring |
| `tests/test_argocd_deploy.py` | Create | Unit tests for all testable functions |
| `README.md` | Modify | Replace manual ArgoCD steps with argocd-deploy.py docs; add verify note near rollback |

---

## Task 1: Scaffold + pure helpers

**Files:**
- Create: `scripts/argocd-deploy.py`
- Create: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_argocd_deploy.py`:

```python
"""Tests for scripts/argocd-deploy.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import base64

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import argocd_deploy as ad


# ── discover_services ─────────────────────────────────────────────────────────

def test_discover_services_returns_sorted_dirs(tmp_path):
    for name in ["weather", "player-projections"]:
        (tmp_path / "envs" / "local" / name).mkdir(parents=True)
    assert ad.discover_services("local", gitops_root=tmp_path) == [
        "player-projections",
        "weather",
    ]


def test_discover_services_missing_env_returns_empty(tmp_path):
    assert ad.discover_services("staging", gitops_root=tmp_path) == []


def test_discover_services_ignores_files(tmp_path):
    env_dir = tmp_path / "envs" / "local"
    env_dir.mkdir(parents=True)
    (env_dir / "weather").mkdir()
    (env_dir / ".gitkeep").write_text("")
    assert ad.discover_services("local", gitops_root=tmp_path) == ["weather"]


# ── app_name ──────────────────────────────────────────────────────────────────

def test_app_name_local():
    assert ad.app_name("weather", "local") == "weather"


def test_app_name_staging():
    assert ad.app_name("weather", "staging") == "weather-staging"


def test_app_name_prod():
    assert ad.app_name("player-projections", "prod") == "player-projections-prod"


# ── write_tag ─────────────────────────────────────────────────────────────────

def test_write_tag_creates_new_file(tmp_path):
    f = tmp_path / "values.yaml"
    ad.write_tag(f, "abc123")
    assert 'tag: "abc123"' in f.read_text()


def test_write_tag_updates_existing(tmp_path):
    f = tmp_path / "values.yaml"
    f.write_text('image:\n  tag: "old"\n')
    ad.write_tag(f, "new456")
    text = f.read_text()
    assert 'tag: "new456"' in text
    assert "old" not in text


def test_write_tag_creates_parent_dirs(tmp_path):
    f = tmp_path / "envs" / "staging" / "weather" / "values.yaml"
    ad.write_tag(f, "xyz")
    assert f.exists()


# ── read_tag ──────────────────────────────────────────────────────────────────

def test_read_tag_returns_value(tmp_path):
    f = tmp_path / "values.yaml"
    f.write_text('image:\n  tag: "abc123"\n')
    assert ad.read_tag(f) == "abc123"


def test_read_tag_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        ad.read_tag(tmp_path / "nonexistent.yaml")


def test_read_tag_no_tag_key_exits(tmp_path):
    f = tmp_path / "values.yaml"
    f.write_text("image:\n  repository: weather\n")
    with pytest.raises(SystemExit):
        ad.read_tag(f)


# ── argo_values_file ──────────────────────────────────────────────────────────

def test_argo_values_file_env_specific_when_exists(tmp_path):
    (tmp_path / "values-staging.yaml").write_text("")
    assert ad.argo_values_file("staging", argo_dir=tmp_path) == tmp_path / "values-staging.yaml"


def test_argo_values_file_falls_back_to_default(tmp_path):
    (tmp_path / "values.yaml").write_text("")
    assert ad.argo_values_file("prod", argo_dir=tmp_path) == tmp_path / "values.yaml"
```

- [ ] **Step 2: Run tests — confirm they all fail**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: `ModuleNotFoundError: No module named 'argocd_deploy'`

- [ ] **Step 3: Create `scripts/argocd-deploy.py` with scaffold and pure helpers**

```python
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
    """Write image tag to a gitops values file, creating it if needed."""
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
```

- [ ] **Step 4: Run tests — confirm they pass**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 16 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): scaffold script with pure helpers and tests"
```

---

## Task 2: Subprocess helpers

**Files:**
- Modify: `scripts/argocd-deploy.py`
- Modify: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Add failing tests** (append to `tests/test_argocd_deploy.py`)

```python
# ── subprocess helpers ────────────────────────────────────────────────────────

def test_kubectl_capture_passes_context():
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(returncode=0, stdout="ok")
        rc, out = ad.kubectl_capture("get", "pods", context="my-ctx")
    cmd = mock.call_args[0][0]
    assert "--context" in cmd
    assert "my-ctx" in cmd
    assert rc == 0
    assert out == "ok"


def test_kubectl_capture_no_context_omits_flag():
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(returncode=0, stdout="result")
        ad.kubectl_capture("get", "pods", context=None)
    cmd = mock.call_args[0][0]
    assert "--context" not in cmd


def test_argo_password_decodes_base64():
    encoded = base64.b64encode(b"supersecret").decode()
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(returncode=0, stdout=encoded)
        result = ad.argo_password(context=None)
    assert result == "supersecret"


def test_argo_password_returns_placeholder_on_failure():
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(returncode=1, stdout="")
        result = ad.argo_password(context=None)
    assert result == "<not found>"
```

- [ ] **Step 2: Run tests — confirm new tests fail**

```
python -m pytest tests/test_argocd_deploy.py -v -k "subprocess or kubectl_capture or argo_password"
```

Expected: `AttributeError: module 'argocd_deploy' has no attribute 'kubectl_capture'`

- [ ] **Step 3: Append subprocess helpers to `scripts/argocd-deploy.py`**

```python
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
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 20 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): add subprocess helpers (kubectl_capture, helmfile_run, argo_password)"
```

---

## Task 3: `poll_applications`

**Files:**
- Modify: `scripts/argocd-deploy.py`
- Modify: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Add failing tests** (append to `tests/test_argocd_deploy.py`)

```python
# ── poll_applications ─────────────────────────────────────────────────────────

def test_poll_applications_returns_true_when_all_healthy():
    with patch("argocd_deploy.kubectl_capture", return_value=(0, "Synced,Healthy")):
        with patch("time.sleep"):
            result = ad.poll_applications(["weather"], "local", None, timeout=30, poll_interval=1)
    assert result is True


def test_poll_applications_returns_false_on_timeout():
    call_count = 0
    def fake_time():
        nonlocal call_count
        call_count += 1
        return 0 if call_count == 1 else 31  # immediate timeout on second call

    with patch("argocd_deploy.kubectl_capture", return_value=(0, "OutOfSync,Progressing")):
        with patch("time.sleep"):
            with patch("time.time", side_effect=fake_time):
                result = ad.poll_applications(["weather"], "local", None, timeout=30, poll_interval=1)
    assert result is False


def test_poll_applications_all_services_must_be_healthy():
    responses = iter([(0, "Synced,Healthy"), (0, "OutOfSync,Progressing")])
    call_count = 0
    def fake_time():
        nonlocal call_count
        call_count += 1
        return 0 if call_count <= 3 else 31

    with patch("argocd_deploy.kubectl_capture", side_effect=lambda *a, **kw: next(responses)):
        with patch("time.sleep"):
            with patch("time.time", side_effect=fake_time):
                result = ad.poll_applications(
                    ["weather", "player-projections"], "local", None, timeout=30, poll_interval=1
                )
    assert result is False


def test_poll_applications_uses_app_name_per_env():
    captured_names = []
    def fake_capture(*args, **kwargs):
        captured_names.append(args[2])
        return (0, "Synced,Healthy")

    with patch("argocd_deploy.kubectl_capture", side_effect=fake_capture):
        with patch("time.sleep"):
            ad.poll_applications(["weather"], "staging", None, timeout=30, poll_interval=1)
    assert captured_names[0] == "weather-staging"
```

- [ ] **Step 2: Run tests — confirm new tests fail**

```
python -m pytest tests/test_argocd_deploy.py -v -k "poll_applications"
```

Expected: `AttributeError: module 'argocd_deploy' has no attribute 'poll_applications'`

- [ ] **Step 3: Append `poll_applications` to `scripts/argocd-deploy.py`**

```python
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
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 24 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): add poll_applications with timeout"
```

---

## Task 4: `git_commit_and_push` + `ensure_application_manifest`

**Files:**
- Modify: `scripts/argocd-deploy.py`
- Modify: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Add failing tests** (append to `tests/test_argocd_deploy.py`)

```python
# ── git_commit_and_push ───────────────────────────────────────────────────────

def test_git_commit_and_push_stages_commits_and_pushes(tmp_path):
    f = tmp_path / "values.yaml"
    f.write_text('image:\n  tag: "abc"\n')
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(returncode=0)
        ad.git_commit_and_push([f], "chore: update tag")
    cmds = [mock.call_args_list[i][0][0] for i in range(len(mock.call_args_list))]
    assert any("add" in c for c in cmds)
    assert any("commit" in c for c in cmds)
    assert any("push" in c for c in cmds)
    assert any("chore: update tag" in str(c) for c in cmds)


def test_git_commit_and_push_exits_on_commit_failure(tmp_path):
    f = tmp_path / "values.yaml"
    f.write_text('image:\n  tag: "abc"\n')
    with patch("subprocess.run") as mock:
        mock.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git commit fails
        ]
        with pytest.raises(SystemExit):
            ad.git_commit_and_push([f], "chore: update tag")


# ── ensure_application_manifest ───────────────────────────────────────────────

def test_ensure_application_manifest_local_returns_none(tmp_path):
    result = ad.ensure_application_manifest("weather", "local", argo_manifests_dir=tmp_path)
    assert result is None


def test_ensure_application_manifest_existing_returns_none(tmp_path):
    (tmp_path / "weather-staging.yaml").write_text("existing")
    result = ad.ensure_application_manifest("weather", "staging", argo_manifests_dir=tmp_path)
    assert result is None
    assert (tmp_path / "weather-staging.yaml").read_text() == "existing"


def test_ensure_application_manifest_creates_env_manifest(tmp_path):
    (tmp_path / "weather.yaml").write_text(
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Application\n"
        "metadata:\n"
        "  name: weather\n"
        "  namespace: argocd\n"
        "spec:\n"
        "  source:\n"
        "    helm:\n"
        "      valueFiles:\n"
        "        - /infra/gitops/envs/local/weather/values.yaml\n"
    )
    result = ad.ensure_application_manifest("weather", "staging", argo_manifests_dir=tmp_path)
    assert result == tmp_path / "weather-staging.yaml"
    content = result.read_text()
    assert "name: weather-staging" in content
    assert "/infra/gitops/envs/staging/weather/values.yaml" in content
    assert "/infra/gitops/envs/local/" not in content


def test_ensure_application_manifest_missing_source_exits(tmp_path):
    with pytest.raises(SystemExit):
        ad.ensure_application_manifest("unknown-svc", "staging", argo_manifests_dir=tmp_path)
```

- [ ] **Step 2: Run tests — confirm new tests fail**

```
python -m pytest tests/test_argocd_deploy.py -v -k "git_commit or ensure_application"
```

Expected: `AttributeError: module 'argocd_deploy' has no attribute 'git_commit_and_push'`

- [ ] **Step 3: Append functions to `scripts/argocd-deploy.py`**

```python
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
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 32 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): add git_commit_and_push and ensure_application_manifest"
```

---

## Task 5: `cmd_install`

**Files:**
- Modify: `scripts/argocd-deploy.py`
- Modify: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Add failing tests** (append to `tests/test_argocd_deploy.py`)

```python
# ── cmd_install ───────────────────────────────────────────────────────────────

def _make_install_args(env="local", context=None):
    return type("Args", (), {"env": env, "context": context})()


def test_cmd_install_calls_helmfile_and_kubectl(tmp_path):
    with patch("argocd_deploy.helmfile_run") as mock_helm, \
         patch("argocd_deploy.kubectl_run") as mock_kubectl, \
         patch("argocd_deploy.poll_applications", return_value=True), \
         patch("argocd_deploy.discover_services", return_value=["weather"]), \
         patch("argocd_deploy.argo_password", return_value="pwd"), \
         patch("argocd_deploy.argo_values_file", return_value=tmp_path / "values.yaml"):
        ad.cmd_install(_make_install_args())
    assert mock_helm.call_count >= 2  # repos + apply
    kubectl_calls = [str(c) for c in mock_kubectl.call_args_list]
    assert any("wait" in c for c in kubectl_calls)
    assert any("apply" in c for c in kubectl_calls)


def test_cmd_install_exits_on_sync_timeout():
    with patch("argocd_deploy.helmfile_run"), \
         patch("argocd_deploy.kubectl_run"), \
         patch("argocd_deploy.poll_applications", return_value=False), \
         patch("argocd_deploy.discover_services", return_value=["weather"]), \
         patch("argocd_deploy.argo_values_file", return_value=Path("values.yaml")):
        with pytest.raises(SystemExit):
            ad.cmd_install(_make_install_args())
```

- [ ] **Step 2: Run tests — confirm new tests fail**

```
python -m pytest tests/test_argocd_deploy.py -v -k "cmd_install"
```

Expected: `AttributeError: module 'argocd_deploy' has no attribute 'cmd_install'`

- [ ] **Step 3: Append `cmd_install` to `scripts/argocd-deploy.py`**

```python
# ── sub-commands ──────────────────────────────────────────────────────────────

def cmd_install(args) -> None:
    ctx = args.context
    env = args.env

    print(f"\nInstalling Argo CD for env '{env}'...")
    values = argo_values_file(env)
    helmfile_run("repos", context=ctx, cwd=ARGO_DIR)
    helmfile_run("apply", "--values", str(values), context=ctx, cwd=ARGO_DIR)

    print("\nWaiting for argocd-server to be ready...")
    kubectl_run(
        "wait", "--for=condition=available",
        "deployment/argocd-server",
        "-n", "argocd",
        "--timeout=180s",
        context=ctx,
    )

    print("\nApplying app-of-apps...")
    kubectl_run(
        "apply", "-f", str(ROOT / "infra/gitops/argo/app-of-apps.yaml"),
        context=ctx,
    )

    print("\nWaiting for all Applications to be Synced + Healthy...")
    services = discover_services(env)
    if services:
        ok = poll_applications(services, env, ctx, timeout=300)
        if not ok:
            print("Timeout: not all Applications reached Synced+Healthy within 300s")
            sys.exit(1)
    else:
        print(f"  No services found in infra/gitops/envs/{env}/ — skipping sync wait.")

    pwd = argo_password(ctx)
    print(f"\n{'=' * 50}")
    print(f"Argo CD installed. Admin password: {pwd}")
    print("Run 'python scripts/argocd-deploy.py ui' to access the UI at http://localhost:8080")
    print("=" * 50)
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 34 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): implement cmd_install"
```

---

## Task 6: `cmd_verify`

**Files:**
- Modify: `scripts/argocd-deploy.py`
- Modify: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Add failing tests** (append to `tests/test_argocd_deploy.py`)

```python
# ── cmd_verify ────────────────────────────────────────────────────────────────

def _make_verify_args(env="local", context=None):
    return type("Args", (), {"env": env, "context": context})()


def test_cmd_verify_passes_when_all_healthy():
    pod_output = "argocd-server-xxx   1/1   Running   0   5m"
    app_output = "Synced,Healthy,2026-06-10T00:00:00Z"
    with patch("argocd_deploy.kubectl_capture") as mock_capture, \
         patch("argocd_deploy.discover_services", return_value=["weather"]):
        mock_capture.return_value = (0, pod_output)
        # First call: pods. Subsequent calls: annotate (no-op rc) + get application.
        mock_capture.side_effect = [
            (0, pod_output),           # get pods
            (0, ""),                   # annotate refresh
            (0, app_output),           # get application status
        ]
        ad.cmd_verify(_make_verify_args())  # should not raise


def test_cmd_verify_exits_when_pods_not_running():
    with patch("argocd_deploy.kubectl_capture") as mock_capture:
        mock_capture.return_value = (0, "argocd-server   0/1   Pending   0   1m")
        with pytest.raises(SystemExit):
            ad.cmd_verify(_make_verify_args())


def test_cmd_verify_exits_when_kubectl_unreachable():
    with patch("argocd_deploy.kubectl_capture", return_value=(1, "")):
        with pytest.raises(SystemExit):
            ad.cmd_verify(_make_verify_args())


def test_cmd_verify_exits_when_app_not_synced():
    pod_output = "argocd-server-xxx   1/1   Running   0   5m"
    with patch("argocd_deploy.kubectl_capture") as mock_capture, \
         patch("argocd_deploy.discover_services", return_value=["weather"]):
        mock_capture.side_effect = [
            (0, pod_output),
            (0, ""),                            # annotate
            (0, "OutOfSync,Degraded,"),         # get application
        ]
        with pytest.raises(SystemExit):
            ad.cmd_verify(_make_verify_args())
```

- [ ] **Step 2: Run tests — confirm new tests fail**

```
python -m pytest tests/test_argocd_deploy.py -v -k "cmd_verify"
```

Expected: `AttributeError: module 'argocd_deploy' has no attribute 'cmd_verify'`

- [ ] **Step 3: Append `cmd_verify` to `scripts/argocd-deploy.py`**

```python
def cmd_verify(args) -> None:
    ctx = args.context
    env = args.env

    print(f"\nVerifying Argo CD ({env})...")

    rc, out = kubectl_capture("get", "pods", "-n", "argocd", "--no-headers", context=ctx)
    if rc != 0:
        print("Error: could not list argocd pods — is the cluster reachable?")
        sys.exit(1)

    pod_lines = [ln for ln in out.splitlines() if ln.strip()]
    not_running = [ln for ln in pod_lines if "Running" not in ln]
    if not_running:
        print("Some Argo CD pods are not Running:")
        for ln in not_running:
            print(f"  {ln}")
        sys.exit(1)
    print(f"  Pods: {len(pod_lines)} Running")

    services = discover_services(env)
    if not services:
        print(f"  No services in infra/gitops/envs/{env}/ — nothing to check.")
        return

    failed = []
    print(f"\n  {'Application':<30} {'Sync':<12} {'Health':<12} Last Sync")
    print(f"  {'-' * 70}")
    for svc in services:
        name = app_name(svc, env)
        kubectl_capture(
            "annotate", "application", name,
            "-n", "argocd",
            "argocd.argoproj.io/refresh=normal",
            "--overwrite",
            context=ctx,
        )
        _, status_out = kubectl_capture(
            "get", "application", name,
            "-n", "argocd",
            "-o", "jsonpath={.status.sync.status},{.status.health.status},{.status.operationState.finishedAt}",
            context=ctx,
        )
        parts = (status_out + ",,").split(",")
        sync, health, last_sync = parts[0], parts[1], parts[2]
        print(f"  {name:<30} {sync:<12} {health:<12} {last_sync}")
        if sync != "Synced" or health != "Healthy":
            failed.append(name)

    if failed:
        print(f"\nNot Synced+Healthy: {', '.join(failed)}")
        sys.exit(1)
    print("\nAll Applications: Synced + Healthy")
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 38 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): implement cmd_verify"
```

---

## Task 7: `cmd_promote`

**Files:**
- Modify: `scripts/argocd-deploy.py`
- Modify: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Add failing tests** (append to `tests/test_argocd_deploy.py`)

```python
# ── cmd_promote ───────────────────────────────────────────────────────────────

def _make_promote_args(service="weather", from_env="local", to_env="staging", context=None, timeout=300):
    return type("Args", (), {
        "service": service, "from_env": from_env, "to_env": to_env,
        "context": context, "timeout": timeout,
    })()


def test_cmd_promote_copies_tag_and_commits(tmp_path):
    from_file = tmp_path / "envs" / "local" / "weather" / "values.yaml"
    from_file.parent.mkdir(parents=True)
    from_file.write_text('image:\n  tag: "sha123"\n')

    with patch("argocd_deploy.GITOPS_ROOT", tmp_path), \
         patch("argocd_deploy.ARGO_MANIFESTS_DIR", tmp_path / "argo"), \
         patch("argocd_deploy.git_commit_and_push") as mock_git, \
         patch("argocd_deploy.poll_applications", return_value=True), \
         patch("argocd_deploy.ensure_application_manifest", return_value=None):
        ad.cmd_promote(_make_promote_args())

    mock_git.assert_called_once()
    committed_files = mock_git.call_args[0][0]
    msg = mock_git.call_args[0][1]
    assert any("staging" in str(f) for f in committed_files)
    assert "sha123" in msg

    to_file = tmp_path / "envs" / "staging" / "weather" / "values.yaml"
    assert to_file.exists()
    assert 'tag: "sha123"' in to_file.read_text()


def test_cmd_promote_exits_on_sync_timeout(tmp_path):
    from_file = tmp_path / "envs" / "local" / "weather" / "values.yaml"
    from_file.parent.mkdir(parents=True)
    from_file.write_text('image:\n  tag: "sha123"\n')

    with patch("argocd_deploy.GITOPS_ROOT", tmp_path), \
         patch("argocd_deploy.ARGO_MANIFESTS_DIR", tmp_path / "argo"), \
         patch("argocd_deploy.git_commit_and_push"), \
         patch("argocd_deploy.poll_applications", return_value=False), \
         patch("argocd_deploy.ensure_application_manifest", return_value=None):
        with pytest.raises(SystemExit):
            ad.cmd_promote(_make_promote_args())
```

- [ ] **Step 2: Run tests — confirm new tests fail**

```
python -m pytest tests/test_argocd_deploy.py -v -k "cmd_promote"
```

Expected: `AttributeError: module 'argocd_deploy' has no attribute 'cmd_promote'`

- [ ] **Step 3: Append `cmd_promote` to `scripts/argocd-deploy.py`**

```python
def cmd_promote(args) -> None:
    service = args.service
    from_env = args.from_env
    to_env = args.to_env
    ctx = args.context

    from_file = GITOPS_ROOT / "envs" / from_env / service / "values.yaml"
    to_file = GITOPS_ROOT / "envs" / to_env / service / "values.yaml"

    tag = read_tag(from_file)
    print(f"\nPromoting {service}: {from_env} -> {to_env} @ {tag}")

    manifest = ensure_application_manifest(service, to_env)
    write_tag(to_file, tag)

    files_to_commit: list[Path] = [to_file]
    if manifest:
        files_to_commit.append(manifest)
    git_commit_and_push(
        files_to_commit,
        f"chore(gitops): promote {service} from {from_env} to {to_env} @ {tag}",
    )

    print(f"\nWaiting for {app_name(service, to_env)} to sync in {to_env}...")
    ok = poll_applications([service], to_env, ctx, timeout=args.timeout)
    if not ok:
        print(f"Timeout: {app_name(service, to_env)} did not reach Synced+Healthy within {args.timeout}s")
        sys.exit(1)
    print(f"\nDone. {service} @ {tag} is live in {to_env}.")
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 40 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): implement cmd_promote"
```

---

## Task 8: `cmd_watch` and `cmd_ui`

**Files:**
- Modify: `scripts/argocd-deploy.py`
- Modify: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Add failing tests** (append to `tests/test_argocd_deploy.py`)

```python
# ── cmd_watch ─────────────────────────────────────────────────────────────────

def _make_watch_args(service="weather", env="local", context=None, timeout=180):
    return type("Args", (), {"service": service, "env": env, "context": context, "timeout": timeout})()


def test_cmd_watch_exits_zero_when_healthy():
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run, \
         patch("argocd_deploy.kubectl_capture", return_value=(0, "Synced,Healthy")):
        ad.cmd_watch(_make_watch_args())
    rollout_calls = [c for c in mock_run.call_args_list if "rollout" in str(c)]
    assert len(rollout_calls) >= 1


def test_cmd_watch_exits_nonzero_when_app_not_healthy():
    with patch("subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("argocd_deploy.kubectl_capture", return_value=(0, "OutOfSync,Degraded")):
        with pytest.raises(SystemExit):
            ad.cmd_watch(_make_watch_args())


# ── cmd_ui ────────────────────────────────────────────────────────────────────

def _make_ui_args(context=None, port=8080):
    return type("Args", (), {"context": context, "port": port})()


def test_cmd_ui_starts_portforward_and_prints_credentials():
    with patch("subprocess.Popen") as mock_popen, \
         patch("argocd_deploy.argo_password", return_value="testpwd"), \
         patch("time.sleep", side_effect=KeyboardInterrupt):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        try:
            ad.cmd_ui(_make_ui_args())
        except SystemExit:
            pass
    mock_popen.assert_called_once()
    pf_cmd = mock_popen.call_args[0][0]
    assert "port-forward" in pf_cmd
    assert "argocd-server" in pf_cmd
    mock_proc.terminate.assert_called_once()
```

- [ ] **Step 2: Run tests — confirm new tests fail**

```
python -m pytest tests/test_argocd_deploy.py -v -k "cmd_watch or cmd_ui"
```

Expected: `AttributeError: module 'argocd_deploy' has no attribute 'cmd_watch'`

- [ ] **Step 3: Append `cmd_watch` and `cmd_ui` to `scripts/argocd-deploy.py`**

```python
def cmd_watch(args) -> None:
    service = args.service
    env = args.env
    ctx = args.context

    print(f"\nWatching {service} rollout in '{env}'...")

    print("\n--- kubectl rollout status ---")
    rollout_cmd = _kubectl_cmd(
        ("rollout", "status", f"deployment/{service}", "-n", "default", f"--timeout={args.timeout}s"),
        ctx,
    )
    subprocess.run(rollout_cmd)

    print("\n--- Application status ---")
    name = app_name(service, env)
    _, out = kubectl_capture(
        "get", "application", name,
        "-n", "argocd",
        "-o", "jsonpath={.status.sync.status},{.status.health.status}",
        context=ctx,
    )
    sync, _, health = out.partition(",")
    print(f"  {name}: {sync or '?'}/{health or '?'}")
    if sync != "Synced" or health != "Healthy":
        print("Application is not Synced+Healthy.")
        sys.exit(1)
    print("Done.")


def cmd_ui(args) -> None:
    ctx = args.context
    port = args.port

    pf_cmd = _kubectl_cmd(
        ("port-forward", "svc/argocd-server", "-n", "argocd", f"{port}:80"),
        ctx,
    )
    proc = subprocess.Popen(pf_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

    pwd = argo_password(ctx)
    print(f"\n{'=' * 50}")
    print(f"Argo CD UI:  http://localhost:{port}")
    print(f"Username:    admin")
    print(f"Password:    {pwd}")
    print("=" * 50)
    print("Press Ctrl+C to stop the port-forward.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping port-forward...")
        proc.terminate()
        print("Done.")
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 45 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): implement cmd_watch and cmd_ui"
```

---

## Task 9: CLI wiring (`build_parser`, `cmd_help`, `main`)

**Files:**
- Modify: `scripts/argocd-deploy.py`
- Modify: `tests/test_argocd_deploy.py`

- [ ] **Step 1: Add failing tests** (append to `tests/test_argocd_deploy.py`)

```python
# ── CLI wiring ────────────────────────────────────────────────────────────────

def test_parser_install_defaults():
    parser = ad.build_parser()
    args = parser.parse_args(["install"])
    assert args.env == "local"
    assert args.context is None


def test_parser_install_env_and_context():
    parser = ad.build_parser()
    args = parser.parse_args(["install", "--env", "staging", "--context", "my-ctx"])
    assert args.env == "staging"
    assert args.context == "my-ctx"


def test_parser_promote_required_args():
    parser = ad.build_parser()
    args = parser.parse_args(["promote", "weather", "--from", "local", "--to", "staging"])
    assert args.service == "weather"
    assert args.from_env == "local"
    assert args.to_env == "staging"


def test_parser_watch_defaults():
    parser = ad.build_parser()
    args = parser.parse_args(["watch", "weather"])
    assert args.env == "local"
    assert args.timeout == 180


def test_parser_ui_defaults():
    parser = ad.build_parser()
    args = parser.parse_args(["ui"])
    assert args.port == 8080


def test_parser_help_command_runs_without_error(capsys):
    parser = ad.build_parser()
    args = parser.parse_args(["help"])
    args.func(args)
    captured = capsys.readouterr()
    assert "install" in captured.out
    assert "verify" in captured.out
    assert "promote" in captured.out


def test_parser_help_with_topic(capsys):
    parser = ad.build_parser()
    args = parser.parse_args(["help", "install"])
    args.func(args)
    captured = capsys.readouterr()
    assert "install" in captured.out
```

- [ ] **Step 2: Run tests — confirm new tests fail**

```
python -m pytest tests/test_argocd_deploy.py -v -k "parser or help_command or help_with"
```

Expected: `AttributeError: module 'argocd_deploy' has no attribute 'build_parser'`

- [ ] **Step 3: Append `build_parser`, `cmd_help`, and `main` to `scripts/argocd-deploy.py`**

```python
# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_help(args, parser: argparse.ArgumentParser) -> None:
    if args.topic:
        for action in parser._subparsers._actions:
            if hasattr(action, "_name_parser_map") and args.topic in action._name_parser_map:
                action._name_parser_map[args.topic].print_help()
                return
        print(f"Unknown command: {args.topic}")
    parser.print_help()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="argocd-deploy",
        description="Manage the Argo CD lifecycle: install, verify, promote, watch, and access the UI.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # install
    p = sub.add_parser("install", help="Install Argo CD and bootstrap app-of-apps")
    p.add_argument("--env", default="local", choices=["local", "staging", "prod"],
                   help="Target environment (default: local)")
    p.add_argument("--context", default=None, help="kubectl context (default: active context)")
    p.set_defaults(func=cmd_install)

    # verify
    p = sub.add_parser("verify", help="Read-only health check: pods, sync status, repo reachability")
    p.add_argument("--env", default="local", choices=["local", "staging", "prod"],
                   help="Target environment (default: local)")
    p.add_argument("--context", default=None, help="kubectl context (default: active context)")
    p.set_defaults(func=cmd_verify)

    # promote
    p = sub.add_parser("promote", help="Promote a service image tag from one env to another")
    p.add_argument("service", help="Service name (e.g. weather)")
    p.add_argument("--from", dest="from_env", required=True, help="Source environment")
    p.add_argument("--to", dest="to_env", required=True, help="Target environment")
    p.add_argument("--context", default=None,
                   help="kubectl context for watching target env sync (default: active context)")
    p.add_argument("--timeout", type=int, default=300, help="Seconds to wait for sync (default: 300)")
    p.set_defaults(func=cmd_promote)

    # watch
    p = sub.add_parser("watch", help="Stream rollout status and confirm Application is Synced+Healthy")
    p.add_argument("service", help="Service name (e.g. weather)")
    p.add_argument("--env", default="local", choices=["local", "staging", "prod"],
                   help="Target environment (default: local)")
    p.add_argument("--context", default=None, help="kubectl context (default: active context)")
    p.add_argument("--timeout", type=int, default=180, help="Seconds to wait (default: 180)")
    p.set_defaults(func=cmd_watch)

    # ui
    p = sub.add_parser("ui", help="Port-forward the Argo CD UI and print credentials")
    p.add_argument("--context", default=None, help="kubectl context (default: active context)")
    p.add_argument("--port", type=int, default=8080, help="Local port (default: 8080)")
    p.set_defaults(func=cmd_ui)

    # help
    p = sub.add_parser("help", help="Show help for a command")
    p.add_argument("topic", nargs="?", default=None, help="Command to get help for")
    p.set_defaults(func=lambda a: cmd_help(a, parser))

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 52 tests PASS

- [ ] **Step 5: Commit**

```
git add scripts/argocd-deploy.py tests/test_argocd_deploy.py
git commit -m "feat(argocd-deploy): wire CLI (build_parser, cmd_help, main)"
```

---

## Task 10: Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace the existing "### Argo CD" section**

Find this block in `README.md` (the full section from `### Argo CD` through `See [docs/deployment-lifecycle.md]...`):

```markdown
### Argo CD

`stack-up.py` installs Argo CD automatically — you don't need to run these steps manually unless you're setting up the cluster without the script.

**What `stack-up.py` does with Argo CD:**
1. Installs Argo CD into the `argocd` namespace via Helmfile (`infra/argo/`)
2. Waits for the server to be ready
3. Applies `infra/gitops/argo/app-of-apps.yaml` — this single manifest creates one Argo CD Application per service
4. Port-forwards the UI to `http://localhost:8080` and prints the admin password

**If you need to set it up manually:**

```bash
# Install Argo CD
cd infra/argo
helmfile repos
helmfile apply

# Wait for the server
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=180s

# Bootstrap the app-of-apps (creates one Application per service)
kubectl apply -f infra/gitops/argo/app-of-apps.yaml

# Port-forward the UI
kubectl port-forward -n argocd svc/argocd-server 8080:80

# Get the admin password
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath="{.data.password}" | base64 -d
```

**What Argo CD is doing:**

Argo CD watches `https://github.com/kakhavai/foundry` (the real GitHub repo) every ~3 minutes. When CI merges a change to `main` and commits a new image tag to `infra/gitops/envs/local/<service>/values.yaml`, Argo CD detects it and runs a Helm upgrade on your local cluster automatically. You never run `helm upgrade` for a production deploy — you commit to Git and Argo CD reconciles.

To trigger a deploy manually (e.g. for rollback), edit the tag file and push to `main`:

```bash
# Or use the rollback script:
python scripts/rollback.py weather <target-tag>
```

See [docs/deployment-lifecycle.md](docs/deployment-lifecycle.md) for the full deploy flow.
```

Replace it with:

```markdown
### Argo CD

`argocd-deploy.py` is the dedicated script for the Argo CD lifecycle.

**First time setup:**

```bash
python scripts/argocd-deploy.py install --env local
python scripts/argocd-deploy.py ui
```

**Sub-commands:**

| Command | What it does |
|---|---|
| `install --env <env>` | Install Argo CD via Helmfile, bootstrap app-of-apps, wait for all Applications to sync |
| `verify --env <env>` | Read-only health check: pods running, Applications Synced+Healthy, repo reachable |
| `promote <svc> --from <env> --to <env>` | Promote an image tag, commit/push, watch target env sync |
| `watch <svc> --env <env>` | Stream rollout status and confirm Application is Synced+Healthy |
| `ui [--port 8080]` | Port-forward the Argo CD UI and print URL + admin password |
| `help [<command>]` | Show usage for a specific command |

All sub-commands accept `--context <ctx>` to target a non-default kubectl context (e.g. an EKS cluster). Omit it to use the active context.

**After a merge (CI updated the image tag — watch the rollout):**

```bash
python scripts/argocd-deploy.py watch weather --env local
```

**Promote a verified build to staging:**

```bash
python scripts/argocd-deploy.py promote weather --from local --to staging --context my-staging-context
python scripts/argocd-deploy.py verify --env staging --context my-staging-context
```

**What Argo CD is doing:** it watches `https://github.com/kakhavai/foundry` every ~3 minutes. When CI merges a change and commits a new image tag to `infra/gitops/envs/<env>/<service>/values.yaml`, Argo CD detects it and rolls out the new image automatically. You never run `helm upgrade` directly.

To roll back a service, use `rollback.py` then confirm with `verify`:

```bash
python scripts/rollback.py weather <target-tag>
python scripts/argocd-deploy.py verify --env local
```

See [docs/deployment-lifecycle.md](docs/deployment-lifecycle.md) for the full deploy flow.
```

- [ ] **Step 2: Run tests to confirm nothing broke**

```
python -m pytest tests/test_argocd_deploy.py -v
```

Expected: all 52 tests PASS

- [ ] **Step 3: Commit**

```
git add README.md
git commit -m "docs: update README Argo CD section to reference argocd-deploy.py"
```

---

## Self-Review Checklist

Before marking the plan complete, verify against the spec:

- [x] `install` — helmfile apply, wait for server, apply app-of-apps, poll sync ✓
- [x] `verify` — pods check, sync/health table, refresh annotation ✓
- [x] `promote` — read tag, write tag, ensure manifest, commit/push, poll sync ✓
- [x] `watch` — rollout status stream + final Application status check ✓
- [x] `ui` — port-forward, print URL + password, block until Ctrl+C ✓
- [x] `help` / `--help` — top-level and per-command ✓
- [x] `--context` propagated to all kubectl + helmfile calls ✓
- [x] Service discovery from filesystem (no hardcoded list) ✓
- [x] Env-aware values file (`values-<env>.yaml` fallback) ✓
- [x] Application naming convention (`<svc>-<env>` for non-local) ✓
- [x] `ensure_application_manifest` creates env manifest from local source ✓
- [x] `write_tag` creates parent dirs (needed for new env promotion) ✓
- [x] README updated with sub-commands + rollback verify note ✓
- [x] No third-party dependencies added ✓
- [x] Standalone — no imports from stack-up.py or deploy-local.py ✓
