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


# ── cmd_verify ────────────────────────────────────────────────────────────────

def _make_verify_args(env="local", context=None):
    return type("Args", (), {"env": env, "context": context})()


def test_cmd_verify_passes_when_all_healthy():
    pod_output = "argocd-server-xxx   1/1   Running   0   5m"
    app_output = "Synced,Healthy,2026-06-10T00:00:00Z"
    with patch("argocd_deploy.kubectl_capture") as mock_capture, \
         patch("argocd_deploy.discover_services", return_value=["weather"]):
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


def test_cmd_promote_exits_when_from_equals_to():
    with patch("argocd_deploy.git_commit_and_push") as mock_git:
        with pytest.raises(SystemExit):
            ad.cmd_promote(_make_promote_args(from_env="prod", to_env="prod"))
    mock_git.assert_not_called()


# ── cmd_watch ─────────────────────────────────────────────────────────────────

def _make_watch_args(service="weather", env="local", context=None, timeout=180):
    return type("Args", (), {"service": service, "env": env, "context": context, "timeout": timeout})()


def test_cmd_watch_exits_zero_when_healthy():
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run, \
         patch("argocd_deploy.poll_applications", return_value=True):
        ad.cmd_watch(_make_watch_args())
    rollout_calls = [c for c in mock_run.call_args_list if "rollout" in str(c)]
    assert len(rollout_calls) >= 1


def test_cmd_watch_exits_nonzero_when_app_not_healthy():
    with patch("subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("argocd_deploy.poll_applications", return_value=False):
        with pytest.raises(SystemExit):
            ad.cmd_watch(_make_watch_args())


def test_cmd_watch_polls_application_as_authoritative_gate():
    # Even when the rollout step exits non-zero, a Synced+Healthy Application
    # means success — the Argo CD poll is the gate, not the rollout exit code.
    with patch("subprocess.run", return_value=MagicMock(returncode=1)), \
         patch("argocd_deploy.poll_applications", return_value=True) as mock_poll:
        ad.cmd_watch(_make_watch_args())
    mock_poll.assert_called_once()


# ── cmd_ui ────────────────────────────────────────────────────────────────────

def _make_ui_args(context=None, port=8080):
    return type("Args", (), {"context": context, "port": port})()


def test_cmd_ui_starts_portforward_and_prints_credentials():
    sleep_count = 0
    def fake_sleep(duration):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 1:  # First sleep is OK, second raises
            raise KeyboardInterrupt()

    with patch("subprocess.Popen") as mock_popen, \
         patch("argocd_deploy.argo_password", return_value="testpwd"), \
         patch("time.sleep", side_effect=fake_sleep):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        ad.cmd_ui(_make_ui_args())

    mock_popen.assert_called_once()
    pf_cmd = mock_popen.call_args[0][0]
    pf_cmd_str = " ".join(pf_cmd) if isinstance(pf_cmd, list) else pf_cmd
    assert "port-forward" in pf_cmd_str
    assert "argocd-server" in pf_cmd_str
    mock_proc.terminate.assert_called_once()


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
