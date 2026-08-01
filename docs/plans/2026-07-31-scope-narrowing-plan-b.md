# Scope Narrowing (Plan B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make three collectors fetch only the players that matter — `usage-share` and `player-stats` on the membership scope, `injury-report` on membership ∪ matchup — and make `scope_aware` a claim a test can falsify.

**Architecture:** Every narrowed collector reads the scope from the **lake** via `ScopeClient` (never `roster-scope` over HTTP), resolves its upstream's native ids **forward** through `IdentityClient` to canonical `fdy-` ids, and keeps only rows in scope. No scope means **zero** upstream calls and a `present: 0` envelope — there is deliberately no unnarrowed fallback.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest + respx, MinIO/S3, uv workspace.

Design doc: [`2026-07-31-collector-cost-controls-and-narrowing-design.md`](2026-07-31-collector-cost-controls-and-narrowing-design.md). Seam design: [`2026-07-30-scope-narrowing-design.md`](2026-07-30-scope-narrowing-design.md).

**Prerequisite:** [`2026-07-31-conditional-get-plan.md`](2026-07-31-conditional-get-plan.md) should be merged first. It is not a code dependency — nothing here imports it — but it is the cost fix, and it ships first by decision.

## Global Constraints

- **Fail closed, always.** No scope ⇒ no fetch ⇒ zero upstream calls. An unnarrowed fallback would blow the vendor budget precisely during an incident. Never add one, not even behind a flag.
- **The scope comes from the lake, not HTTP.** A `roster-scope` outage must cost scope *freshness*, not the availability of all 26 collectors.
- **`player-identity` is authoritative.** Anything not explicitly `resolved: true` is unresolved. Never re-rank `candidates` against a local floor — that adopts an identity the service explicitly refused.
- **256Mi memory limit, unchanged.** Filter as you parse; never hold the upstream twice.
- **`coverage.expected` must never derive from what succeeded.** Use `CoverageAccumulator(floor=...)`. `Coverage.ratio` returns `1.0` when `expected == 0`, so an empty expectation reads as healthy.
- **`POST /refresh` returns 202 — accepted, not done.** Poll with a bounded, loud helper; never a naive read on the next line.
- **Mutation testing is mandatory.** Each task names its pairings. **Verify every pairing empirically**, and report any that does not hold rather than working around it. **Re-run earlier tasks' mutations after a later task edits their tests** — a mutation caught at task N has been silently un-caught by task N+1's own test edit before.
- **Pair every `all(...)`/`any(...)` over a collection with a length assertion.** `all([])` is `True`.
- Commit after every task. Never push to `main`.

---

## File Structure

| File | Responsibility |
|---|---|
| `libs/collector-core/collector_core/scope.py` | **Modify.** Add `ScopeClient.fetch_union` for the membership ∪ matchup case. |
| `libs/collector-core/collector_core/identity.py` | **Modify.** Delete `crosswalk_version`. |
| `services/usage-share/usage_share/adapters/scope.py` | **Create.** Lake-backed scope + forward GSIS resolution for usage-share. |
| `services/usage-share/usage_share/capture.py` | **Modify.** Fetch scope first; fail closed; filter while streaming. |
| `services/player-stats/player_stats/adapters/scope.py` | **Rewrite.** HTTP `ROSTER_SCOPE_URL` → `ScopeClient` on the lake. |
| `services/injury-report/injury_report/adapters/scope.py` | **Create.** Membership ∪ matchup. |
| `services/injury-report/injury_report/capture.py` | **Modify.** Narrow before fetching. |
| `contracts/collector-registry.yaml` | **Modify.** Three `scope_aware` flips; delete the stale `usage-share` comment. |
| `tests/test_scope_aware_gate.py` | **Create.** The behavioural gate that closes #78. |
| `.github/workflows/integration-test.yml`, `scripts/seed-scope-fixture.py` | **Modify/Create.** Seed a scope envelope into MinIO so CI has one. |

---

### Task 1: `ScopeClient.fetch_union`

**Files:**
- Modify: `libs/collector-core/collector_core/scope.py`
- Test: `libs/collector-core/tests/test_scope_client.py`

**Interfaces:**
- Consumes: existing `ScopeClient.fetch`, `Scope`, `ScopeUnavailable`.
- Produces: `ScopeClient.fetch_union(signal_types: Sequence[str], season: int, week: int) -> Scope`. The returned `Scope.members` is the union; `captured_at` is the **oldest** contributing envelope's; `signal_type` is `"+".join(signal_types)`.

- [ ] **Step 1: Write the failing test**

