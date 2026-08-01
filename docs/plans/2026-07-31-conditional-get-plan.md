# Conditional GET Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a collector poll that finds nothing new cost a few hundred bytes instead of a whole document, by sending `If-None-Match` and treating a `304` as a successful capture that writes no envelope.

**Architecture:** A new `collector_core.conditional` module owns the ETag store and the `UpstreamUnchanged` signal. `stream_csv_dicts` gains an opt-in `etag_key`; when the upstream answers `304` it raises `UpstreamUnchanged`, which the capture loop and the `/refresh` path catch *before* their generic handler — advancing `last_capture_at` without replacing `CaptureState.envelopes` and without writing to the lake. Two collectors opt in (`depth-chart`, `roster-scope`), which is ~99% of the projected saving.

**Tech Stack:** Python 3.12, FastAPI, httpx, OpenTelemetry metrics, pytest + respx, uv workspace.

Design doc: [`2026-07-31-collector-cost-controls-and-narrowing-design.md`](2026-07-31-collector-cost-controls-and-narrowing-design.md).

## Global Constraints

- **`libs/collector-core` is a uv workspace member.** Run its tests with `cd libs/collector-core && uv run pytest -v`. A test dependency must be declared in its `pyproject.toml` even when the workspace venv already has it — CI installs each package alone.
- **A collector's memory limit is 256Mi and does not change.** Never buffer an upstream response more than once.
- **`collector_capture_failures_total` is owned by the library.** Do not call `metrics.capture_failure(exc)` alongside `fail_capture`/`publish_capture` — that double-counts.
- **`UpstreamUnchanged` is not a failure.** It must never reach `fail_capture`, which writes a `present: 0` envelope and would destroy a healthy capture's published state.
- **Mutation testing is mandatory.** Each task names its pairings. **Verify every pairing empirically** — apply the mutation, run the named test, confirm it fails. Report any pairing that does not hold rather than working around it (five unsound pairings were found in the last plan this way).
- **Pair every `all(...)`/`any(...)` over a collection with a length assertion.** `all([])` is `True`.
- Commit after every task. Never push to `main`.

---

## File Structure

| File | Responsibility |
|---|---|
| `libs/collector-core/collector_core/conditional.py` | **Create.** `UpstreamUnchanged`, `ETagStore`, the `ETAGS` process singleton, `conditional_headers`. |
| `libs/collector-core/collector_core/streaming.py` | **Modify.** `stream_csv_dicts` gains `etag_key`/`etag_store`. |
| `libs/collector-core/collector_core/metrics.py` | **Modify.** Add `collector_upstream_unchanged` counter. |
| `libs/collector-core/collector_core/routes.py` | **Modify.** `CaptureState.mark_unchanged`; `_run_capture` handles `UpstreamUnchanged`. |
| `libs/collector-core/collector_core/scheduler.py` | **Modify.** `run_capture_loop` handles `UpstreamUnchanged`. |
| `services/depth-chart/depth_chart/adapters/upstream.py` | **Modify.** Pass `etag_key`. |
| `services/depth-chart/depth_chart/capture.py` | **Modify.** Re-raise `UpstreamUnchanged` before `fail_capture`. |
| `services/roster-scope/roster_scope/adapters/depth_chart.py` | **Modify.** Same as depth-chart's adapter. |
| `services/roster-scope/roster_scope/capture.py` | **Modify.** Same as depth-chart's capture. |
| `docs/collectors.md` | **Modify.** Document the opt-in and the 304 contract. |

---

### Task 1: The `conditional` module

**Files:**
- Create: `libs/collector-core/collector_core/conditional.py`
- Test: `libs/collector-core/tests/test_conditional.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `UpstreamUnchanged(url: str, source_ref: str | None = None)` with `.url` and `.source_ref`; `ETagStore` with `get(key) -> str | None`, `set(key, etag: str | None) -> None`, `clear() -> None`; module singleton `ETAGS: ETagStore`; `conditional_headers(key: str, store: ETagStore = ETAGS) -> dict[str, str]`.

- [ ] **Step 1: Write the failing test**

```python
# libs/collector-core/tests/test_conditional.py
"""The ETag store and the 304 signal."""

from collector_core.conditional import (
    ETAGS,
    ETagStore,
    UpstreamUnchanged,
    conditional_headers,
)


def test_a_stored_etag_becomes_an_if_none_match_header():
    store = ETagStore()
    store.set("http://x/doc.csv", 'W/"abc"')
    assert conditional_headers("http://x/doc.csv", store) == {
        "If-None-Match": 'W/"abc"'
    }


