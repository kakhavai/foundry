"""No collector reaches the repo still carrying the scaffolder's placeholder.

Issue #85: `scripts/new-collector.py` generates a field schema and a
`build_signal` that agree with each other **by construction** — both describe
`{key, observed_at, value}` and nothing else. So the generated
`tests/test_capture_contract_conformance.py` proves the two agree, not that
either is right, and a collector author who never revisits them ships a schema
that validates nothing meaningful. The projections generator finds out six
weeks later, if at all.

The scaffolder therefore stamps a `$comment` marker into every placeholder
signal-type block, and this file is the other end of it: the marker is inert
while the output is still a scaffold, and fatal once it is committed.

**Why a sentinel and not a generated failing test.** The scaffolder's promise,
asserted throughout `tests/test_new_collector.py`, is that a fresh collector is
correct *unmodified* — it lints, it renders, its own suite passes. `cd
services/<name> && uv run pytest -v` is the first thing CLAUDE.md tells an
author to run. A scaffold that is red on arrival teaches people that red is
normal, which costs more than this gate is worth. Red at the point the
placeholder stops being a placeholder is the same signal without that cost.

**Why gating the schema alone reaches both halves.** Rewriting the schema is
what forces `build_signal` to follow: the generated conformance test validates
that function's real output against this file, so the two cannot be changed
independently. Marking both would be two gates for one decision.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts" / "signal-envelope" / "collectors"

# The single string the scaffolder and this gate agree on. Read from the
# scaffolder rather than repeated, so the two cannot drift into a gate that
# looks for a marker nothing emits any more -- which fails open, silently.
_SCAFFOLDER = (ROOT / "scripts" / "new-collector.py").read_text(encoding="utf-8")
MARKER = "TODO(new-collector)"

SCHEMAS = sorted(SCHEMA_DIR.glob("*.json"))
IDS = [path.stem for path in SCHEMAS]

# The two conditions, as predicates rather than inline asserts, so
# tests/test_new_collector.py can point them at a freshly scaffolded schema and
# prove the two ends of this actually connect. A gate whose trigger is only
# ever asserted against files that do not trip it is a gate nobody has fired.


def carries_marker(text: str) -> bool:
    """Whether a schema's raw text still has the scaffolder's `$comment`."""
    return MARKER in text


def has_placeholder_field_set(document: dict) -> bool:
    """Whether any signal type still has exactly the placeholder's fields."""
    placeholder = ["key", "observed_at", "value"]
    return any(
        sorted(schema.get("properties", {})) == placeholder
        for schema in document["signal_types"].values()
    )


def test_there_are_schemas_to_check():
    """Guards every parametrized test below from passing vacuously.

    A glob that matched nothing -- a renamed directory, a moved contract --
    would make this whole file green while checking zero collectors.
    """
    assert len(SCHEMAS) >= 5, (
        f"only {len(SCHEMAS)} collector schemas found in {SCHEMA_DIR}"
    )


def test_the_scaffolder_still_stamps_the_marker():
    """The gate is worthless if the scaffolder stops emitting what it looks for.

    This is the fail-open direction, and it is the one that costs nothing to
    introduce: delete the `$comment` from `FIELD_SCHEMA_TYPE` and every test
    below keeps passing forever while checking for a string that no longer
    exists.
    """
    assert f'PLACEHOLDER_SCHEMA_MARKER = "{MARKER}"' in _SCAFFOLDER, (
        "scripts/new-collector.py no longer declares PLACEHOLDER_SCHEMA_MARKER "
        f"as {MARKER!r}. Update this file's MARKER to match, or the gate below "
        "checks for a marker nothing emits."
    )
    assert "%%marker%%" in _SCAFFOLDER, (
        "FIELD_SCHEMA_TYPE no longer interpolates the marker, so freshly "
        "scaffolded schemas carry nothing for this gate to find."
    )


@pytest.mark.parametrize("path", SCHEMAS, ids=IDS)
def test_no_committed_schema_still_carries_the_placeholder_marker(path):
    assert not carries_marker(path.read_text(encoding="utf-8")), (
        f"{path.relative_to(ROOT)} is still the scaffolder's placeholder. "
        "Replace the signal-type block(s) with this collector's real signal "
        f"shape and delete the `$comment` carrying {MARKER!r}. Its generated "
        "build_signal almost certainly needs the same treatment -- the two are "
        "written to agree, so one being untouched means both are."
    )


@pytest.mark.parametrize("path", SCHEMAS, ids=IDS)
def test_no_committed_schema_still_has_the_placeholder_field_set(path):
    """The marker can be deleted without the fields being replaced.

    Deleting a `$comment` is a one-keystroke way past the test above, and the
    result -- `key`/`observed_at`/`value` and nothing else -- is exactly the
    schema issue #85 is about. Checked separately because it is a different
    failure: the one above is "nobody looked", this one is "somebody looked and
    silenced it".
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["signal_types"], (
        f"{path.relative_to(ROOT)} declares no signal types, so the check "
        "below would pass over an empty collection"
    )
    assert not has_placeholder_field_set(document), (
        f"{path.relative_to(ROOT)} still has a signal type with exactly the "
        "scaffolder's placeholder fields (key, observed_at, value). That shape "
        "describes no real signal; it exists so the generated tests have "
        "something to run against."
    )