```python
# append to libs/collector-core/tests/test_scope_client.py
import pytest

from collector_core.scope import ScopeClient, ScopeUnavailable


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
    defender — a partial scope that looks like a working one."""
    _seed(lake, "scope_membership_weekly", 2026, 1, ["fdy-a"])

    with pytest.raises(ScopeUnavailable):
        await ScopeClient(lake).fetch_union(
            ("scope_membership_weekly", "scope_matchup_weekly"), 2026, 1
        )


@pytest.mark.asyncio
async def test_fetch_union_reports_the_oldest_contributing_capture(lake):
    """`age_seconds` must describe the STALEST input, not the freshest —
    otherwise a fresh membership list hides a week-old matchup list."""
    _seed(lake, "scope_membership_weekly", 2026, 1, ["fdy-a"],
          captured_at="2026-09-10T00:00:00Z")
    _seed(lake, "scope_matchup_weekly", 2026, 1, ["fdy-c"],
          captured_at="2026-09-03T00:00:00Z")

    scope = await ScopeClient(lake).fetch_union(
        ("scope_membership_weekly", "scope_matchup_weekly"), 2026, 1
    )

    assert scope.captured_at.isoformat().startswith("2026-09-03")
```

> **Implementer note:** `lake` and `_seed` must follow the fixtures already in `libs/collector-core/tests/test_scope_client.py`. Read that file first; it already seeds envelopes for the single-signal-type `fetch` tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_scope_client.py -k union -v`
Expected: FAIL with `AttributeError: 'ScopeClient' object has no attribute 'fetch_union'`

- [ ] **Step 3: Write minimal implementation**

Add to `ScopeClient` in `scope.py`:

```python
    async def fetch_union(
        self, signal_types: Sequence[str], season: int, week: int
    ) -> Scope:
        """The union of several scope lists — membership ∪ matchup.

        `injury-report` is the case this exists for: an opposing cornerback
        ruled out moves a receiver's projection as much as the receiver's own
        hamstring does, and defenders never appear on the offence-oriented
        membership list. Narrowing to membership alone would silently discard
        the half of the signal that is hardest to get anywhere else.

        **All or nothing.** If any requested signal type is unavailable the
        whole call raises, rather than returning the lists that did resolve.
        A partial union is the dangerous shape: it looks exactly like a
        working narrow while dropping an entire class of player, and the
        collector would publish a confident, short answer. Failing closed
        turns that into an obvious `present: 0`.

        `captured_at` is the OLDEST contributing envelope's, so
        `age_seconds` describes the stalest input rather than the freshest —
        a week-old matchup list must not hide behind a fresh membership one.
        """
        members: set[str] = set()
        captured_at: datetime | None = None
        for signal_type in signal_types:
            scope = await self.fetch(signal_type, season, week)
            members |= scope.members
            if captured_at is None or scope.captured_at < captured_at:
                captured_at = scope.captured_at

        if captured_at is None:
            raise ScopeUnavailable("scope_unavailable")

        return Scope(
            members=frozenset(members),
            captured_at=captured_at,
            signal_type="+".join(signal_types),
        )
```

Add `from collections.abc import Sequence` to the imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd libs/collector-core && uv run pytest tests/test_scope_client.py -v`
Expected: PASS

- [ ] **Step 5: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| Catch `ScopeUnavailable` per signal type and continue | `test_fetch_union_fails_closed_when_any_signal_type_is_missing` |
| Take the newest `captured_at` (`>` instead of `<`) | `test_fetch_union_reports_the_oldest_contributing_capture` |
| Return only the last signal type's members | `test_fetch_union_returns_every_members_set_combined` |

- [ ] **Step 6: Commit**

```bash
git add libs/collector-core/collector_core/scope.py libs/collector-core/tests/test_scope_client.py
git commit -m "collector-core: ScopeClient.fetch_union for membership + matchup"
```

---

### Task 2: Delete `crosswalk_version`

**Files:**
- Modify: `libs/collector-core/collector_core/identity.py:60-80,137-168`
- Modify: `libs/collector-core/tests/test_identity_client.py`
- Modify: `docs/collectors.md:230-263`

**Interfaces:**
- Consumes: nothing.
- Produces: `IdentityClient(base_url, client, token=None)` — the `crosswalk_version` parameter is gone. Cache key becomes `ResolveQuery` alone.

`player-identity` has no version concept: `resolve_queries` returns `{results, count, resolved_count, unresolved_count}` and the word appears nowhere in the service outside `CROSSWALK_KEYS`/`CROSSWALK_SOURCES`. Nothing can populate the parameter, so the cache key it guards is inert. Two live alternatives exist for the day a caller genuinely needs cross-pass caching — `GET /catalog`'s `last_capture_at`, and the `player_identity_crosswalk` envelope's own `captured_at` — and both are recorded in the design doc. Neither is needed now.

- [ ] **Step 1: Delete the parameter and its tests**

Remove from `IdentityClient.__init__`: the `crosswalk_version` parameter, `self._crosswalk_version`, and the tuple in the cache type annotation. Change `self._cache` to `dict[ResolveQuery, str]`, and both cache accesses in `resolve_many` from `self._cache[(self._crosswalk_version, query)]` to `self._cache[query]`.

Delete these tests outright:
- `test_a_new_crosswalk_version_invalidates_the_cache`
- `test_a_crosswalk_version_change_on_the_same_client_invalidates_the_cache`

Update the two tests at `test_identity_client.py:288` and `:316` to construct `IdentityClient(BASE, client, token="t")`.

Update the `__init__` docstring comment to read:

