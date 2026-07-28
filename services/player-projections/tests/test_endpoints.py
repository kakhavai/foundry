import pytest
from fastapi.testclient import TestClient

from player_projections.main import FORMATS, _empty_cache, _state, app

client = TestClient(app)

QB = {
    "id": "p_allenjosh",
    "name": "Josh Allen",
    "team": "BUF",
    "pos": "QB",
    "rank": 1,
    "proj_points": {"floor": 18.4, "expected": 32.1, "ceiling": 41.7},
}
WR = {
    "id": "p_8f3a21",
    "name": "Deebo Samuel",
    "team": "SF",
    "pos": "WR",
    "rank": 3,
    "proj_points": {"floor": 5.2, "expected": 12.4, "ceiling": 20.1},
}
RB = {
    "id": "p_1c9e04",
    "name": "Christian McCaffrey",
    "team": "SF",
    "pos": "RB",
    "rank": 1,
    "proj_points": {"floor": 11.0, "expected": 21.7, "ceiling": 33.5},
}
DST = {
    "id": "p_9a2f77",
    "name": "Baltimore",
    "team": "BAL",
    "pos": "DST",
    "yahoo_rank": 1,
    "espn_rank": 3,
}


@pytest.fixture(autouse=True)
def reset_state():
    for fmt in FORMATS:
        _state[fmt] = _empty_cache()
    yield
    for fmt in FORMATS:
        _state[fmt] = _empty_cache()


def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_projections_empty_with_no_upstream_data():
    r = client.get("/projections")
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "ppr"
    assert body["projections"] == []
    assert body["count"] == 0
    assert body["last_updated"] is None
    assert body["upstream_healthy"] is False


def test_projections_returns_cached_players():
    _state["ppr"]["projections"] = [QB]
    _state["ppr"]["upstream_healthy"] = True

    r = client.get("/projections")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["projections"][0]["name"] == "Josh Allen"
    assert body["upstream_healthy"] is True


@pytest.mark.parametrize("fmt", FORMATS)
def test_each_format_serves_its_own_cache(fmt):
    """The three documents are cached independently — asking for one scoring
    mode must never return another's rows."""
    for f in FORMATS:
        _state[f]["projections"] = [{**WR, "id": f"p_{f.replace('-', '')}"}]

    body = client.get("/projections", params={"format": fmt}).json()

    assert body["format"] == fmt
    assert body["projections"][0]["id"] == f"p_{fmt.replace('-', '')}"


def test_unknown_format_is_rejected():
    r = client.get("/projections", params={"format": "quarter-ppr"})
    assert r.status_code == 422


def test_pos_filters_to_one_position():
    _state["ppr"]["projections"] = [QB, WR, RB, DST]

    body = client.get("/projections", params={"pos": "WR"}).json()

    assert body["count"] == 1
    assert [p["pos"] for p in body["projections"]] == ["WR"]


def test_pos_accepts_multiple_positions_for_the_flex_lane():
    """FLEX is not stored — the frontend asks for its constituent positions."""
    _state["ppr"]["projections"] = [QB, WR, RB, DST]

    body = client.get("/projections", params={"pos": "RB,WR,TE"}).json()

    assert {p["pos"] for p in body["projections"]} == {"RB", "WR"}
    assert body["count"] == 2


def test_pos_is_case_insensitive_and_tolerates_spaces():
    _state["ppr"]["projections"] = [QB, WR, RB, DST]

    body = client.get("/projections", params={"pos": " rb , wr "}).json()

    assert {p["pos"] for p in body["projections"]} == {"RB", "WR"}


def test_pos_omitted_returns_every_position():
    _state["ppr"]["projections"] = [QB, WR, RB, DST]

    body = client.get("/projections").json()

    assert body["count"] == 4


def test_dst_is_filterable_like_any_other_position():
    """DST rows carry yahoo_rank/espn_rank instead of a projection, but the
    filter is on `pos` alone and does not care."""
    _state["ppr"]["projections"] = [QB, WR, RB, DST]

    body = client.get("/projections", params={"pos": "DST"}).json()

    assert body["count"] == 1
    assert body["projections"][0]["yahoo_rank"] == 1


def test_flex_is_rejected_as_a_position():
    """FLEX is a frontend display lane, not a stored position — asking for it
    directly is a client bug and must not silently return nothing."""
    _state["ppr"]["projections"] = [QB, WR, RB, DST]

    r = client.get("/projections", params={"pos": "FLEX"})

    assert r.status_code == 422
    assert "FLEX" in r.json()["detail"]


def test_unknown_position_is_rejected():
    r = client.get("/projections", params={"pos": "WR,PUNTER"})

    assert r.status_code == 422
    assert "PUNTER" in r.json()["detail"]


def test_empty_pos_is_rejected():
    """`?pos=` with no value is a malformed request, not a request for
    everything — silently returning the full set would hide a client bug."""
    r = client.get("/projections", params={"pos": ""})

    assert r.status_code == 422


def test_filter_preserves_upstream_order():
    _state["ppr"]["projections"] = [
        {**WR, "id": f"p_{i}", "rank": 50 - i} for i in range(10)
    ]

    body = client.get("/projections", params={"pos": "WR"}).json()

    assert [p["rank"] for p in body["projections"]] == list(range(50, 40, -1))


def test_filter_does_not_mutate_the_cache():
    """The handler must slice a copy — a filtered request must not shrink the
    cached document for every later reader."""
    _state["ppr"]["projections"] = [QB, WR, RB, DST]

    client.get("/projections", params={"pos": "WR"})
    body = client.get("/projections").json()

    assert body["count"] == 4
