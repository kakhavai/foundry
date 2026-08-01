"""venue's five-route contract surface, its extra route, and auth.

The five routes themselves are `collector_core.routes`' and are proved there
against a fake collector. What this file proves is that THIS service is wired
to them — that its descriptor reaches `/catalog`, that its own `signal_matches`
is consulted, that auth is mounted — plus the one route the spec adds,
`GET /venues/{venue_id}/revisions`.
"""

import time

from collector_core.envelope import ENVELOPE_VERSION

from venue import reference
from venue.capture import ASSIGNMENT, SIGNAL_TYPES

from .conftest import TEST_TOKEN, mock_upstream, season_csv


def wait_for_signals(client, *, count: int, timeout: float = 10.0) -> dict:
    """Poll `/signals` until a dispatched capture has landed.

    **`POST /refresh` returns 202 — accepted, not done.** The capture runs as a
    background task and lands in `CaptureState` whenever it finishes, so reading
    `/signals` on the next line is a race. It is a race that used to be won by
    accident, because nothing in the capture path yielded; the lake write now
    goes through `asyncio.to_thread`, so it genuinely suspends.

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


def _refresh_and_wait(client):
    """Dispatch a capture against a served CSV and wait for it to land.

    The mock has to stay up for the duration of the BACKGROUND task, not just
    the POST — `/refresh` returns 202 immediately and the fetch happens after
    the response — so the polling loop lives inside the `respx` context. A mock
    that closed at the POST would let the dispatched capture reach the real
    nflverse feed on every test run.
    """
    with mock_upstream(season_csv()):
        accepted = client.post("/refresh", json={})
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
    assert body["collector"] == "venue"
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


def test_refresh_is_accepted_and_the_capture_eventually_lands(client):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    body = _refresh_and_wait(client)
    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "venue"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    # Inside the mock even though this test never reads /signals: the FIRST
    # refresh dispatches a real background capture, and an unmocked one would
    # reach the live nflverse feed on every run of this suite.
    with mock_upstream(season_csv()):
        client.post("/refresh", json={})
        response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_the_venue_id_filter_narrows_both_signal_types(client):
    """`signal_matches` is this collector's own, so prove it is consulted — and
    prove it against BOTH row shapes, which is the thing a single-signal-type
    collector's version of this test cannot check."""
    _refresh_and_wait(client)

    body = client.get("/signals?venue_id=lambeau").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["venue_id"] for row in rows} == {"lambeau"}
    # Both types carry venue_id, so both must be represented; a filter that
    # only ever matched one shape would still satisfy the assertion above.
    assert any("game_id" in row for row in rows), "no assignment row survived"
    assert any("content_hash" in row for row in rows), "no static row survived"


def test_the_team_filter_means_tenant_on_static_and_home_club_on_assignment(client):
    _refresh_and_wait(client)

    body = client.get("/signals?team=NYJ").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows

    static_rows = [row for row in rows if "home_team_ids" in row]
    assignment_rows = [row for row in rows if "designated_home_team_id" in row]
    assert len(static_rows) == 1, static_rows
    assert static_rows[0]["venue_id"] == "metlife"
    assert "NYJ" in static_rows[0]["home_team_ids"]
    assert assignment_rows, "no NYJ home games survived the filter"
    assert all(row["designated_home_team_id"] == "NYJ" for row in assignment_rows)


def test_the_game_id_filter_excludes_static_rows(client):
    """A static row has no `game_id`. Passing it through would return all
    thirty venue records beside the one game, which reads as a broken filter."""
    body = _refresh_and_wait(client)
    assignment = next(e for e in body["envelopes"] if e["signal_type"] == ASSIGNMENT)
    game_id = assignment["signals"][0]["game_id"]

    filtered = client.get(f"/signals?game_id={game_id}").json()
    rows = [row for envelope in filtered["envelopes"] for row in envelope["signals"]]
    assert len(rows) == 1, rows
    assert rows[0]["game_id"] == game_id


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


# ── the extra route ──────────────────────────────────────────────────────────


def test_the_revisions_route_returns_a_venues_full_history(client):
    """Served from the committed table, not the lake — which is the point: it
    answers before any capture has run, and it cannot be stale."""
    response = client.get("/venues/lambeau/revisions")
    assert response.status_code == 200
    body = response.json()
    assert body["collector"] == "venue"
    assert body["venue_id"] == "lambeau"
    assert body["count"] == len(reference.revisions_for("lambeau"))
    assert body["count"] >= 1
    assert body["revisions"][0]["venue_id"] == "lambeau"
    assert body["revisions"][-1]["effective_to"] is None
    assert body["table_compiled_on"] == reference.TABLE_COMPILED_ON.isoformat()


def test_the_revisions_route_resolves_a_single_date(client):
    on = reference.TABLE_COMPILED_ON.isoformat()
    body = client.get(f"/venues/lambeau/revisions?on={on}").json()
    assert body["on"] == on
    assert body["count"] == 1
    assert body["revisions"][0]["effective_from"] == on


def test_the_revisions_route_returns_nothing_for_a_date_before_the_table(client):
    """Zero, never the closest revision. The fallback IS the failure mode."""
    body = client.get("/venues/lambeau/revisions?on=2020-01-01").json()
    assert body["count"] == 0
    assert body["revisions"] == []


def test_an_unknown_venue_is_404_not_an_empty_history(client):
    """A typo'd id and a venue with no history are different facts. `[]` gets
    filed as "that venue has no history" rather than as the caller's bug."""
    response = client.get("/venues/not-a-venue/revisions")
    assert response.status_code == 404


def test_the_revisions_route_requires_a_token(client):
    """Every route beyond /health and /metrics is behind the bearer middleware,
    which is middleware precisely so a route added later is protected by
    default."""
    response = client.get("/venues/lambeau/revisions", headers={"Authorization": ""})
    assert response.status_code == 401


def test_a_malformed_on_date_is_422(client):
    assert client.get("/venues/lambeau/revisions?on=not-a-date").status_code == 422
