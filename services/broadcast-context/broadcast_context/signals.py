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

   **That objection applies to the malformed-`as_of` 422 this module DOES
   implement, and two of its three holes are unfixable from here.** Stated
   rather than left for a reviewer to find:

   * `?as_of=garbage` with rows present — **422**. The predicate runs.
   * `?game_id=nope&as_of=garbage` with rows present — **422**. This used to
     be a 200: the equality filter returned before the instant was parsed.
     Fixed by validating ahead of the `ROW_FILTERS` loop.
   * `?as_of=garbage` against an **empty cache** — **200**. `signal_matches`
     is never invoked, because there are no rows to invoke it on.
   * `?week=12&as_of=garbage` when the cache holds another week — **200**.
     The shared router drops the envelope on scope before any row is reached.

   The last two are structural: `CollectorSpec` exposes one hook and it is
   per-row, so a query that reaches no row reaches no validation. Closing them
   needs a pre-filter seam in `collector-core` — out of scope here, and the
   concrete thing that seam would buy if "require `as_of`" is ever revisited.
   `tests/test_as_of.py` pins all four, the two residuals included, so this
   table cannot quietly stop being true.
3. **Every row is self-describing, so a consumer can filter point-in-time
   without `as_of` at all.** `first_observed_at`, `flex_status`,
   `previous_window_id`, `observed_window_count` and `point_in_time_basis`
   are on every row, and `first_observed_at` now covers **every** published
   point-in-time fact — the slot count included — so `as_of` is a
   server-side convenience over a filter the client could apply itself, not
   the only place the information exists.

**What a bare `GET /signals` really returns, stated plainly.** An earlier
revision of this docstring claimed that asking this collector for a past week
returns nothing, because the router filters envelopes by scope
(`routes.py`) and the scope's `week` is `CAPTURE_WEEK`. That was **wrong**,
and it mattered, because the decision above rested on it. The capture is
**season-wide**: one envelope carries all 272 games across all 18 weeks, each
row with its own `week` field. A bare `GET /signals` in week 15 therefore
returns week-12 rows showing their post-flex windows — the spec's named
failure, reachable through the fleet-standard call rather than only through
`POST /refresh {"week": N}`.

That is not a data defect: those rows are honest, and reason 3 above is what
makes them usable. But a consumer that wants the state as it stood must pass
`as_of`, and the reason it is not forced to is reasons 1 and 2, not any claim
that the leak is unreachable.

**When `as_of` is supplied it is applied strictly, and a row with no usable
instant is EXCLUDED.** A record that passes a point-in-time filter because its
timestamp is null is precisely the leak the guard exists to close, so the
predicate fails closed rather than open.

**The predicate is only half the guard; the other half is what
`first_observed_at` covers.** It is derived from the whole published
point-in-time state — `window_id`, `kickoff_at` **and** `games_in_window` —
because `games_in_window`, `is_standalone` and `distribution` are recomputed
from today's slate on every pass. Leaving the count out let a game whose own
window never moved keep its early instant, pass this predicate, and arrive
carrying slot facts from after the cutoff. See `ObservedState` in
`history.py`.

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

    **`as_of` is validated FIRST, before any equality filter can return.**
    That ordering is the whole of it, and getting it wrong was a real bug:
    with the `ROW_FILTERS` loop above the parse,
    `?game_id=no-such-game&as_of=garbage` returned **200 and an empty list**,
    because the first row failed the `game_id` comparison and the malformed
    instant was never looked at. A consumer who believes they are getting
    point-in-time filtering silently is not — which is this collector's own
    failure mode arriving through a typo instead of through a flex, and it is
    the `pos=FLEX` "looks like a quiet week" failure CLAUDE.md names.

    Validation is deliberately separated from application: the parse happens
    unconditionally, the comparison only after the equality filters have had
    their say, so a mismatched `game_id` still wins over an `as_of` that would
    have admitted the row.
    """
    as_of: datetime | None = None
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

    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False

    if as_of is not None:
        return _known_by(row, as_of)

    return True
