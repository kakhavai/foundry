"""Guards the shared `workspace-manifests` build stage against silent rot.

`uv sync --locked --package <x>` resolves the ENTIRE uv workspace graph before
it can sync any single member, so every member's pyproject.toml must be present
in the build context — including members the service does not depend on. The
Dockerfiles used to list them one COPY line at a time, which is quadratic: at
26 collectors that is 26 lines in each of 26 Dockerfiles that must all agree,
and the line you forget breaks an UNRELATED service's image with "the lockfile
needs to be updated".

That break is invisible to every other test in this repo, because **pytest
never touches a Dockerfile**. It has already happened once for real: adding
`roster-scope` to the workspace broke `services/weather/Dockerfile`, and the
agent that did it had built only its own image and concluded Docker was fine.

These tests are the thing that would have caught it. They are static — they
parse Dockerfiles, they do not run `docker build` — so they are fast and need
no daemon, but they cannot prove a build succeeds. They prove the *mechanism*
is still in place, which is what regresses when somebody copies an old
template.

The collector set is derived from the root pyproject.toml's workspace members,
not hardcoded, so a new collector is covered the moment it is registered.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"

# The stage every collector shares. Named here so a rename has to be
# deliberate rather than accidental.
STAGE_NAME = "workspace-manifests"
STAGE_OUTPUT_COPY = f"COPY --from={STAGE_NAME} /manifests/ ./"

# A per-member manifest COPY — the exact quadratic pattern this stage replaced.
# Matches `COPY services/<anything>/pyproject.toml ...`, with or without flags.
PER_MEMBER_COPY = re.compile(
    r"^\s*COPY\s+(?:--\S+\s+)*services/[^/\s]+/pyproject\.toml\b",
    re.MULTILINE,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _logical_lines(text: str) -> list[str]:
    """Dockerfile lines with backslash continuations joined, comments dropped."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _workspace() -> dict:
    data = tomllib.loads(_read(ROOT / "pyproject.toml"))
    return data["tool"]["uv"]["workspace"]


def _workspace_members() -> list[str]:
    return _workspace()["members"]


def _excluded_dirs() -> set[Path]:
    """Paths `[tool.uv.workspace] exclude` removes from the globbed members.

    `members` is a glob (`services/*`) so that adding a collector needs no edit
    to the root pyproject.toml — twenty-four queued collectors would otherwise
    all append to one line. Reading `members` without `exclude` therefore claims
    `player-projections` and `foundry-cli` are workspace members, and every
    assertion below would demand they carry a stage that is meaningless for a
    service that is not in the workspace at all.
    """
    excluded: set[Path] = set()
    for pattern in _workspace().get("exclude", []):
        excluded.update(path.resolve() for path in ROOT.glob(pattern))
    return excluded


def _collector_dirs() -> list[Path]:
    """Workspace members under services/ that ship a Dockerfile."""
    excluded = _excluded_dirs()
    dirs: list[Path] = []
    for member in _workspace_members():
        if not member.startswith("services/"):
            continue
        for path in sorted(ROOT.glob(member)):
            if path.resolve() in excluded:
                continue
            if (path / "Dockerfile").is_file():
                dirs.append(path)
    return dirs


def _all_service_dockerfiles() -> list[Path]:
    return sorted(SERVICES.glob("*/Dockerfile"))


def _stage_body(text: str) -> str:
    """The `workspace-manifests` stage: its FROM line through the next FROM."""
    lines = text.splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith("FROM ") and line.rstrip().endswith(f"AS {STAGE_NAME}")
        ),
        None,
    )
    assert start is not None, f"no `AS {STAGE_NAME}` stage found"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("FROM ")),
        len(lines),
    )
    body = "\n".join(
        line for line in lines[start:end] if not line.lstrip().startswith("#")
    )
    return "\n".join(line.rstrip() for line in body.splitlines() if line.strip())


def _package_name(service_dir: Path) -> str:
    data = tomllib.loads(_read(service_dir / "pyproject.toml"))
    return data["project"]["name"]


COLLECTORS = _collector_dirs()


def test_collectors_were_actually_discovered():
    """A bug in the discovery above would make every test below vacuous."""
    assert COLLECTORS, (
        "no workspace member under services/ has a Dockerfile — either the "
        "root pyproject.toml's [tool.uv.workspace].members changed shape or "
        "this test's discovery is broken. Every assertion below is vacuous "
        "until this passes."
    )


@pytest.mark.parametrize(
    "dockerfile", _all_service_dockerfiles(), ids=lambda p: p.parent.name
)
def test_no_dockerfile_lists_workspace_members_one_by_one(dockerfile: Path):
    """The regression this file exists to catch.

    Applies to EVERY service Dockerfile, not just collectors: the failure mode
    is somebody copying a pre-`workspace-manifests` template, and a
    non-collector is just as capable of carrying the bad pattern forward.
    """
    match = PER_MEMBER_COPY.search(_read(dockerfile))
    assert match is None, (
        f"{dockerfile.relative_to(ROOT)} copies a workspace member's manifest "
        f"one file at a time ({match.group(0).strip()!r} ...). That is the "
        "quadratic pattern the shared `workspace-manifests` stage replaced: "
        "every new member would need a line here, and the one you forget "
        "breaks an unrelated service's image. Use the shared stage — see "
        "services/weather/Dockerfile."
    )


