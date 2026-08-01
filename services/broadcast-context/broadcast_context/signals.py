"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns. A parameter
that is neither universal nor listed in `SUPPORTED_FILTERS` is rejected with
422 rather than silently ignored.

--------------------------------------------------------------------------
`as_of` — the point-in-time guard
--------------------------------------------------------------------------

The spec's named failure mode is retroactive certainty: a consumer reading a
past week's broadcast state sees the flexed outcome and fits a model on
foreknowledge it could never have had. Its guard is *"require an `as_of`
parameter on historical queries and assert `announced_at <= as_of` on every
returned record"*.

**`as_of` is a filter here, and it is optional rather than required.** That is
a disclosed deviation, argued in the README, and it has three parts:

1. The five-route contract is fleet-wide. `scripts/smoke-test.sh` asserts a
   bare `GET /signals` returns 200 with an `envelopes` array for **every**
   registered collector, and a generator that can consume one collector is
   supposed to be able to consume all of them.
2. The shared router hands a collector exactly one hook — a per-row boolean
   predicate. Enforcing "this parameter is required" from inside a row loop
   means the 422 fires only when rows happen to exist, so the same query
   answers 200 against an empty cache and 422 against a populated one. A guard
   whose firing depends on cache state is not a guard.
3. The leak it defends against is narrower than the spec assumes on this API.
   `/signals` serves `CaptureState`, which holds exactly one capture, and the
   router drops any envelope whose scope does not match a requested
   `season`/`week` — so asking this collector for a past week returns nothing
   at all rather than today's state wearing that week's label. The residual
   leak is real but specific: `POST /refresh {"week": N}` re-scopes the cache
   to week N using **today's** upstream, and `as_of` is what closes it.

**When `as_of` is supplied it is applied strictly, and a row with no usable
instant is EXCLUDED.** A record that passes a point-in-time filter because its
timestamp is null is precisely the leak the guard exists to close, so the
predicate fails closed rather than open.

**What it compares against when `announced_at` is null — which is every row
today.** The feed carries no publication instant, so `announced_at` is null
with a reason and the filter falls back to `first_observed_at`: the capture
instant of the first snapshot in our own lake carrying this game's current
broadcast state. That is an **upper bound** on the announcement, so
`first_observed_at <= as_of` implies the record was certainly already public
at `as_of`. The substitution can withhold a record announced before `as_of`
that we observed after it; it can never admit one that was not yet announced.
Under-claiming is the safe error for a foreknowledge guard. Every row carries
`point_in_time_basis` saying which of the two was used, so the fallback is
never silent.
"""

from collections.abc import Mapping
from datetime import datetime

from fastapi import HTTPException

__all__ = ["AS_OF", "ROW_FILTERS", "SUPPORTED_FILTERS", "signal_matches"]

AS_OF = "as_of"

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "game_id",
    "window_id",
    "flex_status",
    AS_OF,
)

# The subset of the above compared by plain string equality. `as_of` is not
# here: it is an instant comparison against a different field, handled below.
# The universal three are already applied by the router before a row arrives.
ROW_FILTERS: tuple[str, ...] = ("game_id", "window_id", "flex_status")


def _parse_instant(value: str) -> datetime | None:
    """An RFC 3339 instant, or `None` if it is not one.

    A **naive** timestamp is rejected rather than assumed to be UTC. "Some
    timezone the caller forgot to state" is not an instant, and guessing one
    here would shift the point-in-time boundary by hours in whichever
    direction the caller happened to mean — silently, and only for the rows
    near the boundary.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _known_by(row: dict, as_of: datetime) -> bool:
    """Whether this row's state was certainly already public at `as_of`.

    Prefers the row's own `announced_at`; falls back to `first_observed_at`,
    which is a sound upper bound. A row carrying neither — or carrying an
    unparseable one — is excluded. **Fails closed**: admitting a record whose
    timestamp is missing is the exact leak this filter exists to close.
    """
    basis = row.get("announced_at") or row.get("first_observed_at")
    if not isinstance(basis, str):
        return False
    instant = _parse_instant(basis)
    if instant is None:
        return False
    return instant <= as_of


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int. `str()` on the row side
    is the fix, and forgetting it is the single most common bug here.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False

    if AS_OF in params:
        as_of = _parse_instant(params[AS_OF])
        if as_of is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{AS_OF} must be an RFC 3339 instant with an explicit "
                    f"offset, e.g. 2026-09-15T12:00:00Z; got {params[AS_OF]!r}"
                ),
            )
        return _known_by(row, as_of)

    return True
