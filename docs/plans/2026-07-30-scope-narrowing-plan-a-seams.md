# Scope Narrowing — Plan A: The Seams

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `collector-core` a `ScopeClient` and an `IdentityClient`, and make `roster-scope` publish a second, bounded matchup list with identity live — so that Plan B's pilot collector has real seams to narrow against.

**Architecture:** Collectors read the scope from the **lake**, not from `roster-scope` over HTTP, so a `player-identity` outage costs scope *freshness* rather than stopping all 26 collectors. `roster-scope` resolves its members through `player-identity` and writes two independently-accounted envelopes per pass: `scope_membership_weekly` (~416 fantasy slots) and `scope_matchup_weekly` (~608 role-matched matchup slots).

**Tech Stack:** Python 3.12, FastAPI, `httpx`, `respx` for mocking, `pytest`, uv workspace with one root lockfile.

**Spec:** `docs/plans/2026-07-30-scope-narrowing-design.md`

## Global Constraints

- **Base branch is `wave0-integration`** (PR #73, unmerged). `main` lacks the scaffolder, the registry-derived tooling and every 8B collector. `git fetch origin && git merge --no-edit origin/wave0-integration` first.
- **Mutation testing is mandatory.** For each behaviour a test claims to protect: break it, confirm a test fails, restore. Report every mutation and its result.
- **Pair every `all(...)`/`any(...)` over a collection with an explicit length assertion.** `all([])` is `True`; that exact shape let mutations survive for three separate agents during 8B.
- **Every test import must be declared** in the package's dev dependencies. CI installs each package alone; a workspace venv hides a missing declaration. Verify with `uv sync --frozen`, not the workspace venv.
- **Do not use `moto`.** CI prunes `collector-core`'s dev deps inside `services/`. Use a fake/spy lake.
- **One lockfile, at the repo root.** Run `uv lock` at the worktree root.
- **Never block the event loop.** `EventLoopGuardedLake` *raises* on a loop-thread boto3 call. All lake access in a capture path goes through the async wrappers `aread`/`alist_keys`/`awrite`.
- **`ruff check` + `ruff format --check` clean; `uv lock --check` clean.** All twelve package suites and repo-root `tests/` stay green.
- **Never push to `main`, never force-push, never merge.** Open a **draft** PR.
- **Commit as you go** — a session limit has killed agents mid-flight in this project.

---

## File Structure

**Created:**
- `libs/collector-core/collector_core/scope.py` — `ScopeClient`, `ScopeUnavailable`, `Scope`. Reads the newest scope envelope from the lake. Knows nothing about HTTP.
- `libs/collector-core/collector_core/identity.py` — `IdentityClient`, `UnresolvedPlayer`. Batch-resolves native ids to `fdy-` ids. Knows nothing about the lake.
- `libs/collector-core/tests/test_scope_client.py`
- `libs/collector-core/tests/test_identity_client.py`
- `services/roster-scope/roster_scope/matchups.py` — matchup slot resolution, mirroring `scope.py`'s role for the player list.
- `services/roster-scope/tests/test_matchups.py`

**Modified:**
- `services/roster-scope/roster_scope/rules.py` — defensive position aliases, matchup quotas.
- `services/roster-scope/roster_scope/capture.py` — emit a second envelope, account coverage per list.
- `services/roster-scope/roster_scope/main.py` — `GET /scope/matchups`.
- `helm/values/roster-scope/values.yaml` — `PLAYER_IDENTITY_URL`, `gateway.publicPaths`.
- `contracts/collector-registry.yaml` — `roster-scope`'s `signal_types`.
- `contracts/signal-envelope/collectors/roster-scope.json` — the new signal's field schema.

Two new library files rather than one: `ScopeClient` depends on the lake and `IdentityClient` depends on HTTP. Keeping them apart means each is testable without the other's fixtures.

---

### Task 1: `ScopeClient` — read the newest scope from the lake, fail closed

**Files:**
- Create: `libs/collector-core/collector_core/scope.py`
- Test: `libs/collector-core/tests/test_scope_client.py`

**Interfaces:**
- Consumes: `collector_core.lake` — `alist_keys(lake, collector, signal_type, season, week) -> list[str]` and `aread(lake, key) -> dict`, both already present and both off-loop.
- Produces:
  - `class ScopeUnavailable(Exception)` — carries `.reason: str`.
  - `@dataclass(frozen=True) class Scope` with `members: frozenset[str]`, `captured_at: datetime`, `signal_type: str`, and property `age_seconds(now: datetime) -> float`.
  - `class ScopeClient` with `async def fetch(self, signal_type: str, season: int, week: int) -> Scope`.

**Key facts for the implementer:**
- Lake keys are `signals/<collector>/v<version>/season=<YYYY>/week=<NN>/<captured_at>-<signal_type>.json`, and `list_keys` returns them **in `captured_at` order**. The newest is therefore `keys[-1]`.
- The scope collector is `roster-scope`. Signal types are `scope_membership_weekly` and `scope_matchup_weekly`.
- An envelope dict has `signals` (list of rows), `captured_at` (RFC 3339 with `Z`), and `coverage`.
- Each membership row carries `player_id`.

- [ ] **Step 1: Write the failing tests**

```python
# libs/collector-core/tests/test_scope_client.py
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
    key = "signals/roster-scope/v1/season=2026/week=01/2026-09-02T00:00:00Z-scope_membership_weekly.json"
    lake = FakeLake(keys=[key], objects={key: _envelope("2026-09-02T00:00:00Z", [])})

    with pytest.raises(ScopeUnavailable) as excinfo:
        await ScopeClient(lake).fetch("scope_membership_weekly", 2026, 1)

    assert excinfo.value.reason == "scope_empty"


@pytest.mark.asyncio
async def test_fetch_asks_the_lake_for_the_right_partition():
    key = "signals/roster-scope/v1/season=2026/week=04/2026-09-30T00:00:00Z-scope_matchup_weekly.json"
    lake = FakeLake(keys=[key], objects={key: _envelope("2026-09-30T00:00:00Z", ["fdy-x"])})

    await ScopeClient(lake).fetch("scope_matchup_weekly", 2026, 4)

    assert lake.list_calls == [("roster-scope", "scope_matchup_weekly", 2026, 4)]


def test_age_seconds_measures_from_captured_at():
    scope = Scope(
        members=frozenset({"fdy-a"}),
        captured_at=datetime(2026, 9, 2, 0, 0, 0, tzinfo=UTC),
        signal_type="scope_membership_weekly",
    )
    assert scope.age_seconds(datetime(2026, 9, 2, 1, 0, 0, tzinfo=UTC)) == 3600.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd libs/collector-core && uv run pytest tests/test_scope_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core.scope'`

- [ ] **Step 3: Implement `scope.py`**

```python
# libs/collector-core/collector_core/scope.py
"""Reading the published scope, so a collector fetches only what matters.

Deliberately reads the LAKE, never `roster-scope` over HTTP. The lake is
append-only and already written by every scope capture, so the last good
scope survives a `player-identity` outage -- which is what stops one service
being a fleet-wide stop. `roster-scope`'s HTTP routes exist for the
out-of-repo generator and for operators; collectors do not use them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from .lake import alist_keys, aread

SCOPE_COLLECTOR = "roster-scope"


class ScopeUnavailable(Exception):
    """No usable scope. The caller must write a `present: 0` envelope and
    make ZERO upstream calls -- an unnarrowed fallback would blow the vendor
    budget precisely during an incident."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Scope:
    members: frozenset[str]
    captured_at: datetime
    signal_type: str

    def age_seconds(self, now: datetime) -> float:
        return (now - self.captured_at).total_seconds()


def _parse_captured_at(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class ScopeClient:
    def __init__(self, lake) -> None:
        self._lake = lake

    async def fetch(self, signal_type: str, season: int, week: int) -> Scope:
        keys = await alist_keys(self._lake, SCOPE_COLLECTOR, signal_type, season, week)
        if not keys:
            raise ScopeUnavailable("scope_unavailable")

        # `list_keys` returns captured_at order, so the newest is last.
        envelope = await aread(self._lake, keys[-1])
        members = frozenset(
            row["player_id"] for row in envelope["signals"] if row.get("player_id")
        )
        if not members:
            raise ScopeUnavailable("scope_empty")

        return Scope(
            members=members,
            captured_at=_parse_captured_at(envelope["captured_at"]),
            signal_type=signal_type,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd libs/collector-core && uv run pytest tests/test_scope_client.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Mutation-test, and record each result**

Apply each, confirm a named test fails, restore:

| # | Mutation | Must be caught by |
|---|---|---|
| M1 | `keys[-1]` → `keys[0]` | `test_fetch_returns_the_newest_envelopes_members` |
| M2 | `raise ScopeUnavailable("scope_unavailable")` → `return Scope(frozenset(), ...)` | `test_fetch_raises_when_no_scope_has_ever_been_written` |
| M3 | drop the `if not members` guard | `test_fetch_raises_rather_than_returning_an_empty_member_set` |
| M4 | `signal_type` argument ignored, hardcode `scope_membership_weekly` | `test_fetch_asks_the_lake_for_the_right_partition` |

- [ ] **Step 6: Commit**

```bash
git add libs/collector-core/collector_core/scope.py libs/collector-core/tests/test_scope_client.py
git commit -m "collector-core: ScopeClient reads the last good scope from the lake

Fails closed on a missing or empty scope: no scope means no fetch, so an
unnarrowed fallback can never blow the vendor budget during an incident."
```

---

### Task 2: `IdentityClient` — batch resolution that never adopts a refusal

**Files:**
- Create: `libs/collector-core/collector_core/identity.py`
- Test: `libs/collector-core/tests/test_identity_client.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient` (passed in — a collector's `client_factory` already provides one).
- Produces:
  - `class UnresolvedPlayer(Exception)` — carries `.reason: str`.
  - `@dataclass(frozen=True) class ResolveQuery` with `name: str | None`, `team: str | None`, `position: str | None`, `source: str | None`, `source_id: str | None`.
  - `class IdentityClient` with `__init__(self, base_url: str, client, token: str | None = None)` and `async def resolve_many(self, queries: list[ResolveQuery]) -> dict[ResolveQuery, str]`, returning only the resolved ones.
  - `BATCH_LIMIT = 500`

**Key facts for the implementer:**
- The endpoint is `POST {base_url}/resolve/batch`, body `{"queries": [ ... ]}`.
- The response is `{"results": [...], "count": int, "resolved_count": int, "unresolved_count": int}`.
- Each result is `{"query": {...}, "resolved": bool, "player_id": str | None, "reason": str | None, "candidates": [...], ...}`.
- **`player-identity` records the miss server-side** on every unresolved query. The caller must not record a second one.
- Every data route requires `Authorization: Bearer <token>`.

- [ ] **Step 1: Write the failing tests**

```python
# libs/collector-core/tests/test_identity_client.py
import httpx
import pytest
import respx

from collector_core.identity import BATCH_LIMIT, IdentityClient, ResolveQuery

BASE = "http://player-identity:8002"


def _result(name: str, resolved: bool, player_id=None, reason=None, candidates=None):
    return {
        "query": {"name": name, "team": None, "position": None,
                  "source": None, "source_id": None},
        "resolved": resolved,
        "player_id": player_id,
        "reason": reason,
        "candidates": candidates or [],
    }


@respx.mock
@pytest.mark.asyncio
async def test_resolved_queries_come_back_mapped_to_their_ids():
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(200, json={
            "results": [_result("Patrick Mahomes", True, "fdy-abc")],
            "count": 1, "resolved_count": 1, "unresolved_count": 0,
        })
    )
    query = ResolveQuery(name="Patrick Mahomes", team="KC", position="QB",
                         source=None, source_id=None)
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([query])

    assert got == {query: "fdy-abc"}
    assert len(got) == 1


