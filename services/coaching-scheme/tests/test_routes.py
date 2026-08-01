"""The five-route contract surface, auth, and `GET /teams/{id}/revisions`.

The standard routes are `collector_core.routes`' and are proved there against
a fake collector. What this file proves is that THIS service is wired to them
— that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, that auth is mounted — plus the extra route in full, because that
one is entirely this collector's.
"""

import time

import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION

from coaching_scheme.capture import PROFILE, SIGNAL_TYPES, STAFF

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

    `POST /refresh` would reach four third-party feeds, so the state is
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
    assert body["collector"] == "coaching-scheme"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "seasonal"
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
    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        accepted = client.post("/refresh", json={})
        assert accepted.status_code == 202

        body = wait_for_signals(client, count=len(SIGNAL_TYPES))

    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "coaching-scheme"
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


def test_the_revision_id_filter_narrows_further(captured):
    """A second row filter, because `signal_matches` loops over `ROW_FILTERS`
    and a loop that only ever runs one iteration proves nothing about the
    second entry."""
    body = captured.get("/signals?revision_id=BBB-2026-r1").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows
    assert {row["revision_id"] for row in rows} == {"BBB-2026-r1"}


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
# GET /teams/{team_id}/revisions
# --------------------------------------------------------------------------


def test_the_extra_route_also_requires_a_token(client):
    """A route added after `build_collector_app` is covered by the middleware
    it mounted — but only because it is middleware. Asserted rather than
    assumed: a route registered on a sub-application would not be."""
    response = client.get("/teams/AAA/revisions", headers={"Authorization": ""})
    assert response.status_code == 401


def test_an_unknown_team_is_404_not_an_empty_200(captured):
    """A consumer that gets an empty timeline for a typo'd code files it as
    'that team never changed staff' rather than as its own bug."""
    response = captured.get("/teams/ZZZ/revisions")
    assert response.status_code == 404
    assert "ZZZ" in response.json()["detail"]


def test_a_malformed_team_id_is_422_not_404(captured):
    """'You asked wrongly' and 'there is no such team' are different answers,
    and collapsing them sends a client looking for a data problem that does
    not exist. This pair is also what distinguishes a live route from one the
    gateway never published — a missing route 404s for BOTH ids."""
    assert captured.get("/teams/not-a-team/revisions").status_code == 422
    assert captured.get("/teams/aaa/revisions").status_code == 422


def test_a_malformed_season_is_422(captured):
    assert captured.get("/teams/AAA/revisions?season=nope").status_code == 422
    assert captured.get("/teams/AAA/revisions?season=12").status_code == 422


def test_the_timeline_is_ordered_and_carries_its_profile(captured):
    body = captured.get("/teams/AAA/revisions").json()
    assert body["collector"] == "coaching-scheme"
    assert body["team_id"] == "AAA"
    assert body["season"] == 2026

    revisions = body["revisions"]
    assert revisions
    weeks = [row["effective_from_week"] for row in revisions]
    assert weeks == sorted(weeks)
    for row in revisions:
        # The join the route exists to save the caller. `revision_id` is this
        # collector's own invention, so a consumer performing it would be
        # re-implementing the route.
        assert row["scheme_profile"] is not None
        assert row["scheme_profile"]["revision_id"] == row["revision_id"]


async def test_a_two_revision_team_returns_both_in_order(client):
    """The shape the route exists for. A team with one revision cannot
    distinguish 'ordered' from 'returned in whatever order the rows came'."""
    from .conftest import coaches_with_change

    envelopes = await run_capture(
        Feeds(coaches=coaches_with_change("AAA", at_week=7)), lake=SpyLake()
    )
    client.app.state.collector_spec.state.envelopes = envelopes

    revisions = client.get("/teams/AAA/revisions").json()["revisions"]
    assert [row["effective_from_week"] for row in revisions] == [1, 7]
    assert revisions[0]["effective_to_week"] == 6
    # Null means current — the end is derived from the NEXT record and never
    # stored, so the last one has no successor to derive from.
    assert revisions[1]["effective_to_week"] is None


def test_a_season_filter_that_matches_nothing_is_404(captured):
    """Well-formed and empty is still 404: the team exists, that season's
    timeline does not, and an empty 200 would read as 'no changes'."""
    assert captured.get("/teams/AAA/revisions?season=2019").status_code == 404


def test_the_route_answers_before_any_capture(client):
    """`officiating`'s lesson: the collector ships `CAPTURE_ENABLED=false`, so
    an empty capture state is the state it is actually deployed in. The route
    must 404 there rather than 500 on a missing envelope."""
    assert client.get("/teams/AAA/revisions").status_code == 404


def test_a_team_known_only_from_staff_still_answers(client):
    """The profile envelope may be a `present: 0` failure envelope on the
    degraded branch. The timeline must still serve, with a null profile —
    that is the collector working, not a missing team."""
    import asyncio

    envelopes = (
        asyncio.get_event_loop_policy()
        .new_event_loop()
        .run_until_complete(run_capture(lake=SpyLake()))
    )
    del envelopes[PROFILE]
    client.app.state.collector_spec.state.envelopes = envelopes

    body = client.get("/teams/AAA/revisions").json()
    assert body["revisions"]
    assert all(row["scheme_profile"] is None for row in body["revisions"])
    assert STAFF  # named so a reader sees which envelope is doing the work
