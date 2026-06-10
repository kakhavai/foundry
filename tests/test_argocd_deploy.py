"""Tests for scripts/argocd-deploy.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import base64
import importlib.util
from itertools import cycle

import pytest

# Import argocd-deploy.py (hyphenated) as a module
scripts_dir = Path(__file__).parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("argocd_deploy", scripts_dir / "argocd-deploy.py")
ad = importlib.util.module_from_spec(spec)
sys.modules["argocd_deploy"] = ad
spec.loader.exec_module(ad)


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
    # Cycle through responses: one always healthy, one always unhealthy
    responses = cycle([
        (0, "Synced,Healthy"),
        (0, "OutOfSync,Progressing"),
    ])
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
