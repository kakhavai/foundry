"""Guards the single collector Dockerfile against silent rot.

There is exactly ONE Dockerfile that builds collectors — `Dockerfile.collector`
at the repo root — parameterised by three build args (SERVICE, PACKAGE, PORT).
This file protects two separate things, and they fail in different ways:

1. **That it stays the only one.** Each collector used to carry its own ~70-line
   copy differing in three tokens. Nine of them had already drifted (one carried
   the reference write-up, the rest carried abbreviations of it). The regression
   is not dramatic — somebody copies a collector by hand, or the scaffolder
   emits a per-service file again — and the symptom is one image quietly missing
   `--reinstall-package` or the numeric UID while the other twenty-five are
   fine.

2. **That the mechanisms inside it survive.** The `workspace-manifests` stage,
   the manifests -> deps -> source ordering that keeps the expensive layer
   cached, `--reinstall-package` against uv's stale-wheel hazard, the numeric
   UID `runAsNonRoot` requires, and the `exec` that keeps PID 1 as uvicorn so
   SIGTERM reaches the app. Every one of those fails SILENTLY: the image
   builds, starts, and passes a health check either way.

These tests are static — they parse the Dockerfile, they do not run
`docker build` — so they are fast and need no daemon, but they cannot prove a
build succeeds. They prove the mechanism is still in place, which is what
regresses when somebody copies an old template.

The collector set is derived from the root pyproject.toml's workspace members,
not hardcoded, so a new collector is covered the moment it is registered.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"

COLLECTOR_DOCKERFILE = ROOT / "Dockerfile.collector"

# The stage every collector build passes through. Named here so a rename has to
# be deliberate rather than accidental.
STAGE_NAME = "workspace-manifests"
STAGE_OUTPUT_COPY = f"COPY --from={STAGE_NAME} /manifests/ ./"

# The one service that legitimately keeps its own Dockerfile: `player-projections`
# is not a uv workspace member, owns its own uv.lock, imports nothing from
# libs/, and builds from its own directory as the context. Folding it into the
# collector Dockerfile would make it resolve against the fleet's lock for no
# benefit. `services/foundry-cli` ships no image at all.
NON_COLLECTOR_DOCKERFILES = {"player-projections"}

# The three build args that are the ONLY difference between two collector
# images. Every one is derived by scripts/collectors.py; none is typed by hand.
BUILD_ARGS = ("SERVICE", "PACKAGE", "PORT")

# A per-member manifest COPY — the quadratic pattern the shared stage replaced.
# Matches `COPY services/<anything>/pyproject.toml ...`, with or without flags,
# but NOT the `${SERVICE}` interpolation the shared Dockerfile uses.
PER_MEMBER_COPY = re.compile(
    r"^\s*COPY\s+(?:--\S+\s+)*services/(?!\$\{?SERVICE)[^/\s]+/pyproject\.toml\b",
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
    all append to one line. Reading `members` without `exclude` would claim
    `player-projections` and `foundry-cli` are workspace members.
    """
    excluded: set[Path] = set()
    for pattern in _workspace().get("exclude", []):
        excluded.update(path.resolve() for path in ROOT.glob(pattern))
    return excluded


def _collector_dirs() -> list[Path]:
    """Workspace members under services/ — every one of them a collector."""
    excluded = _excluded_dirs()
    dirs: list[Path] = []
    for member in _workspace_members():
        if not member.startswith("services/"):
            continue
        for path in sorted(ROOT.glob(member)):
            if path.resolve() in excluded or not path.is_dir():
                continue
            dirs.append(path)
    return dirs


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


COLLECTORS = _collector_dirs()


def test_collectors_were_actually_discovered():
    """A bug in the discovery above would make several tests below vacuous."""
    assert COLLECTORS, (
        "no workspace member found under services/ — either the root "
        "pyproject.toml's [tool.uv.workspace].members changed shape or this "
        "test's discovery is broken."
    )


# ── 1. it stays the only one ──────────────────────────────────────────────────


def test_the_shared_collector_dockerfile_exists():
    assert COLLECTOR_DOCKERFILE.is_file(), (
        "Dockerfile.collector is gone. It is the only Dockerfile that builds a "
        "collector; scripts/deploy-local.py and "
        ".github/actions/changed-services/filters.py both name it."
    )


@pytest.mark.parametrize("service", COLLECTORS, ids=lambda p: p.name)
def test_no_collector_has_its_own_dockerfile(service: Path):
    """The regression this file exists to catch.

    A collector Dockerfile differs from the shared one in exactly three tokens:
    the package name, the module and the port. All three are build args. So a
    per-collector file is ~70 lines of duplication whose only possible
    contribution is drift — and drift here is silent, because a Dockerfile that
    has quietly lost `--reinstall-package` still builds, still starts, and
    still passes its health check while serving a stale wheel.

    If this fired after running `scripts/new-collector.py`, the scaffolder is
    what needs fixing, not the generated file: it must stop templating a
    Dockerfile at all.
    """
    dockerfile = service / "Dockerfile"
    assert not dockerfile.exists(), (
        f"{dockerfile.relative_to(ROOT)} exists. Collectors build from the "
        "single root Dockerfile.collector with "
        "--build-arg SERVICE/PACKAGE/PORT — delete this file rather than "
        "maintaining a copy of it."
    )


