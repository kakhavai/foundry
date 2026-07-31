"""depth-chart's five-route contract surface, plus auth and `/signals/diff`.

The standard five are `collector_core.routes`' and are proved there against a
fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, that auth is mounted, and that the extra route the spec asks for is
reachable and protected like everything else.
"""

import time

import httpx
import respx
from collector_core.envelope import ENVELOPE_VERSION
from collector_core.lake import EventLoopGuardedLake

from depth_chart.adapters.upstream import source_ref
from depth_chart.capture import CHART_SIGNAL, SIGNAL_TYPES, STABILITY_SIGNAL
from depth_chart.main import app

from .conftest import TEST_TOKEN, SpyLake, depth_csv, depth_row

FEED = source_ref(2026, 1)

# A two-team, three-group feed: enough to prove a filter narrows, small enough
# that a route test is not secretly a 160-group capture benchmark.
ROWS = [
    depth_row("KC", "QB", 1, "KC Passer"),
    depth_row("KC", "WR", 1, "KC Receiver"),
    depth_row("BUF", "QB", 1, "BUF Passer"),
]


def mock_feed():
    respx.get(FEED).mock(return_value=httpx.Response(200, text=depth_csv(ROWS)))


def wait_for_signals(client, *, count: int, timeout: float = 10.0) -> dict:
    """Poll `/signals` until a dispatched capture has landed.

    **`POST /refresh` returns 202 — accepted, not done.** The capture runs as a
    background task and lands in `CaptureState` whenever it finishes, so reading
    `/signals` on the next line is a race. It is a race that used to be won by
    accident, because nothing in the capture path yielded; the lake write now
    goes through `asyncio.to_thread`, so it genuinely suspends.

    Bounded and loud: on timeout this fails with what it actually saw rather
    than hanging or asserting against an empty envelope. Never replace it with a
    bare `client.get("/signals")` after a refresh.
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
    assert body["collector"] == "depth-chart"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "volatile"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)
    assert set(SIGNAL_TYPES) == {CHART_SIGNAL, STABILITY_SIGNAL}


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_player_id_is_not_an_accepted_filter(client):
    """It is null on every row, so a `player_id` filter could only ever match
    nothing — which reads exactly like a working filter over a quiet week.
    422 is the honest answer until the ids are real."""
    assert client.get("/signals?player_id=fdy-abc").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


@respx.mock
def test_refresh_is_accepted_and_the_capture_eventually_lands(client):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    mock_feed()
    assert client.post("/refresh", json={}).status_code == 202

    body = wait_for_signals(client, count=len(SIGNAL_TYPES))
    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "depth-chart"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


@respx.mock
def test_a_second_refresh_inside_the_floor_is_429(client):
    mock_feed()
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


@respx.mock
def test_a_row_filter_narrows_the_signals(client):
    """`signal_matches` is this collector's own, so prove it is consulted —
    and prove it against BOTH signal types, since `team` is on each."""
    mock_feed()
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get("/signals?team=BUF").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 2, rows
    assert {row["team"] for row in rows} == {"BUF"}


@respx.mock
def test_a_position_filter_narrows_both_signal_types(client):
    mock_feed()
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get("/signals?position=WR").json()
    per_type = {
        envelope["signal_type"]: envelope["signals"] for envelope in body["envelopes"]
    }
    assert len(per_type[CHART_SIGNAL]) == 1
    assert len(per_type[STABILITY_SIGNAL]) == 1
    assert per_type[CHART_SIGNAL][0]["player_name"] == "KC Receiver"


@respx.mock
def test_the_diff_route_reads_the_lake_off_the_event_loop(client, monkeypatch):
    """The spec's extra route, end to end through the app.

    The lake installed here is an `EventLoopGuardedLake`, which **raises** if a
    synchronous method is called from the loop thread. So this asserts the
    `asyncio.to_thread` offload as well as the diff: drop the offload in
    `main.py` and this test fails rather than the process silently stalling
    `/health` for the duration of a prefix scan.

    The app's real lake is a `NullLakeWriter` (no `LAKE_BUCKET` in tests), which
    lists nothing — so without this swap the route would 404 and the test would
    prove only that 404 works.
    """
    spec = app.state.collector_spec
    monkeypatch.setattr(spec, "lake", EventLoopGuardedLake(SpyLake()))

    mock_feed()
    client.post("/refresh", json={})
    first = wait_for_signals(client, count=len(SIGNAL_TYPES))
    first_at = first["envelopes"][0]["captured_at"]

    response = client.get(f"/signals/diff?from={first_at}&to={first_at}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 0
    assert body["groups"] == []
    assert body["from"] == first_at
    assert body["signal_type"] == CHART_SIGNAL


def test_the_diff_route_404s_for_a_capture_that_never_happened(client):
    """"Nothing changed" and "that capture never happened" are different
    answers, and collapsing them would let a typo'd timestamp read as a quiet
    week."""
    response = client.get(
        "/signals/diff?from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z"
    )
    assert response.status_code == 404


def test_the_diff_route_requires_both_endpoints(client):
    assert client.get("/signals/diff?from=2026-01-01T00:00:00Z").status_code == 422


def test_a_data_route_without_a_token_is_401(client):
    """Auth is enforced in-process, not only at the gateway: a ClusterIP is
    reachable by anything in the namespace, and smoke-test.sh port-forwards the
    Service directly."""
    assert client.get("/signals", headers={"Authorization": ""}).status_code == 401


def test_the_extra_route_is_behind_auth_too(client):
    """Middleware, not a per-route decorator — so a route added after the fact
    is protected by default. Asserted rather than assumed, because the extra
    route is the one that would be forgotten."""
    response = client.get(
        "/signals/diff?from=a&to=b", headers={"Authorization": ""}
    )
    assert response.status_code == 401


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