@respx.mock
@pytest.mark.asyncio
async def test_a_refusal_is_never_adopted_even_when_candidates_score_highly():
    """THE regression test for this file.

    `player-identity` returns `resolved: false` with `candidates` populated
    exactly when it has DECIDED NOT to resolve, and files the miss itself.
    A caller that re-ranks those candidates against its own floor adopts an
    identity that service explicitly refused. That bug shipped once in
    roster-scope (a local 0.5 floor let `ambiguous` at 0.97/0.93 and
    `insufficient_agreeing_attributes` at 0.667 straight through). A wrong
    player_id then propagates into an append-only lake that is never
    rewritten.
    """
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(200, json={
            "results": [_result(
                "A. Smith", False, None, "ambiguous",
                candidates=[{"player_id": "fdy-x", "confidence": 0.97},
                            {"player_id": "fdy-y", "confidence": 0.93}],
            )],
            "count": 1, "resolved_count": 0, "unresolved_count": 1,
        })
    )
    query = ResolveQuery(name="A. Smith", team=None, position=None,
                         source=None, source_id=None)
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([query])

    assert got == {}
    assert len(got) == 0


@respx.mock
@pytest.mark.asyncio
async def test_a_missing_resolved_field_is_not_permission():
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(200, json={
            "results": [{"query": {"name": "X"}, "player_id": "fdy-x"}],
            "count": 1, "resolved_count": 0, "unresolved_count": 1,
        })
    )
    query = ResolveQuery(name="X", team=None, position=None, source=None, source_id=None)
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([query])

    assert got == {}