```python
        # Per-instance, in-memory only -- no TTL, no disk, no eviction, and
        # no version key. A `crosswalk_version` parameter existed here and was
        # removed: `player-identity` exposes no version, so nothing could ever
        # populate it and the key it guarded was inert. If cross-pass
        # invalidation is ever genuinely needed, `GET /catalog`'s
        # `last_capture_at` and the `player_identity_crosswalk` envelope's
        # `captured_at` are both available without a new endpoint.
        self._cache: dict[ResolveQuery, str] = {}
```

- [ ] **Step 2: Run the suite**

Run: `cd libs/collector-core && uv run pytest tests/test_identity_client.py -v`
Expected: PASS, with two fewer tests.

- [ ] **Step 3: Update the docs**

In `docs/collectors.md`, delete the paragraph beginning "**`crosswalk_version` has no source today.**" and the surrounding cache-key discussion (roughly lines 230–263). Replace with two sentences: the cache is keyed on the query alone, per instance, and a fresh process starts empty.

- [ ] **Step 4: Grep for stragglers**

Run: `grep -rn "crosswalk_version" . --include=*.py --include=*.md`
Expected: matches only in `docs/plans/` (historical design records, which stay as written).

- [ ] **Step 5: Commit**

```bash
git add libs/collector-core docs/collectors.md
git commit -m "collector-core: drop crosswalk_version, which had no source"
```

---

### Task 3: `usage-share` narrows

**Files:**
- Create: `services/usage-share/usage_share/adapters/scope.py`
- Modify: `services/usage-share/usage_share/capture.py:264-310`
- Test: `services/usage-share/tests/test_narrowing.py`

**Interfaces:**
- Consumes: `ScopeClient`, `ScopeUnavailable`, `IdentityClient`, `ResolveQuery`, `BATCH_LIMIT`.
- Produces: `resolve_in_scope(rows, *, scope: Scope, identity: IdentityClient) -> AsyncIterator[tuple[UsageRow, str]]` — yields `(row, player_id)` pairs, and only for rows whose GSIS id resolves to an `fdy-` id present in `scope.members`. The second element is the canonical `fdy-` id the envelope must publish as `player_id`.

**The forward direction is the whole trick.** The registry currently claims narrowing is impossible because "membership rows carry no name and no external id, so there is nothing to join a GSIS-keyed feed onto." That assumes the *reverse* lookup. It is stale — it predates `IdentityClient`. Each CSV row carries a `gsis_id`, `gsis` is a published crosswalk source, so `player-identity` adopts the link at **Tier 1** with no scoring at all.

- [ ] **Step 1: Write the failing test**

```python
# services/usage-share/tests/test_narrowing.py
"""Narrowing is behavioural: the assertion is what got fetched and published,
never the `scope_aware` flag."""

import pytest

from collector_core.scope import Scope, ScopeUnavailable


@pytest.mark.asyncio
async def test_only_scoped_players_are_published(lake, now, scoped_client):
    """Upstream carries four players; the scope names two; two are published."""
    scope = _scope({"fdy-aaa", "fdy-bbb"})
    envelopes = await capture_usage_share(
        2026, 1, client=scoped_client, lake=lake, now=now, scope=scope
    )

    published = {
        row["player_id"] for row in envelopes["player_usage_weekly"].signals
    }
    assert published == {"fdy-aaa", "fdy-bbb"}
    assert len(published) == 2


@pytest.mark.asyncio
async def test_no_scope_means_zero_upstream_calls(lake, now):
    """Fail closed. Not 'fetch everything', not 'fetch nothing but pretend' —
    zero calls to the upstream and a `present: 0` envelope."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        raise AssertionError("the upstream must not be reached without a scope")

    envelopes = await _capture_with_unavailable_scope(lake, now, handler)

    assert calls == []
    envelope = envelopes["player_usage_weekly"]
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected >= 1
    assert any(e["reason"] == "scope_unavailable" for e in envelope.errors)
    assert len(envelope.errors) >= 1


@pytest.mark.asyncio
async def test_an_unresolved_row_is_dropped_not_adopted(lake, now):
    """`player-identity` refusing is the answer. A row it would not resolve
    must never be published under a guessed id."""
    scope = _scope({"fdy-aaa"})
    envelopes = await _capture_with_unresolvable_row(lake, now, scope)

    published = {
        row["player_id"] for row in envelopes["player_usage_weekly"].signals
    }
    assert "fdy-aaa" in published
    assert all(pid.startswith("fdy-") for pid in published)
    assert len(published) >= 1


@pytest.mark.asyncio
async def test_resolution_is_batched_within_the_limit(scoped_client):
    """A 1,700-row feed must not become 1,700 requests."""
    from collector_core.identity import BATCH_LIMIT

    batch_sizes = await _capture_recording_batch_sizes(rows=1200)

    assert len(batch_sizes) == 3
    assert all(size <= BATCH_LIMIT for size in batch_sizes)
    assert sum(batch_sizes) == 1200
```