def test_only_non_collectors_keep_a_service_dockerfile():
    """Stated as a whole-tree assertion, not per service, so that a Dockerfile
    appearing under a directory that is not even a workspace member is caught
    too."""
    found = {path.parent.name for path in SERVICES.glob("*/Dockerfile")}
    assert found == NON_COLLECTOR_DOCKERFILES, (
        f"services/*/Dockerfile should exist for exactly "
        f"{sorted(NON_COLLECTOR_DOCKERFILES)} — found {sorted(found)}. A "
        "collector builds from the root Dockerfile.collector; only a service "
        "that is NOT a uv workspace member (its own uv.lock, its own build "
        "context) keeps a Dockerfile of its own."
    )


@pytest.mark.parametrize(
    "dockerfile",
    sorted(SERVICES.glob("*/Dockerfile")) + [COLLECTOR_DOCKERFILE],
    ids=lambda p: p.parent.name,
)
def test_no_dockerfile_lists_workspace_members_one_by_one(dockerfile: Path):
    """`uv sync --locked --package <x>` resolves the ENTIRE workspace graph
    before it can sync any single member, so every member's pyproject.toml must
    be in the build context — including members the service does not depend on.

    Listing them one COPY line at a time is quadratic, and the line you forget
    breaks an UNRELATED service's image with "the lockfile needs to be
    updated". That break is invisible to every other test in this repo, because
    pytest never touches a Dockerfile. It has happened for real: adding
    `roster-scope` to the workspace broke `services/weather/Dockerfile`, and
    the agent that did it had built only its own image and concluded Docker was
    fine.
    """
    match = PER_MEMBER_COPY.search(_read(dockerfile))
    assert match is None, (
        f"{dockerfile.relative_to(ROOT)} copies a workspace member's manifest "
        f"one file at a time ({match.group(0).strip()!r} ...). That is the "
        "quadratic pattern the shared `workspace-manifests` stage replaced. "
        "Use the stage — see Dockerfile.collector."
    )


# ── 2. the mechanisms inside it ───────────────────────────────────────────────


def test_it_uses_the_shared_manifests_stage():
    text = _read(COLLECTOR_DOCKERFILE)

    assert f"AS {STAGE_NAME}" in text, (
        f"Dockerfile.collector has no `{STAGE_NAME}` stage. Without it, "
        "dependency resolution cannot see sibling members' manifests and the "
        "build fails with 'the lockfile needs to be updated' the next time a "
        "member is added."
    )
    assert STAGE_OUTPUT_COPY in text, (
        f"Dockerfile.collector declares the `{STAGE_NAME}` stage but never "
        f"consumes it. Expected the literal line: {STAGE_OUTPUT_COPY}"
    )


def test_the_manifests_stage_selects_members_by_glob_only():
    """The stage must name no member. If it does, it is not generic and the
    quadratic problem has merely moved up a few lines."""
    body = _stage_body(_read(COLLECTOR_DOCKERFILE))

    assert "cp pyproject.toml uv.lock /manifests/" in body, (
        "the stage must copy the workspace root manifest and the single root lockfile"
    )
    assert "cp -a libs /manifests/libs" in body, (
        "libs/ must be copied IN FULL — libs are built as dependencies, not "
        "merely resolved, so a manifest alone is not enough"
    )
    assert "find services" in body and "-name pyproject.toml" in body, (
        "service manifests must be gathered by glob, so that adding a "
        "workspace member needs no edit here"
    )

    for service in COLLECTORS:
        assert service.name not in body, (
            f"the `{STAGE_NAME}` stage names the collector {service.name!r}. "
            "The whole point of the stage is that it names none — adding a "
            "member must require no Dockerfile edit."
        )


@pytest.mark.parametrize("arg", BUILD_ARGS)
def test_every_per_collector_value_is_a_build_arg(arg: str):
    """The three tokens that differ between collectors, and nothing else.

    A fourth divergence appearing here is the signal that one Dockerfile has
    stopped being enough — worth a deliberate decision rather than a quiet
    per-service file.
    """
    lines = _logical_lines(_read(COLLECTOR_DOCKERFILE))
    assert any(line.startswith(f"ARG {arg}") for line in lines), (
        f"Dockerfile.collector declares no `ARG {arg}`. All three of "
        f"{BUILD_ARGS} are what make one file serve the whole fleet; they are "
        "supplied by scripts/deploy-local.py and "
        ".github/actions/changed-services/filters.py."
    )


