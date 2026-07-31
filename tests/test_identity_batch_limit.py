"""`collector_core.identity.BATCH_LIMIT` must agree with `player-identity`'s
own `MAX_BATCH_QUERIES` (services/player-identity/player_identity/
resolution.py).

The two constants are defined independently because `collector-core` is a
library every collector depends on and cannot depend on a service in turn --
there is no import that would let one side enforce the other at runtime. A
lowered server cap would otherwise show up as a silent 422 on every
collector's next identity batch, all at once, with nothing in this repo
having noticed the two numbers drifted apart.

Read by AST, not import: `platform-tests` installs pytest, pyyaml and
jsonschema only (see `tests/test_collector_registry.py`'s module docstring),
and `collector_core.identity` now imports `httpx`, which is not among them.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_CORE_IDENTITY = (
    ROOT / "libs" / "collector-core" / "collector_core" / "identity.py"
)
PLAYER_IDENTITY_RESOLUTION = (
    ROOT / "services" / "player-identity" / "player_identity" / "resolution.py"
)


def _module_level_int_constant(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt.value.value
    raise AssertionError(f"{path}: no module-level {name!r} constant found")


def test_batch_limit_matches_player_identitys_max_batch_queries():
    batch_limit = _module_level_int_constant(COLLECTOR_CORE_IDENTITY, "BATCH_LIMIT")
    max_batch_queries = _module_level_int_constant(
        PLAYER_IDENTITY_RESOLUTION, "MAX_BATCH_QUERIES"
    )
    assert batch_limit == max_batch_queries, (
        f"collector_core.identity.BATCH_LIMIT ({batch_limit}) has drifted from "
        f"player_identity.resolution.MAX_BATCH_QUERIES ({max_batch_queries}) -- "
        f"a collector chunking at the old value will get a 422 from every "
        f"oversized batch against the new server cap"
    )
