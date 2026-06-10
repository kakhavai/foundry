"""Tests for scripts/argocd-deploy.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import base64
import importlib.util

import pytest

# Import argocd-deploy.py (hyphenated) as a module
scripts_dir = Path(__file__).parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("argocd_deploy", scripts_dir / "argocd-deploy.py")
ad = importlib.util.module_from_spec(spec)
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
