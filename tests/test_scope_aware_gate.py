"""#78: `scope_aware` was type-checked and nothing else.

Read by AST, like `test_collector_registry.py` — `platform-tests` installs
only pytest, pyyaml and jsonschema, so importing a service module would pull
in fastapi and httpx and fail.

A collector declaring `scope_aware: true` must import the fleet's one
narrowing seam: `collector_core.scope.ScopeClient`. That is weaker than
proving it fails closed (the per-service behavioural test suites already do
that — see e.g. `services/usage-share/tests/`, `services/player-stats/
tests/`, `services/injury-report/tests/`) but it is the strongest claim
available without a cluster, and it catches the regression that matters: a
collector that keeps the flag while losing the code.

**Why `ScopeClient` alone, and not also `fetch_watchlist`/`fetch_scope`.**
An earlier sketch of this gate keyed on `{"ScopeClient", "fetch_watchlist",
"fetch_scope"}`. The latter two are per-service *local* function names —
`player_stats/adapters/scope.py` happens to call its helper
`fetch_watchlist`, `injury_report/adapters/scope.py` happens to call its
`fetch_scope`, and `usage_share/adapters/scope.py` also happens to define a
`fetch_scope` — but nothing enforces that naming, and a fourth narrowing
collector calling its own helper `get_scope` or `narrow_to_watchlist` would
silently escape detection while still being a true positive today by luck of
naming. `ScopeClient` is the one name every narrowing collector is
structurally required to import: it is `collector_core.scope`'s only
constructor for reading a published scope out of the lake, so importing it
is the act of narrowing, not an incidental label for it. Verified against the
tree: `ScopeClient` (and its exception, `ScopeUnavailable`) appear, as actual
imports, in exactly `usage-share`, `player-stats`, and `injury-report` —
the three collectors this task flips to `scope_aware: true` — and nowhere
else in `services/`.
"""

import ast
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = yaml.safe_load(
    (ROOT / "contracts/collector-registry.yaml").read_text(encoding="utf-8")
)["collectors"]

# The fleet's one shared narrowing seam. See the module docstring for why this
# set holds exactly one name rather than also keying on per-service local
# helper names.
NARROWING_IMPORTS = {"ScopeClient"}


def _imported_names(service: str) -> set[str]:
    """Every name reachable via `from ... import ...` anywhere in a service's
    non-test source tree — the same AST-only approach
    `tests/test_collector_registry.py` uses, and for the same reason: this
    suite has no fastapi/httpx/prometheus_client installed, so importing the
    service module itself is not an option.
    """
    names: set[str] = set()
    service_dir = ROOT / "services" / service
    for path in service_dir.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("entry", REGISTRY, ids=[e["name"] for e in REGISTRY])
def test_scope_aware_matches_what_the_service_imports(entry):
    imported = _imported_names(entry["name"])
    narrows = bool(imported & NARROWING_IMPORTS)
    assert narrows == entry["scope_aware"], (
        f"{entry['name']} declares scope_aware={entry['scope_aware']} but "
        f"{'imports' if narrows else 'does not import'} "
        f"collector_core.scope.ScopeClient"
    )


def test_the_gate_covers_every_registered_collector():
    """`all(...)`/parametrize over an empty registry would pass vacuously —
    pin the length so a registry read that silently returns nothing (a bad
    path, a YAML that fails to load into the expected shape) fails loudly
    instead of reporting a clean run over zero collectors."""
    assert len(REGISTRY) >= 9, (
        f"expected at least 9 registered collectors, found {len(REGISTRY)} — "
        f"a short list here means the fixture above isn't reading the real "
        f"registry"
    )


def test_at_least_one_collector_exercises_each_branch():
    """Guard against the parametrized test passing for the wrong reason: if
    every entry happened to be `scope_aware: false`, or every entry happened
    to import `ScopeClient`, the equality assertion above would be trivially
    satisfied without the gate ever discriminating anything. Pin both sides
    non-empty."""
    true_count = sum(1 for entry in REGISTRY if entry["scope_aware"] is True)
    false_count = sum(1 for entry in REGISTRY if entry["scope_aware"] is False)
    assert true_count > 0, "no registered collector declares scope_aware: true"
    assert false_count > 0, "no registered collector declares scope_aware: false"
