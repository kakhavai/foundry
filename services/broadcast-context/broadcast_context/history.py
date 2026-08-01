"""Point-in-time discipline: the flex fields, derived from our own lake.

This module is the collector's whole reason for existing, and the spec's named
failure mode is what it defends against:

> Retroactive certainty. `/signals` serves the current broadcast state for a
> past week, so a game flexed into Sunday night in Week 12 appears to have
> always been a Sunday night game, and any model fit on that history has
> foreknowledge it could never have had at projection time. The lake is
> append-only and therefore correct; the API is where the leak happens.

Three consequences, all of them counter-intuitive enough to be worth stating
before the code:

**The flex fields are derived by comparing against our own prior snapshots,
never read from upstream.** `games.csv` publishes the *current* schedule and
nothing else — no `flex_status` column, no previous window, no announcement
instant. A collector that invented a flex history out of one fetch would be
manufacturing exactly the foreknowledge the guard exists to remove.

**On a first capture every game is `original`, and that is correct.** There is
no history, so there is no change to report. The failure would be to make one
up.

**`announced_at` is null, and `first_observed_at` is not the same fact.** The
spec is explicit that `announced_at` must be stamped from the source's
publication time and never inferred from the fetch time. This feed carries no
publication instant, so `announced_at` is null with a reason on every row.
What we *do* have is the capture instant of the first snapshot in our own lake
carrying this game's current broadcast state, which is an **upper bound** on
the announcement: the change was announced at or before the moment we first
saw it.

That bound is sound in the direction the guard cares about. Filtering with
`first_observed_at <= as_of` admits only records that were certainly already
public at `as_of` — it can withhold a record announced before `as_of` that we
did not observe until after, but it can never admit one that was not yet
announced. Under-claiming knowledge is the safe error for a foreknowledge
guard; over-claiming is the leak. `signals.py` applies it, and every row
carries `point_in_time_basis` so the substitution is never silent.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from collector_core.lake import LakeWriter, alist_keys, aread

from .windows import PRIMETIME_WINDOWS

__all__ = [
    "FLEX_IN",
    "FLEX_ORIGINAL",
    "FLEX_OUT",
    "FLEX_STATUSES",
    "FLEX_TIME_CHANGED",
    "MAX_HISTORY_SNAPSHOTS",
    "FlexVerdict",
    "History",
    "ObservedState",
    "classify_transition",
    "derive_flex",
    "flex_is_evidenced",
    "read_history",
]

FLEX_ORIGINAL = "original"
FLEX_IN = "flexed_in"
FLEX_OUT = "flexed_out"
FLEX_TIME_CHANGED = "time_changed"

FLEX_STATUSES: tuple[str, ...] = (
    FLEX_ORIGINAL,
    FLEX_IN,
    FLEX_OUT,
    FLEX_TIME_CHANGED,
)

# How many prior snapshots one pass will read back. The digest gate in
# `capture.py` means a snapshot is appended only when the published rows
# actually change, so a season's history is a few dozen objects rather than
# one per pass — this cap is a bound on a pathological case, not the normal
# path.
#
# The cap keeps the NEWEST snapshots, which is the only truncation that is
# safe here: `previous_window_id` and the two-snapshot evidence check both
# read the recent end of the chain. Dropping the oldest can only move
# `first_observed_at` LATER than the truth, which under-claims knowledge —
# the same safe direction the module docstring argues for.
MAX_HISTORY_SNAPSHOTS = 64


@dataclass(frozen=True)
class ObservedState:
    """One broadcast state this collector has evidence of, and when.

    `captured_at` is the capture instant of the snapshot in which this state
    FIRST appeared, not of the latest snapshot that still shows it — which is
    what makes it usable as a lower bound on how long the state has held.

    **`games_in_window` is part of the state, and that is a correctness fix
    rather than a convenience.** It is a count over the whole slate for the
    slot, so it changes when a *different* game moves. Leaving it out made
    `first_observed_at` describe only `(window_id, kickoff_at)` while the
    published row also carried `games_in_window`, `is_standalone` and
    `distribution` recomputed from today's slate — so a game whose own window
    never moved kept its early `first_observed_at`, passed the `as_of` filter,
    and arrived carrying slot facts from **after** the cutoff. Reproduced: two
    16:25 games, one flexed away, and the survivor read `first_observed_at:
    2026-09-15`, `flex_status: original`, `games_in_window: 1`,
    `is_standalone: true`, `distribution: national` — retroactive certainty
    reaching a consumer through a game that never changed.

    With the count in the state, that row's `first_observed_at` advances to
    the pass that saw the change, so an earlier `as_of` **withholds** it. Same
    safe direction as everything else here: under-claim, never over-claim.

    Two dimensions, deliberately kept apart:

    * `broadcast_state` — everything the row publishes as a point-in-time
      fact. Drives dedupe and `first_observed_at`.
    * `window_state` — this game's *own* window and kickoff. Drives
      `flex_status`, `previous_window_id` and the evidence check, because a
      slot-population change is not a flex and must not be reported as
      `time_changed`.
    """

    window_id: str | None
    kickoff_at: str | None
    games_in_window: int | None
    captured_at: str

    @property
    def broadcast_state(self) -> tuple[str | None, str | None, int | None]:
        return (self.window_id, self.kickoff_at, self.games_in_window)

    @property
    def window_state(self) -> tuple[str | None, str | None]:
        return (self.window_id, self.kickoff_at)


@dataclass(frozen=True)
class FlexVerdict:
    """The four flex fields for one game, plus the evidence behind them."""

    flex_status: str
    previous_window_id: str | None
    first_observed_at: str
    evidence: tuple[ObservedState, ...]

    @property
    def observed_window_count(self) -> int:
        return len(self.evidence)


def classify_transition(previous: str | None, current: str | None) -> str:
    """Which of the spec's three change statuses one window move is.

    `flexed_in` / `flexed_out` are defined against `PRIMETIME_WINDOWS` — the
    three national primetime packages — because that is what flex scheduling
    actually does: it moves a game between the Sunday afternoon slates and
    Sunday/Monday/Thursday night. A move between two afternoon windows, or a
    kickoff that shifts inside the same window, is `time_changed`.
    """
    if previous == current:
        return FLEX_TIME_CHANGED
    previous_is_primetime = previous in PRIMETIME_WINDOWS
    current_is_primetime = current in PRIMETIME_WINDOWS
    if current_is_primetime and not previous_is_primetime:
        return FLEX_IN
    if previous_is_primetime and not current_is_primetime:
        return FLEX_OUT
    return FLEX_TIME_CHANGED


def _window_transitions(
    chain: Sequence[ObservedState],
) -> list[ObservedState]:
    """The chain collapsed to the states where THIS GAME's window moved.

    The full chain also advances when only `games_in_window` changed — a
    different game moved, and this one did not. Reading `chain[-2]` directly
    would then report `time_changed` for a game whose kickoff never shifted,
    because `classify_transition` returns `time_changed` whenever the two
    windows are equal. That is a fabricated flex, and it would then need
    evidence it does not have.
    """
    collapsed: list[ObservedState] = []
    for state in chain:
        if collapsed and collapsed[-1].window_state == state.window_state:
            continue
        collapsed.append(state)
    return collapsed


def derive_flex(
    prior: Sequence[ObservedState],
    *,
    window_id: str | None,
    kickoff_at: str | None,
    games_in_window: int | None,
    now: str,
) -> FlexVerdict:
    """The flex fields for one game, from its own observed history.

    `prior` is the game's chain of distinct observed states, oldest first, as
    `read_history` built it. `now` is this capture's `captured_at`.

    **`first_observed_at` and `flex_status` read different dimensions of the
    same chain, and that separation is the fix for the slate-fact leak.**
    `first_observed_at` advances whenever any published point-in-time fact
    changes, `games_in_window` included, so an `as_of` before a slot change
    withholds the row instead of admitting it with a post-cutoff count.
    `flex_status` and `previous_window_id` read only this game's own
    `window_state`, so a slot-population change is never reported as a flex.

    Three cases for the instant, and the middle one is the easy one to get
    wrong:

    * **no history** — first sight. `original`, no previous window, and
      `first_observed_at` is this capture.
    * **history whose newest state is the one we just built** — nothing
      changed *this* pass, but something may have changed on an earlier one.
      `first_observed_at` stays pinned to the snapshot that first showed this
      state. Recomputing it as `now` here would make every pass restate an old
      fact as brand new, which is the retroactive-certainty failure arriving
      by the back door — and it would make the digest gate fire every pass, so
      the lake would fill with snapshots differing only in that field.
    * **the state changed this pass** — `first_observed_at` is this capture.
    """
    current = ObservedState(window_id, kickoff_at, games_in_window, now)

    if not prior:
        return FlexVerdict(FLEX_ORIGINAL, None, now, (current,))

    newest = prior[-1]
    if newest.broadcast_state == current.broadcast_state:
        chain: tuple[ObservedState, ...] = tuple(prior)
        first_observed_at = newest.captured_at
    else:
        chain = (*prior, current)
        first_observed_at = now

    transitions = _window_transitions(chain)
    if len(transitions) < 2:
        return FlexVerdict(FLEX_ORIGINAL, None, first_observed_at, chain)

    previous, latest = transitions[-2], transitions[-1]
    return FlexVerdict(
        classify_transition(previous.window_id, latest.window_id),
        previous.window_id,
        first_observed_at,
        chain,
    )


def flex_is_evidenced(status: str, evidence: Sequence[ObservedState]) -> bool:
    """The spec's consistency check, generalised to the status it forgot.

    > any game with `flex_status != original` has at least two distinct
    > snapshots with differing `window_id` in the lake — one snapshot plus a
    > non-original flex status means the earlier state was never captured.

    Applied literally that rule rejects a legitimate `time_changed`, which by
    construction has ONE `window_id` and two different kickoff instants. So
    the check is: a change requires at least two observed states, and the two
    must differ in the dimension the claimed status is about — `window_id`
    for `flexed_in`/`flexed_out`, this game's whole `window_state` for
    `time_changed`. Disclosed in the README as a refinement rather than taken
    silently.

    **`window_state`, not `broadcast_state`.** Two states differing only in
    `games_in_window` are evidence that a *different* game moved, and they
    must not be accepted as evidence that this one did.

    `original` claims nothing, so it needs no evidence and returns `True` on
    any chain including an empty one — the caller's own `all(...)` over rows
    is paired with a length assertion in the tests for exactly that reason.
    """
    if status == FLEX_ORIGINAL:
        return True
    if len(evidence) < 2:
        return False
    if status in (FLEX_IN, FLEX_OUT):
        return len({state.window_id for state in evidence}) >= 2
    return len({state.window_state for state in evidence}) >= 2


@dataclass(frozen=True)
class History:
    """Everything one pass reads back out of its own partition.

    A record rather than a tuple because it grew a third member the moment
    the slate guard needed a baseline, and a 3-tuple's third element is the
    one every caller unpacks wrong.
    """

    chains: dict[str, list[ObservedState]]
    truncated: int
    # Games per week in the newest snapshot that actually carried rows. The
    # baseline for "did this week SHRINK", which is the only cheap way to
    # notice a document truncated mid-stream: the tail week keeps 13-15 games
    # that all have kickoffs, so neither of the slate guard's other two arms
    # fires. Empty on a first capture, which correctly makes the check inert
    # rather than a guess.
    week_counts: dict[int, int]


def _fold(
    history: dict[str, list[ObservedState]],
    rows: Iterable[dict],
    captured_at: str,
) -> None:
    """Append one snapshot's rows onto the per-game chains, deduped.

    A state identical to the chain's newest is dropped rather than appended:
    the chain records *distinct* observed states, so `len(chain)` is the
    number of things that actually happened to the game rather than the number
    of times we looked at it.
    """
    for row in rows:
        game_id = row.get("game_id")
        if not game_id:
            continue
        state = ObservedState(
            row.get("window_id"),
            row.get("kickoff_at"),
            row.get("games_in_window"),
            captured_at,
        )
        chain = history.setdefault(game_id, [])
        if chain and chain[-1].broadcast_state == state.broadcast_state:
            continue
        chain.append(state)


def _week_counts(rows: Iterable[dict]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in rows:
        week = row.get("week")
        if isinstance(week, int):
            counts[week] = counts.get(week, 0) + 1
    return counts


async def read_history(
    lake: LakeWriter,
    *,
    collector: str,
    signal_type: str,
    season: int,
    week: int,
    limit: int = MAX_HISTORY_SNAPSHOTS,
) -> History:
    """Every game's chain of distinct observed states, plus two baselines.

    Reads this collector's own partition of the append-only lake, oldest
    snapshot first — `lake_key` puts `captured_at` at the front of the object
    name, so a sorted listing is already chronological.

    **A failure here is not caught**, and that is deliberate. Every lake call
    goes through `alist_keys`/`aread`, so a botocore error or a malformed
    object propagates and `capture.py` turns it into a `present: 0` envelope
    that re-raises. Degrading to "no history" instead would publish a snapshot
    claiming `original` for games this collector had previously recorded as
    flexed — into an append-only lake, where that claim then becomes the
    evidence a later pass reads back. An object-store blip would rewrite
    history. Losing a pass is recoverable; that is not.

    Reads only the partition this capture writes to, `(season, week)`. An
    operator who advances `CAPTURE_WEEK` mid-season, or backfills with
    `POST /refresh {"week": N}`, starts a fresh history in the new partition,
    so every game reads `original` there. That is honest — no history means no
    claim — and it is the reason the deployed `CAPTURE_WEEK` is left alone.
    """
    keys = await alist_keys(lake, collector, signal_type, season, week)
    truncated = max(0, len(keys) - limit)

    chains: dict[str, list[ObservedState]] = {}
    week_counts: dict[int, int] = {}
    for key in keys[len(keys) - min(len(keys), limit) :]:
        body = await aread(lake, key)
        captured_at = body.get("captured_at")
        if not captured_at:
            continue
        rows = body.get("signals") or ()
        _fold(chains, rows, captured_at)
        if rows:
            # `if rows` matters: `fail_capture` writes a `present: 0` envelope
            # with an empty `signals` array, and letting that reset the
            # baseline to {} would make the shrink check inert on exactly the
            # pass after an outage — the pass most likely to be short.
            week_counts = _week_counts(rows)
    return History(chains=chains, truncated=truncated, week_counts=week_counts)