@respx.mock
@pytest.mark.asyncio
async def test_more_than_the_batch_limit_is_chunked():
    route = respx.post(f"{BASE}/resolve/batch")
    queries = [ResolveQuery(name=f"P{i}", team=None, position=None,
                            source=None, source_id=None)
               for i in range(BATCH_LIMIT + 1)]

    def _respond(request):
        import json as _json
        sent = _json.loads(request.content)["queries"]
        assert len(sent) <= BATCH_LIMIT, f"sent {len(sent)} > {BATCH_LIMIT}"
        return httpx.Response(200, json={
            "results": [_result(q["name"], True, f"fdy-{q['name']}") for q in sent],
            "count": len(sent), "resolved_count": len(sent), "unresolved_count": 0,
        })

    route.mock(side_effect=_respond)
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many(queries)

    assert route.call_count == 2, route.call_count
    assert len(got) == BATCH_LIMIT + 1


@respx.mock
@pytest.mark.asyncio
async def test_the_bearer_token_is_sent():
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(200, json={
            "results": [], "count": 0, "resolved_count": 0, "unresolved_count": 0})
    )
    async with httpx.AsyncClient() as client:
        await IdentityClient(BASE, client, token="secret").resolve_many(
            [ResolveQuery(name="X", team=None, position=None, source=None, source_id=None)]
        )

    assert route.calls[0].request.headers["Authorization"] == "Bearer secret"


@respx.mock
@pytest.mark.asyncio
async def test_an_empty_query_list_makes_no_request():
    route = respx.post(f"{BASE}/resolve/batch")
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([])

    assert got == {}
    assert route.call_count == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd libs/collector-core && uv run pytest tests/test_identity_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core.identity'`

- [ ] **Step 3: Implement `identity.py`**

