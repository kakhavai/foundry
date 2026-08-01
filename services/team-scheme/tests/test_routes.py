"""The five-route contract surface and auth — and nothing beyond them.

The standard routes are `collector_core.routes`' and are proved there against
a fake collector. What this file proves is that THIS service is wired to them:
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, that auth is mounted.

There is deliberately **no extra route**. `coaching-scheme` had
`GET /teams/{team_id}/revisions`, which served the staff-revision timeline;
that route and everything that made it meaningful moved to the deferred
`coaching-staff` collector. The last section here asserts its absence, because
"add the timeline route back" is a natural-looking change that would advertise
a claim this collector cannot make.
"""

import time

import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION

from team_scheme.capture import PROFILE, SIGNAL_TYPES

from .conftest import TEST_TOKEN, Feeds, SpyLake, run_capture


def wait_for_signals(client, *, count: int, timeout: float = 10.0) -> dict:
    """Poll `/signals` until a dispatched capture has landed.

    **`POST /refresh` returns 202 — accepted, not done.** The capture runs as a
    background task and lands in `CaptureState` whenever it finishes, so
    reading `/signals` on the next line is a race. It is a race that used to be
    won by accident, because nothing in the capture path yielded; the lake
    write now goes through `asyncio.to_thread`, so it genuinely suspends.

    Bounded and loud: on timeout this fails with what it actually saw rather
    than hanging or asserting against an empty envelope.
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


@pytest.fixture
def captured(client):
    """A client whose collector state holds one real capture.

    `POST /refresh` would reach three third-party feeds, so the state is
    installed directly from a mocked pass instead — the same envelopes the
    loop would produce, without a network call from a route test.
    """
    import asyncio

    envelopes = (
        asyncio.get_event_loop_policy()
        .new_event_loop()
        .run_until_complete(run_capture(lake=SpyLake()))
    )
    spec = client.app.state.collector_spec
    spec.state.envelopes = envelopes
    return client


# --------------------------------------------------------------------------
# The standard five
# --------------------------------------------------------------------------


def test_health_is_open_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_scrapeable(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_catalog_declares_this_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "team-scheme"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "seasonal"
    assert set(body["signal_types"]) == {PROFILE}


def test_the_catalog_declares_exactly_one_signal_type(client):
    """`staff_assignment` is the deferred `coaching-staff` collector's, and
    `scripts/check-registry.py` compares this list against the registry live.
    A second entry here would red the drift gate — and would also promise a
    signal type nothing produces."""
    body = client.get("/catalog").json()
    assert body["signal_types"] == [PROFILE]
    assert "staff_assignment" not in body["signal_types"]


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_the_revision_id_filter_is_gone_and_is_a_422(client):
    """Not merely absent — actively refused.

    A consumer that carried a `?revision_id=` call over from `coaching-scheme`
    must get a loud 422, not a silently unfiltered list of every team. The
    second is the worse failure: it looks like the join worked.
    """
    assert client.get("/signals?revision_id=AAA-2026-r1").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


def test_refresh_is_accepted_and_the_capture_eventually_lands(client):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        accepted = client.post("/refresh", json={})
        assert accepted.status_code == 202

        body = wait_for_signals(client, count=len(SIGNAL_TYPES))

    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "team-scheme"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    with respx.mock(assert_all_called=False) as router:
        Feeds().install(router)
        client.post("/refresh", json={})
        response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_row_filter_narrows_the_signals(captured):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    body = captured.get("/signals?team_id=AAA").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["team_id"] for row in rows} == {"AAA"}


def test_a_row_filter_that_matches_nothing_returns_nothing(captured):
    """The negative control for the filter. Without it `signal_matches` could
    return a constant `True` and the test above would still pass."""
    body = captured.get("/signals?team_id=ZZZ").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows == []


def test_a_data_route_without_a_token_is_401(client):
    """Auth is enforced in-process, not only at the gateway: a ClusterIP is
    reachable by anything in the namespace, and smoke-test.sh port-forwards
    the Service directly."""
    assert client.get("/signals", headers={"Authorization": ""}).status_code == 401


def test_a_wrong_token_is_401(client):
    response = client.get(
        "/signals", headers={"Authorization": f"Bearer not-{TEST_TOKEN}"}
    )
    assert response.status_code == 401


def test_an_absent_token_env_fails_closed_with_503(client, monkeypatch):
    """Fails closed, loudly: a Secret that never syncs must not read as an
    open collector. The ArgoCD Application stays Healthy either way."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "")
    assert client.get("/signals").status_code == 503


# --------------------------------------------------------------------------
# The routes that must NOT exist
# --------------------------------------------------------------------------


def test_the_revision_timeline_route_is_not_served(captured):
    """404 for a well-formed team id **and** for a malformed one.

    That pair is the tell. A live route distinguishes them — 422 for "you
    asked wrongly", 404 for "no such team" — so two 404s means there is no
    route at all, which is the state this collector ships in.
    """
    assert captured.get("/teams/AAA/revisions").status_code == 404
    assert captured.get("/teams/not-a-team/revisions").status_code == 404


def test_the_app_declares_only_the_standard_five():
    """Asserted on the declared route surface, not on responses.

    A 404 probe can only ask about paths a test thought to name; this
    enumerates what the app actually serves, so a timeline route restored
    under *any* path is caught. Read from `app.openapi()` because
    `build_collector_app` mounts the standard five through `include_router`,
    and FastAPI's `_IncludedRouter` keeps them off `app.routes` — a naive walk
    of that list sees only `/docs` and friends and passes vacuously.

    The `gateway.publicPaths` entry in this collector's Helm values must stay
    in step with this list: a route added here and not there works in-cluster
    and 404s at the gateway.
    """
    from team_scheme.main import app

    assert set(app.openapi()["paths"]) == {
        "/health",
        "/metrics",
        "/catalog",
        "/signals",
        "/refresh",
    }
