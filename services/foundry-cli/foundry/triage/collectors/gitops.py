import subprocess
from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawDeploy:
    sha: str
    timestamp: datetime
    touched_paths: list[str]
    subject: str

    def minutes_before(self, anomaly_onset: datetime) -> float:
        """Minutes between this deploy and the anomaly onset (positive = before)."""
        return (anomaly_onset - self.timestamp).total_seconds() / 60.0


def recent_deploys(
    repo_dir: str, gitops_subdir: str = "infra/gitops", limit: int = 5
) -> list[RawDeploy]:
    """Read the last `limit` commits that touched the GitOps directory, with their
    timestamps and changed files. Pure `git log` — no network."""

    fmt = "%H%x1f%cI%x1f%s"  # sha, committer ISO date, subject, unit-separated
    result = subprocess.run(
        [
            "git",
            "log",
            f"-{limit}",
            f"--format={fmt}",
            "--name-only",
            "--",
            gitops_subdir,
        ],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )

    deploys: list[RawDeploy] = []
    current_deploy = None

    for line in result.stdout.split("\n"):
        line = line.strip()
        if not line:
            continue

        if "\x1f" in line:
            # This is a commit header line
            if current_deploy:
                deploys.append(current_deploy)
            sha, iso, subject = line.split("\x1f")
            current_deploy = RawDeploy(
                sha=sha,
                timestamp=datetime.fromisoformat(iso),
                touched_paths=[],
                subject=subject,
            )
        elif current_deploy:
            # This is a file path belonging to the current deploy
            current_deploy.touched_paths.append(line)

    if current_deploy:
        deploys.append(current_deploy)

    return deploys
