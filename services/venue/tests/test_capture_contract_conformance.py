"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.

`venue_static` needs no HTTP at all: it comes from a committed table, which is
exactly why this collector was built on one. So these are assertions about real
output rather than about a mock's.
"""

import json
from pathlib import Path
from unittest import mock

import jsonschema
import pytest
from collector_core.conditional import UpstreamUnchanged
from collector_core.publish import PublishResult
from jsonschema import Draft202012Validator, FormatChecker

import venue.capture
from venue import reference
from venue.capture import (
    ASSIGNMENT,
    EXPECTED_FLOOR,
    REASON_SCHEDULE_UNAVAILABLE,
    SIGNAL_TYPES,
    STATIC,
)

from .conftest import (
    SEASON_GAMES,
    SpyLake,
    run_capture,
    season_rows,
    sunday_of,
    to_csv,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "venue.json").read_text(encoding="utf-8"),
)["signal_types"]


def validate(envelopes: dict) -> int:
    """Validate every row of every envelope, and report how many it saw.

    The count is returned rather than discarded so a caller can assert it is
    non-zero. Validating an empty list passes trivially, and a conformance
    test that cannot tell "every row conforms" from "there were no rows" is
    the same shape of vacuous pass as `all([])`.
    """
    seen = 0
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)
            seen += 1
    return seen


async def test_a_complete_capture_conforms():
    envelopes = await run_capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, f"nothing captured for {envelope.signal_type}"
    assert validate(envelopes) == 30 + SEASON_GAMES


async def test_a_complete_capture_reports_full_coverage():
    """Not decoration: `expected` is floored independently of the fetch, so a
    healthy pass reaching the floor is what proves the floor is right."""
    envelopes = await run_capture(SpyLake())
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected >= EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.ratio == 1.0, (signal_type, envelope.errors)
        assert envelope.errors == []


async def test_every_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await run_capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_lake_outage_costs_durability_not_availability():
    """`publish_capture` returns the envelopes anyway and counts the failure.

    The opposite answer would mean an object-store outage cost `/signals` a
    capture that is already built and correct in memory — the exact contract
    inversion the shared tail exists to prevent.
    """
    envelopes = await run_capture(SpyLake(fail_write=True))
    assert set(envelopes) == set(SIGNAL_TYPES)
    assert envelopes[STATIC].signals


async def test_a_failed_lake_write_does_not_suppress_the_next_pass():
    """The bug review found, and it needs TWO passes to see.

    The digest gate used to record before `publish_capture`, which by design
    swallows a failed write. So one object-store blip meant the digest said "the
    lake has this" when it did not — and because this collector polls once a day
    and only publishes on change, the snapshot would never be written again
    until the venue table itself changed. Months, for a `static reference`.

    Reproduced before the fix:

        pass1 (lake write fails) -> objects in lake: 0
        pass2 (healthy lake)     -> UpstreamUnchanged, objects in lake: 0

    The single-pass test above cannot see any of this: it asserts availability,
    which was never broken.
    """
    lake = SpyLake(fail_write=True)
    await run_capture(lake)
    assert lake.objects == {}, "the fixture is not actually failing writes"

    lake.fail_write = False
    envelopes = await run_capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES), (
        "the retry was suppressed — a digest was recorded for content the lake "
        "never received"
    )
    assert len(lake.objects) == len(SIGNAL_TYPES)
    assert set(envelopes) == set(SIGNAL_TYPES)


async def test_a_partial_lake_failure_only_retries_the_type_that_failed():
    """One signal type's write failing must not re-append the other's.

    The digest is recorded per signal type and gated on that type's own write
    landing, so a `venue_static` failure retries `venue_static` and leaves the
    healthy `venue_game_assignment` object alone.
    """

    class FailStaticOnce(SpyLake):
        def __init__(self):
            super().__init__()
            self.fail_static = True

        def write(self, envelope):
            if self.fail_static and envelope.signal_type == STATIC:
                raise RuntimeError("lake unreachable")
            return super().write(envelope)

    lake = FailStaticOnce()
    await run_capture(lake)
    assert [e.signal_type for e in lake.writes] == [ASSIGNMENT]

    lake.fail_static = False
    await run_capture(lake)
    # Exactly one more write, and it is the one that failed. An unconditional
    # retry of the whole pass would append a duplicate assignment object.
    assert [e.signal_type for e in lake.writes] == [ASSIGNMENT, STATIC]


async def test_the_digest_gate_asks_the_library_which_writes_landed():
    """The mechanism, pinned at the seam rather than only through two passes.

    The two-pass tests above prove the *behaviour* and are the ones that
    matter. This one pins that the answer comes from
    `collector_core.publish.PublishResult` — the fleet-wide hook — and not from
    a fourth private copy of the wrapper this collector used to carry. Deleting
    the library call and reinstating a local observer would keep every
    behavioural test green, which is exactly why the seam is asserted.
    """
    seen: list[PublishResult] = []
    real_publish = venue.capture.publish_capture

    async def spy(envelopes, *, lake, metrics):
        result = await real_publish(envelopes, lake=lake, metrics=metrics)
        seen.append(result)
        return result

    lake = SpyLake(fail_write=True)
    with mock.patch.object(venue.capture, "publish_capture", spy):
        await run_capture(lake)

    assert len(seen) == 1
    assert isinstance(seen[0], PublishResult)
    assert seen[0].failed == frozenset(SIGNAL_TYPES)
    assert not any(seen[0].landed(signal_type) for signal_type in SIGNAL_TYPES)
    # ...and the pass that failed to write recorded no digest, so the next one
    # is not suppressed — the property the two-pass tests then exercise.
    assert lake.objects == {}


async def test_an_assignment_change_alone_does_not_re_append_venue_static():
    """The digest is per signal type, as the spec's wording requires.

    "A snapshot appended only when the content hash of a VENUE RECORD changes."
    A flex reschedule moves one game; `venue_static` is byte-identical and must
    not be written again. One digest spanning both types would append it.
    """
    lake = SpyLake()
    await run_capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)

    # Same venues, one game moved to a different (still in-window) week.
    rows = season_rows()
    rows[0] = {**rows[0], "week": "5", "gameday": sunday_of(5)}
    envelopes = await run_capture(lake, csv=to_csv(rows))

    appended = [e.signal_type for e in lake.writes[len(SIGNAL_TYPES) :]]
    assert appended == [ASSIGNMENT], (
        f"venue_static was re-appended for an assignment-only change: {appended}"
    )
    # Both envelopes are still SERVED — only the write was skipped.
    assert set(envelopes) == set(SIGNAL_TYPES)
    assert envelopes[STATIC].signals


async def test_a_schedule_outage_still_publishes_venue_static():
    """The degraded path. `venue_static` reads a committed table, so a dead
    game feed must not take it down with it — and the assignment envelope must
    still be written as an explicit `present: 0` gap rather than be absent."""
    lake = SpyLake()
    envelopes = await run_capture(lake, status=503)

    # EVERY venue the table carries, not just the season's 30 buildings: the
    # thing that would have narrowed the set is the thing that failed, so the
    # superset is the honest expectation rather than a guess at which venues
    # this season uses.
    assert validate(envelopes) == len(reference.REVISIONS)
    assert len(reference.REVISIONS) > 30, "neutral-site venues went missing"
    assert envelopes[STATIC].coverage.ratio == 1.0
    assert envelopes[ASSIGNMENT].signals == []
    assert envelopes[ASSIGNMENT].coverage.present == 0
    assert envelopes[ASSIGNMENT].coverage.expected >= EXPECTED_FLOOR[ASSIGNMENT], (
        "expected: 0 makes Coverage.ratio read 1.0 — a total outage would "
        "report perfect coverage"
    )

    reasons = {error["reason"] for error in envelopes[ASSIGNMENT].errors}
    assert reasons == {REASON_SCHEDULE_UNAVAILABLE}
    static_reasons = {error["reason"] for error in envelopes[STATIC].errors}
    assert REASON_SCHEDULE_UNAVAILABLE in static_reasons, envelopes[STATIC].errors

    # Both envelopes reach the lake: "we failed" and "we never tried" are
    # different facts, and a gap must be explicit rather than inferred.
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)


async def test_a_reference_table_failure_ends_the_pass_and_re_raises(monkeypatch):
    """The contract for a real failure: a `present: 0` envelope per signal type
    with a populated `errors` array, written to the lake, then re-raised so the
    last good capture in `CaptureState` is not overwritten by an empty one.

    The reference table is imported code, so a failure building from it is a
    bug rather than an outage — which is why it takes `fail_capture` and the
    schedule outage above does not.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("reference table is broken")

    monkeypatch.setattr("venue.capture.build_assignment_row", boom)
    lake = SpyLake()

    with pytest.raises(RuntimeError, match="reference table is broken"):
        await run_capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_an_unchanged_pass_raises_rather_than_appending_a_duplicate():
    """The `static reference` cadence rule: re-read daily, append on change.

    Two identical passes must produce ONE lake object, not two. The second
    raises `UpstreamUnchanged`, which `run_capture_loop` turns into
    `mark_unchanged` — `/catalog` reports a fresh pass while `/signals` keeps
    serving the same rows.
    """
    lake = SpyLake()
    await run_capture(lake)
    assert len(lake.writes) == len(SIGNAL_TYPES)

    with pytest.raises(UpstreamUnchanged):
        await run_capture(lake)
    assert len(lake.writes) == len(SIGNAL_TYPES), "an identical snapshot was appended"


