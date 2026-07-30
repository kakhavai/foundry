"""The three guards the Phase 8 spec names, read out of real /metrics output.

Asserting on `generate_latest` rather than the SDK's internal view means
these check exactly what Prometheus scrapes, including OTel's `_total`
mangling — a rename that would break an alert fails here.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import SpyLake, canonical_row, player, sleeper_document

from player_identity.adapters.sleeper import PLAYERS_URL
from player_identity.capture import capture_identities
from player_identity.metrics import metrics
from player_identity.resolution import MissQueue, ResolutionIndex

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def delta(metric_value, name, before, **labels) -> float:
    after = metric_value(name, **labels) or 0.0
    return after - (before or 0.0)


def test_resolution_failures_are_labelled_by_the_disagreeing_attribute(
    client, seeded_state, metric_value
):
    """A spike concentrated on `team` is a staleness problem — a trade a book
    has not caught up with — and reads completely differently from one spread
    across attributes. One unlabelled counter cannot tell them apart."""
    before = metric_value(
        "identity_resolution_failures_total",
        collector="player-identity",
        attribute="team",
        reason="below_threshold",
    )

    # Agrees on nothing but the name; team, number, and position all differ.
    client.get("/resolve?name=Davante%20Adams&team=NYJ&position=S&jersey_number=99")

    assert (
        delta(
            metric_value,
            "identity_resolution_failures_total",
            before,
            collector="player-identity",
            attribute="team",
            reason="below_threshold",
        )
        == 1.0
    )


def test_a_resolution_failure_with_no_disagreement_is_still_counted(
    client, seeded_state, metric_value
):
    """A name nobody has heard of disagrees with nothing. Skipping the metric
    in that case would make a total-unknown outage invisible."""
    before = metric_value(
        "identity_resolution_failures_total",
        collector="player-identity",
        attribute="none",
        reason="no_candidate",
    )

    client.get("/resolve?name=Nobody%20At%20All&team=LV")

    assert (
        delta(
            metric_value,
            "identity_resolution_failures_total",
            before,
            collector="player-identity",
            attribute="none",
            reason="no_candidate",
        )
        == 1.0
    )


def test_near_miss_density_is_recorded(client, metric_value, seeded_state):
    """Rows above the threshold but inside the margin are ties queued
    deliberately; a rising rate means the weights need tuning, not that the
    upstream got worse."""
    rows = [
        canonical_row("fdy-00000000000a", jersey_number=17),
        canonical_row("fdy-00000000000b", jersey_number=88),
    ]
    client.app.state.resolution_index.replace(rows)
    before = metric_value("identity_near_misses_total", collector="player-identity")

    client.get("/resolve?name=Davante%20Adams&team=LV&position=WR")

    assert (
        delta(
            metric_value,
            "identity_near_misses_total",
            before,
            collector="player-identity",
        )
        == 1.0
    )


@respx.mock
async def test_merge_conflicts_are_counted(metric_value):
    before = metric_value(
        "identity_merge_conflicts_total", collector="player-identity", source="gsis"
    )

    duplicate = player("2", full_name="Puka Nacua")
    duplicate["gsis_id"] = player("1")["gsis_id"]
    respx.get(PLAYERS_URL).mock(
        return_value=httpx.Response(200, json=sleeper_document(player("1"), duplicate))
    )
    async with httpx.AsyncClient() as client:
        await capture_identities(
            2026,
            1,
            client=client,
            lake=SpyLake(),
            now=NOW,
            misses=MissQueue(),
            index=ResolutionIndex(),
            roster_floor=0,
        )

    assert (
        delta(
            metric_value,
            "identity_merge_conflicts_total",
            before,
            collector="player-identity",
            source="gsis",
        )
        == 1.0
    )


@respx.mock
async def test_coverage_ratio_is_published_per_signal_type(metric_value):
    respx.get(PLAYERS_URL).mock(
        return_value=httpx.Response(200, json=sleeper_document(player("1")))
    )
    async with httpx.AsyncClient() as client:
        await capture_identities(
            2026,
            1,
            client=client,
            lake=SpyLake(),
            now=NOW,
            misses=MissQueue(),
            index=ResolutionIndex(),
            roster_floor=100,
        )

    ratio = metric_value(
        "collector_coverage_ratio",
        collector="player-identity",
        signal_type="player_identity_crosswalk",
    )
    assert ratio == pytest.approx(0.01)


def test_auth_failures_carry_this_collectors_label(client, metric_value):
    from fastapi.testclient import TestClient

    from player_identity.main import app

    before = metric_value(
        "collector_auth_failures_total", collector="player-identity", reason="missing"
    )
    with TestClient(app) as anon:
        anon.get("/catalog")

    assert (
        delta(
            metric_value,
            "collector_auth_failures_total",
            before,
            collector="player-identity",
            reason="missing",
        )
        == 1.0
    )


def test_the_metrics_instance_is_the_one_the_descriptor_carries():
    """`metrics` is passed into the descriptor, not constructed by the
    library, so `capture` and the routes record against exactly one
    instance."""
    from player_identity.main import app

    assert app.state.collector_spec.metrics is metrics
