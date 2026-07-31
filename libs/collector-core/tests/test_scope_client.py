from datetime import UTC, datetime

import pytest

from collector_core.scope import (
    Scope,
    ScopeClient,
    ScopeUnavailable,
    fetch_scope_or_fail,
)


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


# --- fetch_scope_or_fail: both refusal arms, once, for the whole fleet -------
#
# The two-arm pattern lived in three near-identical ~35-line comment blocks in
# three collectors. What gets copied at twenty-six collectors is one of the
# three, and the copy that drops the SECOND arm fails silently: the exception
# escapes the capture coroutine, no `present: 0` envelope is written, and
# `collector_capture_failures_total` never moves.


class _RecordingLake:
    """Records the failure envelopes `fail_capture` writes."""

    def __init__(self) -> None:
        self.writes = []

    def write(self, envelope):
        self.writes.append(envelope)
        return "key"


class _RecordingMetrics:
    def __init__(self) -> None:
        self.failures: list[tuple[BaseException, str | None]] = []
        self.coverage_calls: list[tuple[str, float]] = []

    def capture_failure(self, exc, reason=None):
        self.failures.append((exc, reason))

    def coverage(self, signal_type, ratio):
        self.coverage_calls.append((signal_type, ratio))


def _context(lake, metrics, **overrides):
    context = dict(
        collector="fake",
        signal_types=("alpha", "beta"),
        adapter="fake-adapter",
        now=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        scope={"season": 2026, "week": 1},
        lake=lake,
        metrics=metrics,
        expected={"alpha": 384, "beta": 384},
    )
    context.update(overrides)
    return context


async def test_it_returns_whatever_the_fetch_returned():
    """Generic on purpose: `injury-report` hands back a `Scope`,
    `player-stats` a `frozenset`, `usage-share` a `(client, Scope)` tuple.
    A signature pinned to `Scope` would have fitted one of the three."""
    lake, metrics = _RecordingLake(), _RecordingMetrics()

    async def fetch():
        return ("identity-client", "the-scope")

    result = await fetch_scope_or_fail(fetch, **_context(lake, metrics))

    assert result == ("identity-client", "the-scope")
    assert lake.writes == [], "a successful fetch must write no failure envelope"
    assert metrics.failures == []


async def test_scope_unavailable_forwards_its_own_reason():
    """`scope_unavailable`, `scope_empty` and a collector's own
    `identity_unavailable` have three different fixes. Flattening them to one
    literal costs an operator the only thing the envelope could have told
    them — and now the Prometheus label too."""
    lake, metrics = _RecordingLake(), _RecordingMetrics()

    async def fetch():
        raise ScopeUnavailable("scope_empty")

    with pytest.raises(ScopeUnavailable):
        await fetch_scope_or_fail(fetch, **_context(lake, metrics))

    assert len(lake.writes) == 2
    assert {e.errors[0]["reason"] for e in lake.writes} == {"scope_empty"}
    assert [reason for _, reason in metrics.failures] == ["scope_empty"]


async def test_a_synchronous_raise_before_the_first_await_is_caught_too():
    """Why the parameter is a callable rather than an awaitable.
    `usage-share`'s `build_identity_client` raises `ScopeUnavailable`
    synchronously when `PLAYER_IDENTITY_URL` is empty — the config half of
    failing closed, and the one an awaitable-shaped signature would miss."""
    lake, metrics = _RecordingLake(), _RecordingMetrics()

    async def fetch():
        raise ScopeUnavailable("identity_unavailable")

    with pytest.raises(ScopeUnavailable):
        await fetch_scope_or_fail(fetch, **_context(lake, metrics))

    assert {e.errors[0]["reason"] for e in lake.writes} == {"identity_unavailable"}


async def test_a_lake_that_fails_outright_still_writes_an_envelope():
    """THE arm. `ScopeUnavailable` is only what `ScopeClient` raises when the
    lake ANSWERED and held nothing usable; botocore errors, JSON decode
    failures and an unparseable `captured_at` propagate untouched. Without the
    second arm they escape the capture coroutine entirely."""
    lake, metrics = _RecordingLake(), _RecordingMetrics()

    async def fetch():
        raise RuntimeError("list_objects_v2: endpoint is unreachable")

    with pytest.raises(RuntimeError):
        await fetch_scope_or_fail(fetch, **_context(lake, metrics))

    assert len(lake.writes) == 2
    for envelope in lake.writes:
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == 384
        assert envelope.coverage.ratio == 0.0
    # Classified, and deliberately NOT mistakable for an absent scope.
    assert {e.errors[0]["reason"] for e in lake.writes} == {"unknown"}
    assert [reason for _, reason in metrics.failures] == [None]


async def test_a_malformed_lake_object_is_classified_not_flattened():
    """`ScopeClient._parse_captured_at` raises `ValueError` on a timestamp it
    does not recognise, which the shared classifier reads as `malformed` — a
    true statement, and one that cannot be confused with `scope_unavailable`."""
    lake, metrics = _RecordingLake(), _RecordingMetrics()

    async def fetch():
        raise ValueError("time data does not match format")

    with pytest.raises(ValueError):
        await fetch_scope_or_fail(fetch, **_context(lake, metrics))

    assert {e.errors[0]["reason"] for e in lake.writes} == {"malformed"}


async def test_the_original_exception_is_re_raised_unchanged():
    """`fail_capture` re-raises so `CaptureState` never installs an empty
    capture over the last good one. The helper must not swallow that."""
    lake, metrics = _RecordingLake(), _RecordingMetrics()
    boom = ScopeUnavailable("scope_unavailable")

    async def fetch():
        raise boom

    with pytest.raises(ScopeUnavailable) as caught:
        await fetch_scope_or_fail(fetch, **_context(lake, metrics))

    assert caught.value is boom
