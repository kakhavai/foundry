"""`GET /signals/return-profile` — the extra route, and its validation.

The spec asks for "the conditional return distribution for one player and one
body part, which is the shape the generator wants when a player is mid-recovery
and a point estimate would hide the variance". So the route is only doing its job
if it publishes a distribution and a population to read it against — a single
median would be the point estimate it exists to replace.

The route is a plain `@app.get` added after `build_collector_app`, reaching the
capture through `app.state.collector_spec`. It also has to be in
`gateway.publicPaths`, or it works in-cluster and 404s at the edge;
`test_helm_values.py` pins that half.
"""

from durability_history import derive
from durability_history.capture import INJURY_HISTORY

from .conftest import BRAVO, CANONICAL_IDS
from .test_routes import refresh_and_wait

PATH = "/signals/return-profile"


def _capture(client):
    refresh_and_wait(client)


def test_an_unknown_body_part_is_422_not_an_empty_distribution(client):
    """An unknown body part and a body part this player has never injured are
    different facts, and a client that gets `count: 0` for a typo files it as
    "he has never hurt that"."""
    _capture(client)
    response = client.get(
        PATH, params={"player_id": CANONICAL_IDS[BRAVO], "body_part": "elbow"}
    )
    assert response.status_code == 422
    assert "hamstring" in response.json()["detail"]


def test_a_missing_required_param_is_422(client):
    _capture(client)
    assert client.get(PATH, params={"body_part": "hamstring"}).status_code == 422
    assert (
        client.get(PATH, params={"player_id": CANONICAL_IDS[BRAVO]}).status_code == 422
    )


def test_before_any_capture_the_route_is_404_not_an_empty_body(client):
    """ "No capture has landed yet" is a different fact from "this player has no
    events at that body part"."""
    response = client.get(
        PATH, params={"player_id": CANONICAL_IDS[BRAVO], "body_part": "hamstring"}
    )
    assert response.status_code == 404
    assert INJURY_HISTORY in response.json()["detail"]


def test_an_unknown_player_is_404(client):
    _capture(client)
    response = client.get(
        PATH, params={"player_id": "fdy-nobody000001", "body_part": "hamstring"}
    )
    assert response.status_code == 404


def test_the_route_requires_a_bearer_token(client):
    """Auth is mounted as middleware, so a route added after
    `build_collector_app` is protected by default — but "by default" is a claim
    worth one assertion, because the whole point of the middleware is that
    nobody has to remember."""
    _capture(client)
    response = client.get(
        PATH,
        params={"player_id": CANONICAL_IDS[BRAVO], "body_part": "hamstring"},
        headers={"Authorization": ""},
    )
    assert response.status_code == 401


def test_it_returns_a_distribution_not_a_point_estimate(client):
    """The reason the route exists. Bravo has two resolved hamstring events."""
    _capture(client)
    body = client.get(
        PATH, params={"player_id": CANONICAL_IDS[BRAVO], "body_part": "hamstring"}
    ).json()

    assert body["collector"] == "durability-history"
    assert body["body_part"] == "hamstring"
    assert body["player"]["distribution"]["count"] == 2
    assert body["player"]["distribution"]["median"] == 9.0
    assert body["player"]["distribution"]["min"] == 9
    assert body["player"]["distribution"]["max"] == 9
    assert len(body["player"]["events"]) == 2
    # The population it is read against, so a two-observation median has
    # something to be compared with.
    assert body["population"]["distribution"]["count"] >= 2
    assert body["population"]["players"] >= 1


def test_it_is_case_insensitive_on_body_part(client):
    _capture(client)
    response = client.get(
        PATH, params={"player_id": CANONICAL_IDS[BRAVO], "body_part": "HAMSTRING"}
    )
    assert response.status_code == 200
    assert response.json()["body_part"] == "hamstring"


def test_a_body_part_the_player_has_never_injured_is_an_empty_distribution(client):
    """200 with `count: 0`, not 404. "We have his history and it contains no
    knee" is an answer; a 404 would read as "we have no history for him"."""
    _capture(client)
    body = client.get(
        PATH, params={"player_id": CANONICAL_IDS[BRAVO], "body_part": "shoulder"}
    ).json()
    assert body["player"]["distribution"]["count"] == 0
    assert body["player"]["events"] == []


def test_unresolved_events_are_counted_rather_than_dropped(client):
    """A player still out is the single most relevant fact when the question is
    "when does he come back". Silently excluding him would make a player who has
    never returned look like one with no history at all."""
    _capture(client)
    from .conftest import DELTA

    body = client.get(
        PATH, params={"player_id": CANONICAL_IDS[DELTA], "body_part": "ankle"}
    ).json()
    assert body["player"]["distribution"]["count"] == 0
    assert body["player"]["unresolved_events"] == 1
    assert len(body["player"]["events"]) == 1


def test_it_publishes_the_sample_floor_and_the_rule(client):
    """A null aggregate on the `/signals` row is otherwise indistinguishable from
    a bug, and `is_recurrence_of` is unreproducible without the window."""
    _capture(client)
    body = client.get(
        PATH, params={"player_id": CANONICAL_IDS[BRAVO], "body_part": "hamstring"}
    ).json()
    assert body["min_sample_events"] == derive.MIN_SAMPLE_EVENTS
    assert body["recurrence_window_days"] == 90
    assert body["history_seasons"] == [2024, 2025, 2026]
