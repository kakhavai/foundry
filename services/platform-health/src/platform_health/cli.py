import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent


def dev():
    subprocess.run(
        [
            "uvicorn",
            "platform_health.main:app",
            "--reload",
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
        ],
        cwd=ROOT,
    )


def test():
    subprocess.run(["pytest"] + sys.argv[1:], cwd=ROOT)


def lint():
    subprocess.run(["ruff", "check", "."], cwd=ROOT)


def fmt():
    subprocess.run(["ruff", "format", "."], cwd=ROOT)
