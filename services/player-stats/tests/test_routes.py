"""player-stats' contract surface: the standard five, `/revisions`, and auth.

The five routes themselves are `collector_core.routes`' and are proved there
against a fake collector. What this file proves is that THIS service is wired
to them — that its descriptor reaches `/catalog`, that its own `signal_matches`
is consulted, that auth is mounted, and that its one extra route reaches the
same lake a capture just wrote to.
"""

import time

import httpx
import respx
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream

from player_stats.adapters.upstream import stats_url
from player_stats.capture import BOX_SIGNAL, SIGNAL_TYPES

from .conftest import NOW, SEASON, TEST_TOKEN, WEEK, SpyLake, feed_csv, feed_row

WEEK_ONE = [
    feed_row(
        player_id="00-0000001",
        position="QB",
        team="KC",
        game_id=f"{SEASON}_01_BUF_KC",
        attempts=30,
        passing_yards=240,
    ),
    feed_row(
        player_id="00-0000002",
        position="WR",
        team="BUF",
        game_id=f"{SEASON}_01_BUF_KC",
        targets=9,
        receptions=6,
        receiving_yards=71,
    ),
]


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


def refresh_and_wait(client, rows=WEEK_ONE) -> dict:
    """Dispatch a capture against a mocked upstream and wait for it to land."""
    with respx.mock:
        respx.get(stats_url(SEASON)).mock(
            return_value=httpx.Response(200, text=feed_csv(rows))
        )
        response = client.post("/refresh", json={"season": SEASON, "week": WEEK})
        assert response.status_code == 202
        return wait_for_signals(client, count=len(SIGNAL_TYPES))


# ── the standard five ─────────────────────────────────────────────────────────


def test_health_is_open_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_scrapeable(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_catalog_declares_this_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "player-stats"
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
    body = refresh_and_wait(client)
    assert body["count"] == len(SIGNAL_TYPES)
    envelope = body["envelopes"][0]
    assert envelope["collector"] == "player-stats"
    assert envelope["signal_type"] == BOX_SIGNAL
    assert len(envelope["signals"]) == len(WEEK_ONE)
    assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


# ── this collector's own row filtering ────────────────────────────────────────


def test_a_team_filter_narrows_the_signals(client):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    refresh_and_wait(client)
    body = client.get("/signals?team=KC").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 1
    assert {row["team"] for row in rows} == {"KC"}


def test_a_position_filter_narrows_the_signals(client):
    refresh_and_wait(client)
    body = client.get("/signals?position=WR").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 1
    assert rows[0]["position"] == "WR"


def test_a_player_id_filter_narrows_the_signals(client):
    refresh_and_wait(client)
    everything = client.get("/signals").json()
    target = everything["envelopes"][0]["signals"][0]["player_id"]

    body = client.get(f"/signals?player_id={target}").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 1
    assert rows[0]["player_id"] == target


def test_a_filter_matching_nothing_returns_an_empty_signal_list(client):
    """Not an error, and not everything: an empty list is the honest answer."""
    refresh_and_wait(client)
    body = client.get("/signals?team=DEN").json()
    assert body["count"] == 1
    assert body["envelopes"][0]["signals"] == []


# ── the extra route ───────────────────────────────────────────────────────────


def test_revisions_is_empty_after_a_single_capture(client):
    """A first appearance is not a restatement."""
    refresh_and_wait(client)
    body = client.get("/revisions").json()
    assert body["revisions"] == []
    assert body["count"] == 0
    assert body["season"] == SEASON
    assert body["week"] == WEEK


def test_revisions_defaults_to_the_capture_loops_own_scope(client):
    """So an operator cannot accidentally ask a different week than the one the
    collector is capturing."""
    spec = client.app.state.collector_spec
    body = client.get("/revisions").json()
    assert (body["season"], body["week"]) == (
        spec.default_scope["season"],
        spec.default_scope["week"],
    )


def test_revisions_reads_the_lake_the_capture_writes_to(client, monkeypatch):
    """Reached through `app.state.collector_spec`, so the route sees the same
    lake object a capture just wrote to — not a module-level global only this
    service's main.py could see."""
    spec = client.app.state.collector_spec
    lake = SpyLake()
    monkeypatch.setattr(spec, "lake", lake)

    def snapshot(yards, revision, hour):
        envelope = Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=spec.name,
            signal_type=BOX_SIGNAL,
            captured_at=NOW.replace(hour=hour),
            upstream=Upstream(adapter="test", fetched_at=NOW),
            scope={"season": SEASON, "week": WEEK},
            coverage=Coverage(expected=1, present=1, missing=[]),
            errors=[],
            signals=[
                {
                    "player_id": "fdy-a1",
                    "game_id": f"{SEASON}_01_BUF_KC",
                    "receiving": {"yards": yards},
                    "passing": {},
                    "rushing": {},
                    "misc": {},
                    "revision": revision,
                }
            ],
        )
        lake.write(envelope)

    snapshot(100, 0, 12)
    snapshot(118, 1, 13)

    body = client.get("/revisions").json()
    assert body["count"] == 1
    assert body["revisions"][0] == {
        "game_id": f"{SEASON}_01_BUF_KC",
        "player_id": "fdy-a1",
        "revision": 1,
        "captured_at": "2026-09-15T13:00:00Z",
    }


def test_an_unreadable_lake_is_503_not_an_empty_revision_list(client, monkeypatch):
    """`{"revisions": [], "count": 0}` with a 200 tells the generator 'nothing
    has been restated', which is indistinguishable from the truth. Found by
    running the image with the chart's environment against a lake endpoint that
    does not resolve, where this route was previously a bare 500."""
    spec = client.app.state.collector_spec
    monkeypatch.setattr(spec, "lake", SpyLake(fail_read=True))
    response = client.get("/revisions")
    assert response.status_code == 503
    assert "lake" in response.json()["detail"]


def test_a_mistyped_since_is_422(client):
    """Ignoring it would return the whole history, which reads as 'nothing has
    been restated in my window'."""
    assert client.get("/revisions?since=last%20tuesday").status_code == 422


def test_a_valid_since_is_accepted(client):
    assert client.get("/revisions?since=2026-09-15T00:00:00Z").status_code == 200


# ── auth ──────────────────────────────────────────────────────────────────────


def test_a_data_route_without_a_token_is_401(client):
    """Auth is enforced in-process, not only at the gateway: a ClusterIP is
    reachable by anything in the namespace, and smoke-test.sh port-forwards the
    Service directly."""
    assert client.get("/signals", headers={"Authorization": ""}).status_code == 401


def test_the_extra_route_is_protected_too(client):
    """The bearer middleware is mounted, so a route added after
    `build_collector_app` is protected by default — prove it rather than
    assume it."""
    assert client.get("/revisions", headers={"Authorization": ""}).status_code == 401


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
