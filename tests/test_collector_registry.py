"""The collector registry drift gate — the half that needs no cluster.

`contracts/collector-registry.yaml` is the source of truth for what the
collector fleet contains. A registry that quietly disagrees with the fleet is
worse than no registry: the projections generator reads it to decide what to
call, so a well-formatted lie is served with the same confidence as the truth.

`docs/architecture/phase-8-data-source-collectors.md` ("The Registry") asserts
three things. Where each one runs, and why:

  1. Every registry entry has a deployed collector, and every deployed
     collector has a registry entry.
     -> HERE, statically: an entry needs services/<name>/, a gateway-enabled
        Helm values stack whose pathPrefix equals `path`, an Argo CD
        Application, and a GitOps env values file. Reversed in two independent
        directions — every gateway-enabled values file must be registered, AND
        every service containing a CollectorDescriptor must be registered. The
        second catches a collector whose values file forgot `gateway.enabled`,
        which the first cannot see.
     -> ALSO at runtime: scripts/check-registry.py 404s on an entry whose
        service was never added to the cluster's deploy step.

  2. Each collector's live `GET /catalog` agrees with its registry entry.
     -> scripts/check-registry.py, from scripts/smoke-test.sh, inside the
        required `integration-test` check. This suite has no cluster.
     -> HERE, its two halves that can run without one: `compare_entry_to_
        catalog` is unit-tested directly, and the registry is checked against
        the service's own SOURCE CODE, read by AST.

     The AST read is the point. A committed `/catalog` fixture would be a copy
     of the answer, and a copy of the answer cannot detect that the answer
     changed. The service's own `CollectorDescriptor` is an independent second
     source of truth — it is what `/catalog` is built from at runtime, so
     agreeing with it is a real check that runs on every PR, cluster or not.

     Why AST and not an import: `platform-tests` installs pytest, pyyaml and
     jsonschema only. Importing services/weather/weather/main.py would pull in
     fastapi, httpx and prometheus_client, none of which are there.

  3. Every `depends_on` names a collector that exists.
     -> HERE. Purely a property of this file, plus self-dependency and cycles.

Not covered, stated rather than implied: `scope_aware` has no representation
in code today, so it is type-checked as a bool and its correctness is
human-reviewed. A green run says nothing about whether it is right.
"""

import ast
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "contracts" / "collector-registry.yaml"
SCHEMA_PATH = ROOT / "contracts" / "collector-registry.schema.json"
CADENCE_PATH = ROOT / "libs" / "collector-core" / "collector_core" / "cadence.py"
ENVELOPE_PATH = ROOT / "libs" / "collector-core" / "collector_core" / "envelope.py"
SERVICES_DIR = ROOT / "services"
HELM_VALUES_DIR = ROOT / "helm" / "values"

# Import check-registry.py (hyphenated) as a module — same pattern as
# tests/test_argocd_deploy.py and tests/test_chaos_scenarios.py.
_spec = importlib.util.spec_from_file_location(
    "check_registry", ROOT / "scripts" / "check-registry.py"
)
check_registry = importlib.util.module_from_spec(_spec)
sys.modules["check_registry"] = check_registry
_spec.loader.exec_module(check_registry)


def _load_registry() -> dict:
    return yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))


REGISTRY = _load_registry()
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
ENTRIES = REGISTRY["collectors"]
IDS = [entry["name"] for entry in ENTRIES]


# ── reading the code, without importing it ────────────────────────────────────

_UNRESOLVED = object()


