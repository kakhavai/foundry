import subprocess
import sys


def dev() -> None:
    subprocess.run(
        ["uvicorn", "player_projections.main:app", "--reload", "--host", "0.0.0.0", "--port", "8001"]
    )


def test() -> None:
    subprocess.run(["pytest", *sys.argv[1:]])


def lint() -> None:
    subprocess.run(["ruff", "check", "src"])


def fmt() -> None:
    subprocess.run(["ruff", "format", "src"])
