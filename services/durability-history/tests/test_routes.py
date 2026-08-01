"""durability-history's five-route contract surface, plus auth.

The routes themselves are `collector_core.routes`' and are proved there against a
fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, and that auth is mounted.
"""

import time

import respx
from collector_core.envelope import ENVELOPE_VERSION

from durability_history.capture import DURABILITY_PROFILE, SIGNAL_TYPES

from .conftest import (
    BRAVO,
    CANONICAL_IDS,
    SEASON,
    TEST_TOKEN,
    WEEK,
    mock_identity,
    mock_upstreams,
)


def wait_for_signals(client, *, count: int, timeout: float = 10.0) -> dict:
    """Poll `/signals` until a dispatched capture has landed.

    **`POST /refresh` returns 202 — accepted, not done.** The capture runs as a
    background task and lands in `CaptureState` whenever it finishes, so reading
    `/signals` on the next line is a race. It is a race that used to be won by
    accident, because nothing in the capture path yielded; the lake write now
    goes through `asyncio.to_thread`, so it genuinely suspends.

    Bounded and loud: on timeout this fails with what it actually saw rather than
    hanging or asserting against an empty envelope. Never replace it with a bare
    `client.get("/signals")` after a refresh.
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


def refresh_and_wait(client) -> dict:
    """Dispatch a capture for the scope the fixture lake actually holds.

    The `season`/`week` body is not decoration. `build_collector_app` reads its
    default scope from `CAPTURE_SEASON`/`CAPTURE_WEEK`, which default to
    `2026`/`1`, while the fixture publishes its `roster-scope` membership for
    week 3. A bare `{}` therefore narrows against a week nothing was published
    for and fails closed with `scope_unavailable` — a 202 followed by an empty
    `/signals` forever. That is exactly the shape `wait_for_signals` exists to
    make loud rather than to hang on.
    """
    with respx.mock:
        mock_upstreams(respx.mock)
        mock_identity(respx.mock)
        accepted = client.post("/refresh", json={"season": SEASON, "week": WEEK})
        assert accepted.status_code == 202
        return wait_for_signals(client, count=len(SIGNAL_TYPES))


def test_health_is_open_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_scrapeable(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_catalog_declares_this_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "durability-history"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "seasonal"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_body_part_is_not_a_declared_filter(client):
    """Deliberately absent: `body_part` lives inside a row's `injury_events`
    array, so a row-level predicate could only answer "this player has ever had a
    hamstring injury" while looking like it returned hamstring injuries.
    `/signals/return-profile` is the route that answers that honestly."""
    assert client.get("/signals?body_part=hamstring").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


def test_refresh_is_accepted_and_the_capture_eventually_lands(client):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    body = refresh_and_wait(client)
    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "durability-history"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    with respx.mock:
        mock_upstreams(respx.mock)
        mock_identity(respx.mock)
        client.post("/refresh", json={"season": SEASON, "week": WEEK})
        response = client.post("/refresh", json={"season": SEASON, "week": WEEK})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_row_filter_narrows_the_signals(client):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    refresh_and_wait(client)

    body = client.get(f"/signals?player_id={CANONICAL_IDS[BRAVO]}").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["player_id"] for row in rows} == {CANONICAL_IDS[BRAVO]}


def test_a_filter_on_a_field_only_one_signal_type_carries_excludes_the_others(client):
    """Only `player_durability_profile` rows carry `position`. Passing the others
    through would make a position filter look like it silently ignored two thirds
    of the response."""
    refresh_and_wait(client)

    body = client.get("/signals?position=RB").json()
    types = {
        envelope["signal_type"] for envelope in body["envelopes"] if envelope["signals"]
    }
    assert types == {DURABILITY_PROFILE}


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
