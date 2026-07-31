"""roster-transactions's five-route contract surface, plus `/events` and auth.

The five routes themselves are `collector_core.routes`' and are proved there
against a fake collector. What this file proves is that THIS service is wired to
them — that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, and that auth is mounted — plus the one route beyond the five.
"""

import time

from collector_core.envelope import ENVELOPE_VERSION

from roster_transactions.capture import SIGNAL_TYPES
from roster_transactions.events import MAX_LIMIT

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
    assert body["collector"] == "roster-transactions"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "volatile"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked —
    and for an event stream, 'everything' and 'a busy week' look alike."""
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
        assert envelope["collector"] == "roster-transactions"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_row_filter_narrows_the_signals(client):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get("/signals?transaction_type=ps_elevation").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 1, rows
    assert {row["transaction_type"] for row in rows} == {"ps_elevation"}


def test_the_team_filter_matches_both_sides_of_a_move(client):
    """A transaction is an edge between two rosters. Matching only `to_team`
    would hide every player a team LOST, which is the half that breaks a depth
    chart."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    departures = client.get("/signals?team=SF").json()
    rows = [row for envelope in departures["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 1, rows
    assert rows[0]["from_team"] == "SF"
    assert rows[0]["to_team"] is None


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


def test_events_requires_a_token_too(client):
    """The extra route is mounted after `build_collector_app`, so it inherits
    the bearer middleware rather than declaring its own. Proving that is the
    point: a route added later must be protected by default."""
    assert client.get("/events", headers={"Authorization": ""}).status_code == 401


def test_events_is_empty_before_any_capture(client):
    assert client.get("/events").json() == {
        "events": [],
        "count": 0,
        "next_cursor": None,
    }


def test_events_pages_through_the_captured_stream(client):
    """Cursor paging end to end: page, resume, exhaust."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    first = client.get("/events?limit=2").json()
    assert first["count"] == 2
    assert first["next_cursor"], "a full page must hand back a resume point"

    second = client.get(f"/events?limit=2&since={first['next_cursor']}").json()
    assert second["count"] == 1
    assert second["next_cursor"] is None, "an exhausted stream ends the cursor"

    seen = [row["transaction_id"] for row in first["events"] + second["events"]]
    assert len(seen) == 3
    assert len(set(seen)) == 3, "paging must neither repeat nor drop a row"


def test_events_orders_by_announced_at(client):
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get("/events").json()
    announced = [row["announced_at"] for row in body["events"]]
    assert len(announced) == 3
    assert announced == sorted(announced)


def test_events_rejects_a_malformed_cursor(client):
    """A cursor this collector did not issue is a client bug, and 422 says so.
    Silently restarting from the beginning would re-deliver a whole week."""
    assert client.get("/events?since=garbage").status_code == 422


def test_events_rejects_an_out_of_range_limit(client):
    assert client.get("/events?limit=0").status_code == 422
    assert client.get(f"/events?limit={MAX_LIMIT + 1}").status_code == 422
