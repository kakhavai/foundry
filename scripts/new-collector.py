#!/usr/bin/env python3
"""Scaffold a new collector — the whole of it, and deliberately not one line more.

    uv run --no-project --with pyyaml==6.0.3 python3 scripts/new-collector.py \
        betting-lines --cadence volatile --signal-types game_line_snapshot

Twenty-four collectors are queued behind this script, so its output shape *is*
the fleet's shape. That cuts both ways: a template that emits a file per
collector makes duplication cheap to create and expensive to change, twenty-four
times over. So this generates only what genuinely remains after Wave 0, and the
list is much shorter than it used to be.

**What it does NOT generate, and why:**

  .github/workflows/<name>.yml   There is no per-service workflow. `services.yml`
                                 is a matrix computed from the fleet by
                                 `.github/actions/changed-services`.
  infra/gitops/argo/<name>.yaml  There is no per-service Application.
                                 `applicationset.yaml` globs `helm/values/*`.
  <pkg>/telemetry.py             `collector_core.telemetry` is the fleet's one
                                 copy. `telemetry_module` defaults to it.
  <pkg>/auth.py                  Bearer middleware is mounted by
                                 `build_collector_app`.
  <pkg>/scheduler.py             The capture loop is `collector_core.scheduler`.
                                 Write one only for a bespoke `next_event_at`.
  per-member Dockerfile COPYs    The shared `workspace-manifests` stage handles
                                 the whole workspace by glob.
  edits to deploy-local.py,      All derived from the registry entry below via
  stack-up.py, smoke-test.sh,    `scripts/collectors.py`. If a generated
  integration-test.yml           collector ever needs one of these edited, that
                                 is a bug in the tooling, not a step here.

**The one shared file it touches is `contracts/collector-registry.yaml`**, which
is append-only by design so two collectors on two branches merge cleanly. The
root `pyproject.toml` needs no edit either: `[tool.uv.workspace]` globs
`services/*`. You still have to run `uv lock` — the lockfile records members.

`tests/test_new_collector.py` is the gate that keeps this honest: it scaffolds
into a tmpdir and asserts the result lints, renders through Helm, satisfies the
registry drift gate and reaches every derived tooling list **unmodified**.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Marker used by every template below. Chosen because it appears in no Python,
# YAML, Dockerfile or shell syntax, so substitution can never be ambiguous.
TOKEN = re.compile(r"%%([a-z_]+)%%")

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SIGNAL_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# Ports are assigned from here upward when `--port` is not given. weather is
# 8000 and player-projections 8001, so the fleet grows past those.
FIRST_PORT = 8000

# How many rows the generated placeholder adapter emits, and therefore the
# `EXPECTED_FLOOR` a scaffolded collector starts with. It is a constant here so
# that the generated floor is a *declared* number, not one computed from a
# fetch — which is the whole point of the floor.
PLACEHOLDER_FLOOR = 3

IMAGE_PREFIX = "ghcr.io/kakhavai/foundry"


class ScaffoldError(Exception):
    """The request cannot produce a valid collector."""


# ── reading what already exists ───────────────────────────────────────────────


def load_fleet_module(root: Path):
    """Import `scripts/collectors.py` by path — same pattern as filters.py."""
    source = root / "scripts" / "collectors.py"
    if not source.exists():
        raise ScaffoldError(f"no {source}; run this from the repository root")
    spec = importlib.util.spec_from_file_location("fleet_collectors", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleet_collectors"] = module
    spec.loader.exec_module(module)
    return module


def cadence_values(root: Path) -> dict[str, str]:
    """`CadenceClass`'s members, read out of cadence.py rather than hardcoded.

    Read by AST for the same reason `tests/test_collector_registry.py` does:
    adding a cadence class in code must not leave this script rejecting a value
    the fleet can legitimately declare, and importing the library here would
    drag the OTel SDK into a scaffolder.
    """
    path = root / "libs" / "collector-core" / "collector_core" / "cadence.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CadenceClass":
            members = {}
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(
                    stmt.value, ast.Constant
                ):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            members[stmt.value.value] = target.id
            if members:
                return members
    raise ScaffoldError(f"{path}: no CadenceClass members found")


def envelope_version(root: Path) -> str:
    """`ENVELOPE_VERSION`, read out of envelope.py. A string — `"1"`, quoted."""
    path = root / "libs" / "collector-core" / "collector_core" / "envelope.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "ENVELOPE_VERSION":
                    return str(stmt.value.value)
    raise ScaffoldError(f"{path}: no module-level ENVELOPE_VERSION")


# ── the request, validated ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Collector:
    name: str
    package: str
    class_prefix: str
    port: int
    cadence: str
    cadence_member: str
    signal_types: tuple[str, ...]
    stage: str
    depends_on: tuple[str, ...]
    scope_aware: bool
    adapter: str
    envelope_version: str
    smoke_hook: bool

    @property
    def path(self) -> str:
        return f"/collectors/{self.name}"


def plan(
    root: Path,
    *,
    name: str,
    port: int | None,
    cadence: str,
    signal_types: list[str],
    stage: str,
    depends_on: list[str],
    scope_aware: bool,
    adapter: str | None,
    smoke_hook: bool,
) -> Collector:
    """Validate the request against the repo as it stands, or refuse."""
    if not NAME_RE.match(name):
        raise ScaffoldError(
            f"{name!r} is not a usable collector name. It becomes a directory, a "
            f"Kubernetes Service, a gateway path segment and a Python package, so "
            f"it must be lowercase kebab-case: e.g. 'betting-lines'."
        )

    cadences = cadence_values(root)
    if cadence not in cadences:
        raise ScaffoldError(
            f"unknown cadence class {cadence!r}; collector_core.cadence."
            f"CadenceClass declares {sorted(cadences)}"
        )

    if not signal_types:
        raise ScaffoldError(
            "--signal-types is required: a collector with no signal type emits "
            "no envelope, and `GET /signals` would have nothing to serve"
        )
    for signal_type in signal_types:
        if not SIGNAL_RE.match(signal_type):
            raise ScaffoldError(
                f"signal type {signal_type!r} must be lowercase snake_case — it "
                f"is a lake path segment and a `/signals?signal_type=` value"
            )
    if len(set(signal_types)) != len(signal_types):
        raise ScaffoldError(f"duplicate signal type in {signal_types}")

    fleet = load_fleet_module(root)
    existing = fleet.services()
    taken = {service.name for service in existing}
    if name in taken:
        raise ScaffoldError(
            f"{name!r} is already in the fleet. The registry is append-only and "
            f"a name is the join key between it, services/, and the gateway."
        )

    known_collectors = {s.name for s in existing if s.is_collector}
    unknown = sorted(set(depends_on) - known_collectors)
    if unknown:
        raise ScaffoldError(
            f"--depends-on names {unknown}, which is not a registered collector. "
            f"A dependency's entry must merge FIRST — the registry is the "
            f"inventory of deployed collectors, so depending on an unregistered "
            f"name means depending on something that is not running."
        )
    if name in depends_on:
        raise ScaffoldError(f"{name!r} cannot depend on itself")

    used_ports = {service.port for service in existing}
    if port is None:
        port = max([*used_ports, FIRST_PORT - 1]) + 1
    if port in used_ports:
        raise ScaffoldError(
            f"port {port} is already claimed. Two services on one port means one "
            f"port-forward silently wins, which reads as a broken service."
        )

    package = name.replace("-", "_")
    return Collector(
        name=name,
        package=package,
        class_prefix="".join(part.capitalize() for part in name.split("-")),
        port=port,
        cadence=cadence,
        cadence_member=cadences[cadence],
        signal_types=tuple(signal_types),
        stage=stage,
        depends_on=tuple(depends_on),
        scope_aware=scope_aware,
        adapter=adapter or f"{name}-upstream",
        envelope_version=envelope_version(root),
        smoke_hook=smoke_hook,
    )


# ── rendering ─────────────────────────────────────────────────────────────────


def render(template: str, collector: Collector, **extra: str) -> str:
    """Substitute every `%%token%%` in a template, refusing an unknown one.

    Refusing rather than leaving it in place: a `%%typo%%` that survives into a
    generated file is a syntax error two steps later, in a file nobody wrote.
    """
    values = {
        "name": collector.name,
        "pkg": collector.package,
        "class_prefix": collector.class_prefix,
        "port": str(collector.port),
        "cadence": collector.cadence,
        "cadence_member": collector.cadence_member,
        "adapter": collector.adapter,
        "path": collector.path,
        "envelope_version": collector.envelope_version,
        "image": f"{IMAGE_PREFIX}/{collector.name}",
        "first_signal": collector.signal_types[0],
        **extra,
    }

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise ScaffoldError(f"template references unknown token %%{key}%%")
        return values[key]

    return TOKEN.sub(replace, template)


def py_tuple(values: tuple[str, ...] | list[str], indent: str = "    ") -> str:
    """A Python tuple literal, one item per line, as ruff-format would leave it."""
    if len(values) == 1:
        return f'("{values[0]}",)'
    body = "".join(f'{indent}"{value}",\n' for value in values)
    return f"(\n{body})"


# ══════════════════════════════════════════════════════════════════════════════
# Templates. Every one of these is written to lint clean under the service's own
# ruff config (line-length 88, rules E/F/I) and to be *readable as a starting
# point* rather than as generated output nobody edits.
# ══════════════════════════════════════════════════════════════════════════════

INIT_PY = '''\
"""%%name%% — a Foundry signal collector."""
'''

MAIN_PY = '''\
"""%%name%%'s process wiring: the descriptor, and nothing else yet.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from collector_core.app import CollectorDescriptor, build_collector_app

