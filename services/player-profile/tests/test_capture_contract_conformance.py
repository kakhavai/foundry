"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.
"""

import json
from datetime import timedelta
from pathlib import Path

import httpx
import jsonschema
import pytest
import respx
from collector_core.conditional import UpstreamUnchanged
from jsonschema import Draft202012Validator, FormatChecker

from player_profile.adapters import upstream as upstream_mod
from player_profile.capture import (
    BIOGRAPHICAL,
    CAREER_LOAD,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    capture_player_profile,
)

from .conftest import (
    FIXTURE_PLAYERS,
    NOW,
    SCOPED_IDS,
    SEASON,
    WEEK,
    SpyLake,
    mock_identity,
    mock_upstreams,
    players_csv,
    scope_envelope,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "player-profile.json").read_text(),
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


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_profile(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


@respx.mock
async def test_a_complete_capture_conforms(lake):
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert set(envelopes) == set(SIGNAL_TYPES)
    for signal_type, envelope in envelopes.items():
        assert envelope.signals, f"{signal_type} captured nothing to validate"
    validate(envelopes)


@respx.mock
async def test_every_scoped_player_gets_exactly_one_row_per_signal_type(lake):
    """One row per scope slot, and the count is asserted alongside the `all(...)`.

    `all([])` is `True`, so the prefix assertion below is vacuous on an empty
    list. The length assertion is what makes it load-bearing.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for signal_type, envelope in envelopes.items():
        ids = [row["player_id"] for row in envelope.signals]
        assert len(ids) == len(SCOPED_IDS), signal_type
        assert set(ids) == set(SCOPED_IDS), signal_type
        assert all(pid.startswith("fdy-") for pid in ids), signal_type


@respx.mock
async def test_every_envelope_is_written_to_the_lake(lake):
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    await capture(lake)

    written = {e.signal_type for e in lake.writes if e.collector == "player-profile"}
    assert written == set(SIGNAL_TYPES)


@respx.mock
async def test_the_scope_carries_the_as_of_date_age_was_taken_against(lake):
    """`age_years` is defined as "as of `scope.as_of_date`". A row published
    without the date it was computed against is not reproducible."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert envelopes[BIOGRAPHICAL].scope["as_of_date"] == NOW.date().isoformat()


@respx.mock
async def test_the_source_ref_names_every_upstream_the_join_read(lake):
    """The spec asks for it by name: "the adapter must record which upstream
    supplied the date in `upstream.source_ref` so a correction can be traced"."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    source_ref = envelopes[BIOGRAPHICAL].upstream.source_ref
    assert "players.csv" in source_ref
    assert "combine.csv" in source_ref
    assert f"snap_counts_{SEASON}.csv" in source_ref


@respx.mock
async def test_a_failed_upstream_writes_a_present_zero_envelope(lake):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""
    mock_identity(respx.mock)
    respx.mock.get(upstream_mod.PLAYERS_URL).mock(
        side_effect=httpx.ConnectError("upstream down")
    )

    with pytest.raises(httpx.ConnectError):
        await capture(lake)

    written = [e for e in lake.writes if e.collector == "player-profile"]
    assert {e.signal_type for e in written} == set(SIGNAL_TYPES)
    for envelope in written:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type], (
            "expected must floor to the real universe — without it a total "
            "outage floors to 1 and the ratio reads better than it is"
        )
        assert envelope.errors, "a failure envelope with no errors explains nothing"


