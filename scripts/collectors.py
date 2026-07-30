#!/usr/bin/env python3
"""The one place the local and CI tooling learns what the fleet contains.

Before this module, adding a collector meant editing six files that each kept
their own copy of the same list: `scripts/deploy-local.py`,
`scripts/stack-up.py`, `scripts/smoke-test.sh`, two separate steps of
`.github/workflows/integration-test.yml`, and the registry itself. With
twenty-four collectors queued that is twenty-four chances for two lists to
disagree, and the failure mode of a disagreement is quiet: a collector that is
registered but never deployed, or deployed but never smoke-tested.

Now all of them derive from two files that already exist:

    contracts/collector-registry.yaml   WHICH collectors exist  (the contract)
    helm/values/<name>/values.yaml      HOW each one is deployed (the artifact)

**Nothing was added to either.** That is the design decision, and it is worth
stating plainly because the obvious alternatives are worse:

  * Widening `collector-registry.yaml` with `port`, Secret names and a Docker
    build flag would publish deployment trivia through a *contract* that an
    out-of-repo consumer (the projections generator) reads to decide what to
    call. It has no use for any of it.
  * A sibling `contracts/collector-deploy.yaml` would be a second copy of
    facts the Helm values file already states — and the values file is the one
    Kubernetes actually obeys. A copy that drifts is worse than no copy:
    `deploy-local.py` would create `weather-collector-token` while the pod
    referenced something else, and the symptom is not a failed deploy but a
    Healthy pod answering 503 on every data route.
  * Convention (`<name>-collector-token`, sequential ports) is a fine
    convention, but reading the real value out of the applied artifact is
    strictly stronger than testing that somebody followed one.

So: the port is `service.port`, the bearer-token Secret is whatever
`COLLECTOR_TOKEN`'s `secretKeyRef` names, the lake Secret is whatever
`AWS_ACCESS_KEY_ID`'s does, and `CAPTURE_ENABLED` is read rather than assumed.
The single field with no home in either file — whether the Docker build needs
the repo root as its context — is not per-service data at all: every collector
depends on `libs/collector-core/` by path, so every collector builds from the
root. It is derived from registry membership and gated by
`tests/test_collector_tooling.py`.

Used as a library by `scripts/deploy-local.py` and `scripts/stack-up.py`, and
as a CLI by `scripts/smoke-test.sh` and `.github/workflows/integration-test.yml`:

    uv run --no-project --with pyyaml==6.0.3 python3 \
      scripts/collectors.py --names
    uv run --no-project --with pyyaml==6.0.3 python3 \
      scripts/collectors.py --smoke-targets

`--no-project` because this needs pyyaml and the standard library and nothing
from the uv workspace; without it, `uv run` from the repo root builds
collector-core and every service into a throwaway venv to run a file that never
imports them.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    # A traceback here sends the reader looking for a bug in this file. The
    # actual problem is always the interpreter it was launched with: the CI
    # runner image ships a bare python3, so every shell caller goes through
    # `uv run --no-project --with pyyaml==6.0.3`.
    print(
        "scripts/collectors.py needs PyYAML. Run it as:\n"
        "  uv run --no-project --with pyyaml==6.0.3 python3 scripts/collectors.py ...",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "contracts" / "collector-registry.yaml"
HELM_VALUES_DIR = ROOT / "helm" / "values"

# Deployed by the local and CI tooling but NOT collectors, so they have no
# registry entry: no gateway route, no /catalog, no signal types.
# `player-projections` polls S3 and serves the result. Named explicitly rather
# than inferred — there is no machine-readable inventory of non-collectors, and
# inventing one for a single service would be the seventh registration point
# this module exists to remove.
NON_COLLECTOR_SERVICES: tuple[str, ...] = ("player-projections",)

# The env vars whose `secretKeyRef` names the Secret `deploy-local.py` creates.
TOKEN_ENV = "COLLECTOR_TOKEN"
LAKE_ENV = "AWS_ACCESS_KEY_ID"


class FleetError(Exception):
    """A registry entry and its Helm values disagree, or one is missing."""


@dataclass(frozen=True)
class Service:
    """One deployable service, assembled from the registry and its Helm values."""

    name: str
    port: int
    is_collector: bool
    # Collectors only. `path` is the gateway prefix; the two Secret names are
    # read from the values file rather than derived from `name`, so a typo
    # there fails the deploy loudly instead of producing a pod that starts
    # fine and 503s on every data route.
    path: str | None = None
    token_secret: str | None = None
    lake_secret: str | None = None
    capture_enabled: bool = False

    @property
    def build_context_root(self) -> bool:
        """Whether `docker build` needs the repo root as its context.

        True for every collector, because every collector depends on the
        `libs/collector-core/` workspace member by path and the lock cannot
        resolve without it. Not a per-service flag — a property of being a
        collector, enforced by tests/test_collector_tooling.py.
        """
        return self.is_collector


def load_registry(registry_path: Path | None = None) -> list[dict]:
    """The registry's `collectors` list, in file (merge) order."""
    path = registry_path or REGISTRY_PATH
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "collectors" not in document:
        raise FleetError(
            f"{path} has no top-level 'collectors' key — see "
            f"contracts/collector-registry.schema.json"
        )
    entries = document["collectors"] or []
    if not entries:
        # A deploy loop over zero collectors succeeds having deployed nothing,
        # and the smoke test then fails somewhere far away from the cause.
        raise FleetError(f"{path} lists no collectors — nothing would be deployed")
    return entries