from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    capture_%%pkg%%,
)
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_%%pkg%%,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`: the loop runs on its cadence class's base
        # interval and never escalates. Add one only if this collector has a
        # genuinely perishable moment to escalate toward — weather's
        # `next_kickoff` is the only example in the fleet.
    )
)

# Routes beyond the standard five go below this line, as plain `@app.get` /
# `@app.post` handlers. Reach the lake and the collector name through
# `app.state.collector_spec` — never a module-level global, which only this
# file could see. Anything that touches the lake must be offloaded with
# `asyncio.to_thread` (or `collector_core.lake`'s awrite/aread/alist_keys);
# the lake you are handed refuses a synchronous call from the loop thread.
#
# Remember to publish any new path in this collector's Helm values under
# `gateway.publicPaths`, or it 404s at the gateway while working in-cluster.
'''

SIGNALS_PY = '''\
"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.
"""

from collections.abc import Mapping

# TODO: declare the filters this collector's rows actually support.
# Do not list one you do not implement below — the router will accept it and
# `signal_matches` will ignore it, which returns everything and looks like a
# working filter.
SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "key",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = ("key",)


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int. Comparing them directly
    makes `?week_index=3` silently match nothing. `str()` on the row side is
    the fix, and forgetting it is the single most common bug in this function.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False
    return True
'''

METRICS_PY = '''\
"""%%name%%'s binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`, `collector_staleness_
seconds`) belong to the library, and the series that answer "is THIS collector
wrong in the way only it can be wrong" belong here. A metric only one service
records must not grow into the shared library — see player-identity's
`identity_merge_conflicts` and roster-scope's `scope_missed_producers` for what
that looks like when it is real.