def test_an_unknown_key_sends_no_conditional_header():
    """A first-ever fetch must be an ordinary unconditional GET."""
    assert conditional_headers("http://x/never-seen.csv", ETagStore()) == {}


def test_setting_none_forgets_the_key_rather_than_storing_a_null():
    """An upstream that stops sending ETags must fall back to unconditional
    GETs, not send `If-None-Match: None` forever."""
    store = ETagStore()
    store.set("k", 'W/"abc"')
    store.set("k", None)
    assert store.get("k") is None
    assert conditional_headers("k", store) == {}


def test_clear_empties_the_store():
    store = ETagStore()
    store.set("k", 'W/"abc"')
    store.clear()
    assert store.get("k") is None


def test_the_module_singleton_is_an_etag_store():
    assert isinstance(ETAGS, ETagStore)


def test_upstream_unchanged_carries_the_url_and_the_source_ref():
    exc = UpstreamUnchanged("http://x/doc.csv", source_ref='W/"abc"')
    assert exc.url == "http://x/doc.csv"
    assert exc.source_ref == 'W/"abc"'
    assert "304" in str(exc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_conditional.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'collector_core.conditional'`

- [ ] **Step 3: Write minimal implementation**

```python
# libs/collector-core/collector_core/conditional.py
"""Conditional GET, so a poll that finds nothing new costs nothing.

A collector on a `volatile` cadence polls 96 times a day. `depth-chart`'s
upstream is a 37.1 MB asset that changes a few times a week, so 95 of those
96 downloads are of a document the collector already has -- ~3.4 GB/day, and
one `CAPTURE_ENABLED` flip away from being real.

Half of the mechanism already existed and went unused: `player-identity`'s
Sleeper adapter has always read `response.headers.get("etag")` into the
envelope's `upstream.source_ref`, described there as "the upstream's own
opaque cursor". Nothing ever sent it back. This module is the other half.

Verified against the live upstreams before it was written (2026-07-31):
`raw.githubusercontent.com` and the nflverse release asset (which 302s to
Azure blob storage) both serve ETags and both answer `If-None-Match` with a
`304` carrying zero bytes. A ranged control request returns `206`, so the 304
is caused by the header rather than a dead URL.

**A 304 is not a failure**, and the distinction is load-bearing. Routing
`UpstreamUnchanged` into `collector_core.failure.fail_capture` would write a
`present: 0` envelope over a perfectly healthy capture -- the exact
destroy-good-data outcome `fail_capture`'s own docstring warns about. Every
collector that opts in must re-raise it ahead of its generic handler.
"""

import threading


class UpstreamUnchanged(Exception):
    """The upstream answered `304`: byte-identical to what we already have.

    Carries `source_ref` (the ETag that produced the 304) so a caller can
    record *which* version was confirmed, matching the shape every other
    refusal in this repo uses.
    """

    def __init__(self, url: str, source_ref: str | None = None) -> None:
        super().__init__(f"{url} unchanged (304)")
        self.url = url
        self.source_ref = source_ref


class ETagStore:
    """`key -> the ETag the last successful fetch returned`.

    In memory, with no TTL and no eviction. A pod restart therefore costs
    exactly one full download per key, which is far cheaper than reading the
    last envelope back from the lake on every capture forever.

    Locked because `LastValueGauge` established the precedent that this
    library's shared state is touched from more than one thread -- the lake
    writes go through `asyncio.to_thread`.
    """

    def __init__(self) -> None:
        self._etags: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._etags.get(key)

    def set(self, key: str, etag: str | None) -> None:
        """Store `etag`, or forget `key` when it is None/empty.

        Forgetting rather than storing a falsy value matters: an upstream
        that stops sending ETags must degrade to unconditional GETs, not
        pin the last one it ever sent.
        """
        with self._lock:
            if etag:
                self._etags[key] = etag
            else:
                self._etags.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._etags.clear()


# Process-global on purpose: exactly one collector runs per process, so this
# is process-scoped state rather than shared-between-tenants state. Passed
# explicitly as a default argument everywhere it is used so a test can supply
# its own instance instead of reaching in and clearing this one.
ETAGS = ETagStore()


def conditional_headers(key: str, store: ETagStore = ETAGS) -> dict[str, str]:
    """`{"If-None-Match": <etag>}`, or `{}` when nothing is stored."""
    etag = store.get(key)
    return {"If-None-Match": etag} if etag else {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd libs/collector-core && uv run pytest tests/test_conditional.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| `set` stores the falsy value instead of popping | `test_setting_none_forgets_the_key_rather_than_storing_a_null` |
| `conditional_headers` returns the header even when `etag` is None | `test_an_unknown_key_sends_no_conditional_header` |

Apply each, run the named test, confirm FAIL, revert.

- [ ] **Step 6: Commit**

```bash
git add libs/collector-core/collector_core/conditional.py libs/collector-core/tests/test_conditional.py
git commit -m "collector-core: an ETag store and the UpstreamUnchanged signal"
```

---

### Task 2: `stream_csv_dicts` sends and honours the ETag

**Files:**
- Modify: `libs/collector-core/collector_core/streaming.py:66-108`
- Test: `libs/collector-core/tests/test_streaming.py`

**Interfaces:**
- Consumes: `UpstreamUnchanged`, `ETagStore`, `ETAGS`, `conditional_headers` from Task 1.
- Produces: `stream_csv_dicts(client, url, *, required_columns=None, max_chars=MAX_UPSTREAM_CHARS, follow_redirects=True, etag_key: str | None = None, etag_store: ETagStore = ETAGS)`. Behaviour is byte-for-byte unchanged when `etag_key` is None.

- [ ] **Step 1: Write the failing test**

```python
# append to libs/collector-core/tests/test_streaming.py
import httpx
import pytest

from collector_core.conditional import ETagStore, UpstreamUnchanged
from collector_core.streaming import stream_csv_dicts

CSV = "team,player_name\nSF,A Player\n"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_second_request_carries_the_first_responses_etag():
    """The whole point: request two must be conditional on request one."""
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, text=CSV, headers={"ETag": 'W/"v1"'})

    store = ETagStore()
    async with _client(handler) as client:
        for _ in range(2):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert len(seen) == 2
    assert "if-none-match" not in seen[0]
    assert seen[1]["if-none-match"] == 'W/"v1"'


@pytest.mark.asyncio
async def test_a_304_raises_upstream_unchanged_and_yields_no_rows():
    store = ETagStore()
    store.set("k", 'W/"v1"')

    def handler(request):
        return httpx.Response(304)

    rows = []
    async with _client(handler) as client:
        with pytest.raises(UpstreamUnchanged) as caught:
            async for row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                rows.append(row)

    assert rows == []
    assert caught.value.source_ref == 'W/"v1"'


@pytest.mark.asyncio
async def test_without_an_etag_key_nothing_changes():
    """Every collector that has not opted in must behave exactly as before."""
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, text=CSV, headers={"ETag": 'W/"v1"'})

    async with _client(handler) as client:
        for _ in range(2):
            async for _row in stream_csv_dicts(client, "http://x/d.csv"):
                pass

    assert len(seen) == 2
    assert all("if-none-match" not in headers for headers in seen)


@pytest.mark.asyncio
async def test_an_upstream_that_sends_no_etag_stays_unconditional():
    """Fails open: no ETag means no conditional request, forever."""
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, text=CSV)

    store = ETagStore()
    async with _client(handler) as client:
        for _ in range(2):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert len(seen) == 2
    assert all("if-none-match" not in headers for headers in seen)
    assert store.get("k") is None


@pytest.mark.asyncio
async def test_a_changed_etag_replaces_the_stored_one():
    etags = iter(['W/"v1"', 'W/"v2"'])

    def handler(request):
        return httpx.Response(200, text=CSV, headers={"ETag": next(etags)})

    store = ETagStore()
    async with _client(handler) as client:
        for _ in range(2):
            async for _row in stream_csv_dicts(
                client, "http://x/d.csv", etag_key="k", etag_store=store
            ):
                pass

    assert store.get("k") == 'W/"v2"'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_streaming.py -k etag or 304 -v`
Expected: FAIL with `TypeError: stream_csv_dicts() got an unexpected keyword argument 'etag_key'`

- [ ] **Step 3: Write minimal implementation**

Add the import at the top of `streaming.py`:

```python
from .conditional import ETAGS, ETagStore, UpstreamUnchanged, conditional_headers
```

Replace the signature and the opening of the `async with` block:

```python
async def stream_csv_dicts(
    client: httpx.AsyncClient,
    url: str,
    *,
    required_columns: frozenset[str] | set[str] | None = None,
    max_chars: int = MAX_UPSTREAM_CHARS,
    follow_redirects: bool = True,
    etag_key: str | None = None,
    etag_store: ETagStore = ETAGS,
) -> AsyncIterator[dict[str, str]]:
    """Stream a CSV document, yielding one header-keyed dict per row.

    Peak memory is one chunk plus one row, independent of the document's size.
    The caller filters as it iterates, so nothing it does not keep is ever
    retained.

    `required_columns`, when given, is asserted against the header before any
    row is yielded — schema drift fails immediately rather than after a
    million rows have been mapped to nulls.

    `etag_key` opts into conditional GET. When set, the request carries
    `If-None-Match` from `etag_store` and a `304` raises `UpstreamUnchanged`
    before a single row is yielded. Left unset (the default), this function
    behaves exactly as it did before conditional GET existed — which is what
    lets a collector opt in one at a time.

    The 304 check precedes `raise_for_status()` deliberately. httpx only
    treats 4xx/5xx as errors so a 304 would fall through today, but relying
    on that would make this correct by accident.
    """
    header: list[str] | None = None
    consumed = 0
    remainder = ""

    headers = conditional_headers(etag_key, etag_store) if etag_key else {}

    async with client.stream(
        "GET", url, follow_redirects=follow_redirects, headers=headers
    ) as response:
        if etag_key is not None and response.status_code == 304:
            raise UpstreamUnchanged(url, source_ref=etag_store.get(etag_key))
        response.raise_for_status()
        if etag_key is not None:
            etag_store.set(etag_key, response.headers.get("etag"))
        async for chunk in response.aiter_text():
```

Everything below `async for chunk in response.aiter_text():` is unchanged.

- [ ] **Step 4: Run the whole collector-core suite**

Run: `cd libs/collector-core && uv run pytest -v`
Expected: PASS, including every pre-existing `stream_csv_dicts` test — no caller passes `etag_key` yet, so nothing else may change.

- [ ] **Step 5: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| Drop `headers=headers` from `client.stream` | `test_the_second_request_carries_the_first_responses_etag` |
| Remove the 304 check | `test_a_304_raises_upstream_unchanged_and_yields_no_rows` |
| `etag_store.set(...)` unconditionally, ignoring `etag_key is not None` | `test_without_an_etag_key_nothing_changes` |
| Store the ETag *before* `raise_for_status()` | *(expected to survive — report it rather than inventing a test)* |

- [ ] **Step 6: Commit**

```bash
git add libs/collector-core/collector_core/streaming.py libs/collector-core/tests/test_streaming.py
git commit -m "collector-core: stream_csv_dicts opts into conditional GET"
```

---

### Task 3: `mark_unchanged` and the metric

**Files:**
- Modify: `libs/collector-core/collector_core/routes.py:90-101`, `libs/collector-core/collector_core/metrics.py:106-141`
- Test: `libs/collector-core/tests/test_routes.py`, `libs/collector-core/tests/test_metrics.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `CaptureState.mark_unchanged(now: datetime) -> None`; `CollectorMetrics.upstream_unchanged() -> None` emitting `collector_upstream_unchanged_total`.

- [ ] **Step 1: Write the failing test**

```python
# append to libs/collector-core/tests/test_routes.py
from datetime import UTC, datetime, timedelta

from collector_core.routes import CaptureState

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _sentinel_envelopes():
    return {"a_signal": object()}


def test_mark_unchanged_advances_the_clock_without_touching_envelopes():
    """A 304 confirms the data is current. Staleness must reset; the
    published envelopes must survive untouched."""
    state = CaptureState()
    envelopes = _sentinel_envelopes()
    state.apply_capture(envelopes, T0)

    state.mark_unchanged(T0 + timedelta(minutes=15))

    assert state.last_capture_at == T0 + timedelta(minutes=15)
    assert state.envelopes is envelopes


def test_mark_unchanged_never_moves_the_clock_backwards():
    """Same belt-and-braces `apply_capture` applies: a pass describing an
    older `now` must not overwrite a newer one."""
    state = CaptureState()
    state.apply_capture(_sentinel_envelopes(), T0)

    state.mark_unchanged(T0 - timedelta(minutes=15))

    assert state.last_capture_at == T0
```

```python
# append to libs/collector-core/tests/test_metrics.py
from collector_core.metrics import CollectorMetrics


def test_upstream_unchanged_is_recordable():
    """Named `collector_upstream_unchanged`; OTel appends `_total`."""
    metrics = CollectorMetrics("a-collector")
    metrics.upstream_unchanged()  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_routes.py -k mark_unchanged tests/test_metrics.py -k upstream_unchanged -v`
Expected: FAIL with `AttributeError: 'CaptureState' object has no attribute 'mark_unchanged'`

- [ ] **Step 3: Write minimal implementation**

In `routes.py`, immediately after `apply_capture`:

```python
    def mark_unchanged(self, now: datetime) -> None:
        """Record a pass that confirmed the upstream is unchanged (a 304).

        Advances `last_capture_at` but leaves `envelopes` alone. Staleness
        means "how long since we confirmed this data is current", not "since
        we last wrote bytes" — otherwise a perfectly healthy collector climbs
        toward a staleness alert precisely *because* its upstream is stable,
        which is backwards.

        Consequence worth knowing: `/catalog`'s `last_capture_at` advances
        while the newest lake envelope's `captured_at` does not. That is the
        two fields meaning different things, not drift — a lake consumer
        reading the older timestamp is reading the truth, because the data
        genuinely is that old.

        Same monotonicity guard as `apply_capture`, for the same reason.
        Call only while holding `lock`.
        """
        if self.last_capture_at is None or now > self.last_capture_at:
            self.last_capture_at = now
```

In `metrics.py`, in `__init__` after `self._auth_failures`:

```python
        self._unchanged = meter.create_counter(
            "collector_upstream_unchanged",
            description=(
                "Capture passes skipped because the upstream answered 304, "
                "by collector."
            ),
        )
```

and as a method beside `capture_attempt`:

```python
    def upstream_unchanged(self) -> None:
        """A pass that fetched nothing because the upstream was unchanged.

        Deliberately NOT `collector_capture_failures_total`: this is a
        healthy outcome, and the whole saving the mechanism exists for is
        invisible without a counter that says how often it fires.
        """
        self._unchanged.add(1, {"collector": self.collector})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd libs/collector-core && uv run pytest tests/test_routes.py tests/test_metrics.py -v`
Expected: PASS

- [ ] **Step 5: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| `mark_unchanged` also sets `self.envelopes = {}` | `test_mark_unchanged_advances_the_clock_without_touching_envelopes` |
| Drop the `now > self.last_capture_at` guard | `test_mark_unchanged_never_moves_the_clock_backwards` |

- [ ] **Step 6: Commit**

```bash
git add libs/collector-core/collector_core/routes.py libs/collector-core/collector_core/metrics.py libs/collector-core/tests/test_routes.py libs/collector-core/tests/test_metrics.py
git commit -m "collector-core: CaptureState.mark_unchanged and the unchanged counter"
```

---

### Task 4: The loop and `/refresh` handle `UpstreamUnchanged`

**Files:**
- Modify: `libs/collector-core/collector_core/scheduler.py:125-146`, `libs/collector-core/collector_core/routes.py:186-203`
- Test: `libs/collector-core/tests/test_scheduler.py`

**Interfaces:**
- Consumes: `UpstreamUnchanged` (Task 1), `CaptureState.mark_unchanged` and `CollectorMetrics.upstream_unchanged` (Task 3).
- Produces: no new names. A `capture` raising `UpstreamUnchanged` no longer logs as a failure.

- [ ] **Step 1: Write the failing test**

```python
# append to libs/collector-core/tests/test_scheduler.py
from datetime import UTC, datetime, timedelta

import pytest

from collector_core.conditional import UpstreamUnchanged
from collector_core.routes import CaptureState

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_304_does_not_replace_the_last_good_envelopes():
    """The regression this whole mechanism must not cause: an unchanged
    upstream must never cost `/signals` the data it is already serving."""
    state = CaptureState()
    good = {"a_signal": object()}
    state.apply_capture(good, T0)

    async def capture(season, week, **kwargs):
        raise UpstreamUnchanged("http://x/d.csv", source_ref='W/"v1"')

    await _run_one_loop_tick(state, capture, now=T0 + timedelta(minutes=15))

    assert state.envelopes is good


@pytest.mark.asyncio
async def test_a_304_advances_last_capture_at():
    """Staleness resets, because the data was confirmed current."""
    state = CaptureState()
    state.apply_capture({"a_signal": object()}, T0)

    async def capture(season, week, **kwargs):
        raise UpstreamUnchanged("http://x/d.csv")

    await _run_one_loop_tick(state, capture, now=T0 + timedelta(minutes=15))

    assert state.last_capture_at == T0 + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_a_304_is_counted_as_unchanged_not_as_a_failure():
    state = CaptureState()
    state.apply_capture({"a_signal": object()}, T0)
    recorded = []

    async def capture(season, week, **kwargs):
        raise UpstreamUnchanged("http://x/d.csv")

    metrics = _RecordingMetrics(recorded)
    await _run_one_loop_tick(
        state, capture, now=T0 + timedelta(minutes=15), metrics=metrics
    )

    assert "unchanged" in recorded
    assert "failure" not in recorded


@pytest.mark.asyncio
async def test_a_real_failure_still_leaves_the_clock_alone():
    """The existing contract, re-asserted: only a 304 is special."""
    state = CaptureState()
    state.apply_capture({"a_signal": object()}, T0)

    async def capture(season, week, **kwargs):
        raise RuntimeError("upstream exploded")

    await _run_one_loop_tick(state, capture, now=T0 + timedelta(minutes=15))

    assert state.last_capture_at == T0
```

> **Implementer note:** `_run_one_loop_tick` and `_RecordingMetrics` are helpers you must write to match the fixtures already in `tests/test_scheduler.py` — that file already drives `run_capture_loop` one tick at a time via `_FakeSleep`/`_StopLoop`. Reuse those rather than inventing a second harness; a `_RecordingMetrics` appends `"unchanged"` from `upstream_unchanged()` and `"failure"` from `capture_failure()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_scheduler.py -k 304 -v`
Expected: FAIL — `state.last_capture_at` stays at `T0` because the generic `except Exception` swallows `UpstreamUnchanged` and logs "capture failed".

- [ ] **Step 3: Write minimal implementation**

In `scheduler.py`, add the import and restructure the body of the `while True` loop:

```python
from .conditional import UpstreamUnchanged
```

```python
    while True:
        try:
            async with state.lock:
                # Read after acquiring the lock -- not before -- so time spent
                # waiting for a dispatched `/refresh` to release the lock is
                # never charged against this pass's own capture deadline.
                now = clock()
                if process_started_at is None:
                    process_started_at = now
                deadline = None if capture_deadline is None else now + capture_deadline
                try:
                    async with client_factory() as client:
                        envelopes = await capture(
                            season,
                            week,
                            client=client,
                            lake=lake,
                            now=now,
                            deadline=deadline,
                        )
                except UpstreamUnchanged:
                    # A healthy pass, not a failure. Confirmed current, so
                    # staleness resets; nothing new to install, so the
                    # published envelopes stay exactly as they are.
                    metrics.upstream_unchanged()
                    state.mark_unchanged(now)
                else:
                    state.apply_capture(envelopes, now)
        except Exception:  # noqa: BLE001 -- the loop must survive anything
            logger.exception("capture failed; retrying on the next tick")
```

In `routes.py`, `_run_capture`, the same shape:

```python
    try:
        async with spec.state.lock:
            now = clock()
            deadline = (
                None if spec.capture_deadline is None else now + spec.capture_deadline
            )
            try:
                async with spec.client_factory() as client:
                    envelopes = await spec.capture(
                        season,
                        week,
                        client=client,
                        lake=spec.lake,
                        now=now,
                        deadline=deadline,
                    )
            except UpstreamUnchanged:
                spec.metrics.upstream_unchanged()
                spec.state.mark_unchanged(now)
            else:
                spec.state.apply_capture(envelopes, now)
    except Exception:
        logger.exception("dispatched capture failed")
```

Add `from .conditional import UpstreamUnchanged` to `routes.py`'s imports.

- [ ] **Step 4: Run the full suite**

Run: `cd libs/collector-core && uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| Replace `except UpstreamUnchanged` with a bare `pass` (no `mark_unchanged`) | `test_a_304_advances_last_capture_at` |
| Call `state.apply_capture({}, now)` in the 304 branch | `test_a_304_does_not_replace_the_last_good_envelopes` |
| Call `metrics.capture_failure(...)` in the 304 branch | `test_a_304_is_counted_as_unchanged_not_as_a_failure` |
| Move `except UpstreamUnchanged` after the generic `except Exception` | `test_a_304_advances_last_capture_at` |

- [ ] **Step 6: Commit**

```bash
git add libs/collector-core/collector_core/scheduler.py libs/collector-core/collector_core/routes.py libs/collector-core/tests/test_scheduler.py
git commit -m "collector-core: a 304 is a successful capture, not a failure"
```

---

### Task 5: `depth-chart` opts in

**Files:**
- Modify: `services/depth-chart/depth_chart/adapters/upstream.py:247-270`, `services/depth-chart/depth_chart/capture.py:214-232`
- Test: `services/depth-chart/tests/test_capture.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: no new names. `fetch_depth_charts` may now raise `UpstreamUnchanged`.

This is the collector the mechanism exists for: a 37.1 MB asset on a 15-minute cadence.

- [ ] **Step 1: Write the failing test**

```python
# append to services/depth-chart/tests/test_capture.py
import httpx
import pytest

from collector_core.conditional import UpstreamUnchanged


@pytest.mark.asyncio
async def test_a_304_propagates_instead_of_writing_a_failure_envelope(
    lake, now
):
    """The trap this guards: `capture_depth_chart`'s generic handler routes
    every exception to `fail_capture`, which writes a `present: 0` envelope
    and re-raises. Doing that for a 304 would destroy a healthy capture's
    published coverage over an upstream that is simply stable."""

    def handler(request):
        return httpx.Response(304)

    from collector_core.conditional import ETAGS
    ETAGS.set(source_ref(2026, 1), 'W/"v1"')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(UpstreamUnchanged):
            await capture_depth_chart(
                2026, 1, client=client, lake=lake, now=now
            )

    assert lake.written == []
```

> **Implementer note:** `lake`, `now` and the import of `capture_depth_chart`/`source_ref` must follow the conventions already in `services/depth-chart/tests/test_capture.py`. Read that file first. Reset `ETAGS` in a fixture (`ETAGS.clear()`) so this test cannot leak into another.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/depth-chart && uv run pytest tests/test_capture.py -k 304 -v`
Expected: FAIL — `lake.written` is non-empty, because `fail_capture` wrote a `present: 0` envelope per signal type.

- [ ] **Step 3: Write minimal implementation**

In `adapters/upstream.py`, pass the key to the stream call (the URL is already what `source_ref` returns):

```python
    rows = stream_csv_dicts(
        client,
        source_ref(season, week),
        required_columns=REQUIRED_COLUMNS,
        # The URL is the cache key: one ETag per season's asset. `source_ref`
        # already returns exactly that string, so the key and the thing the
        # envelope records as its provenance cannot drift apart.
        etag_key=source_ref(season, week),
    )
```

In `capture.py`, re-raise ahead of the generic handler:

```python
    metrics.capture_attempt()
    try:
        fetched = await fetch_depth_charts(
            season, week, client=client, now=now, deadline=deadline
        )
    except UpstreamUnchanged:
        # NOT a failure. `fail_capture` below would write a `present: 0`
        # envelope over a healthy capture and count a failure that did not
        # happen. `run_capture_loop` catches this and marks the pass
        # unchanged. Must stay ABOVE the generic handler.
        raise
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Writes a `present: 0` envelope per signal type, then re-raises `exc`.
        # Never returns — do not add code after this call.
        await fail_capture(
            ...
        )
```

Add `from collector_core.conditional import UpstreamUnchanged` to `capture.py`'s imports. Leave the `fail_capture(...)` call itself byte-for-byte unchanged.

- [ ] **Step 4: Run the service suite**

Run: `cd services/depth-chart && uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| Delete the `except UpstreamUnchanged: raise` clause | `test_a_304_propagates_instead_of_writing_a_failure_envelope` |
| Move it below `except Exception` | same test |
| Drop `etag_key` from the `stream_csv_dicts` call | same test (no conditional header ⇒ the mock still 304s, but `source_ref` is None ⇒ assert on `caught.value.source_ref` if it survives — report if it does) |

- [ ] **Step 6: Commit**

```bash
git add services/depth-chart
git commit -m "depth-chart: conditional GET on the 37 MB season asset"
```

---

### Task 6: `roster-scope` opts in

**Files:**
- Modify: `services/roster-scope/roster_scope/adapters/depth_chart.py:76-82`, `services/roster-scope/roster_scope/capture.py`
- Test: `services/roster-scope/tests/test_capture.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: no new names.

`roster-scope` fetches **the same asset** as `depth-chart` (issue #82) — the identical `{season}` URL template. Until that duplication is removed, both must opt in or the saving is halved.

- [ ] **Step 1: Write the failing test**

Mirror Task 5's test exactly, against `roster-scope`'s own capture entry point and its `DEPTH_CHART_URL`. Repeat the code rather than importing depth-chart's — the two services share no test tree.

```python
# append to services/roster-scope/tests/test_capture.py
import httpx
import pytest

from collector_core.conditional import ETAGS, UpstreamUnchanged


@pytest.mark.asyncio
async def test_a_304_propagates_instead_of_writing_a_failure_envelope(
    lake, now
):
    def handler(request):
        return httpx.Response(304)

    ETAGS.set(DEPTH_CHART_URL.format(season=2026), 'W/"v1"')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(UpstreamUnchanged):
            await capture_scope(2026, 1, client=client, lake=lake, now=now)

    assert lake.written == []
```

> **Implementer note:** read `services/roster-scope/tests/test_capture.py` first and match its fixture names and its capture entry point's real signature. `roster-scope`'s capture has a ledger-unavailable path that writes a `present: 0` envelope for the current week — confirm a 304 does **not** take that path.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/roster-scope && uv run pytest tests/test_capture.py -k 304 -v`
Expected: FAIL — a failure envelope was written.

- [ ] **Step 3: Write minimal implementation**

Pass `etag_key=DEPTH_CHART_URL.format(season=season)` to the `stream_csv_dicts` call in `adapters/depth_chart.py`, and add the same `except UpstreamUnchanged: raise` clause ahead of the generic handler in `capture.py`, with the same comment as Task 5.

- [ ] **Step 4: Run the service suite**

Run: `cd services/roster-scope && uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Verify the mutation pairings empirically**

Same three as Task 5, against `roster-scope`'s test.

- [ ] **Step 6: Commit**

```bash
git add services/roster-scope
git commit -m "roster-scope: conditional GET on the shared depth-chart asset"
```

---

### Task 7: Live verification and documentation

**Files:**
- Modify: `docs/collectors.md`
- Modify: `CLAUDE.md` (the collector-authoring section)

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: documentation only.

**A live container run is the only thing that finds the real bugs here** — every genuine defect in 8A/8B came from running the image with the chart's real environment under its real 256Mi limit, and no unit test found any of them.

- [ ] **Step 1: Build and run `depth-chart` against the real upstream**

```bash
docker build -f Dockerfile.collector --build-arg SERVICE=depth-chart -t depth-chart:local .
docker run --rm -m 256m -e CAPTURE_ENABLED=false -e CAPTURE_SEASON=2026 \
  -e COLLECTOR_TOKEN=local-dev-token -p 8016:8016 depth-chart:local
```

> **Implementer note:** confirm the real build invocation from `Dockerfile.collector` and `.github/actions/build-push` — the `--build-arg` name above may differ. Do not guess; read the file.

- [ ] **Step 2: Dispatch two refreshes and confirm the second is a 304**

```bash
curl -s -XPOST localhost:8016/refresh -H "Authorization: Bearer local-dev-token"
# wait for the first capture to finish (POST /refresh is 202 = accepted, NOT done;
# poll /catalog's last_capture_at with a bounded loop, never a naive sleep)
curl -s -XPOST localhost:8016/refresh -H "Authorization: Bearer local-dev-token"
curl -s localhost:8016/metrics | grep collector_upstream_unchanged
```

Expected: `collector_upstream_unchanged_total{collector="depth-chart"} 1.0` after the second refresh, and `/catalog`'s `last_capture_at` advanced while `/signals` still serves the first capture's rows.

**Record the observed peak RSS.** The saving is the point; if memory regressed, stop and report.

- [ ] **Step 3: Document the opt-in**

Add to `docs/collectors.md`, in the capture-authoring section:

> **Conditional GET is opt-in, one argument wide.** Pass `etag_key=<the URL>`
> to `stream_csv_dicts` and add `except UpstreamUnchanged: raise` **above**
> your generic `except Exception` handler. Both halves are required: without
> the first nothing is saved, and without the second a `304` is routed into
> `fail_capture`, which writes a `present: 0` envelope over a healthy capture
> and counts a failure that did not happen.
>
> A `304` is a **successful** capture. `last_capture_at` advances and no
> envelope is written, so `/catalog` reports a fresh pass while the newest
> lake object stays where it was. That is the two fields meaning different
> things, not drift.
>
> Do not opt in a collector whose upstream is generated per request — an
> ETag that changes every poll costs one extra round trip and saves nothing.

- [ ] **Step 4: Add the same summary to `CLAUDE.md`**

Three or four sentences in the collector section, matching that file's density. Cross-reference `docs/collectors.md` rather than repeating it.

- [ ] **Step 5: Run the full platform suite**

```bash
uv run --with pyyaml==6.0.3 --with pytest==9.0.3 --with jsonschema==4.26.0 pytest tests/ -q
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add docs/collectors.md CLAUDE.md
git commit -m "docs: the conditional-GET opt-in and the 304 contract"
```

---

## Done when

- `collector_upstream_unchanged_total` increments on a second consecutive `depth-chart` refresh against the unchanged real upstream.
- `/signals` still serves the first capture's rows after that second refresh.
- `cd libs/collector-core && uv run pytest` and both service suites pass.
- Every mutation pairing above has been applied, observed to fail the named test, and reverted — with any unsound pairing reported rather than patched.
- **Not** done here: rolling the opt-in out to the remaining collectors, a lake retention policy, or any `cadence_class` change. All three are deliberately out of scope.