```python
# libs/collector-core/collector_core/identity.py
"""Resolving an upstream's native ids to canonical `fdy-` player ids.

`player-identity` is authoritative. `resolved` is the answer; `candidates`
is the working. Anything not explicitly `resolved: true` is unresolved --
that service already filed the miss server-side, and a caller that re-ranks
candidates against its own floor adopts an identity it explicitly refused.
"""

from dataclasses import asdict, dataclass

BATCH_LIMIT = 500


class UnresolvedPlayer(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResolveQuery:
    name: str | None = None
    team: str | None = None
    position: str | None = None
    source: str | None = None
    source_id: str | None = None


class IdentityClient:
    def __init__(self, base_url: str, client, token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def resolve_many(
        self, queries: list[ResolveQuery]
    ) -> dict[ResolveQuery, str]:
        """Map only the queries player-identity RESOLVED. Unresolved ones are
        absent from the result -- the caller counts them in coverage.missing."""
        resolved: dict[ResolveQuery, str] = {}
        for start in range(0, len(queries), BATCH_LIMIT):
            chunk = queries[start : start + BATCH_LIMIT]
            response = await self._client.post(
                f"{self._base_url}/resolve/batch",
                json={"queries": [asdict(q) for q in chunk]},
                headers=self._headers(),
            )
            response.raise_for_status()
            for query, result in zip(chunk, response.json()["results"], strict=True):
                # `is True`, not truthiness: a missing field must not be
                # permission, and neither must a non-empty candidate list.
                if result.get("resolved") is True and result.get("player_id"):
                    resolved[query] = result["player_id"]
        return resolved
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd libs/collector-core && uv run pytest tests/test_identity_client.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Declare `respx` if it is not already a dev dependency**

Check `libs/collector-core/pyproject.toml`. If `respx` is absent, add it to the dev group at the same pin the services use, then `uv lock` at the **repo root**.

**This is not optional bookkeeping.** An undeclared `respx` reddened a PR after a green 200-test local run, because CI installs each package alone while the workspace venv had it. Verify the CI way:

```bash
cd libs/collector-core && uv sync --frozen && uv run pytest -q
```

- [ ] **Step 6: Mutation-test, and record each result**

| # | Mutation | Must be caught by |
|---|---|---|
| M5 | `result.get("resolved") is True` → `result.get("player_id") is not None` | `test_a_refusal_is_never_adopted_even_when_candidates_score_highly` |
| M6 | `is True` → truthiness (`if result.get("resolved")`) | `test_a_missing_resolved_field_is_not_permission` |
| M7 | fall back to `candidates[0]["player_id"]` when unresolved | `test_a_refusal_is_never_adopted_even_when_candidates_score_highly` |
| M8 | `BATCH_LIMIT` → 100000 (one request always) | `test_more_than_the_batch_limit_is_chunked` |
| M9 | drop the `Authorization` header | `test_the_bearer_token_is_sent` |

- [ ] **Step 7: Commit**

```bash
git add libs/collector-core/collector_core/identity.py \
        libs/collector-core/tests/test_identity_client.py \
        libs/collector-core/pyproject.toml uv.lock
git commit -m "collector-core: IdentityClient, and it never adopts a refusal

resolved:false means unresolved even when candidates score highly --
player-identity files the miss itself, and re-ranking its candidates
adopts an identity it explicitly refused."
```

---

### Task 3: Cache resolutions within and across passes

**Files:**
- Modify: `libs/collector-core/collector_core/identity.py`
- Test: `libs/collector-core/tests/test_identity_client.py`

**Interfaces:**
- Consumes: Task 2's `IdentityClient`, `ResolveQuery`, `BATCH_LIMIT`.
- Produces: `IdentityClient.__init__` gains `crosswalk_version: str | None = None`. Same `resolve_many` signature.

**Why a version key:** `player-identity`'s cadence class is `seasonal`, so the crosswalk barely moves and caching across passes is safe — but only while the crosswalk is unchanged. Keying the cache on its version means a republished crosswalk invalidates cleanly instead of serving stale ids for a season.

- [ ] **Step 1: Write the failing tests**

```python
# append to libs/collector-core/tests/test_identity_client.py

@respx.mock
@pytest.mark.asyncio
async def test_a_repeated_query_is_not_re_requested():
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(200, json={
            "results": [_result("P", True, "fdy-p")],
            "count": 1, "resolved_count": 1, "unresolved_count": 0,
        })
    )
    query = ResolveQuery(name="P", team=None, position=None, source=None, source_id=None)
    async with httpx.AsyncClient() as client:
        identity = IdentityClient(BASE, client, token="t", crosswalk_version="v1")
        first = await identity.resolve_many([query])
        second = await identity.resolve_many([query])

    assert first == second == {query: "fdy-p"}
    assert route.call_count == 1, route.call_count


@respx.mock
@pytest.mark.asyncio
async def test_an_unresolved_query_is_retried_rather_than_cached_as_a_refusal():
    """A miss may become a hit when the crosswalk is republished mid-season.
    Caching the refusal would pin the gap until a restart."""
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(200, json={
            "results": [_result("P", False, None, "ambiguous")],
            "count": 1, "resolved_count": 0, "unresolved_count": 1,
        })
    )
    query = ResolveQuery(name="P", team=None, position=None, source=None, source_id=None)
    async with httpx.AsyncClient() as client:
        identity = IdentityClient(BASE, client, token="t", crosswalk_version="v1")
        await identity.resolve_many([query])
        await identity.resolve_many([query])

    assert route.call_count == 2, route.call_count


