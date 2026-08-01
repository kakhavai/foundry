"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures -- both sides hand-maintained -- so it catches fixture drift
and never producer drift. A field renamed in `capture.py` leaves it entirely
green. This file closes that gap by running the real capture path and
validating the rows it actually emits, on the degraded paths as well as the
happy one.
"""

import gzip
import json
from pathlib import Path

import jsonschema
import pytest
from collector_core.streaming import UpstreamSchemaError, UpstreamTruncated
from jsonschema import Draft202012Validator, FormatChecker

from defense_vs_position.capture import EXPECTED_FLOOR, SIGNAL_TYPES
from defense_vs_position.ratings import declared_splits
from defense_vs_position.scoring import ALIGNMENTS, POSITIONS, SCORING_FORMATS

from . import season
from .conftest import SpyLake, run_capture

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "defense-vs-position.json").read_text(),
)["signal_types"]


def validate(envelopes: dict) -> None:
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)


async def test_a_complete_capture_conforms(upstreams):
    envelopes = await run_capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


async def test_the_published_row_set_is_the_declared_cross_product(upstreams):
    """32 defenses x every declared split, and nothing else.

    A row set that is merely *large* satisfies a count assertion while quietly
    dropping a whole scoring format, so this pins the identity of the set
    rather than its size.
    """
    envelopes = await run_capture(SpyLake())
    rows = envelopes["defense_positional_allowance"].signals
    keys = {
        (r["team_id"], r["position"], r["alignment"], r["scoring_format"]) for r in rows
    }
    expected = {
        (team, position, alignment, fmt)
        for team in season.TEAMS
        for position in POSITIONS
        for alignment in ALIGNMENTS
        for fmt in SCORING_FORMATS
    }
    assert keys == expected
    assert len(rows) == len(season.TEAMS) * declared_splits()


async def test_alignment_is_only_ever_all(upstreams):
    """The narrowing, asserted rather than left to the schema.

    `all` is a legal member of the spec's enum, so this is a narrowing within
    its vocabulary -- but synthesising `slot`/`perimeter` from a season-long
    roster label is what the spec itself names as the wrong answer, and this
    is the test that fails if somebody adds them from `players.csv`.
    """
    envelopes = await run_capture(SpyLake())
    rows = envelopes["defense_positional_allowance"].signals
    assert {row["alignment"] for row in rows} == {"all"}


async def test_a_complete_capture_reports_full_coverage(upstreams):
    """Not decoration: `expected` is floored independently of the fetch, so a
    healthy pass reaching the floor is what proves the floor is right."""
    envelopes = await run_capture(SpyLake())
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.present == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.ratio == 1.0
        assert envelope.coverage.missing == []


async def test_every_envelope_is_written_to_the_lake(upstreams):
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await run_capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_lake_outage_costs_durability_not_availability(upstreams):
    """`publish_capture` returns the envelopes anyway.

    Nine collectors hand-rolled this tail and eight let the write escape,
    turning an object-store outage into a loss of `/signals`.
    """
    envelopes = await run_capture(SpyLake(fail_write=True))
    assert envelopes["defense_positional_allowance"].signals


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(season.pbp_document()[:5000], UpstreamTruncated, id="truncated"),
        pytest.param(
            gzip.compress(b"game_id,week,posteam\n2026_01_A_B,1,A\n"),
            UpstreamSchemaError,
            id="schema-drift",
        ),
    ],
)
async def test_a_broken_pbp_writes_a_present_zero_envelope(upstreams, body, expected):
    """The contract: a poll that fails writes `coverage.present: 0` with a
    populated `errors` array -- then re-raises, so the last good capture is not
    overwritten by an empty one.

    Two arms because they fail at different points: truncation raises from the
    gzip trailer check after most of the document has been read, schema drift
    raises from the header before a single row has. A fixture for only the
    second would leave the trailer check untested, and a short body is the one
    corruption that otherwise looks like a genuinely quiet week.
    """
    upstreams.set_pbp(body)
    lake = SpyLake()

    with pytest.raises(expected):
        await run_capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type], (
            "a failure envelope must floor to the real universe size, not to "
            "UNKNOWN_EXPECTED_FLOOR -- otherwise the ratio reads better than "
            "it is"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_an_unreachable_players_feed_fails_the_pass(upstreams):
    """The cheap feed is not optional: without a roster position map every
    opportunity is dropped, and the pass would publish 32 defenses of zeroes
    rather than fail."""
    upstreams.set_players(gzip.compress(b"gsis_id,display_name\n"))
    lake = SpyLake()
    with pytest.raises(UpstreamSchemaError):
        await run_capture(lake)
    assert all(e.coverage.present == 0 for e in lake.writes)


async def test_a_missing_player_identity_url_fails_closed(upstreams, monkeypatch):
    """No identity seam means nothing can be resolved, so nothing can be
    attributed. Fails with its own reason rather than the classifier's
    `unknown`: a config fact and a crash have different fixes."""
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "")
    lake = SpyLake()

    with pytest.raises(Exception) as caught:
        await run_capture(lake)
    assert caught.value.reason == "identity_unavailable"

    reasons = {e["reason"] for envelope in lake.writes for e in envelope.errors}
    assert reasons == {"identity_unavailable"}
    # And no upstream was touched: failing closed means not spending 20.6 MiB
    # before discovering there is nowhere to send the ids.
    assert not upstreams.pbp_route.called


async def test_each_upstream_is_fetched_exactly_once_per_pass(upstreams):
    """Never hold an upstream response more than once, and never ask for it
    twice either: 20.61 MiB is the whole cost story."""
    await run_capture(SpyLake())
    assert upstreams.pbp_route.call_count == 1
    assert upstreams.players_route.call_count == 1


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows
    go unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


def test_the_schema_rejects_an_unknown_field():
    """`additionalProperties: false` is what makes the conformance test above
    two-way rather than a subset check: without it `build_signal` could emit a
    typo'd field name forever and every test would stay green."""
    validator = Draft202012Validator(
        FIELD_SCHEMAS["defense_positional_allowance"], format_checker=FormatChecker()
    )
    row = {
        "team_id": "PHI",
        "position": "WR",
        "alignment": "all",
        "scoring_format": "ppr",
        "observed_at": "2026-09-15T12:00:00Z",
        "games_sampled": 2,
        "opportunities_defended": 10,
        "fantasy_points_allowed_per_game": 1.0,
        "fantasy_points_allowed_per_game_adj": 1.0,
        "fantasy_points_allowed_per_opportunity": 0.1,
        "targets_allowed_per_game": 1.0,
        "receptions_allowed_per_game": 1.0,
        "receiving_yards_allowed_per_game": 1.0,
        "yac_allowed_per_reception": 1.0,
        "rush_yards_allowed_per_carry": None,
        "touchdowns_allowed_per_game": 0.0,
        "opponent_strength_index": 1.0,
        "adjustment_method": "x",
        "adjustment_window_weeks": 2,
        "rank_divergence_flagged": False,
    }
    validator.validate(row)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**row, "fantasy_points_allowed_pergame": 1.0})