@respx.mock
async def test_a_degraded_snap_sweep_still_publishes_the_other_signal_types(lake):
    """Three of the four signal types do not read the snap feed at all.

    Routing a snap-counts outage through `fail_capture` would re-raise and
    discard a perfectly good biographical capture over an upstream that signal
    type never touches.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    for season in range(upstream_mod.CAREER_SNAP_FIRST_SEASON, SEASON + 1):
        respx.mock.get(upstream_mod.SNAP_COUNTS_URL.format(season=season)).mock(
            side_effect=httpx.ConnectError("snap feed down")
        )

    envelopes = await capture(lake)

    assert envelopes[BIOGRAPHICAL].signals, "biographical was discarded"
    validate(envelopes)
    reasons = {error["reason"] for error in envelopes[CAREER_LOAD].errors}
    assert "career_snap_seasons_missing" in reasons, reasons
    career_rows = envelopes[CAREER_LOAD].signals
    assert len(career_rows) == len(SCOPED_IDS)
    assert all(row["career_snaps_complete"] is False for row in career_rows)


@respx.mock
async def test_an_unknown_rookie_season_is_not_a_complete_career(lake):
    """`career_snaps_complete` must only ever be `True` on evidence.

    nflverse publishes snap counts from 2012, so the flag is a claim that the
    player's whole career falls inside the window. A null `rookie_season` is not
    evidence for that claim — it used to skip the comparison and leave the flag
    `True`, so a twelve-year veteran whose rookie season the feed omitted
    published a 2012-onward floor labelled complete, and a consumer weighting
    `career_snap_load_percentile` would treat it as a full total.

    All three states in one pass, because a flag that is simply always `False`
    satisfies the unknown case on its own and would look like a fix.
    """
    variants = [list(record) for record in FIXTURE_PLAYERS]
    variants[1][8] = ""  # Bravo: rookie_season unknown
    variants[2][8] = "2005"  # Charlie: rookie_season before the 2012 window
    # Alpha keeps 2018 — known, and inside the window.
    mock_upstreams(respx.mock, players=players_csv(variants))
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    rows = {row["player_id"]: row for row in envelopes[CAREER_LOAD].signals}
    assert len(rows) == len(SCOPED_IDS)
    assert rows["fdy-bbbb11112222"]["career_snaps_complete"] is False, "unknown"
    assert rows["fdy-cccc11112222"]["career_snaps_complete"] is False, "pre-window"
    assert rows["fdy-aaaa11112222"]["career_snaps_complete"] is True, "in-window"


@respx.mock
async def test_a_birth_date_after_the_as_of_date_is_refused_and_named(lake):
    """A future birth date is a genuine upstream error, and it must not become
    a number.

    Left as a negative age it flowed into `position_age_curve_stage` — where
    every boundary comparison puts it in `pre_peak` — and into the percentile
    population, dragging the whole position's distribution down. Both are
    silent. `age_years` refuses it, and the `errors` entry is what stops the
    resulting null being read as the spec's permitted "no adapter could supply
    a birth date".
    """
    future = [list(record) for record in FIXTURE_PLAYERS]
    future[1][4] = "2030-01-01"  # Bravo, born four years after the capture
    future[2][4] = ""  # Charlie: the spec's PERMITTED missing birth date
    mock_upstreams(respx.mock, players=players_csv(future))
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    biographical = envelopes[BIOGRAPHICAL]
    bravo = next(
        row for row in biographical.signals if row["player_id"] == "fdy-bbbb11112222"
    )
    assert bravo["age_years"] is None
    assert bravo["position_age_curve_stage"] is None, (
        "a refused age must not still be bucketed onto a curve"
    )
    reasons = {error["reason"] for error in biographical.errors}
    assert "birth_date_in_future" in reasons, reasons

    # The clause that separates the two nulls, pinned. Charlie's age is refused
    # for the reason the spec ALLOWS, so naming him here would make the reason
    # code mean the opposite of what it says and send an operator chasing a date
    # column that is working fine. Without a fixture player who has no birth
    # date at all, dropping `row.birth_date is not None` from the guard survives
    # the whole suite.
    charlie = next(
        row for row in biographical.signals if row["player_id"] == "fdy-cccc11112222"
    )
    assert charlie["age_years"] is None, "the fixture no longer omits a birth date"
    detail = next(
        error["detail"]
        for error in biographical.errors
        if error["reason"] == "birth_date_in_future"
    )
    assert "fdy-bbbb11112222" in detail, detail
    assert "fdy-cccc11112222" not in detail, (
        "a permitted missing birth date was filed as an upstream error"
    )
    assert detail.startswith("1 player(s) born after"), detail

    # And it is out of the reference population the other players rank against.
    ages = [
        row["age_years"] for row in biographical.signals if row["age_years"] is not None
    ]
    assert ages, "every age was refused — the fixture is wrong"
    assert all(age > 0 for age in ages)
    validate(envelopes)


@respx.mock
async def test_a_sweep_that_fails_outright_is_degraded_not_fatal(lake, monkeypatch):
    """The other degraded arm, and it needed its own test.

    `fetch_career_snaps` absorbs a per-SEASON failure into `seasons_missing`, so
    the test above never reaches `capture.py`'s own `except` — mutation testing
    showed that arm was entirely unexercised. This makes the sweep itself raise.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    async def boom(*args, **kwargs):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr("player_profile.capture.fetch_career_snaps", boom)

    envelopes = await capture(lake)

    assert envelopes[BIOGRAPHICAL].signals, "a snap failure discarded biographical"
    validate(envelopes)
    career_rows = envelopes[CAREER_LOAD].signals
    assert len(career_rows) == len(SCOPED_IDS)
    assert all(row["career_offensive_snaps"] is None for row in career_rows)
    reasons = {error["reason"] for error in envelopes[CAREER_LOAD].errors}
    assert "career_snap_seasons_missing" in reasons, reasons


