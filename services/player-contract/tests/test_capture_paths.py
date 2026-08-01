"""The capture branches an end-to-end happy path never reaches.

Each of these is a place where the wrong answer is quiet: a 304 written as a
failure, a truncated pass reporting itself complete, or two upstream records for
one person publishing two contradictory contracts for the same `player_id`.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.conditional import ETAGS, UpstreamUnchanged

from player_contract.adapters import upstream as upstream_mod
from player_contract.capture import CONTRACT_STATUS, capture_player_contract

from .conftest import (
    CANONICAL_IDS,
    NOW,
    SEASON,
    WEEK,
    contracts_parquet,
    mock_identity,
    mock_upstream,
    row,
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_contract(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def _writes(lake):
    return [e for e in lake.writes if e.collector == "player-contract"]


@respx.mock
async def test_a_304_is_a_SUCCESSFUL_pass_and_writes_no_failure_envelope(lake):
    """A 304 must escape `capture` untouched.

    `raise_for_status()` gates on `is_success`, which is 2xx only, so a 304 DOES
    raise `HTTPStatusError` — which means a generic handler placed above the
    `except UpstreamUnchanged: raise` would route an unchanged upstream into
    `fail_capture` and write `present: 0` over a healthy capture. Here the ETag
    is pre-seeded so a single pass reproduces the condition.
    """
    ETAGS.set(upstream_mod.UPSTREAM_URL, '"fixture-etag"')
    respx.mock.get(upstream_mod.UPSTREAM_URL).respond(304)
    mock_identity(respx.mock)

    with pytest.raises(UpstreamUnchanged) as raised:
        await capture(lake)

    assert raised.value.url == upstream_mod.UPSTREAM_URL
    assert _writes(lake) == [], (
        "a 304 wrote an envelope — it was routed into fail_capture"
    )


@respx.mock
async def test_a_deadline_records_the_rest_as_missing_rather_than_publishing_them(
    lake,
):
    """A truncated pass that reports itself truncated is useful; one that
    reports itself complete is not. The rows already resolved are kept, and the
    rest land in `coverage.missing` under their own reason.

    The deadline is taken against the **wall clock**, not against the capture's
    frozen `now` — `capture` compares `datetime.now(tz=UTC)`, so a deadline
    derived from `NOW` would sit in the future and silently exercise the happy
    path instead.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    past = datetime.now(tz=UTC) - timedelta(hours=1)
    envelope = (await capture(lake, deadline=past))[CONTRACT_STATUS]

    assert envelope.signals == []
    assert envelope.coverage.present == 0
    reasons = {error["reason"] for error in envelope.errors}
    assert "deadline_exceeded" in reasons, reasons


@respx.mock
async def test_two_upstream_records_for_one_person_publish_ONE_row(lake):
    """`_dedupe` collapses two rows sharing an `otc_id`. This is the case it
    cannot see: two DIFFERENT OverTheCap records that `player-identity` resolves
    to the same person.

    Publishing both would put two contradictory contracts under one `player_id`
    — a shape the schema permits and a generator would silently average.
    """
    rows = [
        row("Alpha Passer", otc_id=11, team="Packers", year_signed=2025,
            years=5, value=150.0, gsis_id="00-0000001"),
        row("Alpha Passer", otc_id=12, team="Packers", year_signed=2019,
            years=3, value=30.0, gsis_id="00-0000001"),
    ]  # fmt: skip
    mock_upstream(respx.mock, body=contracts_parquet(rows))
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    published = [
        row
        for row in envelope.signals
        if row["player_id"] == CANONICAL_IDS["Alpha Passer"]
    ]
    assert len(published) == 1, published
    assert envelope.coverage.present == 1


@respx.mock
async def test_a_discarded_duplicate_active_row_is_reported(lake):
    """A row this collector dropped shows up otherwise only as a scope slot with
    no contract, which is the same symptom as a player who genuinely has none.
    Counting it is what makes an upstream that started emitting duplicates
    visible before it becomes a mystery."""
    rows = [
        row("Alpha Passer", otc_id=11, position="QB", team="Packers",
            year_signed=2025, years=5, value=150.0, gsis_id="00-0000001"),
        row("Alpha Passer", otc_id=11, position="LT", team="Packers",
            year_signed=2025, years=5, value=150.0, gsis_id="00-0000001"),
    ]  # fmt: skip
    mock_upstream(respx.mock, body=contracts_parquet(rows))
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    reasons = {error["reason"] for error in envelope.errors}
    assert "duplicate_active_contract" in reasons, reasons


@respx.mock
async def test_the_envelope_records_the_capture_scope_and_the_source(lake):
    """`scope` is what a `/signals?season=&week=` query filters on, and
    `upstream.source_ref` is how a lake object read a season later says which
    feed it came from — which matters especially here, because this feed is
    going to be replaced."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.scope == {"season": SEASON, "week": WEEK}
    assert envelope.upstream.source_ref == upstream_mod.UPSTREAM_URL
    assert envelope.upstream.adapter == upstream_mod.UPSTREAM_ADAPTER
    assert envelope.collector == "player-contract"