`%%class_prefix%%Metrics` is still a `CollectorMetrics`, so it satisfies
`CollectorDescriptor.metrics` unchanged. Exactly one instance exists per
process — the library never constructs one, it takes the one below.
"""

from collector_core.metrics import CollectorMetrics
from opentelemetry import metrics as otel_metrics

COLLECTOR = "%%name%%"


class %%class_prefix%%Metrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        # PLACEHOLDER. Replace with the series that make THIS collector's own
        # failure mode visible — the one `collector_coverage_ratio` cannot see
        # because coverage is computed against the same input that drove the
        # fetch. If there genuinely is no such series, delete the subclass and
        # use `metrics = CollectorMetrics(COLLECTOR)` (weather does).
        self._rows_captured = meter.create_gauge(
            "%%pkg%%_rows_captured",
            description="Rows captured in the last pass, by collector.",
        )

    def rows_captured(self, count: int) -> None:
        """Record every pass, including zero.

        An absent Prometheus series and a healthy one are indistinguishable in
        PromQL, so a gauge only written when it is interesting cannot be
        alerted on.
        """
        self._rows_captured.set(count, {"collector": self.collector})


metrics = %%class_prefix%%Metrics()
'''

ADAPTER_PY = '''\
"""The upstream adapter — the only module that knows the wire format.

Kept separate from `capture.py` so the orchestration (coverage accounting,
envelopes, the lake) can be tested against a fake upstream without mocking
HTTP, and so swapping the upstream touches one file.

**Two memory rules, both learned the hard way.** roster-scope's first deploy was
OOMKilled at a 256Mi limit because a ~37 MB upstream document was buffered
three times over:

1. **Never hold an upstream response more than once.** `response.text` after
   `response.content`, or `json.loads(response.text)`, is two copies.
2. **Filter as you parse.** Build the rows you are keeping; never materialise
   the whole document and then narrow it. For a large CSV upstream use
   `collector_core.streaming.stream_csv_dicts`, which does both.
"""

from datetime import datetime

import httpx

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake.
UPSTREAM_ADAPTER = "%%adapter%%"

# TODO: the real upstream. While this is empty the placeholder branch in
# `fetch_rows` runs, so a freshly scaffolded collector deploys, captures and
# serves before its upstream exists — the same stub-mode reasoning
# player-projections uses for `PROJECTIONS_SNAPSHOT_URL`.
UPSTREAM_URL = ""

# DELETE ME along with the branch below. Deterministic, offline, and shaped
# exactly like `build_signal` expects, so the generated tests are real tests of
# the orchestration rather than of a mock.
PLACEHOLDER_KEYS: tuple[str, ...] = (
    "placeholder-a",
    "placeholder-b",
    "placeholder-c",
)


def source_ref(season: int, week: int) -> str | None:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Not decorative: it is what makes a lake object reproducible, and what tells
    a reader which of two feeds a row came from a season later.
    """
    if not UPSTREAM_URL:
        return None
    return UPSTREAM_URL.format(season=season, week=week)


async def fetch_rows(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    now: datetime,
) -> list[dict]:
    """One upstream fetch, parsed into this collector's own row shape.

    Raises on an upstream failure rather than returning an empty list. That is
    deliberate: `capture` turns the exception into a `present: 0` envelope with
    a populated `errors` array, and an empty list would instead be recorded as
    a successful capture of nothing.
    """
    if not UPSTREAM_URL:
        return [
            {"key": key, "value": float(index)}
            for index, key in enumerate(PLACEHOLDER_KEYS)
        ]

    response = await client.get(source_ref(season, week))
    response.raise_for_status()
    # Parse straight off the response — see the module docstring. Do not assign
    # `response.text` and then parse that.
    return [{"key": row["key"], "value": row["value"]} for row in response.json()]
'''

CAPTURE_PY = '''\
"""%%name%%'s capture pass: fetch -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**. A collector that reaches
its upstream inside a request handler has inverted that contract.

Two things here are correctness, not style, and both have a fleet-wide history:

**`coverage.expected` never derives from what succeeded.** A collector that
builds its expectation from the document it just fetched reports a truncated
upstream — 100 of 2,900 records — as `expected: 100, present: 100`, ratio 1.0.
Perfectly healthy, while 96% of the league silently vanished. `EXPECTED_FLOOR`
below encodes the size the universe is KNOWN to have, independently of the
fetch, and `CoverageAccumulator` takes it as a floor that never lowers a
genuine count. `acc.expect(key)` is called on the fact that made a key owed;
`acc.record(key)` only after it actually landed. Never the other way round.

**A failed capture still writes an envelope.** `collector_core.failure.
fail_capture` writes one `present: 0` envelope per signal type with a populated
`errors` array, then re-raises. Both halves matter: the write is what makes a
gap in the append-only lake *explicit* rather than something a reader has to
infer from absence, and the re-raise is what stops `CaptureState` installing an
empty capture over the last good one.

Every lake call goes off the event loop via `awrite` — `LakeWriter` is
synchronous boto3, and the lake handed to this function raises if it is called
from the loop thread.
"""

from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter, awrite

from .adapters.upstream import UPSTREAM_ADAPTER, fetch_rows, source_ref
from .metrics import metrics

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "capture_%%pkg%%",
]

COLLECTOR_NAME = "%%name%%"
CADENCE_CLASS = CadenceClass.%%cadence_member%%
SIGNAL_TYPES = %%signal_types%%

