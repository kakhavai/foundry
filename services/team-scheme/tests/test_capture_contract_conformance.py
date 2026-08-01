"""The REAL capture output, validated against the committed schema.

`contracts/signal-envelope/collectors/team-scheme.json` is what a consumer
reads before writing a line of code against this collector. Validating the
real output of `capture_team_scheme` against it means a renamed field fails
here rather than in the generator six weeks later.

**The schema sets `additionalProperties: false`**, so a field added to a row
and not to the schema fails too — the direction a contract test usually
misses, because a permissive schema silently accepts anything new.

The degraded and failure branches are validated as well. They are the branches
that produce the *unusual* row shapes — null personnel rates, null charting
rates — and are therefore where a schema that only ever saw the happy path
turns out to be wrong.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest

from team_scheme.capture import PROFILE, SIGNAL_TYPES

from .conftest import Feeds, SpyLake, run_capture

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "signal-envelope"
    / "collectors"
    / "team-scheme.json"
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
    raw = SCHEMA_PATH.read_text(encoding="utf-8")
    assert "TODO(new-collector)" not in raw
    for signal_type in SIGNAL_TYPES:
        assert "$comment" not in schema["signal_types"][signal_type]
        assert set(schema["signal_types"][signal_type]["required"]) != {
            "key",
            "observed_at",
            "value",
        }


def test_the_schema_covers_exactly_this_collectors_signal_types(schema):
    assert set(schema["signal_types"]) == set(SIGNAL_TYPES)
    assert "staff_assignment" not in schema["signal_types"]


def test_the_schema_declares_no_staff_derived_field(schema):
    """The contract is what a consumer reads, so the removal has to be visible
    there too — a schema still declaring `revision_id` invites a join no row
    can satisfy, and would go on inviting it for as long as nobody validates
    an actual response against it."""
    from .test_rates_window import STAFF_FIELDS

    declared = set(schema["signal_types"][PROFILE]["properties"])
    assert not declared & set(STAFF_FIELDS), sorted(declared & set(STAFF_FIELDS))


async def test_a_healthy_capture_conforms(schema, lake: SpyLake):
    checked = validate_rows(schema, await run_capture(lake=lake))
    assert checked == 4  # four teams, one signal type


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
    """The 46.82 MiB feed, lost. One field goes null; the other twelve do not
    — which is the entire reason it is a field-level failure."""
    envelopes = await run_capture(Feeds(participation_status=500), lake=lake)
    validate_rows(schema, envelopes)
    rows = envelopes[PROFILE].signals
    assert rows
    assert all(row["personnel_rates"] is None for row in rows)
    assert all("participation_unavailable" in row["degraded_upstreams"] for row in rows)
    assert all(row["play_action_rate"] is not None for row in rows)
    assert all(row["neutral_pass_rate"] is not None for row in rows)


async def test_a_capture_with_neither_charted_feed_conforms(schema, lake: SpyLake):
    """Both lost at once, which is the shape whose `degraded_upstreams` array
    has two entries — the only fixture that exercises the enum twice over."""
    envelopes = await run_capture(
        Feeds(ftn_status=500, participation_status=500), lake=lake
    )
    validate_rows(schema, envelopes)
    rows = envelopes[PROFILE].signals
    assert all(len(row["degraded_upstreams"]) == 2 for row in rows)
    assert all(row["neutral_pass_rate"] is not None for row in rows)


async def test_a_capture_with_no_play_by_play_writes_a_conforming_failure(
    schema, lake: SpyLake
):
    """The fatal branch. There is no second signal type to save now that the
    staff half is deferred, so this is `fail_capture`: a `present: 0` envelope
    with no rows and a populated `errors` array, then a re-raise."""
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(pbp_status=500), lake=lake)

    (written,) = lake.writes
    assert written.signals == []
    assert written.coverage.present == 0
    assert written.errors


async def test_a_team_with_no_games_conforms(schema, lake: SpyLake):
    """`games_sampled: 0`, `sampled_weeks: []`, and every rate null. The row
    publishes — 'this team exists and has no games' is a fact worth serving —
    so the schema has to accept it, and a `minimum: 1` on `games_sampled`
    would reject it."""
    feeds = Feeds()
    for play in feeds.plays:
        if play["posteam"] == "DDD":
            play["play_type"] = "punt"
            play["punt_attempt"] = 1

    envelopes = await run_capture(feeds, lake=lake)
    validate_rows(schema, envelopes)
    row = next(r for r in envelopes[PROFILE].signals if r["team_id"] == "DDD")
    assert row["games_sampled"] == 0
    assert row["sampled_weeks"] == []
    assert row["neutral_pass_rate"] is None


async def test_every_null_by_necessity_field_carries_a_reason(schema, lake: SpyLake):
    """An unsourceable value is a null WITH a reason. A bare null is
    indistinguishable from a bug in the collector, and this is the
    machine-readable half of the README's disclosure."""
    envelopes = await run_capture(lake=lake)
    rows = envelopes[PROFILE].signals
    assert rows
    for row in rows:
        assert row["fourth_down_go_rate_over_expected"] is None
        assert set(row["null_field_reason"]) == {"fourth_down_go_rate_over_expected"}
        assert all(reason for reason in row["null_field_reason"].values())


async def test_proe_is_published_in_percentage_points(schema, lake: SpyLake):
    """The unit, asserted on a real capture rather than only documented.

    The fixture stamps `pass_oe = 1.0` on every play, so a correct pass
    publishes 1.0. A collector that divided by 100 to "make it a share" would
    publish 0.01, still validate against the schema (which has no bounds on
    this field, deliberately — PROE is signed and unbounded), and be wrong by
    100x on every row.
    """
    envelopes = await run_capture(lake=lake)
    validate_rows(schema, envelopes)
    values = [row["pass_rate_over_expected"] for row in envelopes[PROFILE].signals]
    assert values
    assert values == pytest.approx([1.0] * len(values))


async def test_a_row_carrying_an_undeclared_field_would_fail(schema, lake: SpyLake):
    """`additionalProperties: false`, asserted rather than assumed.

    Without this, the schema could be permissive and every test above would
    still pass while the contract described only a subset of what ships — and
    a restored `revision_id` would sail through.
    """
    envelopes = await run_capture(lake=lake)
    row = dict(envelopes[PROFILE].signals[0])
    row["revision_id"] = "AAA-2026-r1"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(row, schema["signal_types"][PROFILE])


async def test_a_row_missing_a_required_field_would_fail(schema, lake: SpyLake):
    """The other direction, for the same reason."""
    envelopes = await run_capture(lake=lake)
    row = dict(envelopes[PROFILE].signals[0])
    del row["games_sampled"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(row, schema["signal_types"][PROFILE])
