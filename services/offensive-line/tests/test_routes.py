"""offensive-line's five-route contract surface, plus auth and `/lineups`.

The standard five are `collector_core.routes`' and are proved there against a
fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, that auth is mounted — and it proves `/lineups`, which is this
collector's own.
"""

import time

import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION

from offensive_line.capture import SIGNAL_TYPES
from offensive_line.main import app
from offensive_line.ratings import RECORD_STARTER, RECORD_UNIT, STARTER_POSITIONS

from . import season as season_module
from .conftest import (
    SEASON,
    TEST_TOKEN,
    WEEK,
    Feeds,
    SpyLake,
    resolve_everything,
    run_capture,
)


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
    assert body["collector"] == "offensive-line"
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


def test_lineup_hash_is_not_an_accepted_filter(client):
    """Declared filters must be implemented. `lineup_hash` is an opaque join
    key a caller already read off a row, so filtering on it buys nothing a
    client-side comparison does not — and declaring it without implementing it
    would return everything while looking like a working filter."""
    assert client.get("/signals?lineup_hash=abc").status_code == 422


def _refresh_and_wait(client) -> dict:
    with respx.mock(assert_all_called=False) as router:
        Feeds().install(router)
        resolve_everything(router)
        accepted = client.post("/refresh", json={"season": SEASON, "week": WEEK})
        assert accepted.status_code == 202
        return wait_for_signals(client, count=len(SIGNAL_TYPES))


def test_refresh_is_accepted_and_the_capture_eventually_lands(client):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    body = _refresh_and_wait(client)
    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "offensive-line"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_row_filter_narrows_the_signals(client):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    _refresh_and_wait(client)

    body = client.get(f"/signals?record_type={RECORD_UNIT}").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["record_type"] for row in rows} == {RECORD_UNIT}

    body = client.get("/signals?starter_position=LT").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows
    assert {row["starter_position"] for row in rows} == {"LT"}
    assert {row["record_type"] for row in rows} == {RECORD_STARTER}


def test_a_team_filter_narrows_to_one_team(client):
    _refresh_and_wait(client)
    team = season_module.TEAMS[0]
    body = client.get(f"/signals?team_id={team}").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows
    assert {row["team_id"] for row in rows} == {team}


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


def test_the_extra_route_is_also_behind_auth(client):
    """A route added after `build_collector_app` inherits the middleware,
    which is the whole reason auth is middleware rather than a dependency on
    each of the five."""
    response = client.get(
        f"/lineups?season={SEASON}&week={WEEK}", headers={"Authorization": ""}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------
# /lineups
# --------------------------------------------------------------------------


@pytest.fixture
def route_lake():
    """Swap the process lake for an in-memory one, and put it back.

    `build_lake_writer_from_env` returns a `NullLakeWriter` with no
    `LAKE_BUCKET` set, which accepts every write and returns nothing — so a
    route reading the lake back would find an empty partition however healthy
    the capture was. The route must be exercised against a lake that actually
    holds objects, or it proves only that it does not crash.
    """
    spec = app.state.collector_spec
    original = spec.lake
    spec.lake = SpyLake()
    try:
        yield spec.lake
    finally:
        spec.lake = original


async def test_lineups_reads_the_projected_five_back_out_of_the_lake(
    client, route_lake
):
    """Forward-looking, which is why the spec gives it a route: the
    availabilities describe `week + 1` and `unavailable_starters` names the
    men a generator must not project as playing."""
    await run_capture(Feeds(), lake=route_lake)

    body = client.get(f"/lineups?season={SEASON}&week={WEEK}").json()
    assert body["season"] == SEASON
    assert body["count"] == len(season_module.TEAMS)

    view = {entry["team_id"]: entry for entry in body["lineups"]}
    stable = view[season_module.TEAMS[0]]
    assert [row["starter_position"] for row in stable["starters"]] == list(
        STARTER_POSITIONS
    )
    assert stable["continuity_games"] == season_module.WEEKS - 1
    assert stable["lineup_hash"]
    # AAA's left tackle is listed out for the upcoming week.
    assert stable["unavailable_starters"], "an out starter must be named"

    churned = view[season_module.TEAMS[season_module.CHURN_FROM]]
    assert churned["lineup_changed"] is True
    assert churned["continuity_games"] == 0


async def test_lineups_can_be_narrowed_to_one_team(client, route_lake):
    await run_capture(Feeds(), lake=route_lake)
    team = season_module.TEAMS[1]
    body = client.get(f"/lineups?season={SEASON}&week={WEEK}&team={team}").json()
    assert body["count"] == 1
    assert body["lineups"][0]["team_id"] == team


def test_lineups_for_an_uncaptured_week_is_empty_rather_than_an_error(client):
    """An empty partition is a fact about the lake, not a failure. A 500 here
    would make an ordinary offseason query look like an outage."""
    body = client.get(f"/lineups?season={SEASON}&week=17").json()
    assert body == {"season": SEASON, "week": 17, "lineups": [], "count": 0}


def test_lineups_requires_its_scope(client):
    """`season` and `week` are required rather than defaulted: a default would
    silently answer about a week the caller did not ask for."""
    assert client.get("/lineups").status_code == 422
    assert client.get(f"/lineups?season={SEASON}").status_code == 422


async def test_lineups_reports_the_newest_capture_only(client, route_lake):
    """The lake is append-only and resolved by recency, so an older object is
    a superseded capture. Merging the two would mix two vintages of one week."""
    from datetime import UTC, datetime

    await run_capture(Feeds(), lake=route_lake)
    later = datetime(2026, 12, 1, tzinfo=UTC)
    await run_capture(Feeds(status={"snaps": 500}), lake=route_lake, now=later)

    body = client.get(f"/lineups?season={SEASON}&week={WEEK}").json()
    assert all(entry["starters"] == [] for entry in body["lineups"]), (
        "the newer capture lost its snap feed, so it has no starters"
    )