@respx.mock
async def test_a_lake_write_failure_still_returns_the_capture(lake):
    """Availability beats durability: the capture succeeded and only its
    archival copy did not."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    lake.fail_write = True

    envelopes = await capture(lake)

    assert set(envelopes) == set(SIGNAL_TYPES)
    assert envelopes[BIOGRAPHICAL].signals


@respx.mock
async def test_a_lake_write_failure_does_not_suppress_the_next_pass(lake):
    """The digest gate must not record content the lake never received.

    For a `static reference` collector the next pass that would write it may be
    a season away, so one object-store blip would cost the whole snapshot.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    lake.fail_write = True
    await capture(lake)
    lake.fail_write = False
    second = await capture(lake)

    written = {e.signal_type for e in lake.writes if e.collector == "player-profile"}
    assert written == set(SIGNAL_TYPES)
    assert second[BIOGRAPHICAL].signals


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


@respx.mock
async def test_an_unchanged_pass_raises_rather_than_appending_a_duplicate(lake):
    """A `static reference` cadence re-reads daily; publishing a byte-identical
    envelope on each of those passes fills an append-only lake with 365 objects
    a year that carry no information.

    Asserted against a frozen clock, which is the only condition under which
    nothing at all changed — `age_years` genuinely moves with `as_of_date`.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    await capture(lake)
    before = len(lake.writes)

    with pytest.raises(UpstreamUnchanged):
        await capture(lake)

    assert len(lake.writes) == before


@respx.mock
async def test_a_pass_a_day_later_republishes_only_the_biographical_rows(lake):
    """The other side of the gate: `age_years` really does change daily, and
    suppressing it would serve a stale age forever — while the draft class,
    the combine results and the snap totals genuinely did not move."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    await capture(lake)
    later = NOW + timedelta(days=1)
    async with httpx.AsyncClient() as client:
        second = await capture_player_profile(
            SEASON, WEEK, client=client, lake=lake, now=later
        )

    republished = {
        e.signal_type
        for e in lake.writes
        if e.collector == "player-profile" and e.captured_at == later
    }
    assert republished == {BIOGRAPHICAL}, republished
    assert second[BIOGRAPHICAL].scope["as_of_date"] == later.date().isoformat()


@respx.mock
async def test_a_scope_naming_only_team_defenses_is_not_a_perfect_pass():
    """`Coverage.ratio` is 1.0 when `expected` is 0. A scope whose only members
    are team defenses owes zero profiles, and without the floor that would read
    as a flawless capture rather than as the anomaly it is."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    lake = SpyLake()
    lake.write(scope_envelope(player_ids=[]))

    envelopes = await capture(lake)

    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.present == 0
        assert envelope.coverage.ratio == 0.0