> **Implementer note:** the helpers (`_scope`, `scoped_client`, `_capture_with_unavailable_scope`, `_capture_with_unresolvable_row`, `_capture_recording_batch_sizes`) are yours to write, following `services/usage-share/tests/`'s existing fixture style. Use `respx` for `player-identity` and `httpx.MockTransport` for the CSV, as the existing tests do.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/usage-share && uv run pytest tests/test_narrowing.py -v`
Expected: FAIL — `capture_usage_share()` takes no `scope` argument.

- [ ] **Step 3: Write the scope adapter**

```python
# services/usage-share/usage_share/adapters/scope.py
"""Narrowing this collector to `roster-scope`'s membership list.

**Forward, not backward.** The scope is a set of canonical `fdy-` ids and the
feed is keyed by GSIS id, so the join could run in either direction. It runs
forward — every upstream row's `gsis_id` is resolved to an `fdy-` id and
checked against the scope — because `gsis` is a *published crosswalk source*,
which means `player-identity` adopts the link at Tier 1 with no scoring at
all. The reverse direction (turning 416 `fdy-` ids into GSIS ids) has no seam
today and is deliberately left to 8C, where a per-player API genuinely needs
to name a player before fetching them.

This is why the registry's old claim that narrowing here was "impossible"
was wrong: it predates `IdentityClient` and assumed the reverse direction.

**Batched, and bounded.** `IdentityClient.resolve_many` chunks at
`BATCH_LIMIT` (500, pinned to `player-identity`'s own `MAX_BATCH_QUERIES` by
`tests/test_identity_batch_limit.py`). Rows are buffered to at most one batch
before being resolved and filtered, so peak memory is one batch plus the rows
actually kept — never the whole feed.
"""

from collections.abc import AsyncIterator

from collector_core.identity import BATCH_LIMIT, IdentityClient, ResolveQuery
from collector_core.scope import Scope

# The crosswalk source the feed is keyed by. Tier 1 in `player-identity`'s
# resolution ladder: adopted exactly, never scored.
UPSTREAM_SOURCE = "gsis"


def _query(row) -> ResolveQuery:
    """One upstream row as a resolve query.

    `source`/`source_id` alone are enough for a Tier-1 adoption; `team` and
    `position` are sent anyway so a row whose GSIS id is absent upstream can
    still fall through to attribute scoring rather than failing outright.
    """
    return ResolveQuery(
        name=row.player_name,
        team=row.team,
        position=row.position,
        season=row.season,
        source=UPSTREAM_SOURCE,
        source_id=row.upstream_player_id,
    )


async def resolve_in_scope(
    rows: AsyncIterator,
    *,
    scope: Scope,
    identity: IdentityClient,
) -> AsyncIterator[tuple[object, str]]:
    """Yield `(row, player_id)` for rows resolving into `scope.members`.

    Three outcomes, and only the first is published:

    * resolved AND in scope -> yielded
    * resolved but NOT in scope -> dropped silently; this is the narrowing
    * unresolved -> dropped. `player-identity` already filed the miss
      server-side, and adopting a candidate it refused would attribute a
      real player's usage to the wrong id.
    """
    buffer: list = []

    async def _flush():
        if not buffer:
            return
        resolved = await identity.resolve_many([_query(r) for r in buffer])
        for buffered in buffer:
            player_id = resolved.get(_query(buffered))
            if player_id is not None and player_id in scope.members:
                yield buffered, player_id
        buffer.clear()

    async for row in rows:
        buffer.append(row)
        if len(buffer) >= BATCH_LIMIT:
            async for pair in _flush():
                yield pair
    async for pair in _flush():
        yield pair
```

- [ ] **Step 4: Wire it into `capture.py`**

Fetch the scope **before** the upstream, so a missing scope costs zero calls:

```python
    metrics.capture_attempt()

    # BEFORE the upstream fetch, deliberately. Fail closed means zero
    # upstream calls, so the scope must be resolved before anything is
    # requested -- not fetched-then-filtered.
    try:
        scope = await ScopeClient(lake).fetch("scope_membership_weekly", season, week)
    except ScopeUnavailable as exc:
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope={"season": season, "week": week},
            lake=lake,
            metrics=metrics,
            reason="scope_unavailable",
            expected=EXPECTED_FLOOR,
        )
```

Then pass `scope` and an `IdentityClient` into the row loop, replacing the current unconditional row mapping with `resolve_in_scope`, and emit `player_id` as the resolved canonical id with `"player_id_source": "player_identity"` (replacing `"upstream_gsis"`).

> **Implementer note:** read `capture.py:264-380` in full before editing. The `CoverageAccumulator` floor stays `EXPECTED_FLOOR[signal_type]` — do **not** derive it from the scope size, or a truncated scope reports perfect coverage.

- [ ] **Step 5: Run the tests**

Run: `cd services/usage-share && uv run pytest -v`
Expected: PASS

