import subprocess
import sys


def test_cli_help_lists_triage():
    result = subprocess.run(
        [sys.executable, "-m", "foundry.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "triage" in result.stdout


def test_triage_requires_service():
    result = subprocess.run(
        [sys.executable, "-m", "foundry.cli", "triage"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "service" in (result.stdout + result.stderr).lower()
