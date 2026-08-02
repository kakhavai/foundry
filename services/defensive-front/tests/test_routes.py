"""defensive-front's five-route contract surface, plus auth.

The routes themselves are `collector_core.routes`' and are proved there against
a fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, and that auth is mounted.
"""

import time

import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION

from defensive_front.capture import SIGNAL_TYPES

from . import season as season_module
from .conftest import TEST_TOKEN, Feeds


@pytest.fixture
def feeds_online():
    """The four upstreams, mocked for the duration of a `TestClient` session.

    `respx` patches the httpx transport globally, so it also intercepts the
    capture that `POST /refresh` dispatches onto the app's event loop.
    """
    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        yield feeds


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
    assert body["collector"] == "defensive-front"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "weekly"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


def test_catalog_declares_exactly_the_filters_that_are_implemented(client):
    """A filter declared and not implemented is accepted by the router,
    ignored by the predicate, and returns everything — which looks exactly
    like a filter that works."""
    body = client.get("/catalog").json()
    assert set(body["filters"]) == {
        "season",
        "week",
        "signal_type",
        "team_id",
        "unit",
    }


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


def test_refresh_is_accepted_and_the_capture_eventually_lands(client, feeds_online):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    accepted = client.post("/refresh", json={})
    assert accepted.status_code == 202

    body = wait_for_signals(client, count=len(SIGNAL_TYPES))
    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "defensive-front"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]
        assert envelope["signals"]


def test_a_second_refresh_inside_the_floor_is_429(client, feeds_online):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_the_team_filter_narrows_the_signals(client, feeds_online):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    team = season_module.TEAMS[2]
    body = client.get(f"/signals?team_id={team}").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["team_id"] for row in rows} == {team}


def test_the_unit_filter_narrows_the_signals(client, feeds_online):
    """Declared because `(team_id, unit)` is the row key, and a consumer
    written against `?unit=overall` must keep working the day an alignment
    source appears."""
    client.post("/refresh", json={})
    wait_for_signals(client, count=len(SIGNAL_TYPES))

    matched = client.get("/signals?unit=overall").json()
    rows = [row for envelope in matched["envelopes"] for row in envelope["signals"]]
    assert len(rows) == len(season_module.TEAMS)

    # The narrowing is real, not a pass-through: a unit this collector does
    # not emit must match nothing rather than everything.
    empty = client.get("/signals?unit=interior").json()
    assert not [row for envelope in empty["envelopes"] for row in envelope["signals"]]


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


def test_signal_matches_stringifies_the_row_side():
    """`str()` on the row side, and the reason is the day a non-string filter
    is added rather than today.

    Both declared row filters are string-valued right now, so dropping the
    `str()` is currently an EQUIVALENT mutation of the end-to-end behaviour —
    and stating that is more useful than pretending otherwise. What is not
    equivalent is the function's own contract: its docstring promises that a
    row value "may well be an int", because query values always arrive as
    strings. This drives that contract directly, so the guard is pinned before
    the first int-valued filter rather than after it.
    """
    from defensive_front.signals import signal_matches

    assert signal_matches({"team_id": 5, "unit": "overall"}, {"team_id": "5"})
    assert not signal_matches({"team_id": 5, "unit": "overall"}, {"team_id": "6"})
    assert signal_matches({"team_id": "SEA", "unit": "overall"}, {"unit": "overall"})
    assert not signal_matches({"team_id": "SEA", "unit": "overall"}, {"unit": "edge"})