async def test_an_unchanged_upstream_is_re_raised_not_routed_into_the_degraded_path(
    monkeypatch,
):
    """`UpstreamUnchanged` is a SUCCESSFUL capture, not a failure.

    Its `except` arm sits above the generic one on purpose. Drop it and a 304
    from a conditional GET would take the degraded branch: a `present: 0`
    assignment envelope written over healthy data, and a failure counted that
    did not happen. `stream_csv_dicts` only raises it once an `etag_key` is set,
    which this collector does not do today — so this is the only thing keeping
    the arm from being deleted as dead code by whoever adds one.
    """

    async def unchanged(*args, **kwargs):
        raise UpstreamUnchanged("https://example.invalid/games.csv", source_ref="etag")

    monkeypatch.setattr("venue.capture.fetch_season_games", unchanged)
    lake = SpyLake()

    with pytest.raises(UpstreamUnchanged):
        await run_capture(lake)
    assert lake.writes == [], "a 304 wrote an envelope over healthy data"


async def test_a_pass_over_its_deadline_truncates_and_says_so():
    """Record the rest as missing rather than throwing away what resolved: a
    truncated pass that reports itself truncated is useful; one that reports
    itself complete is not."""
    from datetime import UTC, datetime, timedelta

    envelopes = await run_capture(
        SpyLake(),
        # Already elapsed, so every game is over budget before it is examined.
        deadline=datetime.now(tz=UTC) - timedelta(seconds=1),
    )
    assignment = envelopes[ASSIGNMENT]
    assert assignment.signals == []
    assert len(assignment.coverage.missing) == SEASON_GAMES
    reasons = {e["reason"] for e in assignment.errors}
    assert "deadline_exceeded" in reasons
    # 272 near-identical entries are capped at 50 with an explicit marker,
    # because a silently truncated error list looks like a short list of
    # problems. Asserted rather than filtered out: it is the marker's presence
    # that proves the cap is applied through the accumulator and not by hand.
    assert "errors_truncated" in reasons, assignment.errors
    assert len(assignment.errors) == 51


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
