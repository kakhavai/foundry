import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream

SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "signal-envelope"
    / "envelope.v1.schema.json"
)


def test_to_dict_serializes_timestamps_as_rfc3339_utc():
    body = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=datetime(2026, 9, 17, 14, 3, tzinfo=UTC),
        upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, 57, tzinfo=UTC)),
        scope={"season": 2026, "week": 3},
        coverage=Coverage(expected=1, present=1, missing=[]),
        errors=[],
        signals=[{"game_id": "2026_03_KC_BUF"}],
    ).to_dict()

    assert body["captured_at"] == "2026-09-17T14:03:00Z"
    assert body["upstream"]["fetched_at"] == "2026-09-17T14:02:57Z"


def test_to_dict_validates_against_the_committed_schema():
    body = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=datetime(2026, 9, 17, 14, 3, tzinfo=UTC),
        upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, 57, tzinfo=UTC)),
        scope={"season": 2026, "week": 3},
        coverage=Coverage(expected=16, present=15, missing=["2026_03_BAL_DAL"]),
        errors=[],
        signals=[{"game_id": "2026_03_KC_BUF"}],
    ).to_dict()

    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(body)


def test_naive_captured_at_is_rejected():
    """A naive datetime silently means 'some timezone' and lands wrong in the lake."""
    with pytest.raises(ValueError, match="timezone-aware"):
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector="weather",
            signal_type="venue_forecast_kickoff",
            captured_at=datetime(2026, 9, 17, 14, 3),
            upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, tzinfo=UTC)),
            scope={"season": 2026, "week": 3},
            coverage=Coverage(expected=1, present=1, missing=[]),
            errors=[],
            signals=[],
        )


def test_bad_captured_at_timestamp_is_rejected():
    """`format: date-time` is only enforced when a FormatChecker is attached."""
    body = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=datetime(2026, 9, 17, 14, 3, tzinfo=UTC),
        upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, tzinfo=UTC)),
        scope={"season": 2026, "week": 3},
        coverage=Coverage(expected=1, present=1, missing=[]),
        errors=[],
        signals=[],
    ).to_dict()
    # Corrupt the timestamp after serialization
    body["captured_at"] = "not-a-date-at-all"

    schema = json.loads(SCHEMA.read_text())
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(body)
    )

    assert errors, "a malformed captured_at must fail validation"


def test_failed_capture_envelope_is_valid():
    """A total failure still writes — that is how a gap becomes explicit."""
    body = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=datetime(2026, 9, 17, 14, 3, tzinfo=UTC),
        upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, tzinfo=UTC)),
        scope={"season": 2026, "week": 3},
        coverage=Coverage(expected=16, present=0, missing=[f"g{i}" for i in range(16)]),
        errors=[{"reason": "timeout", "detail": "upstream did not respond"}],
        signals=[],
    ).to_dict()

    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(body)
    assert body["coverage"]["present"] == 0
    assert body["errors"][0]["reason"] == "timeout"


def test_coverage_ratio_for_empty_scope():
    """An empty week is complete, not broken — 0/0 = 1.0, not undefined."""
    coverage = Coverage(expected=0, present=0, missing=[])

    assert coverage.ratio == 1.0


def test_coverage_ratio_for_partial_coverage():
    """A normal case: 3 of 4 records present."""
    coverage = Coverage(expected=4, present=3, missing=["x"])

    assert coverage.ratio == 0.75