@respx.mock
@pytest.mark.asyncio
async def test_a_new_crosswalk_version_invalidates_the_cache():
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(200, json={
            "results": [_result("P", True, "fdy-p")],
            "count": 1, "resolved_count": 1, "unresolved_count": 0,
        })
    )
    query = ResolveQuery(name="P", team=None, position=None, source=None, source_id=None)
    async with httpx.AsyncClient() as client:
        await IdentityClient(BASE, client, token="t", crosswalk_version="v1").resolve_many([query])
        await IdentityClient(BASE, client, token="t", crosswalk_version="v2").resolve_many([query])

    assert route.call_count == 2, route.call_count
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd libs/collector-core && uv run pytest tests/test_identity_client.py -k cache -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'crosswalk_version'`

- [ ] **Step 3: Add the cache**

Replace `__init__` and `resolve_many`:

```python
    def __init__(
        self,
        base_url: str,
        client,
        token: str | None = None,
        crosswalk_version: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._token = token
        self._crosswalk_version = crosswalk_version
        self._cache: dict[tuple[str | None, ResolveQuery], str] = {}

    async def resolve_many(
        self, queries: list[ResolveQuery]
    ) -> dict[ResolveQuery, str]:
        resolved: dict[ResolveQuery, str] = {}
        pending: list[ResolveQuery] = []
        for query in queries:
            cached = self._cache.get((self._crosswalk_version, query))
            if cached is not None:
                resolved[query] = cached
            else:
                pending.append(query)

        for start in range(0, len(pending), BATCH_LIMIT):
            chunk = pending[start : start + BATCH_LIMIT]
            response = await self._client.post(
                f"{self._base_url}/resolve/batch",
                json={"queries": [asdict(q) for q in chunk]},
                headers=self._headers(),
            )
            response.raise_for_status()
            for query, result in zip(chunk, response.json()["results"], strict=True):
                if result.get("resolved") is True and result.get("player_id"):
                    player_id = result["player_id"]
                    resolved[query] = player_id
                    # Only successes are cached. A miss may become a hit when
                    # the crosswalk is republished; caching the refusal would
                    # pin the gap until a restart.
                    self._cache[(self._crosswalk_version, query)] = player_id
        return resolved
```

- [ ] **Step 4: Run the whole file to verify everything passes**

Run: `cd libs/collector-core && uv run pytest tests/test_identity_client.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Mutation-test, and record each result**

| # | Mutation | Must be caught by |
|---|---|---|
| M10 | cache unresolved queries too (store `None` and skip re-request) | `test_an_unresolved_query_is_retried_rather_than_cached_as_a_refusal` |
| M11 | drop `crosswalk_version` from the cache key | `test_a_new_crosswalk_version_invalidates_the_cache` |
| M12 | never read the cache (always request) | `test_a_repeated_query_is_not_re_requested` |

- [ ] **Step 6: Commit**

```bash
git add libs/collector-core/collector_core/identity.py libs/collector-core/tests/test_identity_client.py
git commit -m "collector-core: cache identity resolutions, keyed on crosswalk version

Successes only -- a miss may become a hit when the crosswalk is
republished, so caching a refusal would pin the gap until a restart."
```

---

### Task 4: Defensive position aliases in `roster-scope`

**Files:**
- Modify: `services/roster-scope/roster_scope/rules.py`
- Test: `services/roster-scope/tests/test_rules.py` (create if absent)

**Interfaces:**
- Consumes: existing `POSITION_ALIASES: dict[str, str]` and `canonical_position(raw: str) -> str | None`.
- Produces: `POSITION_ALIASES` additionally maps defensive and offensive-line labels onto `CB`, `S`, `LB`, `DL`, `OL`. `canonical_position` is unchanged in signature and still returns `None` for an unknown label.

**Why this is its own task:** `POSITION_ALIASES` today covers offensive positions only. Defensive labels vary far more between sources, and an unrecognised one must be **dropped and counted missing** — the same treatment unknown team labels already get. Guessing a position is worse than declaring the slot unfilled.

- [ ] **Step 1: Write the failing tests**

```python
# services/roster-scope/tests/test_rules.py
import pytest

from roster_scope.rules import canonical_position


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CB", "CB"), ("DB", "CB"), ("NB", "CB"), ("cb", "CB"),
        ("S", "S"), ("FS", "S"), ("SS", "S"), ("SAF", "S"),
        ("LB", "LB"), ("ILB", "LB"), ("OLB", "LB"), ("MLB", "LB"), ("EDGE", "LB"),
        ("DL", "DL"), ("DE", "DL"), ("DT", "DL"), ("NT", "DL"),
        ("OL", "OL"), ("LT", "OL"), ("LG", "OL"), ("C", "OL"), ("RG", "OL"), ("RT", "OL"),
        ("QB", "QB"), ("WR", "WR"),  # offensive mapping is unchanged
    ],
)
def test_defensive_and_line_labels_collapse_to_canonical_groups(raw, expected):
    assert canonical_position(raw) == expected


@pytest.mark.parametrize("raw", ["P", "LS", "KR", "ATH", "", "   ", "NOT_A_POSITION"])
def test_an_unrecognised_label_is_dropped_not_guessed(raw):
    """Returning None keeps a punter off a CB slot. The slot then reads as
    missing, which is the honest outcome."""
    assert canonical_position(raw) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/roster-scope && uv run pytest tests/test_rules.py -v`
Expected: FAIL — `assert None == 'CB'` for the defensive rows.

- [ ] **Step 3: Extend `POSITION_ALIASES`**

Append to the existing dict in `rules.py`, keeping its `# fmt: skip`:

```python
    # Defensive and offensive-line labels, added for the matchup scope. These
    # vary far more between sources than offensive ones do. EDGE collapses to
    # LB rather than DL: charts use it for stand-up rushers who line up off
    # the line, and the matchup question it answers is coverage/containment.
    "CB": "CB", "DB": "CB", "NB": "CB", "NCB": "CB",
    "S": "S", "FS": "S", "SS": "S", "SAF": "S",
    "LB": "LB", "ILB": "LB", "OLB": "LB", "MLB": "LB", "WLB": "LB",
    "SLB": "LB", "EDGE": "LB",
    "DL": "DL", "DE": "DL", "DT": "DL", "NT": "DL",
    "OL": "OL", "LT": "OL", "LG": "OL", "C": "OL", "RG": "OL", "RT": "OL",
    "OT": "OL", "OG": "OL", "G": "OL", "T": "OL",
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/roster-scope && uv run pytest tests/test_rules.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole roster-scope suite**

Run: `cd services/roster-scope && uv run pytest -q`
Expected: all previously-passing tests still pass. **If any fail, stop and report** — a widened alias map can pull players onto offensive slots that previously fell through.

- [ ] **Step 6: Mutation-test, and record each result**

| # | Mutation | Must be caught by |
|---|---|---|
| M13 | `POSITION_ALIASES.get(...)` → return the raw label when unmapped | `test_an_unrecognised_label_is_dropped_not_guessed` |
| M14 | map `"EDGE"` to `"DL"` | the `("EDGE", "LB")` parametrisation |
| M15 | drop `.upper()` normalisation | the `("cb", "CB")` parametrisation |

- [ ] **Step 7: Commit**

```bash
git add services/roster-scope/roster_scope/rules.py services/roster-scope/tests/test_rules.py
git commit -m "roster-scope: defensive and OL position aliases for the matchup scope

An unrecognised label is still dropped rather than guessed -- the slot
reads as missing, which is the honest outcome."
```

---

### Task 5: Matchup quotas in config

**Files:**
- Modify: `services/roster-scope/roster_scope/rules.py`
- Test: `services/roster-scope/tests/test_rules.py`

**Interfaces:**
- Consumes: `DepthRule(rule_id, position, max_depth, entity_type="player")`, `TEAMS`.
- Produces:
  - `MATCHUP_RULES: tuple[DepthRule, ...]`
  - `expected_matchup_slots() -> int`

**The distinction that must not be lost:** `OL` is **our own** line, not the opponent's. Every other rule in this set is an opposing player. The list is "the players who determine how our scoped players perform", not "the opposing defence" — name and comment it that way or it will be misread.

- [ ] **Step 1: Write the failing tests**

```python
# append to services/roster-scope/tests/test_rules.py
from roster_scope.rules import MATCHUP_RULES, TEAMS, expected_matchup_slots


def test_matchup_rules_are_role_matched_with_the_agreed_quotas():
    quotas = {rule.position: rule.max_depth for rule in MATCHUP_RULES}
    assert quotas == {"CB": 4, "S": 3, "LB": 3, "DL": 4, "OL": 5}, quotas
    assert len(MATCHUP_RULES) == 5


def test_expected_matchup_slots_is_config_derived_not_fetch_derived():
    """Computed from config alone, BEFORE any upstream is contacted -- which
    is what stops a truncated depth chart shrinking the denominator and
    reporting ratio 1.0 on a hole."""
    assert expected_matchup_slots() == len(TEAMS) * (4 + 3 + 3 + 4 + 5)
    assert expected_matchup_slots() == 608


def test_matchup_rule_ids_are_unique_and_distinct_from_the_player_scope():
    from roster_scope.rules import ALL_RULES

    matchup_ids = [rule.rule_id for rule in MATCHUP_RULES]
    assert len(matchup_ids) == len(set(matchup_ids))
    assert not set(matchup_ids) & {rule.rule_id for rule in ALL_RULES}
    assert len(matchup_ids) == 5
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/roster-scope && uv run pytest tests/test_rules.py -k matchup -v`
Expected: FAIL — `ImportError: cannot import name 'MATCHUP_RULES'`

- [ ] **Step 3: Add the rules**

```python
# services/roster-scope/roster_scope/rules.py, after ALL_RULES

# The matchup scope: the players who determine how our SCOPED players
# perform. Not "the opposing defence" -- `OL` is OUR OWN line, because pass
# protection bears on the QB and RB already in the player scope. Every other
# rule here is an opposing player.
#
# Role-matched deliberately: only positions that move a scoped player's
# projection. Including every defensive starter would add positions no 8D
# collector asks about, and each one is a fetch we pay for.
MATCHUP_RULES: tuple[DepthRule, ...] = (
    DepthRule("cb_matchup_le_4", "CB", 4),   # covers WR
    DepthRule("s_matchup_le_3", "S", 3),     # covers WR/TE deep
    DepthRule("lb_matchup_le_3", "LB", 3),   # covers TE/RB
    DepthRule("dl_matchup_le_4", "DL", 4),   # pressure and run defence
    DepthRule("ol_matchup_le_5", "OL", 5),   # OUR OWN line, versus that front
)


def expected_matchup_slots() -> int:
    """Total matchup slots the config demands, across all 32 teams.

    Config-derived and computed before any upstream is contacted, for the
    same reason `expected_slots()` is: an expectation built from what a fetch
    returned reports a truncated document as ratio 1.0.
    """
    return len(TEAMS) * sum(rule.max_depth for rule in MATCHUP_RULES)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/roster-scope && uv run pytest tests/test_rules.py -v`
Expected: PASS

- [ ] **Step 5: Mutation-test, and record each result**

| # | Mutation | Must be caught by |
|---|---|---|
| M16 | `expected_matchup_slots` sums over `ALL_RULES` instead of `MATCHUP_RULES` | `test_expected_matchup_slots_is_config_derived_not_fetch_derived` |
| M17 | reuse a player-scope `rule_id` (e.g. `"wr_depth_le_4"`) for `CB` | `test_matchup_rule_ids_are_unique_and_distinct_from_the_player_scope` |
| M18 | change `OL` quota to 0 | `test_matchup_rules_are_role_matched_with_the_agreed_quotas` |

- [ ] **Step 6: Commit**

```bash
git add services/roster-scope/roster_scope/rules.py services/roster-scope/tests/test_rules.py
git commit -m "roster-scope: role-matched matchup quotas (CB4 S3 LB3 DL4 OL5)

608 slots, config-derived before any fetch. OL is our own line, not the
opponent's -- the list is who determines how our scoped players perform."
```

---

### Task 6: Resolve matchup slots and emit the second envelope

**Files:**
- Create: `services/roster-scope/roster_scope/matchups.py`
- Create: `services/roster-scope/tests/test_matchups.py`
- Modify: `services/roster-scope/roster_scope/capture.py`
- Modify: `contracts/collector-registry.yaml`
- Modify: `contracts/signal-envelope/collectors/roster-scope.json`

**Interfaces:**
- Consumes: `MATCHUP_RULES`, `expected_matchup_slots()`, `canonical_position`, `canonical_team` from Task 4/5; `CoverageAccumulator(floor=...)` and `cap_errors` from `collector_core.coverage`; `publish_capture` from `collector_core.publish`.
- Produces: `async def resolve_matchup_slots(rows, *, season, week, now, resolver) -> tuple[list[dict], CoverageAccumulator]` in `matchups.py`; `capture` returns a dict with **both** `scope_membership_weekly` and `scope_matchup_weekly` keys.

**Read first:** `services/roster-scope/roster_scope/scope.py` — `resolve_matchup_slots` mirrors its structure. Do not invent a different shape.

- [ ] **Step 1: Write the failing tests**

```python
# services/roster-scope/tests/test_matchups.py
from datetime import UTC, datetime

import pytest

from roster_scope.matchups import resolve_matchup_slots
from roster_scope.rules import expected_matchup_slots

NOW = datetime(2026, 9, 2, tzinfo=UTC)


class StubResolver:
    async def resolve(self, ref):
        return f"fdy-{ref.name.lower().replace(' ', '-')}"


def _row(team, position, rank, name):
    return {"team": team, "position": position, "depth_rank": rank, "name": name}


@pytest.mark.asyncio
async def test_expected_is_the_config_total_not_what_the_upstream_returned():
    """A truncated chart must not shrink the denominator -- that is the
    ratio-1.0 bug."""
    rows = [_row("KC", "CB", 1, "A Corner")]
    _, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    envelope_coverage = acc.build()
    assert envelope_coverage.expected == expected_matchup_slots() == 608
    assert envelope_coverage.present == 1


@pytest.mark.asyncio
async def test_a_total_outage_reports_a_low_ratio_not_a_perfect_one():
    _, acc = await resolve_matchup_slots(
        [], season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    coverage = acc.build()
    assert coverage.present == 0
    assert coverage.ratio < 0.01, coverage.ratio
    assert len(coverage.missing) > 0


@pytest.mark.asyncio
async def test_rows_beyond_the_quota_are_dropped():
    rows = [_row("KC", "CB", rank, f"Corner {rank}") for rank in range(1, 7)]
    signals, _ = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert len(signals) == 4, [s["slot_key"] for s in signals]


@pytest.mark.asyncio
async def test_an_unknown_position_is_counted_missing_not_guessed():
    rows = [_row("KC", "PUNTER", 1, "A Punter")]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert signals == []
    assert len(signals) == 0
    assert acc.build().present == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/roster-scope && uv run pytest tests/test_matchups.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'roster_scope.matchups'`

- [ ] **Step 3: Implement `matchups.py`**

Mirror `scope.py`'s slot-resolution loop, substituting `MATCHUP_RULES` for `ALL_RULES` and `expected_matchup_slots()` for `expected_slots()`. Seed the accumulator with **every** config slot key before iterating rows, so an absent slot lands in `coverage.missing` rather than shrinking `expected`. Drop a row whose `canonical_team` or `canonical_position` returns `None`, and drop a row whose `depth_rank` exceeds the rule's `max_depth`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/roster-scope && uv run pytest tests/test_matchups.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Emit the second envelope from `capture`**

In `capture.py`, build a `scope_matchup_weekly` envelope alongside the existing `scope_membership_weekly` one, with its **own** `CoverageAccumulator` — a matchup failure must not mask a healthy player scope. Return both from `capture`, keyed by signal type. Pass both through `publish_capture`.

- [ ] **Step 6: Register the new signal type**

- `contracts/collector-registry.yaml` — add `scope_matchup_weekly` to `roster-scope`'s `signal_types`. **Purely additive; keep the header comment verbatim and reorder nothing.**
- `contracts/signal-envelope/collectors/roster-scope.json` — add the new signal's field schema.

- [ ] **Step 7: Run the full suite plus the platform gates**

```bash
cd services/roster-scope && uv run pytest -q
cd ../.. && uv run --no-project --with pyyaml==6.0.3 --with pytest==9.0.3 \
  --with jsonschema==4.26.0 pytest tests/ -q
```
Expected: both green. The registry drift gate reads the descriptor by AST and will fail if `signal_types` disagrees with the code.

- [ ] **Step 8: Mutation-test, and record each result**

| # | Mutation | Must be caught by |
|---|---|---|
| M19 | seed the accumulator from returned rows instead of config | `test_expected_is_the_config_total_not_what_the_upstream_returned` |
| M20 | drop the `max_depth` check | `test_rows_beyond_the_quota_are_dropped` |
| M21 | pass an unknown position through as-is | `test_an_unknown_position_is_counted_missing_not_guessed` |
| M22 | share one accumulator between both envelopes | a new test asserting a matchup failure leaves membership coverage intact |

- [ ] **Step 9: Commit**

```bash
git add services/roster-scope contracts/collector-registry.yaml \
        contracts/signal-envelope/collectors/roster-scope.json
git commit -m "roster-scope: publish scope_matchup_weekly with its own coverage

Separate accumulator per list, so a matchup resolution failure cannot
mask a healthy player scope."
```

---

### Task 7: Serve `GET /scope/matchups` and wire identity

**Files:**
- Modify: `services/roster-scope/roster_scope/main.py`
- Modify: `helm/values/roster-scope/values.yaml`
- Test: `services/roster-scope/tests/test_routes.py`

**Interfaces:**
- Consumes: `app.state.collector_spec` (never a module-level global); the existing `/scope/players` handler as the model.
- Produces: `GET /scope/matchups` returning `{"season": int, "week": int, "slots": [...], "count": int, "captured_at": str | None}`.

- [ ] **Step 1: Write the failing tests**

```python
# append to services/roster-scope/tests/test_routes.py

def test_scope_matchups_returns_the_matchup_slots(client, auth_headers):
    response = client.get("/scope/matchups", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert "slots" in body
    assert "count" in body
    assert body["count"] == len(body["slots"])


def test_scope_matchups_requires_a_token(client):
    assert client.get("/scope/matchups").status_code == 401
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/roster-scope && uv run pytest tests/test_routes.py -k matchups -v`
Expected: FAIL — 404.

- [ ] **Step 3: Add the route**

A plain `@app.get("/scope/matchups")` after the `build_collector_app` call, reaching state via `app.state.collector_spec`. Body is a call and a return; any shaping goes in `matchups.py`.

- [ ] **Step 4: Publish it at the edge**

In `helm/values/roster-scope/values.yaml`, add `/scope/matchups` to `gateway.publicPaths`. **Without this it 404s at the gateway while working in-cluster.**

- [ ] **Step 5: Wire identity**

Set `PLAYER_IDENTITY_URL` to the in-cluster service URL (`http://player-identity:8002`) in the same values file, replacing `""`.

**Do not remove the stub-resolver fallback.** It must still be selected when the variable is empty — tests and local runs depend on it.

- [ ] **Step 6: Verify the rendered chart**

```bash
helm template roster-scope helm/charts/generic-service \
  -f helm/values/roster-scope/values.yaml \
  -f infra/gitops/envs/local/roster-scope/values.yaml
```
Confirm `PLAYER_IDENTITY_URL` is the service URL and the `HTTPRoute` lists `/scope/matchups`. Diff the rendered Deployment against another collector's; it should differ only by name, port, env and routes.

- [ ] **Step 7: Run everything**

```bash
cd services/roster-scope && uv run pytest -q
cd ../.. && uv run --no-project --with pyyaml==6.0.3 --with pytest==9.0.3 \
  --with jsonschema==4.26.0 pytest tests/ -q
uv lock --check
uv run --no-project --with ruff==0.14.4 ruff check services/ libs/
```

- [ ] **Step 8: Commit**

```bash
git add services/roster-scope helm/values/roster-scope/values.yaml
git commit -m "roster-scope: serve GET /scope/matchups and wire PLAYER_IDENTITY_URL"
```

---

### Task 8: Prove the gate, then open the PR

**Files:**
- Modify: `docs/collectors.md` — document `ScopeClient` and `IdentityClient` as the seams a collector uses.

**The gate before Plan B:** `roster-scope` publishes **both** lists to the lake with identity live.

- [ ] **Step 1: Run the container with the chart's real environment**

Build from the worktree root and run with the rendered ConfigMap plus both Secrets, under the chart's memory limit:

```bash
docker build -f services/roster-scope/Dockerfile -t roster-scope:local .
docker run --rm --memory=256m -e ... roster-scope:local
```

**A live container run is the only thing that has found a real bug in this project** — every genuine defect during 8B came from one, and none from a unit suite. Exercise `/scope/players`, `/scope/matchups`, and a capture, and confirm both envelopes reach the lake.

- [ ] **Step 2: Confirm the image contains your code**

Exercise a line you added (e.g. a matchup slot key) and see it take effect. uv's build cache can serve a stale wheel; if behaviour contradicts the source, suspect that first.

- [ ] **Step 3: Update `docs/collectors.md`**

Add a section: a collector reads its scope via `ScopeClient` (from the lake, fail closed) and resolves ids via `IdentityClient` (never adopting a refusal). State that there is **no unnarrowed fallback** and why.

- [ ] **Step 4: Run the `pr-uat` skill**

Mandatory in this repo before any PR.

- [ ] **Step 5: Open a draft PR**

```bash
gh pr create --draft --base main \
  --title "Scope narrowing Plan A: ScopeClient, IdentityClient, and roster-scope's matchup list"
```

Body must state: the dependency on **#73** (unmerged), every mutation and its result, the container evidence, and an explicit list of what was **not** verified. **Never push to `main`, never force-push, never merge.**

---

## Self-Review

**Spec coverage:** `ScopeClient` → Task 1. `IdentityClient` + no-refusal rule + chunking → Task 2. Caching → Task 3. Defensive aliases → Task 4. Matchup quotas → Task 5. Second envelope + separate coverage → Task 6. Route + identity wiring → Task 7. Gate + docs → Task 8. **Not in this plan, by design:** collector retrofits (Plan B/C), and the behavioural "fetched only scoped players" test, which needs a retrofitted collector to test against.

**Type consistency:** `Scope`/`ScopeUnavailable`/`ScopeClient.fetch` used identically in Tasks 1 and 8. `ResolveQuery`/`IdentityClient.resolve_many`/`BATCH_LIMIT` identical in Tasks 2 and 3. `MATCHUP_RULES`/`expected_matchup_slots()` defined in Task 5, consumed in Task 6. `resolve_matchup_slots` defined and consumed in Task 6.

**Known deferral:** Task 6 Step 3 and Task 7 Step 3 describe implementations by reference to an existing module rather than quoting full code, because both must mirror `scope.py`'s structure exactly and quoting a divergent version here would licence divergence. Both name the file to read first.
