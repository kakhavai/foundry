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


def _envelope_with_status(captured_at: str, rows: list[tuple[str, str]]) -> dict:
    """Like `_envelope`, but each row also carries `membership_status`."""
    return {
        "captured_at": captured_at,
        "signals": [
            {"player_id": pid, "membership_status": status} for pid, status in rows
        ],
    }


class WeekAwareFakeLake:
    """Like `FakeLake`, but `list_keys` actually respects the `week` it is
    given -- `FakeLake` returns the same fixed key list regardless of which
    week is asked for, which cannot exercise a `week - 1` fallback."""

    def __init__(self, keys_by_week: dict[int, list[str]], objects: dict):
        self._keys_by_week = keys_by_week
        self._objects = objects
        self.list_calls = []

    def list_keys(self, collector, signal_type, season, week, version="1"):
        self.list_calls.append((collector, signal_type, season, week))
        return list(self._keys_by_week.get(week, []))

    def read(self, key):
        return self._objects[key]

    def write(self, envelope):  # pragma: no cover - unused here
        raise AssertionError("ScopeClient must never write")


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


@pytest.mark.asyncio
async def test_fetch_falls_back_to_last_week_when_this_weeks_partition_is_missing():
    """At every week rollover, `roster-scope`'s weekly capture has not landed
    yet for consumers running hourly-to-volatile. Without this fallback
    every collector fails closed for the whole gap even though last week's
    scope is sitting right there in the lake and is ~99% correct."""
    week4_key = (
        "signals/roster-scope/v1/season=2026/week=04/"
        "2026-09-23T00:00:00Z-scope_membership_weekly.json"
    )
    lake = WeekAwareFakeLake(
        keys_by_week={4: [week4_key]},
        objects={week4_key: _envelope("2026-09-23T00:00:00Z", ["fdy-a"])},
    )

    scope = await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 5)

    assert scope.members == frozenset({"fdy-a"})
    assert scope.captured_at == datetime(2026, 9, 23, tzinfo=UTC)
    assert lake.list_calls == [
        ("roster-scope", "scope_membership_weekly", 2026, 5),
        ("roster-scope", "scope_membership_weekly", 2026, 4),
    ]


@pytest.mark.asyncio
async def test_fetch_falls_back_when_this_weeks_envelope_is_empty():
    """A total capture failure for the current week still writes a
    `present: 0` envelope (`roster_scope.capture.capture_scope`'s
    ledger-unavailable path) rather than nothing at all -- that must fall
    back exactly like a missing partition does, or the fallback never fires
    for the failure mode it exists for."""
    week5_key = (
        "signals/roster-scope/v1/season=2026/week=05/"
        "2026-09-30T00:00:00Z-scope_membership_weekly.json"
    )
    week4_key = (
        "signals/roster-scope/v1/season=2026/week=04/"
        "2026-09-23T00:00:00Z-scope_membership_weekly.json"
    )
    lake = WeekAwareFakeLake(
        keys_by_week={5: [week5_key], 4: [week4_key]},
        objects={
            week5_key: _envelope("2026-09-30T00:00:00Z", []),
            week4_key: _envelope("2026-09-23T00:00:00Z", ["fdy-a"]),
        },
    )

    scope = await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 5)

    assert scope.members == frozenset({"fdy-a"})


@pytest.mark.asyncio
async def test_fetch_does_not_fall_back_past_one_week():
    """Two weeks stale is a different judgement than one -- the fallback
    stops at `week - 1` and must raise rather than reaching for `week - 2`,
    even though a usable scope exists there too."""
    week3_key = (
        "signals/roster-scope/v1/season=2026/week=03/"
        "2026-09-16T00:00:00Z-scope_membership_weekly.json"
    )
    lake = WeekAwareFakeLake(
        keys_by_week={3: [week3_key]},
        objects={week3_key: _envelope("2026-09-16T00:00:00Z", ["fdy-old"])},
    )

    with pytest.raises(ScopeUnavailable) as excinfo:
        await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 5)

    assert excinfo.value.reason == "scope_unavailable"
    assert lake.list_calls == [
        ("roster-scope", "scope_membership_weekly", 2026, 5),
        ("roster-scope", "scope_membership_weekly", 2026, 4),
    ]


