"""The fixture must be a fixture, not a second implementation of the scope."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_fixture_validates_against_the_envelope_schema(tmp_path):
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/seed-scope-fixture.py"),
            "--season",
            "2026",
            "--week",
            "1",
            "--out",
            str(tmp_path),
        ],
        check=True,
    )
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
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/seed-scope-fixture.py"),
            "--season",
            "2026",
            "--week",
            "1",
            "--out",
            str(tmp_path),
        ],
        check=True,
    )
    types = {json.loads(p.read_text())["signal_type"] for p in tmp_path.rglob("*.json")}
    assert types == {"scope_membership_weekly", "scope_matchup_weekly"}
