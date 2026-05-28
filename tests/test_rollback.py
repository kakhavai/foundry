"""Tests for scripts/rollback.py."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add scripts/ to path so we can import rollback
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import rollback


def test_validate_service_valid(tmp_path):
    """validate_service returns path when service dir exists."""
    svc_dir = tmp_path / "envs" / "local" / "weather"
    svc_dir.mkdir(parents=True)
    result = rollback.validate_service("weather", gitops_root=tmp_path)
    assert result == svc_dir / "values.yaml"


def test_validate_service_invalid(tmp_path):
    """validate_service raises SystemExit for unknown service."""
    (tmp_path / "envs" / "local").mkdir(parents=True)
    with pytest.raises(SystemExit):
        rollback.validate_service("nonexistent", gitops_root=tmp_path)


def test_write_tag(tmp_path):
    """write_tag writes the correct YAML to the values file."""
    values_file = tmp_path / "values.yaml"
    rollback.write_tag(values_file, "abc1234")
    content = values_file.read_text()
    assert 'tag: "abc1234"' in content


def test_write_tag_overwrites_existing(tmp_path):
    """write_tag overwrites the existing tag."""
    values_file = tmp_path / "values.yaml"
    values_file.write_text('image:\n  tag: "old123"\n')
    rollback.write_tag(values_file, "new456")
    content = values_file.read_text()
    assert 'tag: "new456"' in content
    assert "old123" not in content


def test_git_commit_and_push_called(tmp_path):
    """git_commit_and_push calls git commands with correct args."""
    values_file = tmp_path / "values.yaml"
    values_file.write_text("image:\n  tag: abc\n")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        rollback.git_commit_and_push(values_file, "weather", "abc1234")
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("add" in c for c in calls)
    assert any("commit" in c for c in calls)
    assert any("push" in c for c in calls)
    assert any("revert(weather): roll back to abc1234" in c for c in calls)


def test_git_commit_and_push_exits_on_failure(tmp_path):
    """git_commit_and_push exits non-zero when git commit fails."""
    values_file = tmp_path / "values.yaml"
    values_file.write_text("image:\n  tag: old\n")
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git commit fails
        ]
        with pytest.raises(SystemExit) as exc:
            rollback.git_commit_and_push(values_file, "weather", "abc1234")
    assert exc.value.code != 0
    assert mock_run.call_count == 2  # push was never called
