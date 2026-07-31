"""A scaffolded collector must be correct **unmodified**, or the scaffolder is.

Twenty-four collectors are queued behind `scripts/new-collector.py`, so a defect
in its templates is not one bug, it is twenty-four — and each one arrives
already spread across a service tree that somebody then edits on top of. That
makes the scaffolder the single highest-leverage thing in the repo to gate, and
review is the wrong instrument: nobody reads a template and notices that a
26-character collector name pushes a docstring past 88 columns.

So this suite scaffolds a collector that exists nowhere in the repo, into a
tmpdir, and asserts the result:

  * consists of exactly the files a collector is supposed to consist of, and
    none of the ones Wave 0 removed;
  * lints clean (`ruff check` + `ruff format --check`) under the service's own
    ruff config;
  * renders and lints through the real Helm chart;
  * satisfies the registry drift gate — its `CollectorDescriptor`, read by the
    same AST logic `tests/test_collector_registry.py` uses, agrees with the
    registry entry the scaffolder appended;
  * reaches every derived tooling list (`scripts/collectors.py`) and the CI
    matrix (`.github/actions/changed-services`) with **no other file edited**;
  * keeps the two correctness patterns that are easy to delete and expensive to
    lose: the declared coverage floor, and the bounded poll after `POST
    /refresh`.

**The name is deliberately long.** `injury-designation-reports` is 26
characters, longer than any collector in the fleet today, because every E501
the scaffolder ever produced came from interpolating a long name into a
docstring or comment that fit fine for `weather`.

**What this cannot check, stated rather than implied.** It does not run the
generated pytest suite and does not `docker build`, because both need a
resolved uv environment for a workspace member that does not exist in the
tmpdir — network and lock work that does not belong in a 10-minute job. Those
are covered by actually scaffolding into the repo, which is what a collector
agent does on its first command anyway.
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# Long on purpose — see the module docstring.
NAME = "injury-designation-reports"
PACKAGE = "injury_designation_reports"
SIGNAL_TYPES = ("injury_designation_weekly", "injury_practice_participation")
CADENCE = "volatile"
PORT = 8404

_spec = importlib.util.spec_from_file_location(
    "new_collector", ROOT / "scripts" / "new-collector.py"
)
new_collector = importlib.util.module_from_spec(_spec)
sys.modules["new_collector"] = new_collector
_spec.loader.exec_module(new_collector)


# ── the synthetic repository ──────────────────────────────────────────────────

# Everything the scaffolder reads, and nothing else. Copied rather than pointed
# at, so a test that accidentally writes cannot touch the real tree — and so the
# "no other file was edited" assertion below has something exact to compare to.
SEEDED = (
    "scripts/collectors.py",
    "libs/collector-core/collector_core/cadence.py",
    "libs/collector-core/collector_core/envelope.py",
    ".github/actions/changed-services/filters.py",
)
SEEDED_TREES = ("contracts", "helm")


def _seed(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in SEEDED:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for tree in SEEDED_TREES:
        shutil.copytree(ROOT / tree, root / tree)
    return root


def _inventory(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


@pytest.fixture(scope="module")
def scaffolded(tmp_path_factory) -> tuple[Path, set[str], set[str]]:
    """Scaffold once for the whole module. Returns (root, before, after)."""
    root = _seed(tmp_path_factory.mktemp("scaffold"))
    before = _inventory(root)

    collector = new_collector.plan(
        root,
        name=NAME,
        port=PORT,
        cadence=CADENCE,
        signal_types=list(SIGNAL_TYPES),
        stage="8Z",
        depends_on=["player-identity"],
        scope_aware=False,
        adapter=None,
        smoke_hook=True,
    )
    new_collector.scaffold(root, collector)
    return root, before, _inventory(root)


@pytest.fixture(scope="module")
def root(scaffolded) -> Path:
    return scaffolded[0]


@pytest.fixture(scope="module")
def service(root) -> Path:
    return root / "services" / NAME


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── exactly the right files, and none of the removed ones ─────────────────────


EXPECTED_FILES = (
    f"services/{NAME}/{PACKAGE}/__init__.py",
    f"services/{NAME}/{PACKAGE}/main.py",
    f"services/{NAME}/{PACKAGE}/capture.py",
    f"services/{NAME}/{PACKAGE}/metrics.py",
    f"services/{NAME}/{PACKAGE}/signals.py",
    f"services/{NAME}/{PACKAGE}/adapters/__init__.py",
    f"services/{NAME}/{PACKAGE}/adapters/upstream.py",
    f"services/{NAME}/tests/__init__.py",
    f"services/{NAME}/tests/conftest.py",
    f"services/{NAME}/tests/test_routes.py",
    f"services/{NAME}/tests/test_capture_contract_conformance.py",
    f"services/{NAME}/tests/test_coverage_floor.py",
    f"services/{NAME}/Dockerfile",
    f"services/{NAME}/pyproject.toml",
    f"services/{NAME}/README.md",
    f"services/{NAME}/smoke.sh",
    f"helm/values/{NAME}/values.yaml",
    f"infra/gitops/envs/local/{NAME}/values.yaml",
    f"contracts/signal-envelope/collectors/{NAME}.json",
)


def test_the_generated_file_set_is_exactly_what_a_collector_consists_of(scaffolded):
    """Both directions. A missing file is an obvious failure; an EXTRA one is
    the failure that matters, because it is how a per-collector copy of
    something shared grows back twenty-four times over."""
    _, before, after = scaffolded
    assert after - before == set(EXPECTED_FILES)


def test_the_registry_is_the_only_shared_file_touched(scaffolded):
    """The whole of Wave 0's promise, as an assertion: appending one entry is
    the entire registration. Anything else edited here is a bug in the tooling
    that must be fixed there, not absorbed by the scaffolder."""
    root, before, _ = scaffolded
    registry = "contracts/collector-registry.yaml"
    changed = sorted(
        relative
        for relative in before
        if _read(root / relative) != _read(ROOT / relative)
    )
    assert changed == [registry], changed


@pytest.mark.parametrize(
    "removed",
    [
        f".github/workflows/{NAME}.yml",
        f"infra/gitops/argo/{NAME}.yaml",
        f"services/{NAME}/{PACKAGE}/telemetry.py",
        f"services/{NAME}/{PACKAGE}/auth.py",
        f"services/{NAME}/{PACKAGE}/scheduler.py",
        f"services/{NAME}/uv.lock",
    ],
)
def test_wave_0_removals_are_not_regenerated(root, removed):
    """Each of these existed per-service before Wave 0. Regenerating any one of
    them would quietly restore the duplication Wave 0 deleted — and it would
    look like a working collector while doing it."""
    assert not (root / removed).exists()


def test_no_generated_file_mentions_a_telemetry_module(service):
    """`telemetry_module` defaults to `collector_core.telemetry`. A collector
    that sets it is either forking the shared wiring or — far worse — passing a
    callable, which defeats the OTel import guard while every test stays green.
    """
    for path in service.rglob("*.py"):
        text = _read(path)
        assert "telemetry_module=" not in text, path
        assert "import telemetry" not in text, path


# ── it is valid Python, and it lints ──────────────────────────────────────────


def _generated_python(service: Path) -> list[Path]:
    return sorted(service.rglob("*.py"))


def test_generated_python_files_exist(service):
    """Guards every parametrized test below from passing vacuously."""
    assert len(_generated_python(service)) >= 10


@pytest.mark.parametrize(
    "relative",
    [f for f in EXPECTED_FILES if f.endswith(".py")],
    ids=lambda f: Path(f).name,
)
def test_every_generated_python_file_parses(root, relative):
    ast.parse(_read(root / relative), filename=relative)


def test_no_generated_line_exceeds_the_line_length(service):
    """ruff's E501, checked here too because it is the failure a long collector
    name reintroduces and this check needs no toolchain to run."""
    over = [
        f"{path.name}:{number}"
        for path in _generated_python(service)
        for number, line in enumerate(_read(path).splitlines(), start=1)
        if len(line) > 88
    ]
    assert not over, over


def _uv() -> str | None:
    return shutil.which("uv")


def test_the_toolchain_the_lint_and_helm_checks_need_is_present_in_ci():
    """The two heaviest checks below skip when their tool is missing, which is
    right on a developer's box and wrong in CI — a silently skipped lint gate
    is a lint gate that does not exist. `platform-tests` installs both
    (azure/setup-helm, astral-sh/setup-uv), so in CI their absence is a broken
    workflow, not an environment quirk."""
    if not os.getenv("CI"):
        pytest.skip("local run; CI is where the skips would be dishonest")
    assert _uv(), "uv is not on PATH — the generated-service lint gate would skip"
    assert _helm(), "helm is not on PATH — the chart render gate would skip"


@pytest.mark.skipif(_uv() is None, reason="uv not on PATH; CI's platform-tests has it")
@pytest.mark.parametrize("command", [["check"], ["format", "--check"]], ids=str)
def test_the_generated_service_lints_clean(service, command):
    """`ruff check` and `ruff format --check`, run exactly as CI runs them and
    against the generated service's own pyproject.toml config."""
    result = subprocess.run(
        [_uv(), "run", "--no-project", "--with", "ruff", "ruff", *command, "."],
        check=False,
        cwd=service,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── it renders through the real Helm chart ────────────────────────────────────


def _helm() -> str | None:
    return shutil.which("helm")


HELM_REASON = "helm not on PATH; CI's platform-tests installs it"


@pytest.mark.skipif(_helm() is None, reason=HELM_REASON)
def test_the_generated_values_lint(root):
    result = subprocess.run(
        [
            _helm(),
            "lint",
            str(root / "helm" / "charts" / "generic-service"),
            "-f",
            str(root / "helm" / "values" / NAME / "values.yaml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(_helm() is None, reason=HELM_REASON)
def test_the_generated_values_render_the_expected_objects(root):
    """Rendered, not just linted: a values file can lint fine and still produce
    a Deployment on the wrong port or an HTTPRoute on the wrong prefix."""
    result = subprocess.run(
        [
            _helm(),
            "template",
            NAME,
            str(root / "helm" / "charts" / "generic-service"),
            "-f",
            str(root / "helm" / "values" / NAME / "values.yaml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    documents = [d for d in yaml.safe_load_all(result.stdout) if d]
    kinds = {d["kind"] for d in documents}
    assert {"Deployment", "Service", "ConfigMap", "HTTPRoute"} <= kinds, kinds

    deployment = next(d for d in documents if d["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["ports"][0]["containerPort"] == PORT
    env = {item["name"]: item for item in container["env"]}
    token = env["COLLECTOR_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert token["name"] == f"{NAME}-collector-token"
    # optional: true is what lets the pod start before the Secret syncs. Without
    # it the pod does not start at all, which is a different (and louder) bug
    # than the 503-while-Healthy one the flag creates.
    assert token["optional"] is True

    route = next(d for d in documents if d["kind"] == "HTTPRoute")
    prefixes = {
        match["path"]["value"]
        for rule in route["spec"]["rules"]
        for match in rule["matches"]
    }
    assert prefixes == {
        f"/collectors/{NAME}/catalog",
        f"/collectors/{NAME}/signals",
        f"/collectors/{NAME}/refresh",
    }, prefixes
    assert not any("/health" in p or "/metrics" in p for p in prefixes), (
        "/health and /metrics are exempt from bearer auth in-process; publishing "
        "them at the edge would expose two unauthenticated routes"
    )


# ── the registry drift gate is satisfied ──────────────────────────────────────


def _registry(root: Path) -> list[dict]:
    document = yaml.safe_load(_read(root / "contracts" / "collector-registry.yaml"))
    return document["collectors"]


def _entry(root: Path) -> dict:
    return next(e for e in _registry(root) if e["name"] == NAME)


def test_the_appended_entry_matches_the_registry_schema(root):
    schema = json.loads(_read(root / "contracts" / "collector-registry.schema.json"))
    jsonschema.validate({"collectors": _registry(root)}, schema)


def test_the_append_did_not_disturb_the_existing_entries(root):
    """The file is append-only and carries a long explanatory header plus
    per-entry commentary. Re-serialising it with yaml.safe_dump would silently
    delete all of that and reorder the entries."""
    original = _read(ROOT / "contracts" / "collector-registry.yaml")
    appended = _read(root / "contracts" / "collector-registry.yaml")
    assert appended.startswith(original.rstrip("\n") + "\n")


def test_the_entry_agrees_with_the_generated_descriptor(root):
    """The same AST read `tests/test_collector_registry.py` performs, applied to
    the freshly generated service. This is what proves a scaffolded collector
    passes the drift gate on its first CI run rather than after a fixup."""
    registry_gate = importlib.util.spec_from_file_location(
        "registry_gate", ROOT / "tests" / "test_collector_registry.py"
    )
    module = importlib.util.module_from_spec(registry_gate)
    registry_gate.loader.exec_module(module)

    found = module._find_descriptor(root / "services" / NAME)
    assert found is not None, (
        "no CollectorDescriptor(...) could be resolved out of the generated "
        "service — the registry drift gate would fail this collector"
    )
    _path, kwargs = found

    entry = _entry(root)
    assert kwargs["name"] == entry["name"] == NAME
    assert str(kwargs["cadence_class"]) == entry["cadence_class"] == CADENCE
    assert set(kwargs["signal_types"]) == set(entry["signal_types"])
    assert set(kwargs["signal_types"]) == set(SIGNAL_TYPES)


def test_the_entry_envelope_version_is_the_string_from_collector_core(root):
    """Compared exactly, type included: `1` against `"1"` is drift, not a
    formatting preference."""
    assert _entry(root)["envelope_version"] == new_collector.envelope_version(ROOT)
    assert isinstance(_entry(root)["envelope_version"], str)


def test_the_gateway_prefix_agrees_with_the_registry_path(root):
    values = yaml.safe_load(_read(root / "helm" / "values" / NAME / "values.yaml"))
    assert values["gateway"]["enabled"] is True
    assert values["gateway"]["pathPrefix"] == _entry(root)["path"]
    assert values["service"]["port"] == values["containerPort"] == PORT


# ── it reaches every derived list, with nothing else edited ───────────────────


def _fleet(root: Path):
    return new_collector.load_fleet_module(root)


def test_the_deploy_and_smoke_lists_pick_it_up(root):
    """`deploy-local.py`, `stack-up.py`, `smoke-test.sh` and both service-list
    steps of integration-test.yml all derive from this."""
    fleet = _fleet(root)
    services = fleet.services()
    found = next(service for service in services if service.name == NAME)

    assert found.is_collector is True
    assert found.path == f"/collectors/{NAME}"
    assert found.port == PORT
    assert found.token_secret == f"{NAME}-collector-token"
    assert found.lake_secret == f"{NAME}-lake-credentials"
    # Collectors build from the repo root — they depend on libs/collector-core
    # by path and the lock cannot resolve without it in the context.
    assert found.build_context_root is True
    # Collectors deploy before the non-collectors, in registry (merge) order.
    assert [s.name for s in services][-2] == NAME


def test_the_ci_matrix_picks_it_up(root):
    """No `.github/workflows/<name>.yml` exists, so this is the only thing
    standing between a new collector and having no CI at all."""
    filters = importlib.util.spec_from_file_location(
        "changed_services_filters",
        root / ".github" / "actions" / "changed-services" / "filters.py",
    )
    module = importlib.util.module_from_spec(filters)
    filters.loader.exec_module(module)

    rules, entries = module.build(module.load_fleet(root))
    assert NAME in rules and NAME in entries
    assert f"services/{NAME}/**" in rules[NAME]
    assert f"helm/values/{NAME}/**" in rules[NAME]
    # Collectors depend on collector-core through the workspace, so a change
    # there must run their jobs.
    assert "libs/**" in rules[NAME]
    assert entries[NAME]["package"] == PACKAGE
    assert entries[NAME]["context"] == "."


def test_the_generated_field_schema_covers_exactly_the_signal_types(root):
    schema = json.loads(
        _read(root / "contracts" / "signal-envelope" / "collectors" / f"{NAME}.json")
    )
    assert set(schema["signal_types"]) == set(SIGNAL_TYPES)
    for signal_type, field_schema in schema["signal_types"].items():
        jsonschema.Draft202012Validator.check_schema(field_schema), signal_type


# ── the Dockerfile keeps the shared stage ─────────────────────────────────────


def test_the_dockerfile_uses_the_shared_workspace_manifests_stage(service):
    """Not a stylistic preference. `uv sync --package <x>` resolves the whole
    workspace graph first, so a per-member COPY list is quadratic and the line
    you forget breaks an UNRELATED service's image."""
    text = _read(service / "Dockerfile")
    assert "AS workspace-manifests" in text
    assert "COPY --from=workspace-manifests /manifests/ ./" in text
    per_member = re.compile(
        r"^\s*COPY\s+(?:--\S+\s+)*services/[^/\s]+/pyproject\.toml\b", re.MULTILINE
    )
    assert not per_member.search(text), (
        "the generated Dockerfile reintroduced a per-member manifest COPY"
    )


def test_the_final_sync_reinstalls_the_service_package(service):
    """The package version never changes (0.1.0), so uv's build cache can serve
    a previously built wheel for changed source and the image silently ships
    stale code. Observed twice on roster-scope."""
    joined = re.sub(r"\\\s*\n\s*", " ", _read(service / "Dockerfile"))
    final = [
        line
        for line in joined.splitlines()
        if "uv sync" in line and "--no-editable" in line
    ]
    assert final, "no final `uv sync --no-editable` in the generated Dockerfile"
    for line in final:
        assert f"--reinstall-package {NAME}" in line, line


def test_the_dockerfile_matches_the_reference_collectors_stage():
    """The stage is verbatim across the fleet. If the shared one changes and
    the scaffolder's does not, every collector generated afterwards is wrong.

    The reference is now the root `Dockerfile.collector` — the ONE file that
    builds every collector — because `services/weather/Dockerfile` and its
    eight siblings no longer exist.

    Which makes this whole section a transitional check: the scaffolder should
    stop templating a Dockerfile at all, since the file it generates would
    immediately fail
    `tests/test_dockerfile_workspace.py::test_no_collector_has_its_own_dockerfile`
    once committed. Until `scripts/new-collector.py` drops `DOCKERFILE` and its
    `services/<name>/Dockerfile` output, this at least keeps the generated copy
    from drifting away from the real one.
    """

    def commands(text: str) -> list[str]:
        """The stage's instructions, comments and banner styling stripped.

        Compared on what it RUNS, not on how it is decorated: the collectors
        word their explanatory banners differently and always will.
        """
        body = text.split("AS workspace-manifests", 1)[1].split("FROM python", 1)[0]
        joined = re.sub(r"\\\s*\n\s*", " ", body)
        return [
            re.sub(r"\s+", " ", line).strip()
            for line in joined.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    assert commands(new_collector.DOCKERFILE) == commands(
        _read(ROOT / "Dockerfile.collector")
    )


# ── the two correctness patterns survive ──────────────────────────────────────


def test_the_coverage_floor_is_declared_not_derived(root):
    """`coverage.expected` must never come from what a fetch returned: a
    truncated upstream would otherwise report `expected: 100, present: 100`,
    ratio 1.0 — perfectly healthy while 96% of the league silently vanished.

    Checked by AST rather than by string match, so reformatting the template
    cannot make this pass while the pattern is gone.
    """
    tree = ast.parse(_read(root / "services" / NAME / PACKAGE / "capture.py"))

    # `ast.AnnAssign` as well as `ast.Assign`: the generated declaration is
    # annotated (`EXPECTED_FLOOR: dict[str, int] = {...}`), and matching only
    # the bare form would make this test silently stop checking anything.
    floors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "EXPECTED_FLOOR" for t in targets):
            floors.append(value)

    assert floors, "capture.py declares no EXPECTED_FLOOR"
    floor = floors[0]
    assert isinstance(floor, ast.Dict), "EXPECTED_FLOOR must be a literal mapping"
    keys = {key.value for key in floor.keys}
    assert keys == set(SIGNAL_TYPES), keys
    assert all(
        isinstance(value, ast.Constant) and value.value >= 1 for value in floor.values
    ), "every floor must be a declared constant of at least 1"

    accumulators = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "CoverageAccumulator"
    ]
    assert accumulators, "capture.py never builds a CoverageAccumulator"
    for call in accumulators:
        assert any(keyword.arg == "floor" for keyword in call.keywords), (
            "CoverageAccumulator is built without `floor=`, so a truncated "
            "upstream would report full coverage"
        )


def test_a_failed_capture_goes_through_fail_capture(root):
    """A poll that fails must still write a `present: 0` envelope with populated
    `errors`, so a gap in the append-only lake is explicit rather than inferred
    from absence — and must re-raise, so `CaptureState` does not install the
    empty capture over the last good one."""
    text = _read(root / "services" / NAME / PACKAGE / "capture.py")
    tree = ast.parse(text)

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "fail_capture" in imported

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "fail_capture"
    ]
    assert calls, "capture.py imports fail_capture but never calls it"
    keywords = {keyword.arg for keyword in calls[0].keywords}
    assert {"collector", "signal_types", "lake", "metrics", "scope"} <= keywords
    assert "expected" in keywords, (
        "fail_capture must be given the per-signal-type floor, or a total "
        "outage floors to 1 and the coverage ratio reads better than it is"
    )


def test_the_capture_tail_is_the_shared_one_not_a_hand_written_write_loop(root):
    """Two invariants in one, because one mechanism now carries both.

    `LakeWriter` is synchronous boto3 and the lake handed to a collector raises
    if called from the loop thread, so a generated capture must never reach
    `lake.write(...)` directly.

    And the write loop must not be hand-written at all. Nine collectors wrote
    their own, and eight let a failed write escape -- discarding a capture that
    had already succeeded, so an object-store outage cost `/signals` its
    availability rather than its freshness.
    `collector_core.publish.publish_capture` owns the tail: it writes off the
    loop, records coverage, counts the failure, and returns the envelopes. A
    scaffolder that emits the old shape re-acquires the defect seventeen more
    times.
    """
    text = _read(root / "services" / NAME / PACKAGE / "capture.py")
    assert "publish_capture(envelopes, lake=lake, metrics=metrics)" in text
    assert "from collector_core.publish import publish_capture" in text
    assert "lake.write(" not in text
    assert "await awrite(lake," not in text, (
        "the generated capture hand-rolls the write loop the shared tail exists to own"
    )


def test_the_generated_capture_does_not_double_count_a_capture_failure(root):
    """`fail_capture` records `collector_capture_failures_total` itself. A
    generated call to it immediately before counts the same failure twice, and
    twenty-four collectors would inherit that.

    Parsed rather than string-matched: the generated file *talks* about the
    counter in a comment, and the thing under test is whether it calls it.
    """
    text = _read(root / "services" / NAME / PACKAGE / "capture.py")
    assert "await fail_capture(" in text

    tree = ast.parse(text)
    recorded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "capture_failure"
    ]
    assert recorded == [], (
        "the generated capture records capture_failure itself; fail_capture "
        "and publish_capture already own that counter"
    )


def test_the_route_test_polls_after_refresh_rather_than_reading(root):
    """`POST /refresh` returns 202 — accepted, not done. A generated test that
    reads `/signals` on the next line is racy by construction, and it is a race
    that used to be won by accident: nothing in the capture path yielded until
    the lake write moved to `asyncio.to_thread`. Twenty-four collectors will
    copy whatever shape ships here."""
    text = _read(root / "services" / NAME / "tests" / "test_routes.py")
    assert "def wait_for_signals(" in text, "no bounded-poll helper was generated"
    assert "time.monotonic()" in text, "the poll is not bounded by a deadline"
    assert "raise AssertionError(" in text, (
        "the poll must fail loudly with what it saw, not hang or assert against "
        "an empty envelope"
    )

    naive_read = re.compile(
        r'client\.post\("/refresh".*\n\s*(?:assert\s+)?\w*\s*=?\s*client\.get\('
        r'"/signals"'
    )
    # The guard guards: prove the pattern matches the bad shape before trusting
    # it not to match the generated one. A regex that matches nothing passes
    # this test for the wrong reason.
    assert naive_read.search(
        '    client.post("/refresh", json={})\n    body = client.get("/signals")'
    ), "the naive-read detector matches nothing, so it proves nothing"

    naive = naive_read.search(text)
    assert naive is None, (
        "a generated test reads /signals immediately after POST /refresh — that "
        "is a race, not a test"
    )


def test_the_route_test_asserts_auth_is_enforced_in_process(root):
    """Enforcement is in the service, not the gateway, precisely because
    smoke-test.sh port-forwards the Service directly. A collector whose suite
    never checks that would ship an unprotected ClusterIP."""
    text = _read(root / "services" / NAME / "tests" / "test_routes.py")
    assert "401" in text and "503" in text


# ── the scaffolder refuses bad requests ───────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"name": "Injury_Reports"}, "kebab-case"),
        ({"cadence": "hourly"}, "unknown cadence class"),
        ({"signal_types": []}, "--signal-types is required"),
        ({"signal_types": ["Bad-Signal"]}, "snake_case"),
        ({"name": "weather"}, "already in the fleet"),
        ({"depends_on": ["nonexistent-collector"]}, "not a registered collector"),
        ({"port": 8000}, "already claimed"),
    ],
    ids=[
        "bad-name",
        "bad-cadence",
        "no-signal-types",
        "bad-signal-type",
        "duplicate-name",
        "unknown-dependency",
        "taken-port",
    ],
)
def test_an_invalid_request_is_refused_with_a_message(root, kwargs, expected):
    """Refused before anything is written. A half-scaffolded collector is worse
    than none: the registry gate would red on a service that half exists."""
    request = dict(
        name="probe-signal",
        port=PORT + 1,
        cadence=CADENCE,
        signal_types=list(SIGNAL_TYPES),
        stage="8Z",
        depends_on=[],
        scope_aware=False,
        adapter=None,
        smoke_hook=False,
    )
    request.update(kwargs)
    with pytest.raises(new_collector.ScaffoldError, match=expected):
        new_collector.plan(root, **request)


def test_scaffolding_over_an_existing_collector_is_refused(root):
    """Without --force. Silently overwriting a collector's own capture logic
    with a placeholder would be the most destructive thing this script could
    do."""
    collector = new_collector.plan(
        root,
        name="probe-signal",
        port=PORT + 2,
        cadence=CADENCE,
        signal_types=list(SIGNAL_TYPES),
        stage="8Z",
        depends_on=[],
        scope_aware=False,
        adapter=None,
        smoke_hook=False,
    )
    new_collector.scaffold(root, collector)
    with pytest.raises(new_collector.ScaffoldError, match="refusing to overwrite"):
        new_collector.scaffold(root, collector)


def test_an_unknown_template_token_is_an_error_not_a_silent_passthrough():
    """A `%%typo%%` left in place becomes a syntax error two steps later, in a
    file nobody wrote."""
    collector = new_collector.plan(
        ROOT,
        name="probe-signal",
        port=8499,
        cadence=CADENCE,
        signal_types=["probe_reading"],
        stage="8Z",
        depends_on=[],
        scope_aware=False,
        adapter=None,
        smoke_hook=False,
    )
    with pytest.raises(new_collector.ScaffoldError, match="unknown token"):
        new_collector.render("hello %%not_a_token%%", collector)
