"""defense-vs-position's five-route contract surface, plus auth.

The routes themselves are `collector_core.routes`' and are proved there against
a fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, and that auth is mounted.
"""

import time

import pytest
from collector_core.envelope import ENVELOPE_VERSION

from defense_vs_position.capture import SIGNAL_TYPES

from .conftest import TEST_TOKEN


def wait_for_signals(client, *, count: int, timeout: float = 10.0) -> dict:
    """Poll `/signals` until a dispatched capture has landed.

    **`POST /refresh` returns 202 — accepted, not done.** The capture runs as a
    background task and lands in `CaptureState` whenever it finishes, so
    reading `/signals` on the next line is a race. It is a race that used to be
    won by accident, because nothing in the capture path yielded; the lake
    write now goes through `asyncio.to_thread`, so it genuinely suspends.

    Bounded and loud: on timeout this fails with what it actually saw rather
    than hanging or asserting against an empty envelope. Never replace it with
    a bare `client.get("/signals")` after a refresh.
    """
    deadline = time.monotonic() + timeout
    body = {"count": 0}
    while time.monotonic() < deadline:
        body = client.get("/signals").json()
        if body["count"] >= count:
            return body
        time.sleep(0.05)
    raise AssertionError(
        f"dispatched capture did not land within {timeout}s: "
        f"expected count >= {count}, last saw {body}"
    )


def test_health_is_open_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_scrapeable(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_catalog_declares_this_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "defense-vs-position"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "weekly"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


def test_refresh_is_accepted_and_the_capture_eventually_lands(client):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    accepted = client.post("/refresh", json={})
    assert accepted.status_code == 202

    body = wait_for_signals(client, count=len(SIGNAL_TYPES))
    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "defense-vs-position"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def signal_rows(client, query: str = "") -> list[dict]:
    body = client.get(f"/signals{query}").json()
    return [row for envelope in body["envelopes"] for row in envelope["signals"]]


@pytest.mark.parametrize(
    ("query", "field", "value"),
    [
        pytest.param("?team_id=PHI", "team_id", "PHI", id="team_id"),
        pytest.param("?position=WR", "position", "WR", id="position"),
        pytest.param("?scoring_format=ppr", "scoring_format", "ppr", id="format"),
    ],
)
def test_each_declared_row_filter_narrows_the_signals(client, query, field, value):
    """`signal_matches` is this collector's own, so prove every filter it
    declares is actually consulted.

    Parametrised over the whole of `ROW_FILTERS` rather than spot-checking
    one: a filter the router accepts and the predicate ignores returns all 576
    rows, which looks exactly like a filter that matched everything.
    """
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    rows = signal_rows(client, query)
    assert rows, "the filter matched nothing -- is ROW_FILTERS wired up?"
    assert {row[field] for row in rows} == {value}
    assert len(rows) < len(signal_rows(client)), "the filter narrowed nothing"


def test_the_alignment_filter_is_applied_even_though_it_cannot_narrow(client):
    """`alignment` is the one filter every row satisfies, so a "it narrowed
    something" assertion cannot cover it -- and a predicate that skipped it
    would look identical. Asking for a sub-split this collector does not
    source must return nothing rather than everything."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    assert len(signal_rows(client, "?alignment=all")) == len(signal_rows(client))
    assert signal_rows(client, "?alignment=slot") == []


def test_the_flag_filter_accepts_a_lowercase_bool(client):
    """`rank_divergence_flagged` is a bool, so `str()` gives `True`/`False`.
    `?rank_divergence_flagged=true` is what every other HTTP API in this repo
    accepts, and a case-sensitive compare would return nothing while looking
    like a week with no divergences."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    rows = signal_rows(client, "?rank_divergence_flagged=false")
    assert rows
    assert all(row["rank_divergence_flagged"] is False for row in rows)


def test_every_declared_filter_is_implemented(client):
    """The list the router validates against and the list the predicate
    applies must be the same list, not two lists that agree today."""
    from defense_vs_position.signals import ROW_FILTERS, SUPPORTED_FILTERS

    assert set(SUPPORTED_FILTERS) - {"season", "week", "signal_type"} == set(
        ROW_FILTERS
    )
    for name in ROW_FILTERS:
        assert client.get(f"/signals?{name}=x").status_code == 200


def test_a_data_route_without_a_token_is_401(client):
    """Auth is enforced in-process, not only at the gateway: a ClusterIP is
    reachable by anything in the namespace, and smoke-test.sh port-forwards the
    Service directly."""
    assert client.get("/signals", headers={"Authorization": ""}).status_code == 401


def test_a_wrong_token_is_401(client):
    response = client.get(
        "/signals", headers={"Authorization": f"Bearer not-{TEST_TOKEN}"}
    )
    assert response.status_code == 401


def test_an_absent_token_env_fails_closed_with_503(client, monkeypatch):
    """Fails closed, loudly: a Secret that never syncs must not read as an open
    collector. The ArgoCD Application stays Healthy either way."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "")
    assert client.get("/signals").status_code == 503
