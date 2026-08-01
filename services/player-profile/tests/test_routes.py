"""player-profile's five-route contract surface, plus auth and the extra route.

The five routes themselves are `collector_core.routes`' and are proved there
against a fake collector. What this file proves is that THIS service is wired to
them — that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, that auth is mounted — and that `GET /signals/age-curve` behaves.
"""

import time
from pathlib import Path

import httpx
import respx
import yaml
from collector_core.envelope import ENVELOPE_VERSION

from player_profile.capture import (
    BIOGRAPHICAL,
    SIGNAL_TYPES,
    capture_player_profile,
)
from player_profile.derive import MIN_COHORT, POSITION_AGE_CURVE

from .conftest import (
    NOW,
    SEASON,
    TEST_TOKEN,
    WEEK,
    mock_identity,
    mock_upstreams,
    scope_envelope,
)


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


async def install_capture(spec, lake):
    """Run a real capture and install it, so route tests read real rows.

    Deliberately not `POST /refresh`: the refresh path reaches the upstream, and
    these tests are about the read routes rather than about the loop.
    """
    async with httpx.AsyncClient() as http:
        envelopes = await capture_player_profile(
            SEASON, WEEK, client=http, lake=lake, now=NOW
        )
    spec.state.envelopes = envelopes
    spec.state.last_capture_at = NOW
    return envelopes


# ── the standard five ────────────────────────────────────────────────────────


def test_health_is_open_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_scrapeable(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_catalog_declares_this_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "player-profile"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "static reference"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


@respx.mock
async def test_refresh_is_accepted_and_the_capture_eventually_lands(client, lake):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    # The scope override is explicit: `/refresh` with an empty body falls back
    # to CAPTURE_SEASON/CAPTURE_WEEK, and a scope published for a different week
    # fails the pass closed — correctly, but not what this test is about.
    accepted = client.post("/refresh", json={"season": SEASON, "week": WEEK})
    assert accepted.status_code == 202

    body = wait_for_signals(client, count=len(SIGNAL_TYPES))
    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "player-profile"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


@respx.mock
async def test_a_row_filter_narrows_the_signals(client, lake):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await install_capture(client.app.state.collector_spec, lake)

    body = client.get("/signals?position=RB").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]

    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["position"] for row in rows} == {"RB"}


@respx.mock
async def test_a_player_id_filter_reaches_every_signal_type(client, lake):
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await install_capture(client.app.state.collector_spec, lake)

    body = client.get("/signals?player_id=fdy-bbbb11112222").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]

    assert len(rows) == len(SIGNAL_TYPES)
    assert {row["player_id"] for row in rows} == {"fdy-bbbb11112222"}


# ── auth ─────────────────────────────────────────────────────────────────────


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


def test_the_extra_route_is_behind_the_same_auth(client):
    """A route added after `build_collector_app` is protected by the middleware
    by default — but "by default" is exactly the kind of claim worth pinning,
    since the whole point of middleware over a decorator is that nobody has to
    remember."""
    response = client.get(
        "/signals/age-curve?position=RB", headers={"Authorization": ""}
    )
    assert response.status_code == 401


# ── GET /signals/age-curve ───────────────────────────────────────────────────


@respx.mock
async def test_the_age_curve_publishes_the_distribution_the_percentile_used(
    client, lake
):
    """The route exists "so a consumer can reproduce the bucketing rather than
    trust it", which needs the sample AND the boundaries, not either alone."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    envelopes = await install_capture(client.app.state.collector_spec, lake)

    body = client.get("/signals/age-curve?position=RB").json()

    assert body["collector"] == "player-profile"
    assert body["position"] == "RB"
    assert body["as_of_date"] == envelopes[BIOGRAPHICAL].scope["as_of_date"]
    assert body["distribution"]["count"] == len(body["ages"])
    peak_from, post_peak_from, cliff_from = POSITION_AGE_CURVE["RB"]
    assert body["curve_stages"]["peak_from"] == peak_from
    assert body["curve_stages"]["post_peak_from"] == post_peak_from
    assert body["curve_stages"]["cliff_from"] == cliff_from


@respx.mock
async def test_the_age_curve_sample_matches_the_rows_signals_is_serving(client, lake):
    """A distribution that described a different population than `/signals`
    would be worse than none: a consumer would reproduce a percentile that
    disagreed with the published one and trust its own."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    envelopes = await install_capture(client.app.state.collector_spec, lake)

    body = client.get("/signals/age-curve?position=WR").json()

    expected = sorted(
        row["age_years"]
        for row in envelopes[BIOGRAPHICAL].signals
        if row["position"] == "WR" and row["age_years"] is not None
    )
    assert body["ages"] == expected
    assert len(expected) >= 1


def test_an_unknown_position_is_422_not_an_empty_distribution(client):
    """An unknown position and a position with no captured players are different
    facts. A client that gets `count: 0` for a typo files it as "nobody plays
    that position"."""
    response = client.get("/signals/age-curve?position=DT")
    assert response.status_code == 422


def test_the_age_curve_before_any_capture_is_404_not_an_empty_body(client):
    response = client.get("/signals/age-curve?position=RB")
    assert response.status_code == 404


@respx.mock
async def test_the_age_curve_refuses_a_season_it_did_not_capture(client, lake):
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await install_capture(client.app.state.collector_spec, lake)

    assert client.get("/signals/age-curve?position=RB&season=1999").status_code == 404
    assert (
        client.get(f"/signals/age-curve?position=RB&season={SEASON}").status_code == 200
    )


@respx.mock
async def test_the_age_curve_publishes_the_min_cohort_that_nulls_a_percentile(
    client, lake
):
    """Without it a null `position_age_percentile` is indistinguishable from a
    bug."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await install_capture(client.app.state.collector_spec, lake)

    body = client.get("/signals/age-curve?position=RB").json()

    assert body["min_cohort"] == MIN_COHORT


def test_the_gateway_publishes_the_extra_route():
    """A route absent from `gateway.publicPaths` 404s at the edge while working
    perfectly in-cluster, which reads as a routing bug rather than as a missing
    declaration."""
    values = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "helm"
            / "values"
            / "player-profile"
            / "values.yaml"
        ).read_text()
    )
    assert "/signals/age-curve" in values["gateway"]["publicPaths"]


def test_the_scope_fixture_still_names_players():
    """Guards the fixture itself: a `scope_envelope` that stopped naming any
    player would make every capture test silently exercise the empty path while
    staying green."""
    envelope = scope_envelope()
    players = [row for row in envelope.signals if row["entity_type"] == "player"]
    assert len(players) == 5