# TODO: the REAL size of this collector's universe, per signal type — 32
# teams, 272 games, 416 scope slots, ~2,900 rostered players, whatever it is
# here. Scaffolded to the placeholder adapter's row count so a fresh collector
# reports honest coverage on day one. Read the module docstring before you
# change how this number is produced: it must not come from the fetch.
EXPECTED_FLOOR: dict[str, int] = %%expected_floor%%


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_key(row: dict) -> str:
    """The coverage key for one upstream row.

    It must be stable across passes and unique within one: it is what appears
    in `coverage.missing`, so a key that changes between passes makes every
    row look newly missing.
    """
    return str(row["key"])


def build_signal(signal_type: str, row: dict, *, now: datetime) -> dict:
    """One upstream row -> one signal row, for one signal type.

    TODO: this is the collector's actual product. Whatever shape you return,
    mirror it in this collector's schema under
    contracts/signal-envelope/collectors/.
    tests/test_capture_contract_conformance.py validates the REAL output of
    this function against that schema, so a renamed field fails there rather
    than in the generator six weeks later.
    """
    return {
        "key": _row_key(row),
        "observed_at": _rfc3339(now),
        "value": row["value"],
    }


async def capture_%%pkg%%(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one envelope per signal type."""
    scope = {"season": season, "week": week}
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=source_ref(season, week),
    )

    metrics.capture_attempt()
    try:
        rows = await fetch_rows(season, week, client=client, now=now)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        metrics.capture_failure(exc)
        # Writes a `present: 0` envelope per signal type, then re-raises `exc`.
        # Never returns — do not add code after this call.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            expected=EXPECTED_FLOOR,
            source_ref=source_ref(season, week),
        )

    envelopes: dict[str, Envelope] = {}
    for signal_type in SIGNAL_TYPES:
        acc = CoverageAccumulator(floor=EXPECTED_FLOOR[signal_type])
        signals: list[dict] = []
        for row in rows:
            key = _row_key(row)
            # Declared because the row EXISTS and is therefore owed — never
            # because building it below happened to succeed.
            acc.expect(key)
            if deadline is not None and datetime.now(tz=UTC) >= deadline:
                # Over budget. Record the rest as missing rather than throwing
                # away what already resolved: a truncated pass that reports
                # itself truncated is useful; one that reports itself complete
                # is not.
                acc.fail(key, "deadline_exceeded")
                continue
            try:
                signals.append(build_signal(signal_type, row, now=now))
            except Exception as exc:  # noqa: BLE001 — one bad row is not a pass
                acc.fail(key, metrics.reason_for(exc))
                continue
            acc.record(key)
        metrics.rows_captured(len(signals))
        envelopes[signal_type] = Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=signal_type,
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=acc.result(),
            errors=acc.errors,
            signals=signals,
        )

    for signal_type, envelope in envelopes.items():
        await awrite(lake, envelope)
        metrics.coverage(signal_type, envelope.coverage.ratio)
    return envelopes
'''

CONFTEST_PY = '''\
"""Fixtures for %%name%%'s suite.

`SpyLake` is an in-memory `LakeWriter` rather than moto: CI prunes
collector-core's dev dependencies inside `services/`, so a moto import passes
locally off a shared virtualenv and fails only in CI.
"""

from datetime import UTC, datetime

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import lake_key
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from %%pkg%%.main import app

TEST_TOKEN = "test-collector-token"

# One frozen instant the whole suite describes. `Envelope` rejects a naive
# datetime rather than assuming UTC — guessing puts a wrong instant into an
# append-only lake nobody rewrites.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session", autouse=True)
def _meter_provider():
    """One real MeterProvider for the whole session.

    Instruments are created at import time and record nothing until a provider
    exists; anything recorded before it is silently lost. `set_meter_provider`
    is one-shot per process, so this must happen exactly once.
    """
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()])
    )


@pytest.fixture(autouse=True)
def _collector_token(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", TEST_TOKEN)


@pytest.fixture
def client(_collector_token):
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_collector_singletons():
    """`state` and `refresh_gate` are process-level singletons — that is what
    lets `/signals` serve a cache and `/refresh` enforce a floor across
    requests — so something has to reset them between tests."""
    spec = app.state.collector_spec
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    yield
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None


class SpyLake:
    """A minimal in-memory `LakeWriter`."""

    def __init__(self, *, fail_write: bool = False) -> None:
        self.objects: dict[str, dict] = {}
        self.writes: list[Envelope] = []
        self.fail_write = fail_write

    def write(self, envelope: Envelope) -> str:
        if self.fail_write:
            raise RuntimeError("lake unreachable")
        key = lake_key(envelope)
        self.objects[key] = envelope.to_dict()
        self.writes.append(envelope)
        return key

    def list_keys(
        self,
        collector: str,
        signal_type: str,
        season: int,
        week: int,
        version: str = ENVELOPE_VERSION,
    ) -> list[str]:
        prefix = f"signals/{collector}/v{version}/season={season}/week={week:02d}/"
        suffix = f"-{signal_type}.json"
        return sorted(
            k for k in self.objects if k.startswith(prefix) and k.endswith(suffix)
        )

    def read(self, key: str) -> dict:
        return self.objects[key]


@pytest.fixture
def lake() -> SpyLake:
    return SpyLake()
'''

TEST_ROUTES_PY = '''\
"""%%name%%'s five-route contract surface, plus auth.

The routes themselves are `collector_core.routes`' and are proved there against
a fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, and that auth is mounted.
"""

import time

from collector_core.envelope import ENVELOPE_VERSION

from %%pkg%%.capture import SIGNAL_TYPES

from .conftest import TEST_TOKEN


def wait_for_signals(client, *, count: int, timeout: float = 10.0) -> dict:
    """Poll `/signals` until a dispatched capture has landed.

    **`POST /refresh` returns 202 — accepted, not done.** The capture runs as a
    background task and lands in `CaptureState` whenever it finishes, so
    reading `/signals` on the next line is a race. It is a race that used to be
    won by accident, because nothing in the capture path yielded; the lake
    write now goes through `asyncio.to_thread`, so it genuinely suspends.

    Bounded and loud: on timeout this fails with what it actually saw rather
    than hanging or asserting against an empty envelope. Never replace it with
    a bare `client.get("/signals")` after a refresh.
    """
    deadline = time.monotonic() + timeout
    body = {"count": 0}
    while time.monotonic() < deadline:
        body = client.get("/signals").json()
        if body["count"] >= count:
            return body
        time.sleep(0.05)
    raise AssertionError(
        f"dispatched capture did not land within {timeout}s: "
        f"expected count >= {count}, last saw {body}"
    )


def test_health_is_open_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_scrapeable(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_catalog_declares_this_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "%%name%%"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "%%cadence%%"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


def test_refresh_is_accepted_and_the_capture_eventually_lands(client):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    accepted = client.post("/refresh", json={})
    assert accepted.status_code == 202

    body = wait_for_signals(client, count=len(SIGNAL_TYPES))
    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "%%name%%"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_row_filter_narrows_the_signals(client):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get("/signals?key=placeholder-a").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["key"] for row in rows} == {"placeholder-a"}


def test_a_data_route_without_a_token_is_401(client):
    """Auth is enforced in-process, not only at the gateway: a ClusterIP is
    reachable by anything in the namespace, and smoke-test.sh port-forwards the
    Service directly."""
    assert client.get("/signals", headers={"Authorization": ""}).status_code == 401


def test_a_wrong_token_is_401(client):
    response = client.get(
        "/signals", headers={"Authorization": f"Bearer not-{TEST_TOKEN}"}
    )
    assert response.status_code == 401


def test_an_absent_token_env_fails_closed_with_503(client, monkeypatch):
    """Fails closed, loudly: a Secret that never syncs must not read as an open
    collector. The ArgoCD Application stays Healthy either way."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "")
    assert client.get("/signals").status_code == 503
'''

TEST_CONFORMANCE_PY = '''\
"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from %%pkg%%.capture import (
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    capture_%%pkg%%,
)

