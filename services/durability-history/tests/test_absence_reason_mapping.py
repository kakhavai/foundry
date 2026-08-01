"""The registry's `absence_reason` translation table, EXECUTED rather than read.

`durability-history` and `injury-report` publish a field of the same name over
different feeds with different enums. Reconciling them was considered and
rejected — renaming `discipline` to `suspension` mis-labels a coach's decision,
and collapsing `non_injury_other` into `undesignated` deletes the distinction the
named failure mode's guard turns on — so the deliverable of that decision is the
translation table in `contracts/collector-registry.yaml`, which is what a
generator is explicitly told to translate with.

**A presence-only test cannot see a wrong row.** The first revision of that table
filed a healthy scratch under `discipline` when `_NON_INJURY_PATTERNS` maps it to
`rest`, and every test passed. So this file parses the table out of the registry
and *runs* `classify_absence` on every example it quotes: a row that claims
something the code does not do is a failing test rather than misleading prose in
the one artifact a downstream consumer trusts.
"""

import re
from pathlib import Path

import pytest

from durability_history.adapters.upstream import DesignationRow
from durability_history.events import ABSENCE_REASONS, classify_absence

REGISTRY = (
    Path(__file__).resolve().parents[3] / "contracts" / "collector-registry.yaml"
).read_text(encoding="utf-8")

# `injury-report`'s own enum, from services/injury-report/injury_report/
# vocabulary.py. Duplicated rather than imported: `platform-tests` installs no
# service packages, and a cross-service import would not resolve in CI. Pinned
# here so a change there shows up as a failure in the table that claims to map
# onto it.
INJURY_REPORT_REASONS = frozenset(
    {"injury", "rest", "illness", "personal", "suspension", "unspecified"}
)

BEGIN = "ABSENCE_REASON_MAP_BEGIN"
END = "ABSENCE_REASON_MAP_END"
ROW = re.compile(r"#\s+(\w+)\s*->\s*(\w+)(.*)$")


def _table() -> list[tuple[str, str, list[str]]]:
    """`(ours, theirs, examples)` per row of the registry's map block."""
    body = REGISTRY[REGISTRY.index(BEGIN) + len(BEGIN) : REGISTRY.index(END)]
    rows = []
    for line in body.splitlines():
        match = ROW.search(line)
        if match:
            ours, theirs, tail = match.groups()
            rows.append((ours, theirs, re.findall(r'"([^"]+)"', tail)))
    return rows


TABLE = _table()


def designation(primary: str) -> DesignationRow:
    from datetime import date

    return DesignationRow(
        season=2026,
        week=1,
        team="SEA",
        gsis_id="00-0000001",
        game_type="REG",
        report_status="Out",
        report_primary_injury=primary,
        report_secondary_injury="",
        practice_primary_injury="",
        practice_secondary_injury="",
        practice_status="Did Not Participate In Practice",
        reported_at=date(2026, 9, 5),
    )


def test_the_table_was_found_and_is_not_empty():
    """A parser that silently matches nothing turns every assertion below into
    `all([])`, which is `True`."""
    assert len(TABLE) == len(ABSENCE_REASONS), TABLE


def test_the_table_covers_exactly_the_published_enum():
    """A value published but absent from the table leaves a generator with an
    untranslatable reason; a row for a value nobody publishes is a promise about
    data that does not exist."""
    assert {ours for ours, _theirs, _ex in TABLE} == set(ABSENCE_REASONS)


def test_every_target_is_a_real_injury_report_value():
    """The right-hand column has to name something that actually exists over
    there, or the table translates into nothing."""
    for ours, theirs, _examples in TABLE:
        assert theirs in INJURY_REPORT_REASONS, (ours, theirs)


@pytest.mark.parametrize(
    ("ours", "example"),
    [(ours, ex) for ours, _theirs, examples in _table() for ex in examples],
)
def test_every_quoted_example_really_classifies_that_way(ours, example):
    """The assertion the healthy-scratch row would have failed.

    Every example in the registry is a designation string; `classify_absence` is
    the only thing that decides what one means, so running it is the only way the
    table can be checked rather than believed.
    """
    assert classify_absence(designation(example)) == ours, (
        f"the registry claims {example!r} is {ours!r}, but classify_absence "
        f"returns {classify_absence(designation(example))!r}"
    )


def test_a_healthy_scratch_is_rest_and_NOT_discipline():
    """Named explicitly because it is the row that was wrong, and because the two
    are easy to conflate in prose: a club sitting a fit player is `rest`; a
    coach's decision and a suspension are `discipline`."""
    assert classify_absence(designation("Healthy Scratch")) == "rest"
    assert classify_absence(designation("Coach's Decision")) == "discipline"
    assert classify_absence(designation("Suspended by the club")) == "discipline"


def test_undesignated_is_the_row_with_no_example_because_it_has_no_text():
    """It is the ABSENCE of a designation row, not a string, so the table cannot
    quote one — and `classify_absence(None)` is the guard's single most important
    line."""
    row = next(row for row in TABLE if row[0] == "undesignated")
    assert row[2] == [], row
    assert classify_absence(None) == "undesignated"


def test_only_the_four_identities_are_identities():
    """The table's whole purpose is that a generator cannot assume a pass-through.
    If every row became an identity the translation would be a no-op and nobody
    would notice until `discipline` arrived where `suspension` was expected."""
    identities = {ours for ours, theirs, _ex in TABLE if ours == theirs}
    assert identities == {"injury", "illness", "personal", "rest"}