def load_values(name: str, values_dir: Path | None = None) -> dict:
    path = (values_dir or HELM_VALUES_DIR) / name / "values.yaml"
    if not path.exists():
        raise FleetError(
            f"{name}: no {path} — every deployable service needs a Helm values "
            f"file; it is where its port and Secret names are read from"
        )
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _extra_env(values: dict) -> dict[str, dict]:
    return {
        item["name"]: item
        for item in (values.get("extraEnv") or [])
        if isinstance(item, dict) and "name" in item
    }


def _secret_name(values: dict, env_name: str) -> str | None:
    entry = _extra_env(values).get(env_name) or {}
    ref = ((entry.get("valueFrom") or {}).get("secretKeyRef")) or {}
    return ref.get("name")


def _env_value(values: dict, env_name: str) -> str | None:
    entry = _extra_env(values).get(env_name) or {}
    value = entry.get("value")
    return None if value is None else str(value)


def _port(name: str, values: dict) -> int:
    port = (values.get("service") or {}).get("port")
    if not isinstance(port, int):
        raise FleetError(
            f"{name}: helm/values/{name}/values.yaml has no integer "
            f"service.port — the tooling reads the port from the artifact "
            f"Kubernetes applies rather than keeping a second copy"
        )
    return port


def services(
    registry_path: Path | None = None,
    values_dir: Path | None = None,
    non_collectors: tuple[str, ...] = NON_COLLECTOR_SERVICES,
) -> list[Service]:
    """Every service the local and CI tooling deploys, in deploy order.

    Collectors first, in registry (merge) order — which already respects
    `depends_on`, because the drift gate makes a dependency's entry merge
    first — then the non-collectors.
    """
    result: list[Service] = []
    for entry in load_registry(registry_path):
        name = entry["name"]
        values = load_values(name, values_dir)
        token_secret = _secret_name(values, TOKEN_ENV)
        if not token_secret:
            raise FleetError(
                f"{name}: helm/values/{name}/values.yaml declares no "
                f"{TOKEN_ENV} secretKeyRef. Without it the pod starts and then "
                f"answers 503 on every data route — a Healthy deploy that "
                f"serves nothing."
            )
        result.append(
            Service(
                name=name,
                port=_port(name, values),
                is_collector=True,
                path=entry["path"],
                token_secret=token_secret,
                lake_secret=_secret_name(values, LAKE_ENV),
                capture_enabled=_env_value(values, "CAPTURE_ENABLED") == "true",
            )
        )

    registered = {service.name for service in result}
    for name in non_collectors:
        if name in registered:
            raise FleetError(
                f"{name} is listed as a non-collector but also has a registry "
                f"entry — it would be deployed twice"
            )
        values = load_values(name, values_dir)
        result.append(Service(name=name, port=_port(name, values), is_collector=False))
    return result


def collectors(**kwargs) -> list[Service]:
    return [service for service in services(**kwargs) if service.is_collector]


def by_name(**kwargs) -> dict[str, Service]:
    return {service.name: service for service in services(**kwargs)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--names",
        action="store_true",
        help="Every deployable service name, one per line, in deploy order.",
    )
    mode.add_argument(
        "--collector-names",
        action="store_true",
        help="Collector names only, one per line, in registry order.",
    )
    mode.add_argument(
        "--smoke-targets",
        action="store_true",
        help=(
            "Tab-separated name/gateway-path/port/capture-enabled per "
            "collector, for scripts/smoke-test.sh to loop over."
        ),
    )
    mode.add_argument(
        "--port",
        metavar="NAME",
        help="The port of one service, for a shell caller that needs just that.",
    )
    args = parser.parse_args(argv)

    # Bad input exits with a message, not a traceback: these run under `set -e`
    # in a shell, where a stack dump buries the one line a reader needs.
    try:
        fleet = services()
    except (FleetError, OSError, yaml.YAMLError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if args.port:
        found = by_name().get(args.port)
        if found is None:
            print(f"FAIL: unknown service {args.port!r}", file=sys.stderr)
            return 2
        print(found.port)
        return 0

    if args.names:
        for service in fleet:
            print(service.name)
        return 0

    if args.collector_names:
        for service in fleet:
            if service.is_collector:
                print(service.name)
        return 0

    for service in fleet:
        if not service.is_collector:
            continue
        capture = "true" if service.capture_enabled else "false"
        print(f"{service.name}\t{service.path}\t{service.port}\t{capture}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
