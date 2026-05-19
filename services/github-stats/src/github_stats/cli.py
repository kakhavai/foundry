import subprocess
import sys


def dev() -> None:
    subprocess.run(
        [
            "uvicorn",
            "github_stats.main:app",
            "--reload",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ]
    )


def test() -> None:
    subprocess.run(["pytest", *sys.argv[1:]])


def lint() -> None:
    subprocess.run(["ruff", "check", "src"])


def fmt() -> None:
    subprocess.run(["ruff", "format", "src"])