@pytest.mark.asyncio
async def test_fetch_never_asks_the_lake_for_week_zero():
    """Week 1's fallback would be week 0, which cannot exist -- the loop
    must skip it rather than issuing a nonsensical `list_keys` call."""
    lake = WeekAwareFakeLake(keys_by_week={}, objects={})

    with pytest.raises(ScopeUnavailable):
        await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 1)

    assert lake.list_calls == [("roster-scope", "scope_membership_weekly", 2026, 1)]


@pytest.mark.asyncio
async def test_fetch_raised_reason_describes_the_requested_week_not_the_fallback():
    """`week`'s own envelope is empty (a real capture that resolved nothing);
    `week - 1` has no partition at all. Both fail, but the reason reported
    must describe what happened to the week the caller actually asked
    about."""
    week5_key = (
        "signals/roster-scope/v1/season=2026/week=05/"
        "2026-09-30T00:00:00Z-scope_membership_weekly.json"
    )
    lake = WeekAwareFakeLake(
        keys_by_week={5: [week5_key]},
        objects={week5_key: _envelope("2026-09-30T00:00:00Z", [])},
    )

    with pytest.raises(ScopeUnavailable) as excinfo:
        await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 5)

    assert excinfo.value.reason == "scope_empty"


@pytest.mark.asyncio
async def test_fetch_excludes_departed_players_but_keeps_grace():
    """`grace` is intended -- it is the "keep fetching for someone who just
    fell out" state `GRACE_WEEKS` exists for. `excluded` is not:
    `_carry_forward` emits it exactly once, to ANNOUNCE a departure, and a
    collector that kept it in `members` would keep fetching for a player who
    is gone."""
    key = (
        "signals/roster-scope/v1/season=2026/week=01/"
        "2026-09-02T00:00:00Z-scope_membership_weekly.json"
    )
    lake = FakeLake(
        keys=[key],
        objects={
            key: _envelope_with_status(
                "2026-09-02T00:00:00Z",
                [
                    ("fdy-active", "active"),
                    ("fdy-grace", "grace"),
                    ("fdy-gone", "excluded"),
                ],
            )
        },
    )

    scope = await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 1)

    assert scope.members == frozenset({"fdy-active", "fdy-grace"})


@pytest.mark.asyncio
async def test_fetch_keeps_rows_with_no_membership_status_at_all():
    """`scope_matchup_weekly` rows carry no `membership_status` field --
    the exclusion filter must not treat their absence as a reason to drop
    them."""
    key = (
        "signals/roster-scope/v1/season=2026/week=04/"
        "2026-09-30T00:00:00Z-scope_matchup_weekly.json"
    )
    lake = FakeLake(
        keys=[key], objects={key: _envelope("2026-09-30T00:00:00Z", ["fdy-x"])}
    )

    scope = await ScopeClient(lake).fetch("scope_matchup_weekly", 2026, 4)

    assert scope.members == frozenset({"fdy-x"})


def test_age_seconds_measures_from_captured_at():
    scope = Scope(
        members=frozenset({"fdy-a"}),
        captured_at=datetime(2026, 9, 2, 0, 0, 0, tzinfo=UTC),
        signal_type="scope_membership_weekly",
    )
    assert scope.age_seconds(datetime(2026, 9, 2, 1, 0, 0, tzinfo=UTC)) == 3600.0