- [ ] **Step 6: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| Drop the `player_id in scope.members` check | `test_only_scoped_players_are_published` |
| Catch `ScopeUnavailable` and continue unnarrowed | `test_no_scope_means_zero_upstream_calls` |
| Move the scope fetch after the upstream fetch | `test_no_scope_means_zero_upstream_calls` |
| Fall back to the upstream id when unresolved | `test_an_unresolved_row_is_dropped_not_adopted` |
| Raise `BATCH_LIMIT` chunking to one request | `test_resolution_is_batched_within_the_limit` |
| Set `CoverageAccumulator(floor=len(scope.members))` | *(add an assertion if none dies — deriving expected from the scope is the exact failure mode lesson 2 names)* |

- [ ] **Step 7: Commit**

```bash
git add services/usage-share
git commit -m "usage-share: narrow to the membership scope via forward GSIS resolution"
```

---

### Task 4: `player-stats` moves onto the lake

**Files:**
- Rewrite: `services/player-stats/player_stats/adapters/scope.py`
- Modify: `services/player-stats/player_stats/capture.py`, `helm/values/player-stats/values.yaml`
- Test: `services/player-stats/tests/test_scope_adapter.py`

**Interfaces:**
- Consumes: `ScopeClient`, `ScopeUnavailable`.
- Produces: `fetch_watchlist(lake, season, week) -> frozenset[str]`, raising `ScopeUnavailable`. The `client`-taking HTTP signature is gone, as is `ROSTER_SCOPE_URL`.

Two things change together. `player-stats` already declares `scope_aware: true`, but its runtime narrowing ships **off** because it reached `roster-scope` over HTTP and the two id spaces did not intersect (both minted ids from separate stubs). Both services now resolve through the real `player-identity`, so the blocker is gone — and the HTTP path contradicts decision 5 anyway.

- [ ] **Step 1: Write the failing test**

```python
# services/player-stats/tests/test_scope_adapter.py
import pytest

from collector_core.scope import ScopeUnavailable

from player_stats.adapters.scope import fetch_watchlist


@pytest.mark.asyncio
async def test_the_watchlist_comes_from_the_lake(lake):
    _seed_scope(lake, 2026, 1, ["fdy-a", "fdy-b"])
    assert await fetch_watchlist(lake, 2026, 1) == frozenset({"fdy-a", "fdy-b"})


@pytest.mark.asyncio
async def test_an_absent_scope_raises_rather_than_returning_empty(lake):
    """An empty watchlist would shrink `coverage.expected` to whatever the
    box-score feed happened to return — the derive-expected-from-what-
    succeeded failure the coverage block exists to catch."""
    with pytest.raises(ScopeUnavailable):
        await fetch_watchlist(lake, 2026, 1)


def test_roster_scope_url_is_gone():
    """The HTTP path contradicted decision 5 and must not come back."""
    import player_stats.adapters.scope as module

    assert not hasattr(module, "ROSTER_SCOPE_URL_ENV")
    assert "ROSTER_SCOPE_URL" not in module.__doc__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/player-stats && uv run pytest tests/test_scope_adapter.py -v`
Expected: FAIL — `fetch_watchlist` still takes an `httpx.AsyncClient` and returns a tuple.

- [ ] **Step 3: Rewrite the adapter**

```python
# services/player-stats/player_stats/adapters/scope.py
"""The `roster-scope` seam — the watchlist this collector's coverage is owed.

Read from the **lake**, not from `roster-scope` over HTTP. That is decision 5
of the narrowing design: the lake is append-only and already carries every
scope capture, so the last good scope survives a `roster-scope` or
`player-identity` outage. Reaching the service directly would make one
collector's downtime a fleet-wide stop.

This module used to do exactly that, via `ROSTER_SCOPE_URL`, and shipped
disabled because `roster-scope` and this collector minted `player_id`s from
two different stub resolvers so the id spaces did not intersect. Both now
resolve through the real `player-identity`, and the env var is gone.

**Raises rather than returning empty.** An empty watchlist would shrink
`coverage.expected` to whatever the box-score feed happened to return, which
reports a truncated upstream as perfect. `capture.py` still floors the
expectation at `EXPECTED_FLOOR` regardless.
"""

from collector_core.scope import ScopeClient

SCOPE_SIGNAL_TYPE = "scope_membership_weekly"

# A team defense has no box score, so it is not a row this collector can ever
# be owed. This is what turns roster-scope's 416-slot universe into 384.
TEAM_DEFENSE_PREFIX = "fdy-dst-"


async def fetch_watchlist(lake, season: int, week: int) -> frozenset[str]:
    """The canonical `player_id`s this week's capture is owed a row for.

    Raises `ScopeUnavailable` when there is no usable scope. `ScopeClient`
    already falls back to `week - 1` and already drops `excluded` rows while
    keeping `grace` ones — a player who left the depth chart on Tuesday still
    played on Sunday.
    """
    scope = await ScopeClient(lake).fetch(SCOPE_SIGNAL_TYPE, season, week)
    return frozenset(
        player_id
        for player_id in scope.members
        if not player_id.startswith(TEAM_DEFENSE_PREFIX)
    )
```

- [ ] **Step 4: Update `capture.py` and the values file**

Replace the `fetch_watchlist(client)` call with `fetch_watchlist(lake, season, week)`, route `ScopeUnavailable` to `fail_capture` with `reason="scope_unavailable"` **before** any upstream fetch, and delete the `ROSTER_SCOPE_URL` entry from `helm/values/player-stats/values.yaml`.

