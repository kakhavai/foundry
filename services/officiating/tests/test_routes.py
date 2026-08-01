"""officiating's five-route contract surface, its extra route, and auth.

The five routes themselves are `collector_core.routes`' and are proved there
against a fake collector. What this file proves is that THIS service is wired
to them — that its descriptor reaches `/catalog`, that its own `signal_matches`
is consulted, and that auth is mounted — plus `GET /crews/{crew_id}`, which is
entirely this service's.
"""

import time

from collector_core.envelope import ENVELOPE_VERSION

from officiating.capture import ASSIGNMENT, RATES, SIGNAL_TYPES

from .conftest import SEASON, TEST_TOKEN, season_of, upstream_router

CREW = f"{SEASON}-ref703"


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


def _refresh_and_wait(client, games=None):
    """Dispatch a capture against served feeds and wait for it to land.

    The mock stays up for the duration of the BACKGROUND task, not just the
    POST — see `upstream_router`.
    """
    with upstream_router(games if games is not None else season_of()):
        accepted = client.post("/refresh", json={})
        assert accepted.status_code == 202
        return wait_for_signals(client, count=len(SIGNAL_TYPES))


# ---------------------------------------------------------------------------
# the standard five
# ---------------------------------------------------------------------------


def test_health_is_open_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_scrapeable(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_catalog_declares_this_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "officiating"
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
    body = _refresh_and_wait(client)

    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "officiating"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    # Inside the router because a refresh dispatches a real background capture,
    # and an unmocked one would reach nflverse from a unit test.
    with upstream_router(season_of()):
        client.post("/refresh", json={})
        response = client.post("/refresh", json={})

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


# ---------------------------------------------------------------------------
# this collector's own row filters
# ---------------------------------------------------------------------------


def test_the_crew_filter_narrows_both_signal_types(client):
    """`signal_matches` is this collector's own, so prove it is consulted —
    and that it reaches both signal types, since `crew_id` is the one field
    they share."""
    _refresh_and_wait(client)

    body = client.get(f"/signals?crew_id={CREW}").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]

    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["crew_id"] for row in rows} == {CREW}
    types = {e["signal_type"] for e in body["envelopes"] if e["signals"]}
    assert types == set(SIGNAL_TYPES)


def test_the_game_id_filter_excludes_rate_rows(client):
    """A `crew_tendency_rates` row has no `game_id`, and an absent field must
    NOT act as a wildcard. Treating it as one returns every crew's rates for a
    single game — an answer that looks like data and is not."""
    games = season_of()
    _refresh_and_wait(client, games)
    target = games[0].game_id

    body = client.get(f"/signals?game_id={target}").json()

    by_type = {e["signal_type"]: e["signals"] for e in body["envelopes"]}
    assert len(by_type[ASSIGNMENT]) == 1
    assert by_type[ASSIGNMENT][0]["game_id"] == target
    assert by_type[RATES] == []


# ---------------------------------------------------------------------------
# GET /crews/{crew_id}
# ---------------------------------------------------------------------------


def test_the_crew_route_returns_a_profile_and_a_roster(client):
    """The route the spec asks for: a crew's rates and members independent of
    any scheduled game, so a consumer can evaluate a crew before assignments
    publish."""
    _refresh_and_wait(client)

    body = client.get(f"/crews/{CREW}").json()

    assert body["collector"] == "officiating"
    assert body["crew_id"] == CREW
    assert body["referee_name"] == "Ref Number3"
    assert body["games_assigned"] == 6
    assert body["rates"]["crew_id"] == CREW
    assert body["rates"]["games_sampled"] == 6
    # Seven on-field officials, each having worked all six games. The replay
    # official the feed also lists must not be here.
    assert len(body["members"]) == 7
    assert {m["games_worked"] for m in body["members"]} == {6}
    assert not any("Replay" in m["position"] for m in body["members"])


