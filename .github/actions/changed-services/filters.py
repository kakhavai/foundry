#!/usr/bin/env python3
"""Turn the fleet into one `dorny/paths-filter` rule per service.

This is the piece that lets a *single* matrix workflow keep the per-service
path filtering the twenty-six per-service workflows used to provide by hand.
The naive matrix — "list every service, run every job" — would run 26 lint jobs,
26 test jobs and 26 helm-lint jobs on a one-line change to one collector. That
is not a smaller CI, it is the same CI with a bigger bill and a slower merge.

So the matrix is *computed*: this module emits a filter per service naming the
paths that service actually depends on, `dorny/paths-filter` reports which of
those filters matched the diff, and only the matched services enter the matrix.
A PR touching `services/weather/` produces a one-element matrix, exactly as
`.github/workflows/weather.yml` used to.

The rules encoded here are the union of what the per-service workflows carried
before this collapse, and nothing new:

  services/<name>/**              the service's own source
  helm/values/<name>/**           its deployment values
  libs/**                         collectors only — every collector depends on
                                  libs/collector-core through the uv workspace
  helm/charts/generic-service/**  the chart every service renders
  .github/actions/**              the composite actions every job calls
  .github/workflows/services.yml  the workflow itself; a change to CI should
                                  run CI, which the old per-service files did
                                  NOT do for themselves
  contracts/**                    the committed contracts CI checks for drift

`libs/**` is the one rule that is not uniform, and it is derived rather than
declared: `Service.is_collector` already means "depends on collector-core by
path" (that is what `build_context_root` rests on in scripts/collectors.py), so
asking whether a service cares about `libs/**` is asking whether it is a
collector. Nothing here holds a list of names.

Importable so `tests/test_service_ci_coverage.py` can run it against a
synthetic registry: that test appends a collector that exists nowhere in the
repo and asserts a CI filter and a matrix entry appear for it, which is the
property this whole collapse is buying.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

# Paths every service depends on, whatever it is.
SHARED_PATHS: tuple[str, ...] = (
    "helm/charts/generic-service/**",
    ".github/actions/**",
    ".github/workflows/services.yml",
    "contracts/**",
)

# Collectors only. Kept separate from SHARED_PATHS because `player-projections`
# imports nothing from libs/ — giving it this rule would rebuild and retest it
# on every collector-core change for no reason.
COLLECTOR_PATHS: tuple[str, ...] = ("libs/**",)

IMAGE_PREFIX = "ghcr.io/kakhavai/foundry"


class FilterError(Exception):
    """Something this module needs is not where it expects it."""


def load_fleet(root: Path):
    """Import `scripts/collectors.py` and return its service list.

    Loaded by path rather than `import collectors` so this works from any cwd
    and does not depend on `scripts/` being importable as a package.
    """
    source = root / "scripts" / "collectors.py"
    if not source.exists():
        # A traceback here sends the reader looking for a bug in this file when
        # the actual problem is always the directory it was run from: the
        # composite action relies on the runner's default cwd being the
        # workspace root.
        raise FilterError(
            f"no {source} — this must run from the repository root, or be "
            f"given one with --root (got {root})"
        )
    spec = importlib.util.spec_from_file_location("fleet_collectors", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleet_collectors"] = module
    spec.loader.exec_module(module)
    return module.services()


def filter_paths(service) -> list[str]:
    """The paths whose change should run `service`'s jobs."""
    paths = [f"services/{service.name}/**", f"helm/values/{service.name}/**"]
    if service.is_collector:
        paths.extend(COLLECTOR_PATHS)
    paths.extend(SHARED_PATHS)
    return paths


def matrix_entry(service) -> dict[str, str]:
    """One matrix leg: everything a job needs that used to be a literal.

    `package` is the importable name for the runtime-import check, and
    `context` is the Docker build context — the repo root for collectors,
    because they depend on `libs/collector-core/` by path and the lock cannot
    resolve without it in the context.

    `dockerfile` and `build_args` exist because every collector builds from the
    one root `Dockerfile.collector`, parameterised by SERVICE/PACKAGE/PORT.
    `build_args` is EMPTY for a non-collector — its own Dockerfile declares no
    ARGs, and passing them anyway makes BuildKit warn "one or more build args
    were not consumed" on every single build, which is how a repo teaches its
    readers to ignore warnings.

    Every field is derived — from registry membership and from the Helm values
    file Kubernetes actually applies — so adding a collector still changes no
    CI file. In particular PORT comes from `service.port`, so the port the
    container listens on cannot drift from the one the probes dial.
    """
    return {
        "name": service.name,
        "package": service.package,
        "context": "." if service.is_collector else f"services/{service.name}",
        "dockerfile": service.dockerfile,
        "build_args": build_args(service),
        "image": f"{IMAGE_PREFIX}/{service.name}",
    }


def build_args(service) -> str:
    """`docker build --build-arg` lines for this service, or '' if it takes none.

    The newline-separated form `docker/build-push-action`'s `build-args` input
    takes directly.
    """
    if not service.is_collector:
        return ""
    return f"SERVICE={service.name}\nPACKAGE={service.package}\nPORT={service.port}\n"


def build(fleet) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """(paths-filter rules, matrix entries) keyed by service name."""
    return (
        {service.name: filter_paths(service) for service in fleet},
        {service.name: matrix_entry(service) for service in fleet},
    )


def render_filters(rules: dict[str, list[str]]) -> str:
    """The rules as the YAML `dorny/paths-filter` takes in its `filters` input.

    Hand-rendered rather than `yaml.safe_dump`ed to keep the output stable and
    diffable: one service per block, paths in the order `filter_paths` returns
    them, single-quoted so a leading `.` or `*` cannot be read as YAML syntax.
    PyYAML is still needed to *read* the registry (scripts/collectors.py
    imports it), which is why the action wraps this in `uv run --with pyyaml`.
    """
    lines: list[str] = []
    for name, paths in rules.items():
        lines.append(f"{name}:")
        lines.extend(f"  - '{path}'" for path in paths)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--emit",
        choices=("filters", "entries"),
        required=True,
        help="'filters' for the paths-filter YAML, 'entries' for the JSON map.",
    )
    args = parser.parse_args(argv)

    # Bad input exits with a message, not a traceback: this runs inside a
    # composite action step whose failure output is the only thing a reader
    # sees, and a stack dump buries the one line that identifies the cause.
    try:
        fleet = load_fleet(args.root)
    except FilterError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    rules, entries = build(fleet)
    if args.emit == "filters":
        sys.stdout.write(render_filters(rules))
    else:
        json.dump(entries, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