- [ ] **Step 5: Run the tests**

Run: `cd services/player-stats && uv run pytest -v`
Expected: PASS

- [ ] **Step 6: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| Return `frozenset()` instead of raising | `test_an_absent_scope_raises_rather_than_returning_empty` |
| Drop the `TEAM_DEFENSE_PREFIX` filter | add an assertion that a seeded `fdy-dst-sf` is excluded |
| Reintroduce `ROSTER_SCOPE_URL` | `test_roster_scope_url_is_gone` |

- [ ] **Step 7: Commit**

```bash
git add services/player-stats helm/values/player-stats
git commit -m "player-stats: read the watchlist from the lake, not over HTTP"
```

---

### Task 5: `injury-report` narrows to membership ∪ matchup

**Files:**
- Create: `services/injury-report/injury_report/adapters/scope.py`
- Modify: `services/injury-report/injury_report/capture.py`
- Test: `services/injury-report/tests/test_narrowing.py`

**Interfaces:**
- Consumes: `ScopeClient.fetch_union` (Task 1).
- Produces: `fetch_scope(lake, season, week) -> Scope` over `("scope_membership_weekly", "scope_matchup_weekly")`.

- [ ] **Step 1: Write the failing test**

```python
# services/injury-report/tests/test_narrowing.py
import pytest

from collector_core.scope import ScopeUnavailable


@pytest.mark.asyncio
async def test_a_matchup_defender_is_published(lake, now, client):
    """The reason this collector narrows on the UNION. An opposing corner
    ruled out moves a receiver's projection as much as the receiver's own
    hamstring does, and defenders are never on the offensive watchlist."""
    _seed_scope(lake, "scope_membership_weekly", 2026, 1, ["fdy-wr"])
    _seed_scope(lake, "scope_matchup_weekly", 2026, 1, ["fdy-cb"])

    envelopes = await capture_injury_report(
        2026, 1, client=client, lake=lake, now=now
    )
    published = {
        row["player_id"]
        for row in envelopes["player_injury_status"].signals
    }

    assert "fdy-cb" in published
    assert "fdy-wr" in published
    assert len(published) == 2


@pytest.mark.asyncio
async def test_an_out_of_scope_player_is_dropped(lake, now, client):
    _seed_scope(lake, "scope_membership_weekly", 2026, 1, ["fdy-wr"])
    _seed_scope(lake, "scope_matchup_weekly", 2026, 1, ["fdy-cb"])

    envelopes = await capture_injury_report(
        2026, 1, client=client, lake=lake, now=now
    )
    published = {
        row["player_id"]
        for row in envelopes["player_injury_status"].signals
    }

    assert "fdy-deep-bench" not in published


@pytest.mark.asyncio
async def test_a_missing_matchup_scope_fails_closed(lake, now):
    """Membership present, matchup absent: must NOT narrow to offence only."""
    _seed_scope(lake, "scope_membership_weekly", 2026, 1, ["fdy-wr"])
    calls = []

    envelopes = await _capture_recording_upstream_calls(lake, now, calls)

    assert calls == []
    assert envelopes["player_injury_status"].coverage.present == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/injury-report && uv run pytest tests/test_narrowing.py -v`
Expected: FAIL — the collector publishes every player the stub week carries.

- [ ] **Step 3: Write the adapter and wire it**

```python
# services/injury-report/injury_report/adapters/scope.py
"""Narrowing to membership ∪ matchup.

This collector's registry entry used to argue *against* narrowing: "an
opposing cornerback ruled out moves a receiver's projection as much as the
receiver's own hamstring does, and defenders never appear on an
offence-oriented watchlist at all." That is an argument for reading the
matchup list too -- `roster-scope` publishes a separately-bounded 608-slot
CB/S/LB/DL/OL universe for exactly this -- not an argument for fetching all
~1,700 players in the league.

`fetch_union` is all-or-nothing on purpose: a present membership list with an
absent matchup list would narrow to offence alone and silently drop every
defender, which looks exactly like a working narrow.
"""

from collector_core.scope import Scope, ScopeClient

SCOPE_SIGNAL_TYPES = ("scope_membership_weekly", "scope_matchup_weekly")


async def fetch_scope(lake, season: int, week: int) -> Scope:
    """Every player this collector is allowed to fetch for, offence and
    defence. Raises `ScopeUnavailable` if either list is missing."""
    return await ScopeClient(lake).fetch_union(SCOPE_SIGNAL_TYPES, season, week)
```

In `capture.py`, call `fetch_scope` **before** the upstream fetch, route `ScopeUnavailable` to `fail_capture` with `reason="scope_unavailable"`, and filter emitted rows on `player_id in scope.members`.

- [ ] **Step 4: Run the tests**