def test_the_crew_route_reports_a_substitute_as_such(client):
    """`games_worked` per member is what makes churn legible without a second
    request — a crew-level continuity number alone cannot say WHICH member
    dragged it down."""
    games = season_of()
    for game in games:
        if game.referee_id == "703" and game.week == 6:
            game.members = (("90703", "Substitute", "Umpire"),)
    _refresh_and_wait(client, games)

    body = client.get(f"/crews/{CREW}").json()

    substitute = [m for m in body["members"] if m["official_id"] == "90703"]
    assert len(substitute) == 1
    assert substitute[0]["games_worked"] == 1
    # Most-frequent first, so the referee (all six games) leads, the five
    # regulars who kept their place follow, and the substitute is last.
    assert body["members"][0]["position"] == "Referee"
    assert body["members"][0]["games_worked"] == 6
    # Eight people, not seven: the roster is everyone who has EVER worked this
    # crew, which is the referee, the six regulars displaced in week 6, and the
    # substitute who replaced them.
    assert [m["games_worked"] for m in body["members"]] == [6, 5, 5, 5, 5, 5, 5, 1]
    assert body["members"][-1]["official_id"] == "90703"


def test_the_roster_deduplicates_by_official_id_not_by_name(client):
    """One person, two display forms, one roster entry.

    This is the collector's whole thesis applied to its own output. The real
    2025 feeds spell one official "Ron Torbert" and "Ronald Torbert"; a roster
    keyed on the display string reports him as two people who each worked half
    the season, and the crew looks like it churned when nothing happened.

    Every other fixture in this suite gives each official a unique name that
    matches their id, so dedup-by-name and dedup-by-id agree — and a test built
    on those cannot tell the two apart at all.
    """
    games = season_of()
    for game in games:
        if game.referee_id == "703" and game.week >= 4:
            game.members = tuple(
                (official_id, "Ronald Torbert" if index == 0 else name, position)
                for index, (official_id, name, position) in enumerate(game.crew())
            )
        elif game.referee_id == "703":
            game.members = tuple(
                (official_id, "Ron Torbert" if index == 0 else name, position)
                for index, (official_id, name, position) in enumerate(game.crew())
            )
    _refresh_and_wait(client, games)

    body = client.get(f"/crews/{CREW}").json()

    assert len(body["members"]) == 7, [m["name"] for m in body["members"]]
    renamed = [m for m in body["members"] if m["position"] == "Umpire"]
    assert len(renamed) == 1
    assert renamed[0]["games_worked"] == 6


def test_an_unknown_crew_is_404_not_an_empty_profile(client):
    """A consumer that gets an empty profile for a typo'd id files it as "that
    crew has no history" rather than as its own bug."""
    _refresh_and_wait(client)

    response = client.get(f"/crews/{SEASON}-ref9999")

    assert response.status_code == 404
    assert "ref9999" in response.json()["detail"]


def test_a_malformed_crew_id_is_422_not_404(client):
    """ "You asked wrongly" and "there is no such crew" are different answers,
    and collapsing them sends a client looking for a data problem that does not
    exist."""
    for malformed in ("not-a-crew", "2026-ref", "26-ref703", "2026_ref703"):
        response = client.get(f"/crews/{malformed}")
        assert response.status_code == 422, malformed


def test_the_crew_route_answers_before_any_capture_with_404(client):
    """An empty cache is not a malformed request. 404 is right: the collector
    genuinely knows of no such crew yet."""
    assert client.get(f"/crews/{CREW}").status_code == 404


def test_the_crew_route_serves_a_crew_whose_rates_are_absent(client):
    """A crew known only from assignments — play-by-play unavailable — is NOT
    unknown. 200 with a null profile, because "we have no rates for this crew"
    and "there is no such crew" are different answers."""
    import httpx

    games = season_of()
    with upstream_router(games, pbp_response=httpx.Response(404)):
        client.post("/refresh", json={})
        wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get(f"/crews/{CREW}").json()

    assert body["rates"] is None
    assert body["referee_name"] == "Ref Number3"
    assert len(body["members"]) == 7
    assert body["games_assigned"] == 6


def test_the_crew_route_requires_a_token(client):
    """Not exempt. Only `/health` and `/metrics` are, and only because a
    kubelet probe and a Prometheus scrape cannot carry a token."""
    response = client.get(f"/crews/{CREW}", headers={"Authorization": ""})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


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