@pytest.mark.parametrize("arg", BUILD_ARGS)
def test_a_missing_build_arg_fails_the_build(arg: str):
    """Without this, forgetting `--build-arg PACKAGE=` produces an image whose
    CMD is `uvicorn .main:app`. It builds clean, pushes clean, and only fails
    when a pod starts — by which time the tag is already in GitOps."""
    text = _read(COLLECTOR_DOCKERFILE)
    assert re.search(rf'test -n "\$\{{{arg}\}}"', text), (
        f"Dockerfile.collector does not assert `{arg}` was supplied. A missing "
        "build arg must fail the build, not produce a broken image."
    )


def test_dependency_sync_happens_before_service_source_is_copied():
    """Guards the layer caching, which is the constraint most easily lost.

    A broad `COPY services/ ./services/` before the dependency sync would fix
    the member problem and re-resolve every dependency on every source edit —
    a regression, not a fix. The ordering below is what keeps the expensive
    layer cached across source changes.
    """
    lines = _logical_lines(_read(COLLECTOR_DOCKERFILE))

    manifests_at = next(
        i for i, line in enumerate(lines) if line.startswith(STAGE_OUTPUT_COPY)
    )
    deps_at = next(i for i, line in enumerate(lines) if "--no-install-project" in line)
    source_at = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("COPY ") and "services/${SERVICE}/" in line
    )

    assert manifests_at < deps_at < source_at, (
        f"expected manifests -> dependency sync -> service source, got "
        f"positions {manifests_at}, {deps_at}, {source_at}. Any other order "
        "either breaks resolution or invalidates the dependency layer on every "
        "source edit."
    )

    for line in lines[:deps_at]:
        if line.startswith("COPY ") and "--from=" not in line:
            assert "services/" not in line, (
                f"{line!r} pulls service source into the context before the "
                "dependency sync, so every source edit re-resolves and "
                "re-downloads every dependency."
            )


def test_the_final_sync_reinstalls_the_service_package():
    """The stale-wheel guard. Do not remove this as redundant.

    The package version never changes (0.1.0), so uv's build cache under the
    `--mount=type=cache` can serve a PREVIOUSLY BUILT WHEEL even though the
    source just changed. The image then silently ships stale code: it builds,
    it starts, it passes a health check, and it is executing a different
    revision than the tree it was built from. That happened twice while
    debugging roster-scope and invalidated two rounds of measurements.
    """
    lines = _logical_lines(_read(COLLECTOR_DOCKERFILE))

    final_sync = [
        line for line in lines if "uv sync" in line and "--no-editable" in line
    ]
    assert final_sync, "no final `uv sync --no-editable` found"

    for line in final_sync:
        assert "--reinstall-package ${SERVICE}" in line, (
            "the final sync must carry `--reinstall-package ${SERVICE}` or "
            "uv's build cache can serve a stale wheel for an unchanged version "
            "number, and the image silently ships code that is not in this "
            f"tree. Got: {line!r}"
        )


def test_the_runtime_user_is_numeric():
    """Kubernetes `runAsNonRoot` can only verify a NUMERIC user. `USER appuser`
    makes the kubelet refuse to start the pod, and the chart sets
    runAsNonRoot for every service."""
    lines = _logical_lines(_read(COLLECTOR_DOCKERFILE))
    user_lines = [line for line in lines if line.startswith("USER ")]
    assert user_lines == ["USER 65532"], (
        f"expected exactly `USER 65532`, got {user_lines}. A named user makes "
        "the kubelet reject the pod with 'container has runAsNonRoot and image "
        "has non-numeric user'."
    )


def test_the_cmd_execs_so_pid_1_is_uvicorn():
    """The module and port are build args, so CMD must be shell form — exec
    form does no variable expansion. `exec` is what makes shell form safe: it
    REPLACES the shell, so PID 1 is uvicorn and Kubernetes' SIGTERM reaches the
    app's graceful shutdown.

    Drop the `exec` and nothing appears to break. The container starts, serves,
    and passes its probes; it only misbehaves on termination, where the capture
    loop is killed mid-flight and the pod burns the full grace period.
    """
    lines = _logical_lines(_read(COLLECTOR_DOCKERFILE))
    cmds = [line for line in lines if line.startswith("CMD ")]
    assert len(cmds) == 1, f"expected exactly one CMD, got {cmds}"

    cmd = cmds[0]
    assert '"sh", "-c"' in cmd or "'sh', '-c'" in cmd, (
        f"expected a shell-form CMD (the module and port are build args and "
        f"exec form does not expand variables), got: {cmd}"
    )
    assert "exec uvicorn" in cmd, (
        "CMD must `exec uvicorn ...`. Without `exec`, PID 1 is /bin/sh, which "
        "does not forward SIGTERM, and every pod deletion waits out the full "
        f"termination grace period. Got: {cmd}"
    )
