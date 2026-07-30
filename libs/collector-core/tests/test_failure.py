"""Tests for the shared failed-capture path.

Built against fakes -- a spy lake and a spy metrics recorder -- so these prove
the library's own behaviour independent of any real collector. `moto` is
deliberately not used: CI prunes `collector-core`'s dev dependencies inside
`services/`, so a `moto` import passes locally and fails only in CI.

The contract under test is the Phase 8 one: *"A poll that fails writes an
envelope with `coverage.present: 0` and a populated `errors` array. A gap in
the lake is therefore always explicit and never has to be inferred from
absence."*
"""

import asyncio
import threading
from datetime import UTC, datetime

import httpx
import pytest

from collector_core.envelope import Envelope
from collector_core.failure import (
    MAX_DETAIL_CHARS,
    UNKNOWN_EXPECTED_FLOOR,
    fail_capture,
    failure_envelopes,
)

NOW = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)
SIGNAL_TYPES = ("alpha", "beta")
SCOPE = {"season": 2026, "week": 3}


class _SpyLake:
    """Records what was written, and on which thread.

    The thread matters: `LakeWriter` is synchronous boto3, so `fail_capture`
    must reach it through `asyncio.to_thread`. A write arriving on the event
    loop thread is the readiness-inverting bug, not a stylistic preference.
    """

    def __init__(self, fail: bool = False) -> None:
        self.writes: list[Envelope] = []
        self.threads: list[int] = []
        self._fail = fail

    def write(self, envelope: Envelope) -> str:
        self.threads.append(threading.get_ident())
        if self._fail:
            raise OSError("lake unreachable")
        self.writes.append(envelope)
        return "key"

    def list_keys(self, collector, signal_type, season, week, version="1"):
        return []

    def read(self, key):
        raise KeyError(key)


class _SpyMetrics:
    def __init__(self) -> None:
        self.coverage_calls: list[tuple[str, float]] = []

    def coverage(self, signal_type: str, ratio: float) -> None:
        self.coverage_calls.append((signal_type, ratio))


async def _fail(exc, lake, metrics, **overrides):
    kwargs = dict(
        collector="fake",
        signal_types=SIGNAL_TYPES,
        adapter="fake-adapter",
        now=NOW,
        scope=SCOPE,
        lake=lake,
        metrics=metrics,
    )
    kwargs.update(overrides)
    await fail_capture(exc, **kwargs)


# --- the headline property ---------------------------------------------------


async def test_a_total_outage_reports_a_low_coverage_ratio_not_a_perfect_one():
    """THE test this change exists for.

    `Coverage.ratio` returns 1.0 when `expected` is 0 -- correct for a bye
    week that genuinely expects nothing, catastrophic for a capture that
    failed before it could learn what to expect. A total outage that reports
    `collector_coverage_ratio 1.0` is indistinguishable from a healthy
    collector on the one metric built to catch exactly this.
    """
    lake, metrics = _SpyLake(), _SpyMetrics()

    with pytest.raises(httpx.ConnectError):
        await _fail(httpx.ConnectError("upstream down"), lake, metrics)

    assert [ratio for _, ratio in metrics.coverage_calls] == [0.0, 0.0]
    for envelope in lake.writes:
        assert envelope.coverage.ratio == 0.0
        assert envelope.coverage.ratio != 1.0


async def test_expected_is_floored_even_when_a_caller_asks_for_zero():
    """A caller passing `expected: 0` would reintroduce ratio 1.0 on a total
    outage. The floor is enforced by the library, not trusted to callers."""
    lake, metrics = _SpyLake(), _SpyMetrics()

    with pytest.raises(ValueError):
        await _fail(
            ValueError("boom"),
            lake,
            metrics,
            expected=dict.fromkeys(SIGNAL_TYPES, 0),
        )

    for envelope in lake.writes:
        assert envelope.coverage.expected >= UNKNOWN_EXPECTED_FLOOR
        assert envelope.coverage.ratio == 0.0


# --- write ------------------------------------------------------------------


async def test_a_failed_capture_writes_one_envelope_per_signal_type():
    """A gap must be explicit -- `present: 0` with populated errors -- never
    inferred from the absence of an object in an append-only lake."""
    lake, metrics = _SpyLake(), _SpyMetrics()

    with pytest.raises(RuntimeError):
        await _fail(RuntimeError("boom"), lake, metrics)

    assert len(lake.writes) == 2
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        assert envelope.coverage.present == 0
        assert envelope.signals == []
        assert len(envelope.errors) == 1
        assert envelope.errors[0]["detail"] == "RuntimeError: boom"
        assert envelope.scope == SCOPE
        assert envelope.collector == "fake"
        assert envelope.upstream.adapter == "fake-adapter"


