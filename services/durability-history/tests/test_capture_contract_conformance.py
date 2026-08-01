"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import respx
from collector_core.conditional import UpstreamUnchanged
from jsonschema import Draft202012Validator, FormatChecker

from durability_history.adapters import upstream as upstream_mod
from durability_history.capture import (
    DURABILITY_PROFILE,
    EXPECTED_FLOOR,
    INJURY_HISTORY,
    RETURN_TRAJECTORY,
    SIGNAL_TYPES,
    capture_durability_history,
)

from .conftest import (
    CANONICAL_IDS,
    CHARLIE,
    HISTORY_SEASONS,
    NOW,
    SEASON,
    WEEK,
    mock_identity,
    mock_upstreams,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
COLLECTOR_SCHEMA = json.loads(
    (CONTRACTS / "collectors" / "durability-history.json").read_text()
)
FIELD_SCHEMAS = COLLECTOR_SCHEMA["signal_types"]


def validate(envelopes: dict) -> None:
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        # `$defs` lives at the document root and the per-signal-type subschemas
        # `$ref` into it, so a validator built on a subschema alone cannot
        # resolve them.
        schema = {**FIELD_SCHEMAS[signal_type], "$defs": COLLECTOR_SCHEMA["$defs"]}
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        assert body["signals"], f"{signal_type} emitted no rows to validate"
        for row in body["signals"]:
            validator.validate(row)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def _own_writes(lake) -> list:
    return [e for e in lake.writes if e.collector == "durability-history"]


@respx.mock
async def test_a_complete_capture_conforms(lake):
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert set(envelopes) == set(SIGNAL_TYPES)
    validate(envelopes)


@respx.mock
async def test_the_recurrence_rule_is_emitted_in_upstream_adapter(lake):
    """The spec asks for this by name, and it is not decoration.

    Without it, widening the recurrence window rewrites `is_recurrence_of` on
    every historical event at once, and a consumer diffing two lake objects sees
    every player's history change simultaneously with nothing in either object to
    say a rule changed rather than a league of hamstrings.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    adapter = envelopes[INJURY_HISTORY].upstream.adapter
    assert "recurrence=v1" in adapter, adapter
    assert f"{upstream_mod.RECURRENCE_WINDOW_DAYS}d" in adapter, adapter


@respx.mock
async def test_every_envelope_is_written_to_the_lake(lake):
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    await capture(lake)

    assert {e.signal_type for e in _own_writes(lake)} == set(SIGNAL_TYPES)


@respx.mock
async def test_the_scope_records_the_history_window(lake):
    """`career_games_possible` means nothing without the window it was counted
    over, and a lake object read a year later has only its own scope to go on."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        assert envelope.scope["history_seasons"] == HISTORY_SEASONS
        assert (
            envelope.scope["recurrence_window_days"]
            == upstream_mod.RECURRENCE_WINDOW_DAYS
        )


@respx.mock
async def test_a_failed_capture_writes_a_present_zero_envelope(lake, monkeypatch):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake is
    explicit rather than inferred from absence — then re-raises, so the last good
    capture is not overwritten by an empty one."""
    mock_identity(respx.mock)

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("upstream down")

    monkeypatch.setattr("durability_history.capture.fetch_players", boom)

    with pytest.raises(httpx.ConnectError):
        await capture(lake)

    written = _own_writes(lake)
    assert {e.signal_type for e in written} == set(SIGNAL_TYPES)
    for envelope in written:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type], (
            "expected: 0 makes Coverage.ratio read 1.0 — a total outage would "
            "report perfect coverage"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


@respx.mock
async def test_no_readable_designation_season_is_fatal(lake):
    """`absence_reason` has no source but this feed, so a pass that cannot read
    one cannot tell an injury from a suspension for anybody.

    Publishing "zero injuries league-wide" would be the most confident possible
    version of the named failure mode, which is why this one feed is fatal while
    the other two degrade.
    """
    mock_identity(respx.mock)
    mock_upstreams(respx.mock)
    for season in HISTORY_SEASONS:
        respx.mock.get(upstream_mod.INJURIES_URL.format(season=season)).respond(503)

    with pytest.raises(RuntimeError):
        await capture(lake)

    reasons = {error["reason"] for e in _own_writes(lake) for error in e.errors}
    assert "no_designation_season_readable" in reasons, reasons


@respx.mock
async def test_a_missing_production_season_degrades_rather_than_failing(lake):
    """Every signal type survives an absent weekly-stats feed, and only one field
    goes null. Discarding a good injury reconstruction over the single most
    expensive and least load-bearing feed would be backwards."""
    mock_identity(respx.mock)
    mock_upstreams(respx.mock)
    for season in HISTORY_SEASONS:
        respx.mock.get(upstream_mod.STATS_URL.format(season=season)).respond(503)

    envelopes = await capture(lake)

    assert envelopes[INJURY_HISTORY].signals, "the injury history was discarded"
    for row in envelopes[RETURN_TRAJECTORY].signals:
        assert row["post_return_production_delta"] is None
    reasons = {e["reason"] for e in envelopes[RETURN_TRAJECTORY].errors}
    assert "history_seasons_missing" in reasons, reasons
    validate(envelopes)


@respx.mock
async def test_a_lake_write_failure_still_returns_the_capture(lake):
    """Availability beats durability: the capture succeeded and only its archival
    copy did not, so `/signals` must not lose data it already has."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    lake.fail_write = True

    envelopes = await capture(lake)

    assert envelopes[DURABILITY_PROFILE].signals


@respx.mock
async def test_a_failed_write_does_not_record_the_digest(lake):
    """A digest committed for content the lake never received permanently
    suppresses the retry: the next pass digests identical content, matches, and
    raises `UpstreamUnchanged` — so the object is never written again until the
    data itself changes, which on a seasonal cadence is a whole season.

    This was a Critical on `venue`.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    lake.fail_write = True
    await capture(lake)

    lake.fail_write = False
    await capture(lake)

    assert {e.signal_type for e in _own_writes(lake)} == set(SIGNAL_TYPES), (
        "the second pass published nothing — a digest was recorded for a write "
        "that never landed"
    )


@respx.mock
async def test_a_partial_lake_failure_only_retries_the_type_that_failed(lake):
    """One signal type's write failing must not re-append the other two.

    The total-outage test above cannot see this: with nothing landing, "record
    per signal type" and "record only if everything landed" behave identically.
    Review found the coarser gate

        if any(published.landed(st) for st in published):

    survived this whole suite, while `venue` — which has this test — killed it
    at once. A guard whose two arms look alike needs a fixture per arm.

    Concretely: `player_injury_history` is the large one, and suppose that
    single PUT fails — a transient 500, an object-size rejection, a per-object
    permission problem — while the other two land. Under the coarse gate the
    pass records its digest anyway, the next pass digests identical content,
    all three match, `UpstreamUnchanged` is raised, and
    `player_injury_history` is never written again for the rest of the season
    while `/signals` and `/catalog` both look perfectly healthy.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    real_write = lake.write

    def write(envelope):
        if lake.failing and envelope.signal_type == INJURY_HISTORY:
            raise RuntimeError("lake unreachable")
        return real_write(envelope)

    lake.failing = True
    lake.write = write

    await capture(lake)
    written = [e.signal_type for e in _own_writes(lake)]
    healthy = [t for t in SIGNAL_TYPES if t != INJURY_HISTORY]
    assert sorted(written) == sorted(healthy), written
    assert INJURY_HISTORY not in written

    lake.failing = False
    await capture(lake)

    # Exactly one more write, and it is the one that failed. An unconditional
    # retry of the whole pass would re-append the two that already landed.
    written = [e.signal_type for e in _own_writes(lake)]
    assert len(written) == len(SIGNAL_TYPES), written
    assert written[-1] == INJURY_HISTORY, written
    assert sorted(written) == sorted(SIGNAL_TYPES), written


@respx.mock
async def test_an_unchanged_second_pass_raises_upstream_unchanged(lake):
    """A `seasonal` cadence must not append a byte-identical snapshot daily."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    await capture(lake)
    with pytest.raises(UpstreamUnchanged):
        await capture(lake)


@respx.mock
async def test_every_absence_carries_a_reason(lake):
    """Every missed game carries an `absence_reason`, per the spec — and here
    every one of Charlie's is a reason that is NOT an injury."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    rows = envelopes[INJURY_HISTORY].signals
    absences = [a for row in rows for a in row["absences"]]
    assert absences, "nobody missed a game; the fixture is broken"
    assert all(a["absence_reason"] for a in absences)

    charlie = next(r for r in rows if r["player_id"] == CANONICAL_IDS[CHARLIE])
    assert len(charlie["absences"]) == 4, charlie["absences"]
    assert not any(a["absence_reason"] == "injury" for a in charlie["absences"])


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