from .conftest import NOW, SpyLake

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "%%name%%.json").read_text()
)["signal_types"]


def validate(envelopes: dict) -> None:
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_%%pkg%%(
            2026, 1, client=client, lake=lake, now=NOW, **kwargs
        )


async def test_a_complete_capture_conforms():
    envelopes = await capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


async def test_a_complete_capture_reports_full_coverage():
    """Not decoration: `expected` is floored independently of the fetch, so a
    healthy pass reaching the floor is what proves the floor is right."""
    envelopes = await capture(SpyLake())
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected >= EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.ratio == 1.0
        assert envelope.errors == []


async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_failed_capture_writes_a_present_zero_envelope(monkeypatch):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("upstream down")

    monkeypatch.setattr("%%pkg%%.capture.fetch_rows", boom)
    lake = SpyLake()

    with pytest.raises(httpx.ConnectError):
        await capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1, (
            "expected: 0 makes Coverage.ratio read 1.0 — a total outage would "
            "report perfect coverage"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
'''

TEST_COVERAGE_PY = '''\
"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned. These tests
are the ones that fail if somebody "simplifies" `capture.py` by computing the
expectation from `rows`.
"""

import httpx

from %%pkg%%.capture import (
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    capture_%%pkg%%,
)

from .conftest import NOW, SpyLake


async def _capture(rows, monkeypatch, lake):
    async def fake(*args, **kwargs):
        return list(rows)

    monkeypatch.setattr("%%pkg%%.capture.fetch_rows", fake)
    async with httpx.AsyncClient() as client:
        return await capture_%%pkg%%(
            2026, 1, client=client, lake=lake, now=NOW
        )


async def test_a_truncated_upstream_does_not_report_full_coverage(monkeypatch):
    """The failure this floor exists for: an upstream returning one row of many
    must not yield `expected: 1, present: 1`, ratio 1.0."""
    envelopes = await _capture(
        [{"key": "only-one", "value": 1.0}], monkeypatch, SpyLake()
    )
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.present == 1
        assert envelope.coverage.ratio < 1.0
        reasons = {error["reason"] for error in envelope.errors}
        assert "below_expected_floor" in reasons, reasons


async def test_an_empty_upstream_reports_zero_not_one(monkeypatch):
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a pass that captured nothing."""
    envelopes = await _capture([], monkeypatch, SpyLake())
    for envelope in envelopes.values():
        assert envelope.coverage.present == 0
        assert envelope.coverage.ratio == 0.0


async def test_expansion_past_the_floor_still_reports_honestly(monkeypatch):
    """The floor must not CAP a genuine count, only raise a short one."""
    rows = [{"key": f"k{i}", "value": float(i)} for i in range(50)]
    envelopes = await _capture(rows, monkeypatch, SpyLake())
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == max(50, EXPECTED_FLOOR[signal_type])
        assert envelope.coverage.ratio == 1.0


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())
'''

PYPROJECT_TOML = '''\
[project]
name = "%%name%%"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "httpx",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-prometheus",
    "opentelemetry-instrumentation-fastapi",
    "opentelemetry-instrumentation-httpx",
    "prometheus-client",
    "collector-core",
]

