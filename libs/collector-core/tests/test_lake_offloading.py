"""The lake must never block the event loop.

`LakeWriter` is boto3, which is synchronous. Calling it from a coroutine runs
it on the event loop thread and blocks the whole process -- including
`/health`. That is what broke `roster-scope`'s first deploy: the ledger read
was the first statement of the lifespan-started coroutine with no `await`
ahead of it, so uvicorn never began serving, the readiness probe never passed,
and `kubectl rollout status` timed out at 180s. Readiness became gated on
object-store latency, which inverts the collector contract's promise that an
upstream outage degrades *freshness*, not *availability*.

`weather` was safe only by accident of statement ordering. These tests pin the
fix as an invariant rather than an ordering nobody can see, against a fake
collector -- no real service is involved.
"""

import asyncio
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from collector_core.app import CollectorDescriptor, build_collector_app
from collector_core.cadence import CadenceClass
from collector_core.envelope import Coverage, Envelope, Upstream
from collector_core.lake import EventLoopGuardedLake, alist_keys, aread, awrite
from collector_core.metrics import CollectorMetrics

NOW = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)

# How long a blackholed object store "hangs" for. botocore's real worst case is
# a 60-second connect timeout, retried; a second is enough to prove the
# property without making the suite slow.
BLACKHOLE_SECONDS = 1.0


def _envelope() -> Envelope:
    return Envelope(
        envelope_version="1",
        collector="fake",
        signal_type="alpha",
        captured_at=NOW,
        upstream=Upstream(adapter="fake-adapter", fetched_at=NOW),
        scope={"season": 2026, "week": 1},
        coverage=Coverage(expected=1, present=1, missing=[]),
        errors=[],
        signals=[],
    )


class _BlackholedLake:
    """Stands in for an unreachable object store: every call hangs.

    A hang, not an exception -- an endpoint that refuses connections fails
    fast and was never the dangerous case. The one that gated readiness was
    an endpoint that simply never answered.
    """

    def __init__(self, seconds: float = BLACKHOLE_SECONDS) -> None:
        self.seconds = seconds
        self.threads: list[int] = []

    def _hang(self) -> None:
        self.threads.append(threading.get_ident())
        time.sleep(self.seconds)

    def write(self, envelope) -> str:
        self._hang()
        return "key"

    def list_keys(self, collector, signal_type, season, week, version="1"):
        self._hang()
        return []

    def read(self, key):
        self._hang()
        return {}


# --- the async accessors run off the loop ------------------------------------


async def test_awrite_does_not_run_on_the_event_loop_thread():
    lake = _BlackholedLake(seconds=0.0)
    loop_thread = threading.get_ident()

    await awrite(lake, _envelope())

    assert lake.threads == [t for t in lake.threads if t != loop_thread]
    assert len(lake.threads) == 1


async def test_alist_keys_and_aread_also_run_off_the_loop():
    lake = _BlackholedLake(seconds=0.0)
    loop_thread = threading.get_ident()

    await alist_keys(lake, "fake", "alpha", 2026, 1)
    await aread(lake, "some/key.json")

    assert len(lake.threads) == 2
    assert all(tid != loop_thread for tid in lake.threads)


async def test_a_blackholed_lake_does_not_stall_other_coroutines():
    """The behavioural version: while a write hangs for a second, another
    coroutine must still be scheduled promptly."""
    lake = _BlackholedLake()

    writing = asyncio.create_task(awrite(lake, _envelope()))
    await asyncio.sleep(0.05)

    started = asyncio.get_running_loop().time()
    await asyncio.sleep(0)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < BLACKHOLE_SECONDS / 2
    await writing


# --- the guard ---------------------------------------------------------------


def test_the_guard_delegates_normally_from_synchronous_code():
    """Ordinary sync code, including every existing test, has no running loop
    and must be entirely unaffected."""
    lake = _BlackholedLake(seconds=0.0)
    guarded = EventLoopGuardedLake(lake)

    assert guarded.write(_envelope()) == "key"
    assert guarded.list_keys("fake", "alpha", 2026, 1) == []
    assert guarded.read("k") == {}


