"""The five standard routes plus the three resolve routes, over HTTP."""

from conftest import STAMP

from player_identity.resolution import MAX_BATCH_QUERIES


def test_health_is_unauthenticated_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_catalog_describes_the_collector(client, seeded_state):
    body = client.get("/catalog").json()

    assert body["collector"] == "player-identity"
    assert body["envelope_version"] == "1"
    assert body["cadence_class"] == "seasonal"
    assert set(body["signal_types"]) == {
        "player_identity_crosswalk",
        "name_resolution_miss",
    }
    assert set(body["coverage"]) == set(body["signal_types"])
    assert body["last_capture_at"] == STAMP


def test_signals_returns_the_cached_envelopes(client, seeded_state):
    body = client.get("/signals").json()
    assert body["count"] == 2


def test_signals_filters_by_player_id(client, seeded_state):
    body = client.get("/signals?signal_type=player_identity_crosswalk").json()
    rows = body["envelopes"][0]["signals"]
    assert len(rows) == 2

    filtered = client.get(
        "/signals?signal_type=player_identity_crosswalk&player_id=fdy-000000000002"
    ).json()
    assert [r["player_id"] for r in filtered["envelopes"][0]["signals"]] == [
        "fdy-000000000002"
    ]


def test_signals_filters_by_team_and_position(client, seeded_state):
    body = client.get("/signals?signal_type=player_identity_crosswalk&team=LAR").json()
    assert [r["team"] for r in body["envelopes"][0]["signals"]] == ["LAR"]


def test_an_unsupported_filter_is_422_not_silently_ignored(client, seeded_state):
    assert client.get("/signals?nickname=CMC").status_code == 422


def test_an_unknown_signal_type_is_422(client, seeded_state):
    assert client.get("/signals?signal_type=nonsense").status_code == 422


def test_resolve_returns_ranked_candidates_with_confidence_and_method(
    client, seeded_state
):
    body = client.get(
        "/resolve?name=Davante%20Adams&team=NYJ&position=WR&jersey_number=17"
    ).json()

    assert body["resolved"] is True
    assert body["player_id"] == "fdy-000000000001"
    assert body["link_method"] == "attribute_score"
    assert round(body["confidence"], 3) == 0.765
    candidate = body["candidates"][0]
    assert 0.0 <= candidate["confidence"] <= 1.0
    assert candidate["link_method"] == "attribute_score"
    assert candidate["disagreeing_attributes"] == ["team"]


def test_resolve_by_crosswalk_id_reports_the_crosswalk_method(client, seeded_state):
    body = client.get("/resolve?source=gsis&source_id=00-0031381").json()

    assert body["resolved"] is True
    assert body["link_method"] == "crosswalk"
    assert body["confidence"] == 1.0
    assert body["candidates"][0]["link_method"] == "crosswalk"


def test_an_unresolvable_name_lands_in_the_miss_queue(client, seeded_state):
    body = client.get("/resolve?name=Nobody%20At%20All&team=LV").json()
    assert body["resolved"] is False
    assert body["player_id"] is None

    unresolved = client.get("/unresolved").json()
    assert unresolved["count"] == 1
    assert unresolved["misses"][0]["raw_name"] == "Nobody At All"
    assert unresolved["misses"][0]["occurrence_count"] == 1


def test_the_miss_queue_is_ordered_by_occurrence_count(client, seeded_state):
    client.get("/resolve?name=Rare%20Miss&team=LV")
    for _ in range(3):
        client.get("/resolve?name=Common%20Miss&team=LV")

    misses = client.get("/unresolved").json()["misses"]
    assert [m["raw_name"] for m in misses] == ["Common Miss", "Rare Miss"]
    assert [m["occurrence_count"] for m in misses] == [3, 1]


def test_unresolved_honours_its_limit(client, seeded_state):
    for name in ("A Miss", "B Miss", "C Miss"):
        client.get(f"/resolve?name={name.replace(' ', '%20')}&team=LV")

    body = client.get("/unresolved?limit=2").json()
    assert body["count"] == 2
    assert body["total"] == 3


def test_a_query_with_nothing_to_match_on_is_422(client, seeded_state):
    """Not an empty list: "you asked nothing" and "no such player" are
    different answers and must not look the same."""
    assert client.get("/resolve").status_code == 422


def test_an_unknown_position_is_422(client, seeded_state):
    assert client.get("/resolve?name=Someone&position=FLEX").status_code == 422


def test_resolve_batch_resolves_a_whole_slate(client, seeded_state):
    body = client.post(
        "/resolve/batch",
        json={
            "queries": [
                {"name": "Davante Adams", "team": "LV", "jersey_number": 17},
                {"name": "Puka Nacua", "team": "LAR", "jersey_number": 12},
                {"name": "Nobody At All", "team": "LV"},
            ]
        },
    ).json()

    assert body["count"] == 3
    assert body["resolved_count"] == 2
    assert body["unresolved_count"] == 1


def test_resolve_batch_accepts_bare_strings(client, seeded_state):
    body = client.post("/resolve/batch", json={"queries": ["Davante Adams"]}).json()
    assert body["count"] == 1


def test_resolve_batch_rejects_more_than_the_maximum(client, seeded_state):
    response = client.post(
        "/resolve/batch",
        json={"queries": [{"name": "x"} for _ in range(MAX_BATCH_QUERIES + 1)]},
    )
    assert response.status_code == 422
    assert str(MAX_BATCH_QUERIES) in response.json()["detail"]


def test_resolve_batch_accepts_exactly_the_maximum(client, seeded_state):
    response = client.post(
        "/resolve/batch",
        json={"queries": [{"name": "x"} for _ in range(MAX_BATCH_QUERIES)]},
    )
    assert response.status_code == 200


def test_resolve_batch_rejects_a_missing_queries_array(client, seeded_state):
    assert client.post("/resolve/batch", json={}).status_code == 422


def test_resolve_batch_rejects_a_non_object_query(client, seeded_state):
    assert client.post("/resolve/batch", json={"queries": [5]}).status_code == 422
