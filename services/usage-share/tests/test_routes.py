"""usage-share's five-route contract surface, plus auth.

The routes themselves are `collector_core.routes`' and are proved there against
a fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, and that auth is mounted.
"""

import time

from collector_core.envelope import ENVELOPE_VERSION

from usage_share.capture import SIGNAL_TYPES
from usage_share.signals import ROW_FILTERS, SUPPORTED_FILTERS

from .conftest import SAMPLE_PLAYER_ROWS, TEST_TOKEN, canonical_id


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
    assert body["collector"] == "usage-share"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "weekly"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_position_is_not_an_accepted_filter(client):
    """`position` is on the adapter's row but not on the published one, so a
    filter for it could only ever match nothing. 422 says that; an empty list
    would look like a quiet week."""
    assert client.get("/signals?position=WR").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


def test_every_declared_filter_is_implemented():
    """A filter the router accepts but `signal_matches` ignores returns
    everything, which is indistinguishable from a working filter."""
    universal = {"season", "week", "signal_type"}
    assert ROW_FILTERS, "no row filters — the comparison below would be vacuous"
    assert set(SUPPORTED_FILTERS) - universal == set(ROW_FILTERS)


def test_refresh_is_accepted_and_the_capture_eventually_lands(client, upstream):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    accepted = client.post("/refresh", json={"season": 2026, "week": 1})
    assert accepted.status_code == 202

    body = wait_for_signals(client, count=len(SIGNAL_TYPES))
    assert body["count"] == len(SIGNAL_TYPES)
    assert body["envelopes"], "no envelopes to assert against"
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "usage-share"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]
        assert len(envelope["signals"]) == SAMPLE_PLAYER_ROWS


def test_a_second_refresh_inside_the_floor_is_429(client, upstream):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_row_filter_narrows_the_signals(client, upstream):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    client.post("/refresh", json={"season": 2026, "week": 1})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get("/signals?team=KC").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 5, f"expected KC's five skill players, got {len(rows)}"
    assert {row["team"] for row in rows} == {"KC"}


def test_a_player_id_filter_narrows_to_one_row(client, upstream):
    """The filter is on the CANONICAL id, because that is what is published —
    the upstream's GSIS key is the join's input and never leaves the pod."""
    client.post("/refresh", json={"season": 2026, "week": 1})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    wanted = canonical_id("00-KC-WR1")
    body = client.get(f"/signals?player_id={wanted}").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 1
    assert rows[0]["player_id"] == wanted

    stale = client.get("/signals?player_id=00-KC-WR1").json()
    stale_rows = [row for envelope in stale["envelopes"] for row in envelope["signals"]]
    assert stale_rows == []


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
