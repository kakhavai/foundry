"""The two series `collector_coverage_ratio` structurally cannot see.

`player_contract_expired_records` is the important one. An expired deal is a
*present* record with every field populated, so coverage reads it as a success —
which means the ratio is 1.0 on a pass where every single published row
describes a contract that ended two seasons ago. That is the exact shape of the
phase doc's "well-formed record describing a deal that expired in 2019", and it
is live today: the upstream `.csv.gz` has not been regenerated since 2022-05-29.

Both gauges are asserted through `/metrics` rather than through the wrapper, so
a value recorded into a `meter.create_gauge` — which OTel *consumes* on
collection, leaving the series absent from every scrape that does not
immediately follow a capture — fails here.
"""

import gzip

import httpx
import pytest
import respx

from player_contract.adapters import upstream as upstream_mod
from player_contract.capture import CONTRACT_STATUS, capture_player_contract

from .conftest import (
    CANONICAL_IDS,
    NOW,
    SEASON,
    WEEK,
    SpyLake,
    contracts_csv,
    mock_identity,
    mock_upstream,
    scope_envelope,
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_contract(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def _series(client, name: str) -> list[str]:
    body = client.get("/metrics").text
    return [line for line in body.splitlines() if line.startswith(name)]


@respx.mock
async def test_an_expired_contract_is_counted_while_coverage_reads_healthy(lake):
    """The whole reason this gauge exists, stated as one assertion.

    Charlie's deal ran 2021-2024 and the capture season is 2026. His record is
    complete: a non-null `contract_end_season`, so `acc.record` fires and
    coverage counts him present. Only the gauge and the priority error say
    anything is wrong.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    charlie = next(
        row
        for row in envelope.signals
        if row["player_id"] == CANONICAL_IDS["Charlie Catcher"]
    )
    assert charlie["seasons_remaining"] == -2
    assert charlie["contract_end_season"] == 2024
    assert f"player:{CANONICAL_IDS['Charlie Catcher']}" not in envelope.coverage.missing

    reasons = {error["reason"] for error in envelope.errors}
    assert "contract_end_season_precedes_capture_season" in reasons, reasons


@respx.mock
async def test_the_expired_error_survives_the_fifty_entry_cap():
    """`add_priority_error`, not `add_error`. A scope of 400 players against a
    one-row feed files 399 routine `no_active_contract` entries, and an appended
    explanation would be the one the cap deletes."""
    lake = SpyLake()
    lake.write(
        scope_envelope(
            player_ids=[f"fdy-pad{i:09d}" for i in range(400)]
            + [CANONICAL_IDS["Charlie Catcher"]],
            include_team_defenses=False,
        )
    )
    rows = [("3", "Charlie Catcher", "WR", "49ers", "TRUE", 2021, 4, 1, 1)]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    reasons = [error["reason"] for error in envelope.errors]
    assert len(reasons) > 1, reasons
    assert "contract_end_season_precedes_capture_season" in reasons, reasons
    assert reasons[-1] == "errors_truncated", reasons[-1]


@respx.mock
async def test_a_pass_with_no_expired_contracts_records_a_ZERO_not_nothing(
    lake, client
):
    """An absent Prometheus series and a healthy one are indistinguishable, so a
    gauge only written when it is interesting cannot be alerted on."""
    rows = [("1", "Alpha Passer", "QB", "Packers", "TRUE", 2025, 5, 1, 1)]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))
    mock_identity(respx.mock)

    await capture(lake)

    lines = _series(client, "player_contract_expired_records")
    assert lines, "the series is absent from /metrics entirely"
    assert any(line.rstrip().endswith(" 0.0") for line in lines), lines


@respx.mock
async def test_the_expired_gauge_reaches_prometheus_with_the_real_count(lake, client):
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    await capture(lake)

    lines = _series(client, "player_contract_expired_records")
    assert any(line.rstrip().endswith(" 1.0") for line in lines), lines


@respx.mock
async def test_unresolved_scope_slots_reaches_prometheus(lake, client):
    """Coverage names the missing slots individually, which is right for an
    operator reading one envelope and useless for an alert: `missing` is capped
    at 50 entries and is not a number PromQL can threshold."""
    rows = [("1", "Alpha Passer", "QB", "Packers", "TRUE", 2025, 5, 1, 1)]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))
    mock_identity(respx.mock)

    await capture(lake)

    lines = _series(client, "player_contract_unresolved_scope_slots")
    # Six scoped players, one covered.
    assert any(line.rstrip().endswith(" 5.0") for line in lines), lines


@respx.mock
async def test_a_failed_pass_RESETS_the_gauges_rather_than_leaving_them_stale(
    lake, client
):
    """A gauge that simply stops on a failure path leaves PromQL reading the
    LAST GOOD pass's number forever, which is worse than an absent series: the
    dashboard says one expired contract while the collector has published
    nothing for a week.

    Asserting only that the series *exists* after a failure proves nothing —
    `LastValueGauge` is process-global, so a value set by any earlier pass keeps
    it present. The assertion has to be on the value, which is why this test
    deliberately sets a non-zero one first.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)
    await capture(lake)
    assert any(
        line.rstrip().endswith(" 1.0")
        for line in _series(client, "player_contract_expired_records")
    ), "the precondition did not hold; this test would pass vacuously"

    respx.mock.get(upstream_mod.UPSTREAM_URL).respond(503)
    with pytest.raises(httpx.HTTPStatusError):
        await capture(lake)

    for name in (
        "player_contract_expired_records",
        "player_contract_unresolved_scope_slots",
    ):
        lines = _series(client, name)
        assert lines, name
        assert any(line.rstrip().endswith(" 0.0") for line in lines), (name, lines)
