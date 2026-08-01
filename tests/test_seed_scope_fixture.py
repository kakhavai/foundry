"""The fixture must be a fixture, not a second implementation of the scope.

Unlike every other test in `tests/`, which is AST- or stdlib-only (see
CLAUDE.md's collector-registry drift-gate table), the tests below invoke
`scripts/seed-scope-fixture.py` as a real subprocess that imports
`collector_core.envelope`. That module has no third-party dependencies of its
own, but `collector_core` itself is not on PyPI — it is only importable
because the root `pyproject.toml`'s `[tool.uv.workspace]` includes `libs/*`
as a member, so `uv run` (without `--no-project`) syncs the whole workspace
before running pytest. This is the opposite of `platform-lint`'s
`uv run --no-project --with ruff==0.16.0 ...`, and it is why that flag must
never be added to the `platform-tests` job's own invocation
(`uv run --with pyyaml==6.0.3 --with pytest==9.0.3 --with jsonschema==4.26.0
pytest tests/ -q`) — `--no-project` there would make `collector_core`
unimportable and every test below would fail with `CalledProcessError`.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_SCHEMA = ROOT / "contracts/signal-envelope/collectors/roster-scope.json"


def _seed(out_dir: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/seed-scope-fixture.py"),
            "--season",
            "2026",
            "--week",
            "1",
            "--out",
            str(out_dir),
        ],
        check=True,
    )


def test_the_fixture_validates_against_the_envelope_schema(tmp_path):
    _seed(tmp_path)
    written = sorted(tmp_path.rglob("*.json"))
    assert len(written) == 2

    import jsonschema

    schema = json.loads(
        (ROOT / "contracts/signal-envelope/envelope.v1.schema.json").read_text()
    )
    for path in written:
        jsonschema.validate(json.loads(path.read_text()), schema)


def test_both_scope_signal_types_are_seeded(tmp_path):
    """A membership-only fixture would make injury-report fail closed in CI
    for a reason that has nothing to do with the code under test."""
    _seed(tmp_path)
    types = {json.loads(p.read_text())["signal_type"] for p in tmp_path.rglob("*.json")}
    assert types == {"scope_membership_weekly", "scope_matchup_weekly"}


def test_membership_rows_conform_to_the_collector_field_schema(tmp_path):
    """`envelope.v1.schema.json`'s `signals` property is deliberately opaque
    (`{"type": "array", "items": {"type": "object"}}`) — the first two tests
    above pass just as well with `signals: []` or `signals: [{}]`. This is
    the check that actually looks inside a row, the same way
    `tests/test_signal_envelope_conformance.py` does for every committed
    fixture, against the same per-collector field schema.
    """
    _seed(tmp_path)
    envelope = json.loads((tmp_path / "scope_membership_weekly.json").read_text())
    rows = envelope["signals"]
    # Asserted before `all(...)` below: `all([])` is `True`, so a fixture that
    # silently narrowed itself to zero rows would pass every check that
    # follows without this.
    assert len(rows) > 0, "fixture produced zero membership rows"

    import jsonschema

    field_schema = json.loads(COLLECTOR_SCHEMA.read_text())["signal_types"][
        "scope_membership_weekly"
    ]
    validator = jsonschema.Draft202012Validator(
        field_schema, format_checker=jsonschema.FormatChecker()
    )
    for row in rows:
        validator.validate(row)
    assert all(row.get("player_id") for row in rows)


def test_matchup_rows_conform_to_the_collector_field_schema(tmp_path):
    _seed(tmp_path)
    envelope = json.loads((tmp_path / "scope_matchup_weekly.json").read_text())
    rows = envelope["signals"]
    assert len(rows) > 0, "fixture produced zero matchup rows"

    import jsonschema

    field_schema = json.loads(COLLECTOR_SCHEMA.read_text())["signal_types"][
        "scope_matchup_weekly"
    ]
    validator = jsonschema.Draft202012Validator(
        field_schema, format_checker=jsonschema.FormatChecker()
    )
    for row in rows:
        validator.validate(row)
    assert all(row.get("player_id") for row in rows)
