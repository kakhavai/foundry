from datetime import UTC, datetime

import pytest

from collector_core.scope import Scope, ScopeClient, ScopeUnavailable


class FakeLake:
    """Spy lake. Not moto -- CI prunes collector-core's dev deps in services/."""

    def __init__(self, keys=None, objects=None):
        self._keys = keys or []
        self._objects = objects or {}
        self.list_calls = []

    def list_keys(self, collector, signal_type, season, week, version="1"):
        self.list_calls.append((collector, signal_type, season, week))
        return list(self._keys)

    def read(self, key):
        return self._objects[key]

    def write(self, envelope):  # pragma: no cover - unused here
        raise AssertionError("ScopeClient must never write")


def _envelope(captured_at: str, player_ids: list[str]) -> dict:
    return {
        "captured_at": captured_at,
        "signals": [{"player_id": pid} for pid in player_ids],
    }


@pytest.mark.asyncio
async def test_fetch_returns_the_newest_envelopes_members():
    keys = [
        "signals/roster-scope/v1/season=2026/week=01/2026-09-01T00:00:00Z-scope_membership_weekly.json",
        "signals/roster-scope/v1/season=2026/week=01/2026-09-02T00:00:00Z-scope_membership_weekly.json",
    ]
    lake = FakeLake(
        keys=keys,
        objects={
            keys[0]: _envelope("2026-09-01T00:00:00Z", ["fdy-old"]),
            keys[1]: _envelope("2026-09-02T00:00:00Z", ["fdy-a", "fdy-b"]),
        },
    )
    scope = await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 1)

    assert scope.members == frozenset({"fdy-a", "fdy-b"}), scope.members
    assert len(scope.members) == 2
    assert scope.captured_at == datetime(2026, 9, 2, tzinfo=UTC)
    assert scope.signal_type == "scope_membership_weekly"


@pytest.mark.asyncio
async def test_fetch_raises_when_no_scope_has_ever_been_written():
    """Fail closed. An empty scope and a missing scope must not be confusable:
    returning an empty set would narrow every collector to nothing, silently."""
    with pytest.raises(ScopeUnavailable) as excinfo:
        await ScopeClient(FakeLake(keys=[])).fetch("scope_membership_weekly", 2026, 1)

    assert excinfo.value.reason == "scope_unavailable"


@pytest.mark.asyncio
async def test_fetch_raises_rather_than_returning_an_empty_member_set():
    """A written envelope with zero rows is a failed scope capture, not a
    legitimately empty league."""
    key = (
        "signals/roster-scope/v1/season=2026/week=01/"
        "2026-09-02T00:00:00Z-scope_membership_weekly.json"
    )
    lake = FakeLake(keys=[key], objects={key: _envelope("2026-09-02T00:00:00Z", [])})

    with pytest.raises(ScopeUnavailable) as excinfo:
        await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 1)

    assert excinfo.value.reason == "scope_empty"


@pytest.mark.asyncio
async def test_fetch_asks_the_lake_for_the_right_partition():
    key = (
        "signals/roster-scope/v1/season=2026/week=04/"
        "2026-09-30T00:00:00Z-scope_matchup_weekly.json"
    )
    lake = FakeLake(
        keys=[key], objects={key: _envelope("2026-09-30T00:00:00Z", ["fdy-x"])}
    )

    await ScopeClient(lake).fetch("scope_matchup_weekly", 2026, 4)

    assert lake.list_calls == [("roster-scope", "scope_matchup_weekly", 2026, 4)]


def test_age_seconds_measures_from_captured_at():
    scope = Scope(
        members=frozenset({"fdy-a"}),
        captured_at=datetime(2026, 9, 2, 0, 0, 0, tzinfo=UTC),
        signal_type="scope_membership_weekly",
    )
    assert scope.age_seconds(datetime(2026, 9, 2, 1, 0, 0, tzinfo=UTC)) == 3600.0
