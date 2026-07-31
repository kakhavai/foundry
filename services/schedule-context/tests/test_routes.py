"""schedule-context's five-route contract surface, plus auth.

The routes themselves are `collector_core.routes`' and are proved there against
a fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, and that auth is mounted.
"""

import time

import pytest
from collector_core.envelope import ENVELOPE_VERSION

from schedule_context.capture import REST, SIGNAL_TYPES, SITUATIONAL

from .conftest import TEST_TOKEN, mock_upstream, season_csv


@pytest.fixture
def served_upstream():
    """The feed, served over a mocked transport for the whole test.

    The capture a `/refresh` dispatches runs on the app's own event loop
    thread, so this has to be in place before the POST rather than awaited
    around it.
    """
    with mock_upstream(season_csv()):
        yield


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
    assert body["collector"] == "schedule-context"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "weekly"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_player_id_is_not_an_accepted_filter(client):
    """This collector emits no `player_id`. Accepting one would return every
    row for a query that asked about one player."""
    assert client.get("/signals?player_id=fdy-a1b2c3").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


def test_refresh_is_accepted_and_the_capture_eventually_lands(
    client, served_upstream
):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    accepted = client.post("/refresh", json={})
    assert accepted.status_code == 202

    body = wait_for_signals(client, count=len(SIGNAL_TYPES))
    assert body["count"] == len(SIGNAL_TYPES)
    assert len(body["envelopes"]) == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "schedule-context"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]
        assert len(envelope["signals"]) == 32


def test_a_second_refresh_inside_the_floor_is_429(client, served_upstream):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_row_filter_narrows_the_signals(client, served_upstream):
    """`signal_matches` is this collector's own, so prove it is consulted —
    and that it maps the `team` parameter onto the row's `team_id`, which is
    the mismatch that returns everything while looking like a working
    filter."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get("/signals?team=BUF").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert len(rows) == len(SIGNAL_TYPES), "one row per signal type for one club"
    assert {row["team_id"] for row in rows} == {"BUF"}


def test_the_two_signal_types_carry_their_own_fields(client, served_upstream):
    """The split is the whole reason there are two: rest survives an
    unresolvable venue and travel does not."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    by_type = {
        envelope["signal_type"]: envelope
        for envelope in client.get("/signals").json()["envelopes"]
    }
    assert set(by_type) == {SITUATIONAL, REST}
    assert "days_rest" in by_type[REST]["signals"][0]
    assert "travel_distance_mi" in by_type[SITUATIONAL]["signals"][0]
    assert "days_rest" not in by_type[SITUATIONAL]["signals"][0]


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