class SignalTypeAwareFakeLake:
    """Like `WeekAwareFakeLake`, but keyed by `signal_type` as well as `week`.

    `fetch_union` calls `fetch` once per signal type against the *same*
    partition, and both existing fakes return one fixed key list regardless
    of which signal type is asked for -- neither can exercise a real union
    across two distinct lists living in the same season/week. Like
    `WeekAwareFakeLake`, `season` is accepted but ignored -- these tests only
    ever exercise one season.
    """

    def __init__(self, keys_by_partition=None, objects=None):
        self._keys_by_partition = keys_by_partition or {}
        self._objects = objects or {}
        self.list_calls = []

    def list_keys(self, collector, signal_type, season, week, version="1"):
        self.list_calls.append((collector, signal_type, season, week))
        return list(self._keys_by_partition.get((signal_type, week), []))

    def read(self, key):
        return self._objects[key]

    def write(self, envelope):  # pragma: no cover - unused here
        raise AssertionError("ScopeClient must never write")


@pytest.fixture
def lake():
    return SignalTypeAwareFakeLake()


def _seed(
    lake,
    signal_type: str,
    season: int,
    week: int,
    player_ids: list[str],
    captured_at: str = "2026-09-02T00:00:00Z",
) -> None:
    """Write one envelope for `signal_type`/`season`/`week` into `lake`."""
    key = (
        f"signals/roster-scope/v1/season={season}/week={week:02d}/"
        f"{captured_at}-{signal_type}.json"
    )
    lake._keys_by_partition.setdefault((signal_type, week), []).append(key)
    lake._objects[key] = _envelope(captured_at, player_ids)


@pytest.mark.asyncio
async def test_fetch_union_returns_every_members_set_combined(lake):
    """injury-report needs offensive watchlist AND matchup defenders."""
    _seed(lake, "scope_membership_weekly", 2026, 1, ["fdy-a", "fdy-b"])
    _seed(lake, "scope_matchup_weekly", 2026, 1, ["fdy-c"])

    scope = await ScopeClient(lake).fetch_union(
        ("scope_membership_weekly", "scope_matchup_weekly"), 2026, 1
    )

    assert scope.members == frozenset({"fdy-a", "fdy-b", "fdy-c"})
    assert len(scope.members) == 3


@pytest.mark.asyncio
async def test_fetch_union_fails_closed_when_any_signal_type_is_missing(lake):
    """Strictly all-or-nothing. A present membership list with an absent
    matchup list would narrow to offence only and silently drop every
    defender -- a partial scope that looks like a working one."""
    _seed(lake, "scope_membership_weekly", 2026, 1, ["fdy-a"])

    with pytest.raises(ScopeUnavailable):
        await ScopeClient(lake).fetch_union(
            ("scope_membership_weekly", "scope_matchup_weekly"), 2026, 1
        )


@pytest.mark.asyncio
async def test_fetch_union_reports_the_oldest_contributing_capture(lake):
    """`age_seconds` must describe the STALEST input, not the freshest --
    otherwise a fresh membership list hides a week-old matchup list."""
    _seed(
        lake,
        "scope_membership_weekly",
        2026,
        1,
        ["fdy-a"],
        captured_at="2026-09-10T00:00:00Z",
    )
    _seed(
        lake,
        "scope_matchup_weekly",
        2026,
        1,
        ["fdy-c"],
        captured_at="2026-09-03T00:00:00Z",
    )

    scope = await ScopeClient(lake).fetch_union(
        ("scope_membership_weekly", "scope_matchup_weekly"), 2026, 1
    )

    assert scope.captured_at.isoformat().startswith("2026-09-03")


@pytest.mark.asyncio
async def test_fetch_union_with_no_signal_types_fails_closed(lake):
    """An empty union has no contributing envelope to be stale or fresh --
    returning an empty `Scope` here would be indistinguishable from a
    caller-error fail-open, not a legitimately narrowed-to-nothing scope."""
    with pytest.raises(ScopeUnavailable):
        await ScopeClient(lake).fetch_union((), 2026, 1)
