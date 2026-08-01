"""The REAL capture output, validated against the committed schema.

`contracts/signal-envelope/collectors/coaching-scheme.json` is what a consumer
reads before writing a line of code against this collector. Validating the
real output of `capture_coaching_scheme` against it means a renamed field
fails here rather than in the generator six weeks later.

**Both schemas set `additionalProperties: false`**, so a field added to a row
and not to the schema fails too — the direction a contract test usually
misses, because a permissive schema silently accepts anything new.

The degraded and failure branches are validated as well. They are the branches
that produce the *unusual* row shapes — null personnel rates, null charting
rates — and are therefore where a schema that only ever saw the happy path
turns out to be wrong.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from coaching_scheme.capture import PROFILE, SIGNAL_TYPES, STAFF

from .conftest import (
    Feeds,
    SpyLake,
    coaches_with_change,
    proe_with_shift,
    run_capture,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "signal-envelope"
    / "collectors"
    / "coaching-scheme.json"
)


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_rows(schema: dict, envelopes, *, expect_rows: bool = True) -> int:
    """Validate every row of every signal type. Returns how many were checked.

    The count is returned and asserted by callers rather than discarded:
    `all(...)` over an empty list is `True`, and a validation loop over zero
    rows is the same vacuous pass wearing a different hat.
    """
    checked = 0
    for signal_type in SIGNAL_TYPES:
        envelope = envelopes.get(signal_type)
        if envelope is None:
            continue
        row_schema = schema["signal_types"][signal_type]
        for row in envelope.signals:
            jsonschema.validate(row, row_schema)
            checked += 1
    if expect_rows:
        assert checked > 0, "validated nothing — the fixture produced no rows"
    return checked


def test_the_schema_carries_no_placeholder_marker(schema):
    """`tests/test_placeholder_schemas.py` in `platform-tests` enforces this
    repo-wide; asserted here too so it fails in the service's own suite,
    where the author is actually looking."""
    for signal_type in SIGNAL_TYPES:
        assert "$comment" not in schema["signal_types"][signal_type]
        assert set(schema["signal_types"][signal_type]["required"]) != {
            "key",
            "observed_at",
            "value",
        }


def test_the_schema_covers_exactly_this_collectors_signal_types(schema):
    assert set(schema["signal_types"]) == set(SIGNAL_TYPES)


async def test_a_healthy_capture_conforms(schema, lake: SpyLake):
    checked = validate_rows(schema, await run_capture(lake=lake))
    assert checked >= 8  # four teams x two signal types, at minimum


async def test_a_multi_revision_capture_conforms(schema, lake: SpyLake):
    """The row shape that carries a non-null `effective_to_week` and a
    `change_event` of `unclassified` — neither of which a single-revision
    fixture ever produces."""
    envelopes = await run_capture(
        Feeds(coaches=coaches_with_change("AAA", at_week=7)), lake=lake
    )
    validate_rows(schema, envelopes)
    rows = [r for r in envelopes[STAFF].signals if r["team_id"] == "AAA"]
    assert {r["change_event"] for r in rows} == {"none", "unclassified"}
    assert {r["effective_to_week"] for r in rows} == {6, None}


async def test_an_unchecked_changepoint_row_conforms(schema, lake: SpyLake):
    """The shipped shape: guard 2 is disabled, so `changepoint_unexplained` is
    **null** rather than `false` and carries a reason. A schema typing it as a
    bare `boolean` — which the first revision did — rejects every row."""
    envelopes = await run_capture(lake=lake)
    validate_rows(schema, envelopes)
    rows = envelopes[PROFILE].signals
    assert rows
    assert all(row["changepoint_unexplained"] is None for row in rows)
    assert all(row["null_field_reason"]["changepoint_unexplained"] for row in rows)


async def test_a_flagged_changepoint_row_conforms(schema, lake: SpyLake, monkeypatch):
    """`changepoint_week`, `changepoint_shift` and `changepoint_p_value` are
    null on every shipped row, so only this fixture proves the schema accepts
    their populated forms — including `exclusiveMinimum: 0` on the p-value."""
    from coaching_scheme import changepoint as changepoint_module

    monkeypatch.setattr(changepoint_module, "CHANGEPOINT_ENABLED", True)
    envelopes = await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=40.0, jitter=2.0)),
        lake=lake,
    )
    validate_rows(schema, envelopes)
    flagged = [r for r in envelopes[PROFILE].signals if r["changepoint_unexplained"]]
    assert flagged
    assert flagged[0]["changepoint_week"] is not None
    assert 0 < flagged[0]["changepoint_p_value"] <= 1


async def test_a_capture_with_no_charting_feed_conforms(schema, lake: SpyLake):
    """The degraded shape: null `play_action_rate` and `pre_snap_motion_rate`,
    with the loss named in `degraded_upstreams`. A schema written only against
    the happy path types those as `number` and fails here."""
    envelopes = await run_capture(Feeds(ftn_status=500), lake=lake)
    validate_rows(schema, envelopes)
    rows = envelopes[PROFILE].signals
    assert rows
    assert all(row["play_action_rate"] is None for row in rows)
    assert all(row["pre_snap_motion_rate"] is None for row in rows)
    assert all("ftn_charting_unavailable" in row["degraded_upstreams"] for row in rows)
    # The feed that did survive is unaffected — a field-level failure must not
    # spread.
    assert all(row["personnel_rates"] is not None for row in rows)


async def test_a_capture_with_no_participation_feed_conforms(schema, lake: SpyLake):
    """The 46.82 MiB feed, lost. One field goes null; the other thirteen do
    not — which is the entire reason it is a field-level failure."""
    envelopes = await run_capture(Feeds(participation_status=500), lake=lake)
    validate_rows(schema, envelopes)
    rows = envelopes[PROFILE].signals
    assert rows
    assert all(row["personnel_rates"] is None for row in rows)
    assert all("participation_unavailable" in row["degraded_upstreams"] for row in rows)
    assert all(row["play_action_rate"] is not None for row in rows)
    assert all(row["neutral_pass_rate"] is not None for row in rows)


async def test_a_capture_with_no_play_by_play_conforms(schema, lake: SpyLake):
    """The degraded branch: `staff_assignment` publishes normally and
    `team_scheme_profile` is a `present: 0` failure envelope with no rows."""
    envelopes = await run_capture(Feeds(pbp_status=500), lake=lake)
    validate_rows(schema, envelopes)
    assert envelopes[STAFF].signals
    assert envelopes[PROFILE].signals == []
    assert envelopes[PROFILE].coverage.present == 0
    assert envelopes[PROFILE].errors


async def test_every_null_by_necessity_field_carries_a_reason(schema, lake: SpyLake):
    """An untested value is a null WITH a reason. A bare null is
    indistinguishable from a bug in the collector, and this is the
    machine-readable half of the README's disclosure."""
    envelopes = await run_capture(lake=lake)
    for row in envelopes[STAFF].signals:
        assert row["offensive_coordinator_id"] is None
        assert row["defensive_coordinator_id"] is None
        assert row["change_reported_at"] is None
        assert set(row["null_field_reason"]) == {
            "offensive_coordinator_id",
            "defensive_coordinator_id",
            "change_reported_at",
        }
        assert all(reason for reason in row["null_field_reason"].values())
    for row in envelopes[PROFILE].signals:
        assert row["fourth_down_go_rate_over_expected"] is None
        assert row["null_field_reason"]["fourth_down_go_rate_over_expected"]


async def test_a_row_carrying_an_undeclared_field_would_fail(schema, lake: SpyLake):
    """`additionalProperties: false`, asserted rather than assumed.

    Without this, the schema could be permissive and every test above would
    still pass while the contract described only a subset of what ships.
    """
    envelopes = await run_capture(lake=lake)
    row = dict(envelopes[STAFF].signals[0])
    row["a_field_nobody_declared"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(row, schema["signal_types"][STAFF])


async def test_a_row_missing_a_required_field_would_fail(schema, lake: SpyLake):
    """The other direction, for the same reason."""
    envelopes = await run_capture(lake=lake)
    row = dict(envelopes[PROFILE].signals[0])
    del row["games_sampled"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(row, schema["signal_types"][PROFILE])
