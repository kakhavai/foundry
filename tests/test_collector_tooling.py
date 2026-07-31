"""The registry is the single registration point — proved, not asserted.

`tests/test_collector_registry.py` proves the registry agrees with the fleet.
This suite proves the *tooling* reads it: that `scripts/deploy-local.py`,
`scripts/stack-up.py`, `scripts/smoke-test.sh` and the two service-list steps
of `.github/workflows/integration-test.yml` all derive from
`contracts/collector-registry.yaml` plus each service's Helm values, rather
than keeping a fifth, sixth and seventh copy of the same list.

The load-bearing test here is `test_a_synthetic_collector_reaches_every_list`:
it invents a collector that exists nowhere in the repo and asserts it flows
through every derived list. That is the acceptance criterion for Wave 0 stated
as code — adding a collector touches the registry and the service's own files,
and nothing else.

Its companion is `test_no_collector_name_is_hardcoded_in_the_tooling`, which is
what stops the duplication growing back: the moment somebody writes a collector
name into one of those four files, it reds.

Deployment facts (port, Secret names, CAPTURE_ENABLED) deliberately live in
`helm/values/<name>/values.yaml` and nowhere else — see scripts/collectors.py's
docstring for why neither the registry nor a sibling deploy file was widened to
hold them. The tests below are what make that safe: they check the values file
actually carries what the tooling expects to find there.
"""

import importlib.util
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "contracts" / "collector-registry.yaml"
HELM_VALUES_DIR = ROOT / "helm" / "values"
SERVICES_DIR = ROOT / "services"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "integration-test.yml"
SMOKE_TEST_PATH = ROOT / "scripts" / "smoke-test.sh"
COLLECTORS_PATH = ROOT / "scripts" / "collectors.py"

# The four files that used to each keep their own copy of the service list.
DERIVED_TOOLING = (
    ROOT / "scripts" / "deploy-local.py",
    ROOT / "scripts" / "stack-up.py",
    SMOKE_TEST_PATH,
    WORKFLOW_PATH,
)

# Same import-a-hyphenated-script pattern as tests/test_collector_registry.py.
# collectors.py has no hyphen, but scripts/ is not a package either.
_spec = importlib.util.spec_from_file_location("fleet_collectors", COLLECTORS_PATH)
fleet = importlib.util.module_from_spec(_spec)
sys.modules["fleet_collectors"] = fleet
_spec.loader.exec_module(fleet)

REGISTRY = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
ENTRIES = REGISTRY["collectors"]
IDS = [entry["name"] for entry in ENTRIES]

SERVICES = fleet.services()
BY_NAME = {service.name: service for service in SERVICES}


# ── the derived lists agree with the registry ────────────────────────────────


def test_derived_collectors_are_exactly_the_registry():
    derived = [s.name for s in SERVICES if s.is_collector]
    assert derived == [entry["name"] for entry in ENTRIES], (
        "scripts/collectors.py's collector list has drifted from "
        "contracts/collector-registry.yaml — it is supposed to BE the registry"
    )


def test_derived_deploy_list_is_the_registry_plus_the_non_collectors():
    assert [s.name for s in SERVICES] == [
        *[entry["name"] for entry in ENTRIES],
        *fleet.NON_COLLECTOR_SERVICES,
    ]


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_derived_gateway_path_is_the_registry_path(entry):
    assert BY_NAME[entry["name"]].path == entry["path"]


# ── deployment facts come from the artifact Kubernetes applies ───────────────


@pytest.mark.parametrize("name", sorted(BY_NAME), ids=sorted(BY_NAME))
def test_port_is_the_helm_values_service_port(name):
    """Not a second copy in a registry or a deploy file — the applied value.

    A copy that drifts from the values file gives a port-forward to a port
    nothing is listening on, which reads as a broken service.
    """
    values = yaml.safe_load(
        (HELM_VALUES_DIR / name / "values.yaml").read_text(encoding="utf-8")
    )
    assert BY_NAME[name].port == values["service"]["port"]
    assert values["service"]["port"] == values["containerPort"], (
        f"{name}: service.port and containerPort disagree, so a port-forward "
        f"to the Service reaches nothing"
    )