[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "httpx",
    "respx",
    "jsonschema[format]",
]

[tool.uv]
package = true

[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "--cov=%%pkg%% --cov-branch --cov-report=term --cov-fail-under=80"

[tool.coverage.run]
branch = true
source = ["%%pkg%%"]

[tool.coverage.report]
show_missing = true

[tool.uv.sources]
collector-core = { workspace = true }
'''

DOCKERFILE = '''\
# Stage 1 — workspace-manifests. Verbatim in every collector; the write-up
# lives in services/weather/Dockerfile, and tests/test_dockerfile_workspace.py
# enforces that they agree.
#
# Short version: `uv sync --package <x>` resolves the whole workspace graph
# before it can sync any member, so every member's pyproject.toml must be in
# the context — even members this service does not depend on. This stage
# reduces the context to exactly that set by glob, so adding a workspace member
# requires no edit here. Do NOT replace it with per-member COPY lines.
FROM python:3.12-slim AS workspace-manifests

WORKDIR /src

COPY . .

RUN set -eu; \\
    mkdir -p /manifests; \\
    cp pyproject.toml uv.lock /manifests/; \\
    cp -a libs /manifests/libs; \\
    find services -mindepth 2 -maxdepth 2 -name pyproject.toml \\
        -exec cp --parents {} /manifests/ \\;

# Stage 2 — builder
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \\
    UV_LINK_MODE=copy \\
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Everything uv needs to resolve the workspace and build this service's
# dependencies — and deliberately none of this service's own source, so the
# sync below stays cached across source edits.
COPY --from=workspace-manifests /manifests/ ./

RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --locked --no-dev --no-install-project --package %%name%%

COPY services/%%name%%/%%pkg%%/ ./services/%%name%%/%%pkg%%/
# `--reinstall-package` is not optional, and the reason is nasty: the package
# version never changes (0.1.0), so uv's build cache under the mount above can
# serve a PREVIOUSLY BUILT WHEEL even though the source just changed. The image
# then silently ships stale code — it built, it ran, it passed a health check,
# and it was executing a different revision than the tree it was built from.
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --locked --no-dev --no-editable --package %%name%% \\
        --reinstall-package %%name%%

# Stage 3 — runtime
FROM python:3.12-slim

# A numeric UID: Kubernetes `runAsNonRoot` can only verify a numeric user.
RUN addgroup --system --gid 65532 app \\
    && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE %%port%%

CMD ["uvicorn", "%%pkg%%.main:app", "--host", "0.0.0.0", "--port", "%%port%%"]
'''

HELM_VALUES = '''\
service:
  name: %%name%%
  port: %%port%%

image:
  repository: %%image%%

containerPort: %%port%%

# The chart defaults. Measure before raising either: roster-scope's first deploy
# was OOMKilled at 256Mi because its adapter buffered a 37 MB document three
# times, and raising the limit would have hidden the bug rather than fixed it.
resources:
  limits:
    cpu: 250m
    memory: 256Mi
  requests:
    cpu: 100m
    memory: 128Mi

otel:
  resourceAttributes: "service.name=%%name%%,service.namespace=foundry"

grafanaAnnotations:
  enabled: true

gateway:
  enabled: true
  pathPrefix: %%path%%
  # Only the paths this collector publishes as its contract. /health and
  # /metrics are deliberately absent: they are exempt from bearer auth
  # in-process (a kubelet probe and a Prometheus scrape cannot carry a token),
  # and that is a reason to reach them in-cluster, not at the edge.
  # Add a path here when you add a route beyond the standard five.
  publicPaths:
    - /catalog
    - /signals
    - /refresh

extraEnv:
  # optional: true lets the pod start before the Secret exists. %%name%% then
  # answers 503 on every data route until it arrives — the ArgoCD Application
  # reports Healthy the whole time, so recognise that combination for what it
  # is before assuming the deploy is broken.
  - name: COLLECTOR_TOKEN
    valueFrom:
      secretKeyRef:
        name: %%name%%-collector-token
        key: token
        optional: true
  - name: LAKE_BUCKET
    value: "foundry-signals"
  - name: LAKE_ENDPOINT_URL
    value: "http://minio.monitoring.svc.cluster.local:9000"
  - name: REFRESH_MIN_INTERVAL_SECONDS
    value: "300"
  # Scaffolded false. Flip to "true" once the real upstream is wired AND you
  # have decided the third-party load is acceptable: the cluster is recreated
  # on every CI run and every pod restart re-captures, so a large or
  # rate-limited upstream should stay false here. A dispatched POST /refresh
  # reaches the upstream regardless of this flag.
  - name: CAPTURE_ENABLED
    value: "%%capture_enabled%%"
  - name: CAPTURE_SEASON
    value: "2026"
  - name: CAPTURE_WEEK
    value: "1"
  # Wall-clock ceiling for one capture pass, so total upstream failure costs at
  # most this much rather than items x per-call timeout.
  - name: CAPTURE_DEADLINE_SECONDS
    value: "300"
  # Same optional:true pattern — the pod starts before the Secret exists and
  # the lake simply has no credentials until it arrives.
  - name: AWS_ACCESS_KEY_ID
    valueFrom:
      secretKeyRef:
        name: %%name%%-lake-credentials
        key: access-key-id
        optional: true
  - name: AWS_SECRET_ACCESS_KEY
    valueFrom:
      secretKeyRef:
        name: %%name%%-lake-credentials
        key: secret-access-key
        optional: true
  - name: AWS_DEFAULT_REGION
    value: "us-east-1"
'''

GITOPS_VALUES = '''\
# The image tag ArgoCD deploys. Overwritten on every push to main by
# .github/actions/update-gitops-tag with the Git SHA that built the image —
# 0.1.0 is only the placeholder that makes the generated Application render
# before the first build lands.
image:
  tag: "0.1.0"
'''

FIELD_SCHEMA_HEAD = '''\
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://foundry.internal/signal-envelope/collectors/%%name%%.json",
  "title": "%%name%% collector signal fields",
  "signal_types": {
'''

FIELD_SCHEMA_TYPE = '''\
    "%%signal_type%%": {
      "type": "object",
      "required": ["key", "observed_at", "value"],
      "properties": {
        "key": { "type": "string", "minLength": 1 },
        "observed_at": { "type": "string", "format": "date-time" },
        "value": { "type": "number" }
      }
    }'''

README_MD = '''\
# %%name%%

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `%%port%%` |
| Gateway path | `%%path%%` |
| Cadence class | `%%cadence%%` |
| Signal types | %%signal_types_md%% |
| Status | **Stub upstream** — see `adapters/upstream.py` |

## What it captures

TODO: one paragraph. What raw signal is this, where does it come
from, and what makes it worth a collector of its own?

## Routes

The standard five, from `collector_core.routes`: `GET /health`,
`GET /metrics`, `GET /catalog`, `GET /signals`, `POST /refresh`. Everything
except `/health` and `/metrics` requires `Authorization: Bearer <token>`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Before this is real

1. Point `adapters/upstream.py`'s `UPSTREAM_URL` at the real feed and delete
   the placeholder branch.
2. Set `EXPECTED_FLOOR` in `capture.py` to the size this collector's universe
   is actually known to have. It must not be derived from a fetch.
3. Rewrite `build_signal` and mirror its output in
   `contracts/signal-envelope/collectors/%%name%%.json`.
4. Replace the placeholder series in `metrics.py`, or drop the subclass.
5. Decide `CAPTURE_ENABLED` in `helm/values/%%name%%/values.yaml`.

## Tests

```bash
cd services/%%name%%
uv run pytest -v
```
'''

SMOKE_SH = '''\
#!/usr/bin/env bash
#
# %%name%%'s own smoke assertions — the routes beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
# services/weather/smoke.sh is the model.
#
# Do NOT POST /refresh from here when this collector runs with
# CAPTURE_ENABLED=false: a dispatched refresh reaches the upstream regardless
# of that flag, so it would hit a third party on every PR.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

# TODO: assert this collector's own routes here. Assert them at BOTH
# the gateway and the Service — the direct-to-Service call is the one that
# matters, because under gateway-only auth it would return 200 and this
# required check would pass over an unprotected path.
curl -sf -H "$AUTH" "$SMOKE_GATEWAY_URL/catalog" >/dev/null
echo "%%name%%: extra-route smoke OK"
'''

REGISTRY_ENTRY = '''\

  - name: %%name%%
    path: %%path%%
    stage: %%stage%%
    cadence_class: %%cadence%%
    envelope_version: "%%envelope_version%%"
    signal_types: [%%signal_types_inline%%]
    # TYPE-CHECKED ONLY — nothing verifies this is correct. Set it true if this
    # collector narrows to roster-scope's membership list before it fetches.
    scope_aware: %%scope_aware%%
    depends_on: [%%depends_on_inline%%]
'''


# ── writing ───────────────────────────────────────────────────────────────────


def build_files(collector: Collector, *, capture_enabled: bool) -> dict[str, str]:
    """Every file a new collector consists of, as {relative path: content}.

    Returned rather than written so the acceptance test and `--dry-run` see
    exactly what a real run would produce.
    """
    name, pkg = collector.name, collector.package
    service = f"services/{name}"

    signal_types = collector.signal_types
    extra = {
        "signal_types": py_tuple(signal_types),
        "signal_types_inline": ", ".join(signal_types),
        "signal_types_md": ", ".join(f"`{s}`" for s in signal_types),
        # PLACEHOLDER_FLOOR, not a count of anything the adapter returned. The
        # two happen to agree only so a scaffolded collector reports 1.0 on its
        # first run; the generated comment says to replace it.
        "expected_floor": (
            "{\n"
            + "".join(
                f'    "{signal_type}": {PLACEHOLDER_FLOOR},\n'
                for signal_type in signal_types
            )
            + "}"
        ),
        "stage": collector.stage,
        "scope_aware": "true" if collector.scope_aware else "false",
        "depends_on_inline": ", ".join(collector.depends_on),
        "capture_enabled": "true" if capture_enabled else "false",
    }

    def r(template: str, **more: str) -> str:
        return render(template, collector, **{**extra, **more})

    field_schema = r(FIELD_SCHEMA_HEAD)
    field_schema += ",\n".join(
        r(FIELD_SCHEMA_TYPE, signal_type=signal_type) for signal_type in signal_types
    )
    field_schema += "\n  }\n}\n"

    files = {
        f"{service}/{pkg}/__init__.py": r(INIT_PY),
        f"{service}/{pkg}/main.py": r(MAIN_PY),
        f"{service}/{pkg}/capture.py": r(CAPTURE_PY),
        f"{service}/{pkg}/metrics.py": r(METRICS_PY),
        f"{service}/{pkg}/signals.py": r(SIGNALS_PY),
        f"{service}/{pkg}/adapters/__init__.py": r(INIT_PY),
        f"{service}/{pkg}/adapters/upstream.py": r(ADAPTER_PY),
        f"{service}/tests/__init__.py": "",
        f"{service}/tests/conftest.py": r(CONFTEST_PY),
        f"{service}/tests/test_routes.py": r(TEST_ROUTES_PY),
        f"{service}/tests/test_capture_contract_conformance.py": r(
            TEST_CONFORMANCE_PY
        ),
        f"{service}/tests/test_coverage_floor.py": r(TEST_COVERAGE_PY),
        f"{service}/Dockerfile": r(DOCKERFILE),
        f"{service}/pyproject.toml": r(PYPROJECT_TOML),
        f"{service}/README.md": r(README_MD),
        f"helm/values/{name}/values.yaml": r(HELM_VALUES),
        f"infra/gitops/envs/local/{name}/values.yaml": r(GITOPS_VALUES),
        f"contracts/signal-envelope/collectors/{name}.json": field_schema,
    }
    if collector.smoke_hook:
        files[f"{service}/smoke.sh"] = r(SMOKE_SH)
    return files


def registry_addition(collector: Collector) -> str:
    """The block appended to contracts/collector-registry.yaml.

    Appended as text, not re-serialised: the file is append-only and carries a
    long explanatory header plus per-entry commentary, all of which
    `yaml.safe_dump` would silently delete.
    """
    return render(
        REGISTRY_ENTRY,
        collector,
        stage=collector.stage,
        scope_aware="true" if collector.scope_aware else "false",
        signal_types_inline=", ".join(collector.signal_types),
        depends_on_inline=", ".join(collector.depends_on),
    )


def scaffold(
    root: Path,
    collector: Collector,
    *,
    capture_enabled: bool = False,
    force: bool = False,
) -> list[Path]:
    """Write the collector into `root`. Returns the paths written, in order."""
    files = build_files(collector, capture_enabled=capture_enabled)

    if not force:
        clashes = sorted(rel for rel in files if (root / rel).exists())
        if clashes:
            raise ScaffoldError(
                f"refusing to overwrite {clashes}. Pass --force if that is what "
                f"you meant."
            )

    written = []
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" so a Windows checkout does not produce CRLF in a file
        # that goes into a Linux container or is parsed by `bash -n`.
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)

    registry = root / "contracts" / "collector-registry.yaml"
    text = registry.read_text(encoding="utf-8")
    if f"- name: {collector.name}\n" not in text:
        registry.write_text(
            text.rstrip("\n") + "\n" + registry_addition(collector),
            encoding="utf-8",
            newline="\n",
        )
    written.append(registry)

    smoke = root / "services" / collector.name / "smoke.sh"
    if smoke.exists():
        smoke.chmod(0o755)
    return written


NEXT_STEPS = """
Next, in this order:

  1. uv lock                       # the lockfile records workspace members
  2. cd services/{name} && uv run pytest -v
  3. uv run --with pyyaml==6.0.3 --with pytest --with jsonschema pytest tests/ -q
  4. Read services/{name}/README.md — the five TODOs there are the actual work.

Nothing else needs registering. `scripts/deploy-local.py`, `stack-up.py`,
`smoke-test.sh`, the CI matrix and the ArgoCD Application all derive from the
registry entry and helm/values/{name}/values.yaml. If you find yourself
editing one of them, that is a bug in the tooling — report it.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scaffold a new Foundry collector.",
        epilog="See docs/collectors.md for what each generated file is for.",
    )
    parser.add_argument("name", help="Collector name, lowercase kebab-case.")
    parser.add_argument(
        "--cadence",
        required=True,
        help="A collector_core.cadence.CadenceClass value, e.g. 'weekly'.",
    )
    parser.add_argument(
        "--signal-types",
        required=True,
        help="Comma-separated snake_case signal types this collector emits.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Service port. Defaults to one past the highest already claimed.",
    )
    parser.add_argument(
        "--stage", default="8B", help="Phase-8 sub-stage (documentary)."
    )
    parser.add_argument(
        "--depends-on",
        default="",
        help="Comma-separated registered collectors this one depends on.",
    )
    parser.add_argument(
        "--scope-aware",
        action="store_true",
        help="Set if this collector narrows to roster-scope's membership list.",
    )
    parser.add_argument(
        "--adapter",
        default=None,
        help="upstream.adapter label. Defaults to '<name>-upstream'.",
    )
    parser.add_argument(
        "--capture-enabled",
        action="store_true",
        help="Deploy with CAPTURE_ENABLED=true. Off by default; see the values file.",
    )
    parser.add_argument(
        "--smoke-hook",
        action="store_true",
        help="Also generate services/<name>/smoke.sh. Only needed for extra routes.",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing files."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the file list and write nothing.",
    )
    args = parser.parse_args(argv)

    def split(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    # A message, not a traceback: a stack dump buries the one line that says
    # what is wrong with the request.
    try:
        collector = plan(
            args.root,
            name=args.name,
            port=args.port,
            cadence=args.cadence,
            signal_types=split(args.signal_types),
            stage=args.stage,
            depends_on=split(args.depends_on),
            scope_aware=args.scope_aware,
            adapter=args.adapter,
            smoke_hook=args.smoke_hook,
        )
        if args.dry_run:
            for relative in build_files(
                collector, capture_enabled=args.capture_enabled
            ):
                print(relative)
            print("contracts/collector-registry.yaml (append)")
            return 0
        written = scaffold(
            args.root,
            collector,
            capture_enabled=args.capture_enabled,
            force=args.force,
        )
    except ScaffoldError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    for path in written:
        print(path.relative_to(args.root).as_posix())
    print(NEXT_STEPS.format(name=collector.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