async def test_the_failure_reason_is_classified_from_the_exception():
    lake, metrics = _SpyLake(), _SpyMetrics()

    with pytest.raises(httpx.ConnectTimeout):
        await _fail(httpx.ConnectTimeout("slow"), lake, metrics)

    assert {e.errors[0]["reason"] for e in lake.writes} == {"timeout"}


async def test_an_explicit_reason_overrides_the_classifier():
    """`player-identity` labels a schema break `schema`, which is narrower
    than the classifier's `malformed`."""
    lake, metrics = _SpyLake(), _SpyMetrics()

    with pytest.raises(ValueError):
        await _fail(ValueError("shape moved"), lake, metrics, reason="schema")

    assert {e.errors[0]["reason"] for e in lake.writes} == {"schema"}


async def test_a_per_signal_type_floor_is_honoured():
    lake, metrics = _SpyLake(), _SpyMetrics()

    with pytest.raises(RuntimeError):
        await _fail(RuntimeError("boom"), lake, metrics, expected={"alpha": 2900})

    by_type = {e.signal_type: e.coverage.expected for e in lake.writes}
    assert by_type["alpha"] == 2900
    # Unnamed types get the floor rather than 0.
    assert by_type["beta"] == UNKNOWN_EXPECTED_FLOOR


async def test_a_giant_exception_detail_is_bounded():
    """An upstream error can carry a whole response body. One failure must
    not write a multi-megabyte lake object."""
    lake, metrics = _SpyLake(), _SpyMetrics()

    with pytest.raises(ValueError):
        await _fail(ValueError("x" * 50_000), lake, metrics)

    for envelope in lake.writes:
        assert len(envelope.errors[0]["detail"]) == MAX_DETAIL_CHARS


# --- re-raise ---------------------------------------------------------------


async def test_the_original_exception_is_re_raised_unchanged():
    """`run_capture_loop` catches and leaves `CaptureState` untouched.
    Returning the failure envelopes normally instead would install them over
    the last good capture -- turning an upstream outage into a loss of
    availability, when the whole point of a cache is that it costs only
    freshness."""
    lake, metrics = _SpyLake(), _SpyMetrics()
    original = httpx.ConnectError("upstream down")

    with pytest.raises(httpx.ConnectError) as caught:
        await _fail(original, lake, metrics)

    assert caught.value is original


async def test_a_lake_outage_does_not_shadow_the_original_failure():
    """A secondary S3 error would lose the fact that actually explains the
    pass."""
    lake, metrics = _SpyLake(fail=True), _SpyMetrics()
    original = httpx.ConnectError("upstream down")

    with pytest.raises(httpx.ConnectError) as caught:
        await _fail(original, lake, metrics)

    assert caught.value is original
    assert lake.writes == []
    # The gauge still fires: dropping it would make a lake outage look like a
    # healthy collector.
    assert [ratio for _, ratio in metrics.coverage_calls] == [0.0, 0.0]


# --- off the event loop ------------------------------------------------------


async def test_the_lake_write_never_runs_on_the_event_loop_thread():
    """boto3 is synchronous. A write on the loop thread blocks every other
    request including `/health`, which is what gated `roster-scope`'s
    readiness on object-store latency."""
    lake, metrics = _SpyLake(), _SpyMetrics()
    loop_thread = threading.get_ident()

    with pytest.raises(RuntimeError):
        await _fail(RuntimeError("boom"), lake, metrics)

    assert lake.threads, "the lake was never called at all"
    assert all(tid != loop_thread for tid in lake.threads)
    assert len(lake.threads) == 2


async def test_health_still_answers_while_a_failure_envelope_is_being_written():
    """The behavioural version of the test above: a lake that takes seconds
    to answer must not stop other coroutines from running."""
    metrics = _SpyMetrics()

    class _SlowLake(_SpyLake):
        def write(self, envelope):
            threading.Event().wait(1.0)
            return super().write(envelope)

    async def health() -> float:
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(0)
        return asyncio.get_running_loop().time() - started

    failing = asyncio.create_task(_fail(RuntimeError("boom"), _SlowLake(), metrics))
    await asyncio.sleep(0.05)
    elapsed = await health()

    assert elapsed < 0.5
    with pytest.raises(RuntimeError):
        await failing


# --- failure_envelopes on its own -------------------------------------------


def test_failure_envelopes_writes_nothing_and_raises_nothing():
    """Split out so a collector with an unusual failure path can build the
    envelopes without the write-and-raise behaviour."""
    envelopes = failure_envelopes(
        RuntimeError("boom"),
        collector="fake",
        signal_types=SIGNAL_TYPES,
        adapter="fake-adapter",
        now=NOW,
        scope=SCOPE,
        source_ref="etag-123",
    )

    assert set(envelopes) == set(SIGNAL_TYPES)
    assert all(e.upstream.source_ref == "etag-123" for e in envelopes.values())
    assert len(envelopes) == 2