async def test_the_envelope_names_the_artifact_it_read(upstreams):
    """`source_ref` is what makes a lake object reproducible a season later,
    and it is the same string the ETag store is keyed by."""
    envelopes = await run_capture(SpyLake())
    for envelope in envelopes.values():
        assert envelope.upstream.source_ref.endswith("play_by_play_2026.csv.gz")


async def test_a_capture_of_an_earlier_week_excludes_later_plays(upstreams):
    """`POST /refresh {"week": 2}` must rate through week 2, not the whole file.

    Driven at two weeks that are neither the first nor equal: an off-by-one in
    the `row_week > week` filter passes a week-1 test by accident, because
    week 1 is also the minimum.
    """
    upstreams.set_pbp(season.pbp_document(weeks=3))
    week_two = await run_capture(SpyLake(), week=2)
    week_three = await run_capture(SpyLake(), week=3)

    def window(envelopes):
        return {
            row["adjustment_window_weeks"]
            for row in envelopes["defense_positional_allowance"].signals
        }

    assert window(week_two) == {2}
    assert window(week_three) == {3}


async def test_every_published_float_is_json_serialisable(upstreams):
    """A NaN from a zero-denominator division serialises as bare `NaN`, which
    is not valid JSON: the lake object writes and every reader fails on it.
    That is why `_rate` returns `None` rather than dividing."""
    envelopes = await run_capture(SpyLake())
    body = envelopes["defense_positional_allowance"].to_dict()
    json.dumps(body, allow_nan=False)