Run: `cd services/injury-report && uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| Narrow on `scope_membership_weekly` only | `test_a_matchup_defender_is_published` |
| Drop the `in scope.members` filter | `test_an_out_of_scope_player_is_dropped` |
| Catch `ScopeUnavailable` and continue | `test_a_missing_matchup_scope_fails_closed` |

- [ ] **Step 6: Commit**

```bash
git add services/injury-report
git commit -m "injury-report: narrow to membership union matchup"
```

---

### Task 6: CI gets a scope

**Files:**
- Create: `scripts/seed-scope-fixture.py`
- Modify: `.github/workflows/integration-test.yml`
- Test: `tests/test_seed_scope_fixture.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a CLI writing one `scope_membership_weekly` and one `scope_matchup_weekly` envelope into the lake for a given season/week.

`roster-scope` ships `CAPTURE_ENABLED: "false"` for a documented third-party-load reason, so no scope envelope ever reaches the lake in CI — and a narrowed collector then **correctly** fails closed and fetches nothing. That is right behaviour that reads as a broken integration test.

Enabling capture in CI and having the smoke test POST `/refresh` were both rejected: `helm/values/roster-scope/values.yaml` states outright that the smoke test must not, because a dispatched refresh reaches the upstream regardless of the flag.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seed_scope_fixture.py
"""The fixture must be a fixture, not a second implementation of the scope."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_fixture_validates_against_the_envelope_schema(tmp_path):
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/seed-scope-fixture.py"),
         "--season", "2026", "--week", "1", "--out", str(tmp_path)],
        check=True,
    )
    written = sorted(tmp_path.rglob("*.json"))
    assert len(written) == 2

    import jsonschema
    schema = json.loads(
        (ROOT / "contracts/signal-envelope/envelope.v1.schema.json").read_text()
    )
    for path in written:
        jsonschema.validate(json.loads(path.read_text()), schema)


def test_both_scope_signal_types_are_seeded(tmp_path):
    """A membership-only fixture would make injury-report fail closed in CI
    for a reason that has nothing to do with the code under test."""
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/seed-scope-fixture.py"),
         "--season", "2026", "--week", "1", "--out", str(tmp_path)],
        check=True,
    )
    types = {
        json.loads(p.read_text())["signal_type"] for p in tmp_path.rglob("*.json")
    }
    assert types == {"scope_membership_weekly", "scope_matchup_weekly"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest==9.0.3 --with jsonschema==4.26.0 pytest tests/test_seed_scope_fixture.py -v`
Expected: FAIL — `scripts/seed-scope-fixture.py` does not exist.

- [ ] **Step 3: Write the script**

A small CLI that writes two envelopes (one per scope signal type) with a handful of deterministic `fdy-` members each, either to `--out` on disk or to the lake via `collector_core.lake`. Match `envelope.v1.schema.json` exactly — `envelope_version` is the **string** `"1"`.

Members must be deterministic and documented as fixtures, not sampled from a real feed.

- [ ] **Step 4: Wire it into `integration-test.yml`**

Add a step after "Deploy services" and before "Smoke test services":

```yaml
      - name: Seed a scope fixture into the lake
        # roster-scope ships CAPTURE_ENABLED=false (37 MB upstream, weekly
        # cadence, cluster rebuilt per run), so nothing ever writes a scope
        # here and every narrowed collector would correctly fail closed.
        # Seeding is not a workaround for that decision -- it is what lets
        # the decision stand while still exercising the narrowed path.
        run: |
          uv run --no-project --with boto3 python3 scripts/seed-scope-fixture.py \
            --season 2026 --week 1 --lake
```

> **Implementer note:** confirm the real lake credentials/env the other steps use before writing this — read the "Deploy services" step. Do not invent an env var.

- [ ] **Step 5: Run the platform suite**

Run: `uv run --with pyyaml==6.0.3 --with pytest==9.0.3 --with jsonschema==4.26.0 pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/seed-scope-fixture.py tests/test_seed_scope_fixture.py .github/workflows/integration-test.yml
git commit -m "ci: seed a scope fixture so narrowed collectors have one to read"
```

---

### Task 7: Close #78 — make `scope_aware` falsifiable

**Files:**
- Create: `tests/test_scope_aware_gate.py`
- Modify: `contracts/collector-registry.yaml`

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces: a platform test asserting the registry's `scope_aware` matches what the service does.

`scope_aware` is type-checked and nothing else — the registry says so, and `CLAUDE.md` repeats it. A green gate today confirms only that the value is a bool.

- [ ] **Step 1: Flip the three flags and delete the stale comment**

In `contracts/collector-registry.yaml`: set `scope_aware: true` for `usage-share` and `injury-report` (it is already `true` for `player-stats`). Delete `usage-share`'s "Narrowing is impossible today" comment block entirely — it predates `IdentityClient` and assumes the reverse join direction — and replace it with two lines recording that it narrows forward on `gsis`.

Update `injury-report`'s comment: it now narrows on the **union**, and the cornerback argument is the *reason for the union*, not a reason to stay unnarrowed.

Leave `roster-transactions` and `depth-chart` at `false` with their existing reasons intact.

- [ ] **Step 2: Write the gate**

```python
# tests/test_scope_aware_gate.py
"""#78: `scope_aware` was type-checked and nothing else.

