"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `ratings.py` leaves it entirely
green. This file closes that gap by running the real capture path and
validating the rows it actually emits, on the degraded paths as well as the
happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from offensive_line.capture import (
    EXPECTED_FLOOR,
    NULL_FIELD_REASON,
    SIGNAL_TYPES,
    STRENGTH,
)
from offensive_line.ratings import RECORD_STARTER, RECORD_UNIT

from . import season as season_module
from .conftest import Feeds, SpyLake, run_capture, starters, units

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "offensive-line.json").read_text(),
)["signal_types"]

ROW_SHAPES = {branch["title"]: branch for branch in FIELD_SCHEMAS[STRENGTH]["oneOf"]}


def validate(envelopes: dict) -> None:
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)


async def test_a_complete_capture_conforms():
    envelopes = await run_capture(Feeds(), lake=SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


async def test_both_row_shapes_are_actually_emitted():
    """A `oneOf` schema whose second branch nothing produces would validate
    forever and describe a row shape that does not exist."""
    envelopes = await run_capture(Feeds(), lake=SpyLake())
    kinds = {row["record_type"] for row in envelopes[STRENGTH].signals}
    assert kinds == {RECORD_UNIT, RECORD_STARTER}


async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_failed_capture_writes_a_present_zero_envelope():
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the
    last good capture is not overwritten by an empty one."""
    lake = SpyLake()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(status={"pbp": 503}), lake=lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1
        assert envelope.errors


async def test_a_participation_outage_is_fatal_rather_than_field_level():
    """A `offensive_line_strength` unit row with every pressure column null is
    a complete-looking row describing nothing, which is worse than a
    `present: 0` envelope that says so. Same call `defensive-front` makes for
    the same feed."""
    lake = SpyLake()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(status={"participation": 500}), lake=lake)
    assert all(e.coverage.present == 0 for e in lake.writes)


# --------------------------------------------------------------------------
# The degraded paths — where a conformance test usually stops looking
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("feed", "reason"),
    [
        ("snaps", "snap_counts_unavailable"),
        ("depth", "depth_charts_unavailable"),
        ("players", "players_unavailable"),
    ],
)
async def test_losing_a_starter_feed_costs_the_starter_rows_and_nothing_else(
    feed, reason
):
    """Field-level, not fatal. Each of the three feeds behind the starter half
    is a different link in one chain — who played, what slot they play, and
    the crosswalk between the two vocabularies — so losing any of them costs
    the same five rows per team and none of the rates."""
    envelopes = await run_capture(Feeds(status={feed: 500}), lake=SpyLake())
    validate(envelopes)
    assert starters(envelopes) == {}
    for row in units(envelopes).values():
        assert row["lineup_hash"] is None
        assert reason in row["degraded_upstreams"]
        assert row["pressure_rate_allowed"] is not None, (
            "a roster-side outage must not touch the pressure metrics"
        )
        assert row["adjusted_line_yards"] is not None


async def test_an_injury_outage_costs_only_availability():
    """The one feed whose loss costs a single field. A starter with no report
    reads `active`, which is what a man absent from the injury report is."""
    envelopes = await run_capture(Feeds(status={"injuries": 500}), lake=SpyLake())
    validate(envelopes)
    for rows in starters(envelopes).values():
        for row in rows:
            assert row["starter_availability"] in {"active", "ir"}
    for team, row in units(envelopes).items():
        assert row["degraded_upstreams"] == ["injuries_unavailable"]
        if team == season_module.UNLABELLED_TEAM:
            # This one has no hash on any pass — its centre carries no
            # depth-chart label at all. Excluded so the assertion below is
            # about the injury feed rather than about the fixture.
            continue
        assert row["lineup_hash"] is not None, (
            "the injury feed has nothing to do with the lineup"
        )


async def test_a_healthy_pass_declares_no_degradation():
    """The negative arm. Without it, a collector that reported every feed as
    degraded on every pass would pass every test above."""
    for row in units(await run_capture(Feeds(), lake=SpyLake())).values():
        assert row["degraded_upstreams"] == []


# --------------------------------------------------------------------------
# The nulls that are nulls BY NECESSITY
# --------------------------------------------------------------------------


async def test_the_unsourceable_fields_are_null_with_a_reason():
    """An unsourceable value is a null WITH a machine-readable reason — never
    a default, never quietly dropped from the schema, and never derived from a
    different quantity that happens to be available."""
    for row in units(await run_capture(Feeds(), lake=SpyLake())).values():
        for field in NULL_FIELD_REASON:
            assert row[field] is None
            assert row["null_field_reason"][field]
        if row["lineup_change_known"]:
            assert row["null_field_reason"] == NULL_FIELD_REASON
        else:
            # A row that cannot tell whether its five changed nulls two more
            # fields, each with its own reason. Superset, never a replacement:
            # the unsourceable ones stay unsourceable.
            assert NULL_FIELD_REASON.items() <= row["null_field_reason"].items()
            assert row["pressure_rate_allowed_adj"] is None
            assert row["null_field_reason"]["pressure_rate_allowed_adj"]


def test_the_schema_forbids_a_value_for_the_unsourceable_fields():
    """`"type": "null"`, not `["number", "null"]`. A collector that later
    "filled in" yards before contact from adjusted line yards would then fail
    conformance rather than publish a plausible wrong number under a name the
    generator trusts."""
    properties = ROW_SHAPES["unit row"]["properties"]
    for field in NULL_FIELD_REASON:
        assert properties[field]["type"] == "null", field


def test_the_null_fields_mirror_defensive_fronts_stems():
    """The pairing constraint applied to a *gap*. A differential where one
    term is real and the other is null looks computable and is not, so the two
    collectors null the same stem deliberately rather than by coincidence."""
    sibling = json.loads(
        (CONTRACTS / "collectors" / "defensive-front.json").read_text()
    )["signal_types"]["defensive_front_strength"]["properties"]
    theirs = {
        name.replace("_allowed", "")
        for name, schema in sibling.items()
        if schema.get("type") == "null"
    }
    ours = set(NULL_FIELD_REASON)
    assert theirs == ours, (
        "the two collectors' deliberately-null fields have diverged; a "
        f"differential over {theirs ^ ours} would look computable"
    )


def test_the_schema_restricts_the_availability_enum():
    """Left open, a row could claim a status no free feed publishes."""
    enum = ROW_SHAPES["starter row"]["properties"]["starter_availability"]["enum"]
    assert enum == ["active", "questionable", "doubtful", "out", "ir"]
    assert ROW_SHAPES["starter row"]["additionalProperties"] is False
    assert ROW_SHAPES["unit row"]["additionalProperties"] is False


def test_the_schema_restricts_the_provenance_enum():
    """The spec's hard part: a modelled delta must not look measured. An open
    enum would let a fourth value appear and mean nothing."""
    enum = ROW_SHAPES["starter row"]["properties"]["replacement_delta_provenance"][
        "enum"
    ]
    assert enum == ["measured", "league_positional_prior", "unavailable"]


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows
    go unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


def test_the_schema_carries_no_scaffold_marker():
    """`tests/test_placeholder_schemas.py` enforces this repo-wide; asserting
    it here too means the service's own suite fails first, where the fix is."""
    assert "$comment" not in FIELD_SCHEMAS[STRENGTH]
    for branch in ROW_SHAPES.values():
        assert not {"key", "observed_at", "value"} <= set(branch["properties"])


async def test_every_declared_row_field_is_actually_emitted():
    """`required` on the schema proves the rows carry the fields; this proves
    the schema is not describing a field nothing produces — the drift that
    only ever shows up in the generator."""
    envelopes = await run_capture(Feeds(), lake=SpyLake())
    rows = envelopes[STRENGTH].signals
    for title, record_type in (
        ("unit row", RECORD_UNIT),
        ("starter row", RECORD_STARTER),
    ):
        emitted = {
            key for row in rows if row["record_type"] == record_type for key in row
        }
        assert emitted == set(ROW_SHAPES[title]["properties"]), title
        assert emitted == set(ROW_SHAPES[title]["required"]), title


async def test_the_envelope_names_the_artifact_it_read():
    """`source_ref` is what makes a lake object reproducible a season later,
    and `adapter` is where the spec asks this collector to declare that its
    pressure attribution is unit-level rather than per-blocker."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.upstream.source_ref.endswith(
        f"play_by_play_{season_module.SEASON}.csv.gz"
    )
    assert envelope.upstream.adapter == "nflverse-offensive-line-unit-attributed"
    assert envelope.coverage.expected == EXPECTED_FLOOR[STRENGTH]
