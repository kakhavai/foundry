import subprocess
from datetime import datetime, timezone

from foundry.triage.collectors.gitops import RawDeploy, recent_deploys


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_recent_deploys_parses_git_log(tmp_path):
    repo = tmp_path
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    (repo / "infra").mkdir()
    gitops = repo / "infra" / "gitops"
    gitops.mkdir()
    (gitops / "weather.yaml").write_text("tag: 0.1.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "bump weather to 0.4.1")

    deploys = recent_deploys(repo_dir=str(repo), gitops_subdir="infra/gitops", limit=5)
    assert len(deploys) == 1
    d = deploys[0]
    assert isinstance(d, RawDeploy)
    assert len(d.sha) >= 7
    assert "infra/gitops/weather.yaml" in d.touched_paths
    assert d.timestamp.tzinfo is not None


def test_minutes_before_helper():
    deploy = RawDeploy(
        sha="abc1234",
        timestamp=datetime(2026, 6, 14, 14, 32, tzinfo=timezone.utc),
        touched_paths=["infra/gitops/weather.yaml"],
        subject="bump",
    )
    anomaly_onset = datetime(2026, 6, 14, 14, 35, tzinfo=timezone.utc)
    assert abs(deploy.minutes_before(anomaly_onset) - 3.0) < 1e-6