Read by AST, like `test_collector_registry.py` — `platform-tests` installs
only pytest, pyyaml and jsonschema, so importing a service module would pull
in fastapi and httpx and fail.

A collector declaring `scope_aware: true` must import a narrowing seam. That
is weaker than proving it fails closed (the per-service behavioural tests in
Tasks 3-5 do that) but it is the strongest claim available without a cluster,
and it catches the regression that matters: a collector that keeps the flag
while losing the code.
"""

import ast
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = yaml.safe_load(
    (ROOT / "contracts/collector-registry.yaml").read_text()
)["collectors"]

NARROWING_IMPORTS = {"ScopeClient", "fetch_watchlist", "fetch_scope"}


def _imported_names(service: str) -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "services" / service).rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize(
    "entry", REGISTRY, ids=[e["name"] for e in REGISTRY]
)
def test_scope_aware_matches_what_the_service_imports(entry):
    imported = _imported_names(entry["name"])
    narrows = bool(imported & NARROWING_IMPORTS)
    assert narrows == entry["scope_aware"], (
        f"{entry['name']} declares scope_aware={entry['scope_aware']} but "
        f"{'imports' if narrows else 'does not import'} a narrowing seam"
    )


def test_the_gate_covers_every_registered_collector():
    """`all(...)` over an empty registry is True — pin the length."""
    assert len(REGISTRY) >= 9
```

- [ ] **Step 3: Run it**

Run: `uv run --with pyyaml==6.0.3 --with pytest==9.0.3 pytest tests/test_scope_aware_gate.py -v`
Expected: PASS for all nine collectors.

- [ ] **Step 4: Verify the mutation pairings empirically**

| Mutation | Must kill |
|---|---|
| Flip `usage-share` to `scope_aware: false` in the registry | `test_scope_aware_matches_what_the_service_imports[usage-share]` |
| Remove the `ScopeClient` import from `usage_share` | same test |
| Empty `NARROWING_IMPORTS` | every `scope_aware: true` case |

- [ ] **Step 5: Update the docs**

In `CLAUDE.md` and `contracts/collector-registry.yaml`'s header, replace the "TYPE-CHECKED ONLY … human-reviewed" language for `scope_aware` with a note that `tests/test_scope_aware_gate.py` now checks it structurally, and that the per-service fail-closed tests check it behaviourally.

- [ ] **Step 6: Commit**

```bash
git add tests/test_scope_aware_gate.py contracts/collector-registry.yaml CLAUDE.md
git commit -m "contracts: make scope_aware falsifiable, closing #78"
```

---

### Task 8: Live verification

**Files:** none — verification only.

**Only a live container run finds the real bugs.** Every genuine defect in 8A/8B came from running the image with the chart's real environment under its real 256Mi limit, and no unit test found any of them.

- [ ] **Step 1: Bring up the local stack**

```bash
python scripts/stack-up.py
```

- [ ] **Step 2: Seed a scope and confirm narrowing**

```bash
uv run --no-project --with boto3 python3 scripts/seed-scope-fixture.py \
  --season 2026 --week 1 --lake
uv run --no-project --with pyyaml==6.0.3 python3 scripts/refresh-collector.py usage-share
```

Poll `/catalog`'s `last_capture_at` with a **bounded** loop — `POST /refresh` is 202, accepted not done — then read `/signals` and confirm the published `player_id` set is exactly the seeded scope's members.

- [ ] **Step 3: Confirm it fails closed**

Delete the seeded scope objects, refresh again, and confirm: zero upstream requests, `coverage.present == 0`, and a `scope_unavailable` error in the envelope. Record the observed peak RSS for each collector.

- [ ] **Step 4: Run everything**

```bash
uv run --with pyyaml==6.0.3 --with pytest==9.0.3 --with jsonschema==4.26.0 pytest tests/ -q
cd libs/collector-core && uv run pytest -q && cd ../..
for s in usage-share player-stats injury-report; do (cd services/$s && uv run pytest -q); done
```

- [ ] **Step 5: Re-run every earlier task's mutations**

Tasks 3–5 and 7 all edited tests that earlier tasks relied on. A mutation caught at task N has been silently un-caught by task N+1's own test edit before — re-run each pairing and report any that no longer holds.

- [ ] **Step 6: Run `superpowers:pr-uat` before opening the final PR**

Required by `CLAUDE.md` for any PR that goes to main. Do not skip it.

---

## Done when

- `usage-share`, `player-stats` and `injury-report` publish **only** scoped players, proven behaviourally rather than by a flag.
- Each makes **zero** upstream calls when no scope exists, and writes a `present: 0` envelope saying `scope_unavailable`.
- `injury-report` publishes a matchup defender that is absent from the membership list.
- `integration-test` passes with the seeded scope fixture.
- `crosswalk_version` appears nowhere outside `docs/plans/`.
- Every mutation pairing has been applied, observed to fail its named test, reverted — and re-run after later tasks touched the same tests.
- **Not** done here: the reverse `fdy-` → native id map (8C), a lake retention policy, issue #77, or narrowing `roster-transactions`/`depth-chart` (both deliberately exempted).