def _cadence_members() -> dict[str, str]:
    """`CadenceClass`'s members, read out of cadence.py.

    Read rather than hardcoded so that adding a cadence class in code cannot
    drift away from what this gate believes the legal values are.
    """
    tree = ast.parse(CADENCE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CadenceClass":
            members = {}
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(
                    stmt.value, ast.Constant
                ):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            members[target.id] = stmt.value.value
            assert members, f"{CADENCE_PATH}: CadenceClass has no members"
            return members
    raise AssertionError(f"{CADENCE_PATH}: no CadenceClass definition found")


CADENCE_MEMBERS = _cadence_members()


def _envelope_version() -> str:
    """`ENVELOPE_VERSION`, read out of envelope.py."""
    tree = ast.parse(ENVELOPE_PATH.read_text(encoding="utf-8"))
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "ENVELOPE_VERSION":
                    return stmt.value.value
    raise AssertionError(f"{ENVELOPE_PATH}: no module-level ENVELOPE_VERSION found")


ENVELOPE_VERSION = _envelope_version()


def _service_sources(service_dir: Path) -> list[Path]:
    """Non-test .py files under a service, in a stable order."""
    return sorted(
        path
        for path in service_dir.rglob("*.py")
        if "tests" not in path.parts and not path.name.startswith("test_")
    )


def _resolve(node: ast.AST, table: dict) -> object:
    """Best-effort static value of an expression, or `_UNRESOLVED`."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        pass
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "CadenceClass" and node.attr in CADENCE_MEMBERS:
            return CADENCE_MEMBERS[node.attr]
        return _UNRESOLVED
    if isinstance(node, ast.Name):
        return table.get(node.id, _UNRESOLVED)
    if isinstance(node, (ast.Tuple, ast.List)):
        values = [_resolve(element, table) for element in node.elts]
        if any(value is _UNRESOLVED for value in values):
            return _UNRESOLVED
        return tuple(values)
    return _UNRESOLVED


def _constant_table(service_dir: Path) -> dict[str, object]:
    """Module-level constants across a whole service tree, flattened.

    Flattened rather than per-module because the descriptor's kwargs are
    typically imported from a sibling (weather's come from `capture.py`), and
    resolving imports properly would be a worse trade than a name collision
    that has never occurred in this repo. Two passes so a constant defined in
    a later file still resolves for an earlier one.
    """
    table: dict[str, object] = {}
    trees = [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in _service_sources(service_dir)
    ]
    for _ in range(2):
        for _path, tree in trees:
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign):
                    targets, value = stmt.targets, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                else:
                    continue
                resolved = _resolve(value, table)
                if resolved is _UNRESOLVED:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        table[target.id] = resolved
    return table


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_descriptor(service_dir: Path) -> tuple[Path, dict] | None:
    """The service's `CollectorDescriptor(...)` kwargs, resolved statically."""
    table = _constant_table(service_dir)
    for path in _service_sources(service_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node.func) == "CollectorDescriptor"
            ):
                kwargs = {}
                for keyword in node.keywords:
                    if keyword.arg is None:
                        continue
                    resolved = _resolve(keyword.value, table)
                    if resolved is not _UNRESOLVED:
                        kwargs[keyword.arg] = resolved
                return path, kwargs
    return None


def _services_with_a_descriptor() -> set[str]:
    found = set()
    for service_dir in sorted(SERVICES_DIR.iterdir()):
        if not service_dir.is_dir():
            continue
        if _find_descriptor(service_dir) is not None:
            found.add(service_dir.name)
    return found


def _gateway_enabled_values() -> dict[str, dict]:
    """Every helm/values/<name>/values.yaml with `gateway.enabled: true`."""
    enabled = {}
    for values_path in sorted(HELM_VALUES_DIR.glob("*/values.yaml")):
        values = yaml.safe_load(values_path.read_text(encoding="utf-8")) or {}
        if (values.get("gateway") or {}).get("enabled") is True:
            enabled[values_path.parent.name] = values
    return enabled


# ── the file itself ───────────────────────────────────────────────────────────


def test_registry_is_not_empty():
    """A drift gate over zero collectors passes having proved nothing."""
    assert ENTRIES, (
        f"{REGISTRY_PATH} lists no collectors — every assertion below would "
        f"pass vacuously"
    )


def test_registry_matches_its_schema():
    jsonschema.validate(REGISTRY, SCHEMA)


def test_collector_names_are_unique():
    names = [entry["name"] for entry in ENTRIES]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, (
        f"{REGISTRY_PATH}: duplicate collector name(s) {duplicates} — a name "
        f"is the join key between the registry, services/, and the gateway"
    )


def test_schema_cadence_enum_matches_the_code():
    """The schema's legal cadence values are the ones `CadenceClass` defines.

    Without this, adding a cadence class in code leaves the schema silently
    rejecting a value the fleet can legitimately declare.
    """
    schema_enum = set(
        SCHEMA["$defs"]["collector"]["properties"]["cadence_class"]["enum"]
    )
    assert schema_enum == set(CADENCE_MEMBERS.values()), (
        f"{SCHEMA_PATH}'s cadence_class enum {sorted(schema_enum)} has drifted "
        f"from collector_core.cadence.CadenceClass "
        f"{sorted(CADENCE_MEMBERS.values())}"
    )


# ── assertion 1: entry <-> deployed collector ─────────────────────────────────


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_entry_has_a_service_directory(entry):
    service_dir = SERVICES_DIR / entry["name"]
    assert service_dir.is_dir(), (
        f"registry entry {entry['name']!r} has no services/{entry['name']}/ — "
        f"the registry is the inventory of DEPLOYED collectors, not the plan. "
        f"An entry lands in the same PR as its service; the staging table in "
        f"docs/architecture/phase-8-data-source-collectors.md is where a "
        f"not-yet-built collector belongs."
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_entry_path_matches_its_helm_gateway_prefix(entry):
    name = entry["name"]
    values_path = HELM_VALUES_DIR / name / "values.yaml"
    assert values_path.exists(), (
        f"registry entry {name!r} has no {values_path.relative_to(ROOT)}"
    )
    values = yaml.safe_load(values_path.read_text(encoding="utf-8")) or {}
    gateway = values.get("gateway") or {}
    assert gateway.get("enabled") is True, (
        f"registry entry {name!r}: {values_path.relative_to(ROOT)} does not "
        f"set gateway.enabled: true, so the collector is not reachable at "
        f"{entry['path']} and the generator cannot call it"
    )
    assert gateway.get("pathPrefix") == entry["path"], (
        f"registry entry {name!r}: path drift — registry says "
        f"{entry['path']!r}, {values_path.relative_to(ROOT)} routes it at "
        f"{gateway.get('pathPrefix')!r}"
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_entry_is_deployed_by_gitops(entry):
    name = entry["name"]
    for relative in (
        Path("infra") / "gitops" / "argo" / f"{name}.yaml",
        Path("infra") / "gitops" / "envs" / "local" / name / "values.yaml",
    ):
        assert (ROOT / relative).exists(), (
            f"registry entry {name!r} has no {relative.as_posix()} — a "
            f"registered collector must actually be deployed"
        )


def test_every_gateway_enabled_service_is_registered():
    """Reverse direction one: routed at the gateway but absent from the file."""
    registered = {entry["name"] for entry in ENTRIES}
    unregistered = sorted(set(_gateway_enabled_values()) - registered)
    assert not unregistered, (
        f"service(s) {unregistered} are routed through the collector gateway "
        f"but have no entry in {REGISTRY_PATH.name} — the generator reads the "
        f"registry, so an unregistered collector is invisible to it"
    )


def test_every_service_with_a_descriptor_is_registered():
    """Reverse direction two: a collector whose values file forgot the gateway.

    Independent of the check above on purpose. A collector missing
    `gateway.enabled` is invisible to that one, and it is exactly the mistake
    that produces a service nobody can reach and nobody notices.
    """
    registered = {entry["name"] for entry in ENTRIES}
    unregistered = sorted(_services_with_a_descriptor() - registered)
    assert not unregistered, (
        f"service(s) {unregistered} build a CollectorDescriptor — they are "
        f"collectors — but have no entry in {REGISTRY_PATH.name}"
    )


# ── assertion 2's static half: the registry vs the service's own source ───────


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_entry_agrees_with_the_services_collector_descriptor(entry):
    name = entry["name"]
    service_dir = SERVICES_DIR / name
    if not service_dir.is_dir():
        pytest.skip("covered by test_entry_has_a_service_directory")

    found = _find_descriptor(service_dir)
    # Hard failure, never a skip: a registered collector whose descriptor
    # cannot be found would otherwise silently opt out of every assertion
    # below it, which is the vacuous pass this whole suite exists to prevent.
    assert found is not None, (
        f"registry entry {name!r}: no CollectorDescriptor(...) call found "
        f"under services/{name}/. Every collector builds one and passes it to "
        f"collector_core.app.build_collector_app — see services/weather/"
        f"weather/main.py. If this collector genuinely constructs its "
        f"descriptor some other way, this gate cannot verify it and the "
        f"convention needs revisiting rather than the entry exempting."
    )
    descriptor_path, kwargs = found
    where = descriptor_path.relative_to(ROOT).as_posix()

    for field in ("name", "cadence_class", "signal_types"):
        assert field in kwargs, (
            f"registry entry {name!r}: {where}'s CollectorDescriptor has no "
            f"statically-resolvable {field!r} kwarg, so the registry cannot "
            f"be checked against it"
        )

    assert kwargs["name"] == name, (
        f"registry entry {name!r}: {where} names the collector {kwargs['name']!r}"
    )
    assert str(kwargs["cadence_class"]) == str(entry["cadence_class"]), (
        f"registry entry {name!r}: cadence_class drift — registry says "
        f"{entry['cadence_class']!r}, {where} declares "
        f"{str(kwargs['cadence_class'])!r}"
    )
    declared = set(entry["signal_types"])
    in_code = set(kwargs["signal_types"])
    assert declared == in_code, (
        f"registry entry {name!r}: signal_types drift — registry says "
        f"{sorted(declared)}, {where} declares {sorted(in_code)}"
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_entry_envelope_version_matches_collector_core(entry):
    """Compared as strings — see contracts/collector-registry.yaml's header
    for why the two sides have different types today."""
    assert str(entry["envelope_version"]) == str(ENVELOPE_VERSION), (
        f"registry entry {entry['name']!r}: envelope_version "
        f"{entry['envelope_version']!r} does not match "
        f"collector_core.envelope.ENVELOPE_VERSION {ENVELOPE_VERSION!r}"
    )


# ── assertion 2's comparison logic, exercised without a cluster ───────────────


def _catalog_for(entry: dict) -> dict:
    return {
        "collector": entry["name"],
        "envelope_version": str(entry["envelope_version"]),
        "cadence_class": entry["cadence_class"],
        "signal_types": list(entry["signal_types"]),
    }


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_compare_accepts_a_catalog_that_agrees(entry):
    assert check_registry.compare_entry_to_catalog(entry, _catalog_for(entry)) == []


# Each mutation derives its wrong value from the correct one, so none of them
# can accidentally coincide with what the registry happens to say today.
@pytest.mark.parametrize(
    "mutate,expected",
    [
        (
            lambda c: c.update(collector=c["collector"] + "-impostor"),
            "identifies itself",
        ),
        (
            lambda c: c.update(envelope_version=str(int(c["envelope_version"]) + 1)),
            "envelope_version drift",
        ),
        (
            lambda c: c.update(cadence_class=c["cadence_class"] + "-drifted"),
            "cadence_class drift",
        ),
        (lambda c: c["signal_types"].append("surprise"), "signal_types drift"),
        (lambda c: c["signal_types"].clear(), "signal_types drift"),
        (lambda c: c.pop("cadence_class"), "no 'cadence_class' field"),
    ],
    ids=[
        "wrong-collector",
        "wrong-envelope-version",
        "wrong-cadence-class",
        "extra-signal-type",
        "missing-signal-types",
        "absent-field",
    ],
)
def test_compare_rejects_a_catalog_that_drifts(mutate, expected):
    entry = ENTRIES[0]
    catalog = _catalog_for(entry)
    mutate(catalog)
    problems = check_registry.compare_entry_to_catalog(entry, catalog)
    assert problems, f"drift went undetected: {catalog}"
    assert any(expected in problem for problem in problems), problems


def test_compare_tolerates_int_versus_string_envelope_version():
    """The registry stores an int and /catalog returns a string. Until that
    inconsistency is settled, agreement must not depend on which is which."""
    entry = dict(ENTRIES[0], envelope_version=1)
    catalog = dict(_catalog_for(entry), envelope_version="1")
    assert check_registry.compare_entry_to_catalog(entry, catalog) == []


# ── assertion 3: depends_on ───────────────────────────────────────────────────


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_depends_on_names_registered_collectors(entry):
    known = {other["name"] for other in ENTRIES}
    unknown = sorted(set(entry["depends_on"]) - known)
    assert not unknown, (
        f"registry entry {entry['name']!r}: depends_on names {unknown}, which "
        f"is not in the registry. A dependency's entry must merge first — the "
        f"registry is the inventory of deployed collectors, so depending on "
        f"an unregistered name means depending on something that is not "
        f"running."
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_no_collector_depends_on_itself(entry):
    assert entry["name"] not in entry["depends_on"], (
        f"registry entry {entry['name']!r} depends on itself"
    )


def test_depends_on_has_no_cycles():
    graph = {entry["name"]: list(entry["depends_on"]) for entry in ENTRIES}
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str, trail: list[str]) -> None:
        if node in done:
            return
        assert node not in visiting, (
            f"dependency cycle in {REGISTRY_PATH.name}: {' -> '.join([*trail, node])}"
        )
        visiting.add(node)
        for dependency in graph.get(node, []):
            walk(dependency, [*trail, node])
        visiting.discard(node)
        done.add(node)

    for name in graph:
        walk(name, [])


# ── scope_aware: stated, not implied ──────────────────────────────────────────


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_scope_aware_is_a_bool_and_nothing_more_is_claimed(entry):
    """There is no representation of scope-awareness in code today, so this
    is the whole of the automated check. Whether the value is CORRECT is
    human-reviewed at PR time — do not read a green run as confirming it."""
    assert isinstance(entry["scope_aware"], bool)
