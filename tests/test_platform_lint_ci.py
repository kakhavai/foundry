"""The gate that keeps `scripts/` and repo-root `tests/` linted.

Issue #76: `.github/actions/python-lint` is only ever invoked with
`working-directory` pointing at a service root or at `libs/collector-core`, so
the two directories of Python that are NOT inside a package -- `scripts/` and
this one -- were linted by nothing at all, and had accumulated 49 violations
before anybody looked.

The fix is a `platform-lint` job in `.github/workflows/integration-test.yml`.
The tests below are what stop that job from being deleted, narrowed to one
directory, or quietly de-fanged by dropping the format check -- each of which
would restore the original gap without failing anything else in the repo.

They assert the *shape* of the job, not its result: the job's result is the
job's own business, and re-running ruff from here would only prove that this
process can run ruff.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "integration-test.yml"
PYPROJECT_PATH = ROOT / "pyproject.toml"
LOCK_PATH = ROOT / "uv.lock"

JOB = "platform-lint"

# The two directories the job exists to cover. Named here rather than inferred,
# so adding a third one is a deliberate edit in one place.
LINTED_DIRS = ("scripts/", "tests/")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _job() -> dict:
    jobs = _workflow()["jobs"]
    assert JOB in jobs, (
        f"{WORKFLOW_PATH.relative_to(ROOT)} has no `{JOB}` job. Nothing else in "
        f"the repo lints {' or '.join(LINTED_DIRS)} -- see issue #76."
    )
    return jobs[JOB]


def _run_steps() -> list[str]:
    steps = [step.get("run", "") for step in _job()["steps"]]
    commands = [text for text in steps if text.strip()]
    assert commands, f"`{JOB}` has no `run:` steps at all"
    return commands


def _lock_ruff_version() -> str:
    lock = tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))
    versions = {
        package["version"] for package in lock["package"] if package["name"] == "ruff"
    }
    assert len(versions) == 1, (
        f"expected exactly one ruff version in uv.lock, found {sorted(versions)}"
    )
    return versions.pop()


# ── the job exists and does both halves ───────────────────────────────────────


@pytest.mark.parametrize("subcommand", ["ruff check", "ruff format --check"])
def test_platform_lint_runs_both_ruff_halves(subcommand):
    """`ruff check` alone is half a gate.

    The 49 original violations included E501s that only `ruff format` removes,
    and formatting drift is invisible to `ruff check` entirely. Every other
    lint entry point in this repo (`.github/actions/python-lint`) runs both.
    """
    commands = _run_steps()
    assert any(subcommand in text for text in commands), (
        f"`{JOB}` never runs `{subcommand}`. Commands found: {commands}"
    )


@pytest.mark.parametrize("directory", LINTED_DIRS)
@pytest.mark.parametrize("subcommand", ["ruff check", "ruff format --check"])
def test_both_directories_reach_both_halves(directory, subcommand):
    """Covering one directory and not the other is the bug, restated."""
    matching = [text for text in _run_steps() if subcommand in text]
    assert matching, f"`{JOB}` never runs `{subcommand}`"
    assert any(directory in text for text in matching), (
        f"`{JOB}` runs `{subcommand}` but not over `{directory}`: {matching}"
    )


def test_the_ruff_version_is_pinned_and_matches_the_lockfile():
    """An unpinned ruff reds a PR that changed nothing.

    Matching uv.lock is the second half: the per-service `lint` legs in
    services.yml run whatever ruff the lock resolves, so a different pin here
    would mean two ruffs disagreeing about the same repo -- and the one that
    disagrees is always the one you are not looking at.
    """
    pins = set()
    for text in _run_steps():
        pins.update(re.findall(r"ruff==([0-9][^\s'\"]*)", text))
    assert pins, f"`{JOB}` installs ruff without a `==` pin. Commands: {_run_steps()}"
    assert len(pins) == 1, f"`{JOB}` pins more than one ruff version: {sorted(pins)}"
    assert pins == {_lock_ruff_version()}, (
        f"`{JOB}` pins ruff=={pins.pop()} but uv.lock resolves {_lock_ruff_version()}"
    )


# ── the path filter actually reaches the job ──────────────────────────────────


def test_the_job_is_gated_on_a_filter_output_that_exists():
    """A job gated on an output the `changes` job never emits never runs.

    GitHub Actions does not error on an unknown output -- it evaluates to the
    empty string, the `if` is false, and the job is skipped forever with no
    signal anywhere. That is a strictly worse outcome than not having the job.
    """
    workflow = _workflow()
    condition = _job()["if"]
    referenced = re.findall(r"needs\.changes\.outputs\.([\w-]+)", condition)
    assert referenced, f"`{JOB}`'s `if` names no changes output: {condition}"
    declared = workflow["jobs"]["changes"]["outputs"]
    for name in referenced:
        assert name in declared, (
            f"`{JOB}` gates on `needs.changes.outputs.{name}`, which the "
            f"`changes` job does not declare. Declared: {sorted(declared)}"
        )


@pytest.mark.parametrize(
    "path",
    [
        "scripts/**",
        "tests/**",
        # Where the rule set lives. Change the select list and nothing else,
        # and without this entry no job runs to check the result.
        "pyproject.toml",
    ],
)
def test_the_filter_covers_every_path_the_job_lints(path):
    workflow = _workflow()
    steps = workflow["jobs"]["changes"]["steps"]
    filter_steps = [step for step in steps if "filters" in step.get("with", {})]
    assert len(filter_steps) == 1, (
        f"expected exactly one paths-filter step, found {len(filter_steps)}"
    )
    filters = yaml.safe_load(filter_steps[0]["with"]["filters"])

    condition = _job()["if"]
    names = re.findall(r"needs\.changes\.outputs\.([\w-]+)", condition)
    assert names, f"`{JOB}`'s `if` names no changes output: {condition}"

    patterns = []
    for name in names:
        assert name in filters, f"filter `{name}` is not defined"
        patterns.extend(filters[name])
    assert patterns, f"filter(s) {names} list no paths"
    assert path in patterns, (
        f"`{JOB}` lints `{path}` but its filter does not list it, so a PR "
        f"touching only that path never runs the job. Filter: {patterns}"
    )


# ── the rule set is declared, and declared once ───────────────────────────────


def test_the_repo_root_declares_a_ruff_rule_set():
    """Without an explicit `select`, ruff falls back to its default (E4/E7/E9/F).

    That default contains neither `PLW1510` nor `SIM117` -- two of the four
    codes issue #76 names -- so dropping this section would silently shrink the
    gate to a fraction of what it was added to catch, while `platform-lint`
    kept reporting green.
    """
    config = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    lint = config["tool"]["ruff"]["lint"]
    select = lint["select"]
    assert len(select) >= 3, f"[tool.ruff.lint] select is suspiciously small: {select}"
    for code in ("E", "F", "I", "SIM", "PLW"):
        assert code in select, (
            f"[tool.ruff.lint] select dropped `{code}`. It is there because "
            f"issue #76's violations included it; see the comment above it."
        )
