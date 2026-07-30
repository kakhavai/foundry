"""Capture: coverage accounting, the two validation levels, the failure
envelope, and the invariants.

The coverage tests are the sharpest ones here. `coverage.expected` must
never be derived from what the fetch happened to return — that is precisely
the bug where a truncated document reports a ratio of 1.0.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from conftest import SpyLake, player, sleeper_document

from player_identity import capture as capture_module
from player_identity.adapters.sleeper import PLAYERS_URL, UpstreamSchemaError
from player_identity.capture import (
    EXPECTED_ROSTER_FLOOR,
    MAX_ERRORS,
    capture_identities,
)
from player_identity.resolution import MissQueue, ResolutionIndex

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def mock_upstream(document, *, status: int = 200, etag: str | None = 'W/"abc"'):
    headers = {"ETag": etag} if etag else {}
    respx.get(PLAYERS_URL).mock(
        return_value=httpx.Response(status, json=document, headers=headers)
    )


async def run_capture(*, lake=None, floor=0, deadline=None, misses=None):
    lake = lake if lake is not None else SpyLake()
    misses = misses if misses is not None else MissQueue()
    index = ResolutionIndex()
    async with httpx.AsyncClient() as client:
        envelopes = await capture_identities(
            2026,
            1,
            client=client,
            lake=lake,
            now=NOW,
            deadline=deadline,
            misses=misses,
            index=index,
            roster_floor=floor,
        )
    return envelopes, lake, index


@respx.mock
async def test_a_healthy_capture_emits_both_signal_types():
    mock_upstream(sleeper_document(player("1"), player("2", full_name="Puka Nacua")))

    envelopes, lake, index = await run_capture(floor=0)

    assert set(envelopes) == {"player_identity_crosswalk", "name_resolution_miss"}
    crosswalk = envelopes["player_identity_crosswalk"]
    assert crosswalk.coverage.present == 2
    assert crosswalk.coverage.expected == 2
    assert crosswalk.coverage.ratio == 1.0
    assert crosswalk.upstream.source_ref == 'W/"abc"'
    assert len(index) == 2
    assert [e.signal_type for e in lake.written] == list(envelopes)


def _document():
    return sleeper_document(
        player("1"),
        player("2", full_name="Puka Nacua", first_name="Puka", last_name="Nacua"),
    )


@respx.mock
async def test_expected_never_derives_from_what_the_fetch_returned():
    """THE coverage test. A truncated document — 2 records where the season
    floor says ~2,900 — must not report itself complete.

    Derive `expected` from `len(document)` instead and this goes green while
    96% of the league has silently vanished. That is the 8A bug, one level
    up.
    """
    mock_upstream(_document())

    envelopes, _, _ = await run_capture(floor=EXPECTED_ROSTER_FLOOR)

    coverage = envelopes["player_identity_crosswalk"].coverage
    assert coverage.expected == EXPECTED_ROSTER_FLOOR
    assert coverage.present == 2
    assert coverage.ratio < 0.01
    reasons = {e["reason"] for e in envelopes["player_identity_crosswalk"].errors}
    assert "below_expected_roster_floor" in reasons


@respx.mock
async def test_the_floor_never_lowers_a_genuinely_larger_roster():
    mock_upstream(_document())
    envelopes, _, _ = await run_capture(floor=1)

    coverage = envelopes["player_identity_crosswalk"].coverage
    assert coverage.expected == 2
    reasons = {e["reason"] for e in envelopes["player_identity_crosswalk"].errors}
    assert "below_expected_roster_floor" not in reasons


@respx.mock
async def test_a_structurally_invalid_record_stays_inside_expected():
    """`number` -> `jersey` upstream. Without the per-record key check the
    row is built with `jersey_number: null` and counted present, which looks
    exactly like a player whose number is genuinely unknown.

    It stays counted in `expected` because we cannot know whether it
    qualified, and assuming it did not is derivation-from-success again.
    """
    broken = player("2", full_name="Puka Nacua")
    broken["jersey"] = broken.pop("number")
    mock_upstream(sleeper_document(player("1"), broken))

    envelopes, _, _ = await run_capture(floor=0)

    coverage = envelopes["player_identity_crosswalk"].coverage
    assert coverage.present == 1
    assert coverage.expected == 2, "an invalid record must not shrink `expected`"
    assert coverage.missing == ["2"]
    assert {"reason": "schema", "detail": "2"} in envelopes[
        "player_identity_crosswalk"
    ].errors


@respx.mock
async def test_a_document_level_schema_break_fails_the_capture():
    document = _document()
    for record in document.values():
        record["nfl_id"] = record.pop("gsis_id")
    mock_upstream(document)

    lake = SpyLake()
    with pytest.raises(UpstreamSchemaError):
        await run_capture(lake=lake, floor=EXPECTED_ROSTER_FLOOR)

    assert [e.signal_type for e in lake.written] == [
        "player_identity_crosswalk",
        "name_resolution_miss",
    ]
    assert {e["reason"] for e in lake.written[0].errors} == {"schema"}


@respx.mock
async def test_a_failed_capture_still_writes_an_envelope_and_then_re_raises():
    """Both halves matter.

    Writing, because a failed capture that leaves no object is
    indistinguishable from one that never ran. Re-raising, because
    `run_capture_loop` catches and leaves the cache alone — returning the
    failure envelopes normally would let `apply_capture` install them over
    the last good capture, turning an upstream outage into a loss of
    *availability* rather than only freshness.
    """
    respx.get(PLAYERS_URL).mock(return_value=httpx.Response(503))
    lake = SpyLake()

    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(lake=lake, floor=EXPECTED_ROSTER_FLOOR)

    assert len(lake.written) == 2
    crosswalk = lake.written[0]
    assert crosswalk.coverage.present == 0
    assert crosswalk.coverage.expected == EXPECTED_ROSTER_FLOOR
    assert crosswalk.coverage.ratio == 0.0
    assert crosswalk.errors and crosswalk.errors[0]["reason"] == "http_status"
    assert crosswalk.signals == []


@respx.mock
async def test_a_failure_survives_an_unwritable_lake_and_still_re_raises():
    """The lake being down must not mask the upstream failure underneath."""
    respx.get(PLAYERS_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(lake=SpyLake(fail=True), floor=0)


@respx.mock
async def test_a_lake_failure_on_a_successful_capture_raises():
    mock_upstream(_document())
    with pytest.raises(OSError):
        await run_capture(lake=SpyLake(fail=True), floor=0)


@respx.mock
async def test_a_free_agent_the_upstream_marks_active_qualifies():
    """The documented 30-day-window proxy. Sleeper carries no transaction
    dates, so `team is null AND status is Active` stands in for a recently
    transacted free agent."""
    mock_upstream(
        sleeper_document(player("1", team=None, status="Active", number=None))
    )

    envelopes, _, _ = await run_capture(floor=0)

    assert envelopes["player_identity_crosswalk"].coverage.expected == 1
    assert envelopes["player_identity_crosswalk"].signals[0]["team"] is None
    assert (
        envelopes["player_identity_crosswalk"].signals[0]["roster_status"] == "active"
    )


@respx.mock
async def test_an_inactive_free_agent_is_outside_the_population_entirely():
    """Not a miss and not an error — simply not part of what `expected`
    means. It is the floor, not this exclusion, that keeps the ratio
    honest."""
    mock_upstream(
        sleeper_document(player("1"), player("2", team=None, status="Inactive"))
    )

    envelopes, _, _ = await run_capture(floor=0)

    coverage = envelopes["player_identity_crosswalk"].coverage
    assert coverage.expected == 1
    assert coverage.missing == []


@respx.mock
async def test_a_non_football_position_is_outside_the_population():
    mock_upstream(sleeper_document(player("1"), player("2", position="ZZ")))
    envelopes, _, _ = await run_capture(floor=0)
    assert envelopes["player_identity_crosswalk"].coverage.expected == 1


@respx.mock
async def test_a_record_with_no_full_name_is_recorded_as_malformed():
    mock_upstream(sleeper_document(player("1"), player("2", full_name="")))
    envelopes, _, _ = await run_capture(floor=0)

    coverage = envelopes["player_identity_crosswalk"].coverage
    assert coverage.present == 1
    assert coverage.expected == 2
    assert {"reason": "malformed", "detail": "2"} in envelopes[
        "player_identity_crosswalk"
    ].errors


@respx.mock
async def test_a_merge_conflict_is_reported_in_the_envelope():
    """The injectivity invariant: each external_ids.<source>.id maps to
    exactly one player_id."""
    duplicate = player("2", full_name="Puka Nacua")
    duplicate["gsis_id"] = player("1")["gsis_id"]
    mock_upstream(sleeper_document(player("1"), duplicate))

    envelopes, _, _ = await run_capture(floor=0)

    reasons = [e["reason"] for e in envelopes["player_identity_crosswalk"].errors]
    assert "merge_conflict" in reasons


@respx.mock
async def test_the_errors_array_is_capped_with_a_truncation_marker():
    """At 2,900 records a total schema break produces 2,900 near-identical
    entries in every envelope and in every lake object."""
    document = sleeper_document(*(player(str(i)) for i in range(MAX_ERRORS + 20)))
    for record in document.values():
        record.pop("status")
    mock_upstream(document)

    envelopes, _, _ = await run_capture(floor=0)

    errors = envelopes["player_identity_crosswalk"].errors
    assert len(errors) == MAX_ERRORS + 1
    assert errors[-1]["reason"] == "errors_truncated"
    assert "20 further error(s) omitted" in errors[-1]["detail"]


@respx.mock
async def test_the_deadline_truncates_rather_than_discarding_the_pass(monkeypatch):
    """Checked between records, never by wrapping the coroutine in a timeout:
    a wrapper that cancels on expiry discards everything already captured."""
    mock_upstream(sleeper_document(player("1"), player("2"), player("3")))
    ticks = iter([NOW, NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=1)])
    monkeypatch.setattr(capture_module, "_wall_clock", lambda: next(ticks))

    envelopes, _, _ = await run_capture(floor=0, deadline=NOW + timedelta(minutes=5))

    coverage = envelopes["player_identity_crosswalk"].coverage
    assert coverage.present == 2
    assert coverage.expected == 3
    assert {"reason": "deadline_exceeded", "detail": "3"} in envelopes[
        "player_identity_crosswalk"
    ].errors


@respx.mock
async def test_the_miss_queue_is_emitted_as_its_own_signal_type():
    from player_identity.resolution import ResolveQuery

    mock_upstream(_document())
    misses = MissQueue()
    index = ResolutionIndex()
    query = ResolveQuery(raw_name="Nobody At All", source="book")
    misses.record(query, index.resolve(query, now=NOW), now=NOW)

    envelopes, _, _ = await run_capture(floor=0, misses=misses)

    miss_envelope = envelopes["name_resolution_miss"]
    assert miss_envelope.coverage.present == 1
    assert miss_envelope.signals[0]["raw_name"] == "Nobody At All"
    # The miss queue has no roster floor: its population *is* the queue.
    assert miss_envelope.coverage.expected == 1


@respx.mock
async def test_an_empty_miss_queue_is_complete_not_broken():
    mock_upstream(_document())
    envelopes, _, _ = await run_capture(floor=0)
    assert envelopes["name_resolution_miss"].coverage.ratio == 1.0


@respx.mock
async def test_capture_populates_the_shared_resolution_index():
    """`/resolve` reads what capture built — through the same object, bound
    by `functools.partial`, not a module-level global."""
    mock_upstream(_document())
    _, _, index = await run_capture(floor=0)

    from player_identity.identity import normalized_key
    from player_identity.resolution import ResolveQuery

    resolution = index.resolve(
        ResolveQuery(
            raw_name="Davante Adams",
            normalized_key=normalized_key("Davante Adams"),
            team="LV",
            position_group="offense_skill",
            jersey_number=17,
        ),
        now=NOW,
    )
    assert resolution.resolved
