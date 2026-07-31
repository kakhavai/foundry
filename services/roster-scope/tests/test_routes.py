"""The five shared routes, plus roster-scope's own three.

Route tests pre-populate the cache directly, per the repo's convention — the
routes never call an upstream.
"""

from roster_scope.matchups import MATCHUP_SIGNAL
from roster_scope.scope import CHANGE_SIGNAL, MEMBERSHIP_SIGNAL


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_prometheus_text(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "# HELP" in response.text


def test_catalog_describes_the_collector(client, seeded_state):
    body = client.get("/catalog").json()
    assert body["collector"] == "roster-scope"
    assert body["envelope_version"] == "1"
    assert body["cadence_class"] == "weekly"
    assert set(body["signal_types"]) == {
        MEMBERSHIP_SIGNAL,
        CHANGE_SIGNAL,
        MATCHUP_SIGNAL,
    }
    assert "player_id" in body["filters"]
    assert body["last_capture_at"] == "2026-09-15T12:00:00Z"
    assert body["coverage"][MEMBERSHIP_SIGNAL]["expected"] == 3


def test_catalog_agrees_with_the_registry_entry(client):
    """CI asserts a deployed `/catalog` matches `contracts/collector-registry.yaml`.
    Pinning it here means the mismatch surfaces in the unit suite first."""
    import pathlib

    import yaml

    registry = yaml.safe_load(
        (
            pathlib.Path(__file__).resolve().parents[3]
            / "contracts"
            / "collector-registry.yaml"
        ).read_text()
    )
    entry = next(c for c in registry["collectors"] if c["name"] == "roster-scope")
    body = client.get("/catalog").json()
    assert entry["cadence_class"] == body["cadence_class"]
    # Exact, type included: the registry stores the STRING "1", matching
    # ENVELOPE_VERSION and /catalog. The str() that used to be here was a
    # workaround for an undecided type; see contracts/collector-registry.yaml.
    assert entry["envelope_version"] == body["envelope_version"]
    assert set(entry["signal_types"]) == set(body["signal_types"])


def test_signals_returns_both_envelopes(client, seeded_state):
    body = client.get("/signals").json()
    assert body["count"] == 2
    assert {e["signal_type"] for e in body["envelopes"]} == {
        MEMBERSHIP_SIGNAL,
        CHANGE_SIGNAL,
    }


def test_signals_filters_by_collector_specific_fields(client, seeded_state):
    body = client.get(f"/signals?signal_type={MEMBERSHIP_SIGNAL}&team=KC").json()
    rows = body["envelopes"][0]["signals"]
    assert {r["team"] for r in rows} == {"KC"}
    assert len(rows) == 2


def test_signals_filters_by_membership_status(client, seeded_state):
    body = client.get(
        f"/signals?signal_type={MEMBERSHIP_SIGNAL}&membership_status=grace"
    ).json()
    assert [r["player_id"] for r in body["envelopes"][0]["signals"]] == ["fdy-ccc"]


def test_signals_rejects_an_undeclared_filter(client, seeded_state):
    """A client bug must surface as a loud error, not look like a quiet week."""
    assert client.get("/signals?jersey_number=15").status_code == 422


def test_signals_rejects_an_unknown_signal_type(client, seeded_state):
    assert client.get("/signals?signal_type=nonsense").status_code == 422


def test_signals_filters_by_scope(client, seeded_state):
    assert client.get("/signals?season=2025").json()["count"] == 0
    assert client.get("/signals?season=2026&week=1").json()["count"] == 2


def test_refresh_is_accepted_then_floored(client):
    first = client.post("/refresh", json={"season": 2026, "week": 1})
    assert first.status_code == 202
    assert first.json()["refresh_id"]
    second = client.post("/refresh", json={"season": 2026, "week": 1})
    assert second.status_code == 429
    assert "Retry-After" in second.headers


# --------------------------------------------------------------------------
# roster-scope's own routes
# --------------------------------------------------------------------------


def test_scope_players_defaults_to_the_latest_version(client, seeded_state):
    body = client.get("/scope/players").json()
    assert body["scope_version"] == 3
    assert body["count"] == 3


def test_scope_players_filters_by_status(client, seeded_state):
    body = client.get("/scope/players?status=active").json()
    assert body["count"] == 2
    assert all(p["membership_status"] == "active" for p in body["players"])


def test_scope_players_accepts_the_current_version_explicitly(client, seeded_state):
    assert client.get("/scope/players?scope_version=3").json()["count"] == 3


def test_scope_players_404s_for_a_version_the_lake_does_not_hold(client, seeded_state):
    """A consumer pinning a version it cannot get must be told so, not handed
    a different version's list."""
    assert client.get("/scope/players?scope_version=99").status_code == 404


def test_scope_players_is_empty_before_the_first_capture(client):
    body = client.get("/scope/players").json()
    assert body == {"scope_version": None, "players": [], "count": 0}


def test_scope_players_serves_an_older_version_from_the_lake(
    client, seeded_state, lake_backed
):
    """Consumers pin a version for a whole fetch cycle, so a version that has
    already rolled off memory must still be answerable."""
    body = client.get("/scope/players?scope_version=1").json()
    assert body["scope_version"] == 1
    assert [p["player_id"] for p in body["players"]] == ["fdy-historic"]


def test_scope_players_404s_for_a_version_the_lake_does_not_hold_either(
    client, seeded_state, lake_backed
):
    assert client.get("/scope/players?scope_version=2").status_code == 404


def test_scope_diff_prefers_the_lake_over_this_process_memory(
    client, seeded_state, lake_backed
):
    """A process that restarted holds only its own captures; the lake holds
    the history the route is actually asked about."""
    body = client.get("/scope/diff?from=0&to=1").json()
    assert [t["player_id"] for t in body["transitions"]] == ["fdy-historic"]


def test_scope_rules_reports_the_config_as_applied(client, seeded_state):
    body = client.get("/scope/rules").json()
    assert body["teams"] == 32
    assert body["grace_weeks"] == 2
    rules = {r["rule_id"]: r for r in body["rules"]}
    assert rules["wr_depth_le_4"]["slots_expected"] == 128
    assert rules["wr_depth_le_4"]["slots_filled"] == 2
    assert rules["wr_depth_le_4"]["produced_zero_slots"] is False
    # The whole reason this route exists: a rule matching nothing is otherwise
    # indistinguishable from a rule that is working.
    assert rules["all_team_defenses"]["produced_zero_slots"] is True


def test_scope_rules_lists_every_configured_rule(client):
    body = client.get("/scope/rules").json()
    assert {r["rule_id"] for r in body["rules"]} == {
        "qb_depth_le_2",
        "rb_depth_le_3",
        "wr_depth_le_4",
        "te_depth_le_2",
        "all_kickers",
        "all_team_defenses",
    }
    assert all(r["produced_zero_slots"] for r in body["rules"])


def test_scope_diff_returns_transitions_in_the_half_open_range(client, seeded_state):
    body = client.get("/scope/diff?from=1&to=3").json()
    assert body["count"] == 2
    assert [t["scope_version"] for t in body["transitions"]] == [2, 3]


def test_scope_diff_excludes_the_from_version_itself(client, seeded_state):
    """`from=2` answers "what changed since I pinned version 2", which must
    not re-report the events that produced version 2."""
    body = client.get("/scope/diff?from=2&to=3").json()
    assert [t["scope_version"] for t in body["transitions"]] == [3]


def test_scope_diff_rejects_an_inverted_range(client, seeded_state):
    assert client.get("/scope/diff?from=5&to=2").status_code == 422


def test_scope_diff_requires_both_bounds(client, seeded_state):
    assert client.get("/scope/diff?from=1").status_code == 422
    assert client.get("/scope/diff").status_code == 422


def test_scope_matchups_returns_the_matchup_slots(client, seeded_matchup_state):
    response = client.get("/scope/matchups")
    assert response.status_code == 200
    body = response.json()
    assert "slots" in body
    assert "count" in body
    assert body["count"] == len(body["slots"])
    assert body["season"] == 2026
    assert body["week"] == 1
    assert body["captured_at"] == "2026-09-15T12:00:00Z"
    assert {s["player_id"] for s in body["slots"]} == {"fdy-corner-1", "fdy-corner-2"}


def test_scope_matchups_is_empty_before_the_first_capture(client):
    """Mirrors `test_scope_players_is_empty_before_the_first_capture`: a
    well-shaped body, not a 404 or a `null`, even with nothing captured yet."""
    body = client.get("/scope/matchups").json()
    assert body == {
        "season": 2026,
        "week": 1,
        "slots": [],
        "count": 0,
        "captured_at": None,
    }