@pytest.mark.parametrize("service", COLLECTORS, ids=lambda p: p.name)
def test_collector_uses_the_shared_manifests_stage(service: Path):
    text = _read(service / "Dockerfile")

    assert f"AS {STAGE_NAME}" in text, (
        f"{service.name} has no `{STAGE_NAME}` stage. Without it, dependency "
        "resolution cannot see sibling members' manifests and the build fails "
        "with 'the lockfile needs to be updated' the next time a member is "
        "added."
    )
    assert STAGE_OUTPUT_COPY in text, (
        f"{service.name} declares the `{STAGE_NAME}` stage but never consumes "
        f"it. Expected the literal line: {STAGE_OUTPUT_COPY}"
    )


@pytest.mark.parametrize("service", COLLECTORS, ids=lambda p: p.name)
def test_manifests_stage_selects_members_by_glob_only(service: Path):
    """The stage must name no member. If it does, it is not generic and the
    quadratic problem has merely moved up a few lines.
    """
    body = _stage_body(_read(service / "Dockerfile"))

    assert "cp pyproject.toml uv.lock /manifests/" in body, (
        f"{service.name}: the stage must copy the workspace root manifest and "
        "the single root lockfile"
    )
    assert "cp -a libs /manifests/libs" in body, (
        f"{service.name}: libs/ must be copied IN FULL — libs are built as "
        "dependencies, not merely resolved, so a manifest alone is not enough"
    )
    assert "find services" in body and "-name pyproject.toml" in body, (
        f"{service.name}: service manifests must be gathered by glob, so that "
        "adding a workspace member needs no edit here"
    )

    for member in _workspace_members():
        name = member.rsplit("/", 1)[-1]
        if name == "*":
            continue
        assert name not in body, (
            f"{service.name}: the `{STAGE_NAME}` stage names the member "
            f"{name!r}. The whole point of the stage is that it names none — "
            "adding a member must require no Dockerfile edit."
        )


def test_manifests_stage_is_identical_across_collectors():
    """One mechanism, not N drifting copies.

    Docker has no include directive, so the stage is physically duplicated per
    service. This is what keeps the duplicates honest — the copies must agree
    byte for byte once comments are stripped.
    """
    bodies = {s.name: _stage_body(_read(s / "Dockerfile")) for s in COLLECTORS}
    distinct = set(bodies.values())
    assert len(distinct) == 1, (
        "collectors' `workspace-manifests` stages have drifted apart:\n"
        + "\n\n".join(f"--- {name} ---\n{body}" for name, body in bodies.items())
    )


@pytest.mark.parametrize("service", COLLECTORS, ids=lambda p: p.name)
def test_dependency_sync_happens_before_service_source_is_copied(service: Path):
    """Guards the layer caching, which is the constraint most easily lost.

    A broad `COPY services/ ./services/` before the dependency sync would fix
    the member problem and re-resolve every dependency on every source edit —
    a regression, not a fix. The ordering below is what keeps the expensive
    layer cached across source changes.
    """
    lines = _logical_lines(_read(service / "Dockerfile"))

    manifests_at = next(
        i for i, line in enumerate(lines) if line.startswith(STAGE_OUTPUT_COPY)
    )
    deps_at = next(i for i, line in enumerate(lines) if "--no-install-project" in line)
    source_at = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("COPY ") and f"services/{service.name}/" in line
    )

    assert manifests_at < deps_at < source_at, (
        f"{service.name}: expected manifests -> dependency sync -> service "
        f"source, got positions {manifests_at}, {deps_at}, {source_at}. Any "
        "other order either breaks resolution or invalidates the dependency "
        "layer on every source edit."
    )

    for line in lines[:deps_at]:
        if line.startswith("COPY ") and "--from=" not in line:
            assert "services/" not in line, (
                f"{service.name}: {line!r} pulls service source into the "
                "context before the dependency sync, so every source edit "
                "re-resolves and re-downloads every dependency."
            )


@pytest.mark.parametrize("service", COLLECTORS, ids=lambda p: p.name)
def test_final_sync_reinstalls_the_service_package(service: Path):
    """The stale-wheel guard. Do not remove this as redundant.

    The package version never changes (0.1.0), so uv's build cache under the
    `--mount=type=cache` can serve a PREVIOUSLY BUILT WHEEL even though the
    source just changed. The image then silently ships stale code: it builds,
    it starts, it passes a health check, and it is executing a different
    revision than the tree it was built from. That happened twice while
    debugging roster-scope and invalidated two rounds of measurements.
    """
    package = _package_name(service)
    lines = _logical_lines(_read(service / "Dockerfile"))

    final_sync = [
        line for line in lines if "uv sync" in line and "--no-editable" in line
    ]
    assert final_sync, f"{service.name}: no final `uv sync --no-editable` found"

    for line in final_sync:
        assert f"--reinstall-package {package}" in line, (
            f"{service.name}: the final sync must carry "
            f"`--reinstall-package {package}` or uv's build cache can serve a "
            "stale wheel for an unchanged version number, and the image "
            "silently ships code that is not in this tree."
        )