async def test_the_guard_refuses_a_synchronous_write_from_a_coroutine():
    """The bug this exists to make impossible to reintroduce. Twenty-four more
    collectors are about to be written against this library; a convention they
    must each remember is not an invariant."""
    guarded = EventLoopGuardedLake(_BlackholedLake(seconds=0.0))

    with pytest.raises(RuntimeError, match="event loop thread"):
        guarded.write(_envelope())


async def test_the_guard_refuses_synchronous_reads_and_listings_too():
    guarded = EventLoopGuardedLake(_BlackholedLake(seconds=0.0))

    with pytest.raises(RuntimeError, match="event loop thread"):
        guarded.list_keys("fake", "alpha", 2026, 1)
    with pytest.raises(RuntimeError, match="event loop thread"):
        guarded.read("k")


async def test_the_guard_permits_the_async_accessors():
    """`awrite` and friends reach the inner writer through `to_thread`, where
    there is no running loop -- so the guard must not fire on the very path it
    exists to steer callers onto."""
    lake = _BlackholedLake(seconds=0.0)
    guarded = EventLoopGuardedLake(lake)

    assert await awrite(guarded, _envelope()) == "key"
    assert await alist_keys(guarded, "fake", "alpha", 2026, 1) == []
    assert await aread(guarded, "k") == {}


async def test_the_guard_permits_a_whole_sync_helper_offloaded_in_one_go():
    """weather's `/signals/convergence` offloads its whole helper rather than
    awaiting each call, because it does one `list_keys` plus one `read` per
    snapshot."""
    guarded = EventLoopGuardedLake(_BlackholedLake(seconds=0.0))

    def helper(lake):
        return [lake.read(key) for key in lake.list_keys("fake", "alpha", 2026, 1)]

    assert await asyncio.to_thread(helper, guarded) == []


# --- build_collector_app wires the guard in ----------------------------------


def _descriptor(**overrides) -> CollectorDescriptor:
    base = dict(
        name="fake",
        cadence_class=CadenceClass.VOLATILE,
        signal_types=("alpha",),
        supported_filters=("season", "week", "signal_type"),
        capture=_never_called,
        signal_matches=lambda row, params: True,
        metrics=CollectorMetrics("lake-offloading-fake"),
    )
    base.update(overrides)
    return CollectorDescriptor(**base)


async def _never_called(season, week, *, client, lake, now, deadline=None):
    return {}


def test_every_collector_gets_a_guarded_lake():
    spec = build_collector_app(_descriptor()).state.collector_spec
    assert isinstance(spec.lake, EventLoopGuardedLake)


def test_health_answers_promptly_while_a_capture_hammers_a_blackholed_lake(
    monkeypatch,
):
    """The end-to-end property, and the one that actually broke a deploy: a
    collector whose lake never answers must still pass its readiness probe.

    `/health` answering here is not incidental -- it is the readiness probe,
    and an unanswered probe is `CrashLoopBackOff` regardless of how healthy
    the process really is. Note that `/health` is exempt from bearer auth for
    exactly this reason, while `/refresh` below is not.
    """
    monkeypatch.setenv("COLLECTOR_TOKEN", "s3cr3t")
    lake = _BlackholedLake(seconds=BLACKHOLE_SECONDS)
    captures = threading.Event()

    async def hammering_capture(season, week, *, client, lake, now, deadline=None):
        captures.set()
        # Exactly what a collector's capture does at the end of a pass.
        await awrite(lake, _envelope())
        return {}

    app = build_collector_app(_descriptor(capture=hammering_capture))
    # Point the app at the blackholed writer, keeping the guard in place so
    # this exercises the real wiring rather than a bypass.
    app.state.collector_spec.lake = EventLoopGuardedLake(lake)

    with TestClient(app) as client:
        # Dispatch a capture the same way `POST /refresh` does, then prove
        # `/health` is still answered while the lake hangs.
        dispatched = client.post("/refresh", headers={"Authorization": "Bearer s3cr3t"})
        assert dispatched.status_code == 202
        assert captures.wait(timeout=5.0)

        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert elapsed < BLACKHOLE_SECONDS / 2, (
        f"/health took {elapsed:.3f}s while the lake hung for "
        f"{BLACKHOLE_SECONDS}s -- readiness is gated on object-store latency"
    )
    assert lake.threads, "the blackholed lake was never actually reached"