def test_ports_are_unique():
    """Two services on one port means one port-forward silently wins."""
    ports = [s.port for s in SERVICES]
    duplicates = sorted({p for p in ports if ports.count(p) > 1})
    assert not duplicates, f"port(s) {duplicates} are claimed by more than one service"


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_collector_secrets_come_from_the_values_files_own_secret_refs(entry):
    """The Secret deploy-local.py creates is the one the pod references.

    Derived rather than conventional on purpose: a values file naming
    `weather-token` while the tooling created `weather-collector-token` would
    produce a pod that starts, reports Healthy, and answers 503 on every data
    route — the exact combination CLAUDE.md warns is not a broken deploy.
    """
    name = entry["name"]
    values = yaml.safe_load(
        (HELM_VALUES_DIR / name / "values.yaml").read_text(encoding="utf-8")
    )
    env = {item["name"]: item for item in values["extraEnv"]}

    token_ref = env["COLLECTOR_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert BY_NAME[name].token_secret == token_ref["name"]

    lake_ref = env["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]
    assert BY_NAME[name].lake_secret == lake_ref["name"]
    assert (
        env["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"]
        == (lake_ref["name"])
    ), f"{name}: the two lake credential keys come from different Secrets"


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_capture_enabled_is_read_not_assumed(entry):
    name = entry["name"]
    values = yaml.safe_load(
        (HELM_VALUES_DIR / name / "values.yaml").read_text(encoding="utf-8")
    )
    env = {item["name"]: item for item in values["extraEnv"]}
    assert BY_NAME[name].capture_enabled == (env["CAPTURE_ENABLED"]["value"] == "true")


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_collectors_build_from_the_repo_root(entry):
    """`build_context_root` is derived from being a collector, so prove it holds.

    Every collector depends on the `libs/collector-core/` workspace member by
    path, so `uv sync` cannot resolve its lock without the repo root in the
    build context. That is a property of being a collector rather than a
    per-service flag — but only as long as the dependency is really there.
    """
    name = entry["name"]
    assert BY_NAME[name].build_context_root is True
    pyproject = (SERVICES_DIR / name / "pyproject.toml").read_text(encoding="utf-8")
    assert "collector-core" in pyproject, (
        f"{name} is registered as a collector, so scripts/deploy-local.py "
        f"builds it from the repo root — but services/{name}/pyproject.toml "
        f"does not depend on collector-core, so that context is wrong"
    )


@pytest.mark.parametrize("name", fleet.NON_COLLECTOR_SERVICES)
def test_non_collectors_build_from_their_service_directory(name):
    assert BY_NAME[name].build_context_root is False
    assert BY_NAME[name].path is None


# ── the acceptance criterion, as code ────────────────────────────────────────


def _synthetic_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A registry and Helm values tree containing one collector, invented here.

    Deliberately not a copy of the real files: if the synthetic collector shows
    up in every derived list, the lists are genuinely derived rather than
    filtered against something committed.
    """
    registry = tmp_path / "collector-registry.yaml"
    registry.write_text(
        textwrap.dedent("""\
            collectors:
              - name: throwaway-signal
                path: /collectors/throwaway-signal
                stage: 8Z
                cadence_class: weekly
                envelope_version: 1
                signal_types: [throwaway_probe]
                scope_aware: false
                depends_on: []
            """),
        encoding="utf-8",
    )

    values_dir = tmp_path / "values"
    for name, port, capture in (
        ("throwaway-signal", 8099, "true"),
        ("player-projections", 8001, None),
    ):
        service_dir = values_dir / name
        service_dir.mkdir(parents=True)
        document = {
            "service": {"name": name, "port": port},
            "containerPort": port,
        }
        if capture is not None:
            document["extraEnv"] = [
                {
                    "name": "COLLECTOR_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": f"{name}-collector-token",
                            "key": "token",
                        }
                    },
                },
                {
                    "name": "AWS_ACCESS_KEY_ID",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": f"{name}-lake-credentials",
                            "key": "access-key-id",
                        }
                    },
                },
                {"name": "CAPTURE_ENABLED", "value": capture},
            ]
        (service_dir / "values.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    return registry, values_dir


def test_a_synthetic_collector_reaches_every_list(tmp_path):
    """Append one entry; it appears everywhere, with no other edit.

    This is Wave 0's whole promise. `throwaway-signal` exists in no committed
    file, has no services/ directory and no Helm values in the repo — only the
    two files a collector's PR touches, faked in a tmpdir.
    """
    registry, values_dir = _synthetic_repo(tmp_path)
    derived = fleet.services(registry_path=registry, values_dir=values_dir)

    names = [service.name for service in derived]
    assert names == ["throwaway-signal", "player-projections"], names

    fake = derived[0]
    assert fake.is_collector is True
    assert fake.path == "/collectors/throwaway-signal"
    assert fake.port == 8099
    assert fake.token_secret == "throwaway-signal-collector-token"
    assert fake.lake_secret == "throwaway-signal-lake-credentials"
    assert fake.capture_enabled is True
    # deploy-local.py builds it from the repo root without being told to.
    assert fake.build_context_root is True


def test_a_registry_entry_with_no_helm_values_fails_loudly(tmp_path):
    """The failure mode being prevented is a silent one, so prove it is loud."""
    registry, values_dir = _synthetic_repo(tmp_path)
    (values_dir / "throwaway-signal" / "values.yaml").unlink()
    with pytest.raises(fleet.FleetError, match="Helm values file"):
        fleet.services(registry_path=registry, values_dir=values_dir)


def test_a_collector_with_no_token_secret_fails_loudly(tmp_path):
    """A pod that starts, reports Healthy and 503s on every data route is the
    worst available outcome, so the deploy refuses instead."""
    registry, values_dir = _synthetic_repo(tmp_path)
    values_path = values_dir / "throwaway-signal" / "values.yaml"
    document = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    document["extraEnv"] = [
        item for item in document["extraEnv"] if item["name"] != "COLLECTOR_TOKEN"
    ]
    values_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(fleet.FleetError, match="COLLECTOR_TOKEN"):
        fleet.services(registry_path=registry, values_dir=values_dir)


def test_an_empty_registry_fails_rather_than_deploying_nothing(tmp_path):
    registry = tmp_path / "empty.yaml"
    registry.write_text("collectors: []\n", encoding="utf-8")
    with pytest.raises(fleet.FleetError, match="lists no collectors"):
        fleet.services(registry_path=registry)


# ── the duplication cannot grow back ─────────────────────────────────────────


@pytest.mark.parametrize("path", DERIVED_TOOLING, ids=[p.name for p in DERIVED_TOOLING])
def test_no_collector_name_is_hardcoded_in_the_tooling(path):
    """The regression guard for the whole of Wave 0.

    Each of these four files used to name every collector. If one of them names
    a collector again, the fleet has two lists that can disagree, and the
    disagreement is quiet: a collector deployed but never smoke-tested, or
    smoke-tested but never waited for.

    Non-collectors are exempt — `player-projections` has no registry entry, so
    naming it is the only way to deploy it.
    """
    text = path.read_text(encoding="utf-8")
    named = sorted(
        entry["name"]
        for entry in ENTRIES
        if re.search(rf"(?<![\w-]){re.escape(entry['name'])}(?![\w-])", text)
    )
    assert not named, (
        f"{path.relative_to(ROOT).as_posix()} names collector(s) {named}. It is "
        f"supposed to derive its list from contracts/collector-registry.yaml "
        f"via scripts/collectors.py — see that module's docstring."
    )


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _step(job: str, name: str) -> dict:
    steps = _workflow()["jobs"][job]["steps"]
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"{WORKFLOW_PATH.name}: no {name!r} step in job {job!r}")


def test_workflow_is_parseable_yaml():
    assert _workflow()["jobs"], WORKFLOW_PATH


@pytest.mark.parametrize("step_name", ["Deploy services", "Wait for pods ready"])
def test_workflow_service_lists_are_derived(step_name):
    run = _step("integration-test", step_name)["run"]
    assert "scripts/collectors.py" in run and "--names" in run, (
        f"the {step_name!r} step does not derive its service list from "
        f"scripts/collectors.py, so it is a second copy again"
    )


def test_wait_step_still_uses_rollout_status():
    """`wait --for=condition=ready pod -l <label>` also matches pods left
    Terminating by a rollout, which never reach Ready — so the wait times out
    on a Deployment that is in fact healthy. Do not let a rewrite lose this."""
    run = _step("integration-test", "Wait for pods ready")["run"]
    # Comments stripped: the step explains the label-selector trap in prose,
    # and the check is about what it RUNS, not what it says.
    commands = "\n".join(
        line for line in run.splitlines() if not line.lstrip().startswith("#")
    )
    assert "rollout status" in commands
    assert "--for=condition=ready pod -l" not in commands


def test_diagnose_failure_step_survives():
    """Before it existed, a pod-readiness failure printed `timed out waiting
    for the condition` and nothing else — no pod phase, no container state, no
    logs, no events — and two plausible-but-wrong hypotheses were pursued
    before OOMKilled surfaced. With 24 collectors coming, it stays."""
    step = _step("integration-test", "Diagnose failure")
    assert step["if"] == "failure()"
    run = step["run"]
    for expected in ("kubectl get pods", "kubectl describe pod", "kubectl logs"):
        assert expected in run, f"Diagnose failure no longer runs `{expected}`"


def test_changes_filter_still_covers_the_deployable_surface():
    steps = _workflow()["jobs"]["changes"]["steps"]
    filters = next(
        step["with"]["filters"] for step in steps if step.get("id") == "filter"
    )
    for surface in (
        "services/**",
        "libs/**",
        "helm/**",
        "infra/**",
        "scripts/**",
        "tests/**",
        "contracts/**",
        "chaos/**",
    ):
        assert surface in filters, f"the changes filter dropped {surface}"


# ── the smoke test's per-collector loop and its hooks ────────────────────────


def test_smoke_test_loops_over_the_registry():
    text = SMOKE_TEST_PATH.read_text(encoding="utf-8")
    assert "--smoke-targets" in text, (
        "scripts/smoke-test.sh no longer derives its collector list"
    )
    assert "services/$NAME/smoke.sh" in text, (
        "scripts/smoke-test.sh no longer runs the per-collector hook, so a "
        "collector with extra routes has nowhere to put its own assertions "
        "except this shared file"
    )


def test_smoke_test_still_asserts_direct_to_service_auth():
    """The direct-to-Service 401 is the assertion that justifies enforcing auth
    in-process at all: under gateway-only auth it would return 200 and this
    required check would pass over an unprotected path."""
    text = SMOKE_TEST_PATH.read_text(encoding="utf-8")
    direct = 'expect_status 401 "$NAME: direct Service call without token"'
    assert f'{direct} "$BASE/signals"' in text


def _bash_works() -> bool:
    """Whether `bash` on PATH can actually run.

    On a Windows checkout `bash` resolves to the WSL relay, which reports
    `execvpe(/bin/bash) failed` when no distribution is installed. That is an
    environment fact, not a broken script — skip locally, run for real in CI,
    where these files execute.
    """
    try:
        return subprocess.run(["bash", "-c", "true"], check=False).returncode == 0
    except OSError:
        return False


BASH_AVAILABLE = _bash_works()
needs_bash = pytest.mark.skipif(
    not BASH_AVAILABLE, reason="no working bash (Windows checkout); CI runs these"
)


@needs_bash
@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_any_smoke_hook_is_valid_bash(entry):
    """Optional — a collector with no routes beyond the standard five needs no
    file. When one exists it must parse, or it fails inside a running cluster."""
    hook = SERVICES_DIR / entry["name"] / "smoke.sh"
    if not hook.exists():
        pytest.skip(f"{entry['name']} has no extra routes to assert")
    result = subprocess.run(
        ["bash", "-n", str(hook)], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@needs_bash
def test_smoke_test_itself_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(SMOKE_TEST_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_shell_entrypoints_are_executable_or_run_with_bash():
    """The hooks are invoked as `bash <path>`, so the mode bit is not load
    bearing — asserted here only so a future change to exec them directly does
    not silently depend on a bit nobody set."""
    for entry in ENTRIES:
        hook = SERVICES_DIR / entry["name"] / "smoke.sh"
        if hook.exists():
            assert hook.read_text(encoding="utf-8").startswith("#!"), hook
            # Mode is informational on Windows checkouts; do not assert it.
            assert stat.S_ISREG(hook.stat().st_mode)
