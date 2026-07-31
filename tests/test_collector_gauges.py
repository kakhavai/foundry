"""No collector may build a gauge with OTel's *synchronous* instrument.

`meter.create_gauge` is last-value aggregated with the data point **consumed**
by a collection: `_LastValueAggregation.collect` returns the point once and
clears it. A collector records on a capture cadence — minutes to hours — while
Prometheus scrapes every 15-30 seconds, so the series is exported on the first
scrape after a recording and is **absent from every scrape after that**.

Absence is not a small error here. It is the failure the whole coverage
apparatus exists to prevent, arriving by a different route: PromQL cannot tell
a series that never existed from one that is healthy and idle, which is exactly
why `scripts/run-chaos.py` treats an empty query result as a hard error rather
than a zero. `collector_coverage_ratio` — the series that makes a collector
silently capturing 3% of the league visible — was the one that did not reliably
arrive.

It reached nine collectors because **a single scrape passes either way**. Only
a second consecutive scrape with no intervening recording distinguishes them,
and no per-service test took one.
`libs/collector-core/tests/test_metrics.py::test_coverage_ratio_is_present_on_a_second_consecutive_scrape`
is the behavioural test; this one is the fleet-wide gate, so collectors #10
through #26 cannot reintroduce it by copying an old file.

Static and import-free on purpose: `platform-tests` installs only pytest,
pyyaml and jsonschema, so importing a service's `metrics.py` would pull in
opentelemetry and fail. The AST is enough — the defect is a constructor call.

Counters are untouched. `create_counter` is a different instrument class,
already cumulative, and correct as it stands.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The banned constructor, and the wrapper that replaces it.
SYNCHRONOUS_GAUGE = "create_gauge"
OBSERVABLE_WRAPPER = "LastValueGauge"

# The scaffolder emits `metrics.py` as a template string, so an AST parse of
# this file would not see inside it. Checked as text instead — and it must be
# checked, because what it generates is the shape 17 more collectors inherit.
SCAFFOLDER = ROOT / "scripts" / "new-collector.py"


def metrics_modules() -> list[Path]:
    """Every `metrics.py` in the repo, service and library alike.

    Globbed rather than listed: a collector added tomorrow is covered without
    editing this file, which is the same reason `test_dockerfile_workspace.py`
    derives its collector set instead of hardcoding one.
    """
    found = sorted(ROOT.glob("services/*/*/metrics.py"))
    found += sorted(ROOT.glob("libs/*/*/metrics.py"))
    return found


def gauge_calls(source: str) -> list[str]:
    """Attribute calls named `create_gauge`, however the meter was spelled."""
    return [
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == SYNCHRONOUS_GAUGE
    ]


MODULES = metrics_modules()


def test_the_scan_actually_found_the_fleets_metrics_modules():
    """Guards every parametrised test below against passing vacuously.

    An empty glob would make `create_gauge` unfindable and every assertion
    trivially true, which is precisely the shape of a test that protects
    nothing. Eleven packages ship today; the floor is deliberately below that
    so a legitimate addition does not red this, and far above zero.
    """
    assert len(MODULES) >= 8, [str(p.relative_to(ROOT)) for p in MODULES]
    assert ROOT / "libs" / "collector-core" / "collector_core" / "metrics.py" in MODULES


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_metrics_module_builds_a_synchronous_gauge(path: Path):
    calls = gauge_calls(path.read_text(encoding="utf-8"))
    assert calls == [], (
        f"{path.relative_to(ROOT)} builds {len(calls)} synchronous OTel gauge(s). "
        "A synchronous gauge is absent from every scrape that does not "
        "immediately follow a recording — use collector_core.metrics."
        f"{OBSERVABLE_WRAPPER} instead. See docs/collectors.md."
    )


def test_the_shared_library_still_offers_the_replacement():
    """A ban with nothing to migrate to is a ban nobody can obey."""
    library = ROOT / "libs" / "collector-core" / "collector_core" / "metrics.py"
    tree = ast.parse(library.read_text(encoding="utf-8"))
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert OBSERVABLE_WRAPPER in classes


def test_the_shared_fleet_gauges_use_the_wrapper():
    """`collector_coverage_ratio` and `collector_staleness_seconds` are the two
    the defect was found on. Named explicitly so a future edit that reverts
    only these — while the rest of the file stays clean — is still caught."""
    library = ROOT / "libs" / "collector-core" / "collector_core" / "metrics.py"
    tree = ast.parse(library.read_text(encoding="utf-8"))

    wrapped = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == OBSERVABLE_WRAPPER
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
    }
    assert {"collector_coverage_ratio", "collector_staleness_seconds"} <= wrapped


def test_the_scaffolder_generates_the_wrapper_not_the_raw_instrument():
    """Otherwise the fix lasts exactly until the next `new-collector.py` run,
    and 17 more collectors inherit the old shape."""
    source = SCAFFOLDER.read_text(encoding="utf-8")
    assert f"meter.{SYNCHRONOUS_GAUGE}(" not in source, (
        "scripts/new-collector.py still templates a synchronous gauge"
    )
    assert f"{OBSERVABLE_WRAPPER}(" in source
    assert f"import CollectorMetrics, {OBSERVABLE_WRAPPER}" in source
