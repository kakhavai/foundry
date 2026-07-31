"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jsonschema
import pytest
from collector_core.streaming import UpstreamSchemaError
from jsonschema import Draft202012Validator, FormatChecker

from injury_report.capture import (
    MIN_SCHEDULED_TEAMS,
    SIGNAL_TYPES,
    capture_injury_report,
)
from injury_report.report import practice_days_elapsed

from .conftest import NOW, SpyLake

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "injury-report.json").read_text()
)["signal_types"]

DAYS = practice_days_elapsed(NOW)


def validate(envelopes: dict) -> None:
    checked = 0
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)
            checked += 1
    assert checked, "no rows were validated — the assertion above was vacuous"


async def capture(lake, **kwargs):
    """The real capture, against the adapters' own deterministic stub week —
    not a mocked `fetch_*`. That is the point: a renamed field has to fail here
    rather than in the generator six weeks later."""
    async with httpx.AsyncClient() as client:
        return await capture_injury_report(
            2026, 1, client=client, lake=lake, now=NOW, **kwargs
        )


async def test_a_complete_capture_conforms():
    envelopes = await capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


async def test_a_complete_capture_reports_honest_coverage():
    """The stub week is a full 32-club slate in which one club (`LV`) files
    nothing. Coverage must be short by exactly that club's days — high, but
    deliberately not 1.0, because a collector that cannot report a real gap
    cannot report an unreal one either."""
    envelopes = await capture(SpyLake())
    assert envelopes
    for envelope in envelopes.values():
        assert envelope.coverage.expected == 32 * len(DAYS)
        assert envelope.coverage.expected >= MIN_SCHEDULED_TEAMS * len(DAYS)
        assert envelope.coverage.present == envelope.coverage.expected - len(DAYS)
        assert 0.9 < envelope.coverage.ratio < 1.0
        assert {error["reason"] for error in envelope.errors} == {
            "report_not_published"
        }


async def test_the_empty_filer_is_present_and_the_silent_one_is_missing():
    """The distinction, end to end through the real capture: `NYJ` files an
    empty report every day and is coverage-present; `LV` files nothing and is
    coverage-missing. Neither reads as the other."""
    envelopes = await capture(SpyLake())
    team_envelope = envelopes["team_injury_report"]

    nyj = [row for row in team_envelope.signals if row["team"] == "NYJ"]
    assert len(nyj) == len(DAYS)
    assert {row["filing_status"] for row in nyj} == {"empty"}
    assert not any(key.startswith("NYJ:") for key in team_envelope.coverage.missing)

    assert not [row for row in team_envelope.signals if row["team"] == "LV"]
    assert team_envelope.coverage.missing == sorted(f"LV:{day}" for day in DAYS)


async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


@pytest.mark.parametrize(
    ("target", "exc", "expected_reason"),
    [
        (
            "injury_report.capture.fetch_scheduled_games",
            httpx.ConnectError("schedule upstream down"),
            "transport",
        ),
        (
            "injury_report.capture.fetch_report_rows",
            httpx.ConnectError("injury upstream down"),
            "transport",
        ),
        (
            "injury_report.capture.fetch_report_rows",
            UpstreamSchemaError("report_status column renamed"),
            "schema",
        ),
    ],
)
async def test_a_failed_capture_writes_a_present_zero_envelope(
    monkeypatch, target, exc, expected_reason
):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one.

    A schema break is classified `schema` rather than the generic `malformed`:
    a renamed column and a bad value are different incidents.
    """

    async def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(target, boom)
    lake = SpyLake()

    with pytest.raises(type(exc)):
        await capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert len(lake.writes) == len(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= MIN_SCHEDULED_TEAMS * len(DAYS), (
            "a failure envelope must floor to the real universe — otherwise a "
            "total outage reports a ratio far better than it is"
        )
        assert envelope.coverage.ratio == 0.0
        assert envelope.errors, "a failure envelope with no errors explains nothing"
        assert envelope.errors[0]["reason"] == expected_reason


async def test_an_unreachable_lake_is_counted_but_does_not_cost_the_capture():
    """Found against a running container whose MinIO endpoint did not resolve:
    the pass raised, `/signals` kept serving the last good capture, and every
    metric looked healthy. Only half of that was right.

    The metric half is fixed in the library --
    `collector_core.publish.publish_capture` increments
    `collector_capture_failures_total` per failed write, so an object-store
    outage is no longer indistinguishable from a quiet cadence.

    The propagation half was the wrong fix. Every upstream fetch succeeded
    here; the envelopes were built and correct. Re-raising discarded them, so
    `CaptureState` was never updated and `/signals` fell back to the previous
    capture -- or to nothing at all on a first run. The contract says an
    outage costs freshness, not availability, so the capture is returned.
    """
    lake = SpyLake(fail_write=True)

    envelopes = await capture(lake)

    assert set(envelopes) == set(SIGNAL_TYPES)
    assert len(envelopes) == 2
    assert all(e.coverage.present > 0 for e in envelopes.values())
    # Nothing was durably stored -- which is exactly why the counter matters.
    assert lake.writes == []


async def test_a_capture_already_past_its_deadline_fails_rather_than_empties():
    """Out of budget before the injury feed is reached. Reported as a failure —
    which re-raises and leaves the last good capture on `/signals` — rather
    than returned as an empty capture that would install itself over it."""
    lake = SpyLake()
    # In the past on any wall clock, not `NOW`: the deadline is checked against
    # real elapsed time, while `NOW` is the frozen instant the pass describes.
    with pytest.raises(TimeoutError):
        await capture(lake, deadline=datetime(2000, 1, 1, tzinfo=UTC))
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        assert envelope.coverage.present == 0
        assert envelope.errors[0]["reason"] == "deadline_exceeded"


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
