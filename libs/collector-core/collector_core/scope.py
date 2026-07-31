"""Reading the published scope, so a collector fetches only what matters.

Deliberately reads the LAKE, never `roster-scope` over HTTP. The lake is
append-only and already written by every scope capture, so the last good
scope survives a `player-identity` outage -- which is what stops one service
being a fleet-wide stop. `roster-scope`'s HTTP routes exist for the
out-of-repo generator and for operators; collectors do not use them.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from .failure import fail_capture
from .lake import alist_keys, aread

SCOPE_COLLECTOR = "roster-scope"

T = TypeVar("T")


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
        """The newest usable scope for `week`, falling back to `week - 1`.

        `roster-scope`'s cadence is weekly; a collector calling this can run
        hourly-to-volatile. Without the fallback, every week rollover fails
        every collector closed until the new week's capture lands -- even
        though last week's scope is sitting right there in the lake and is
        ~99% correct. `roster_scope.scope.load_previous_scope` already
        solves the identical problem for the version ledger; this mirrors
        its two-candidate shape (`week`, then `week - 1`, never further --
        two weeks stale is a different judgement than one, and is left to
        raise rather than silently reached for).

        A candidate is skipped, not just an absent partition: a total
        capture failure still writes a `present: 0` envelope for the
        *current* week (see `roster_scope.capture.capture_scope`'s
        ledger-unavailable path), so an envelope that exists but resolved
        zero members must fall back exactly like a missing one does, or the
        fallback would never fire for the failure mode it exists for.

        The raised reason describes `week`'s own state, not `week - 1`'s --
        an operator debugging "why did this collector narrow to nothing"
        wants to know what happened to the week they asked for, not that the
        fallback also came up empty.

        `Scope.captured_at` on a fallback result is `week - 1`'s, which is
        what makes the fallback visible: `age_seconds(now)` reads roughly a
        week rather than however stale the scope would read on a healthy
        capture, with no separate flag required to know a fallback happened.
        """
        reason = "scope_unavailable"
        for candidate_week in (week, week - 1):
            if candidate_week < 1:
                continue

            keys = await alist_keys(
                self._lake, SCOPE_COLLECTOR, signal_type, season, candidate_week
            )
            if not keys:
                continue

            # `list_keys` returns captured_at order, so the newest is last.
            envelope = await aread(self._lake, keys[-1])
            members = frozenset(
                row["player_id"]
                for row in envelope["signals"]
                if row.get("player_id")
                # `grace` is intended: it is exactly the "keep fetching for
                # someone who fell out this week" state `GRACE_WEEKS` exists
                # for. `excluded` is not -- `roster_scope.scope._carry_forward`
                # emits an `excluded` row exactly once, in the version where
                # the departure happens, specifically to ANNOUNCE it, and a
                # collector reading it as still-in-scope would keep fetching
                # for a player who is gone. Rows with no `membership_status`
                # at all (every `scope_matchup_weekly` row) are unaffected --
                # `.get(...)` reads as `None`, which never equals the literal
                # string, so this only ever drops a genuinely `excluded` row.
                if row.get("membership_status") != "excluded"
            )
            if not members:
                if candidate_week == week:
                    reason = "scope_empty"
                continue

            return Scope(
                members=members,
                captured_at=_parse_captured_at(envelope["captured_at"]),
                signal_type=signal_type,
            )

        raise ScopeUnavailable(reason)

    async def fetch_union(
        self, signal_types: Sequence[str], season: int, week: int
    ) -> Scope:
        """The union of several scope lists -- membership ∪ matchup.

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
        `age_seconds` describes the stalest input rather than the freshest --
        a week-old matchup list must not hide behind a fresh membership one.
        """
        members: set[str] = set()
        captured_at: datetime | None = None
        # Sequential, not asyncio.gather: fail-closed means the first
        # unavailable signal type should raise immediately rather than
        # waiting on the rest of the fleet's lake reads.
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


async def fetch_scope_or_fail(
    fetch: Callable[[], Awaitable[T]], **failure_context
) -> T:
    """Resolve a collector's narrowing seam, or fail the whole pass closed.

    `fetch` is a zero-argument callable returning an awaitable — whatever this
    collector must have before it is allowed to touch its upstream. Usually one
    `ScopeClient` call; sometimes more (`usage-share` also builds its
    `IdentityClient` in here, because a scope with no identity seam to join
    through narrows to nothing just as completely as no scope does). It is a
    callable rather than an awaitable so a *synchronous* raise inside it — a
    missing `PLAYER_IDENTITY_URL`, say — is caught here too.

    `**failure_context` is passed straight to `fail_capture`: `collector`,
    `signal_types`, `adapter`, `now`, `scope`, `lake`, `metrics`, `expected`,
    `source_ref`. Do **not** pass `reason` — this function supplies it.

    **Two `except` arms, and the second one is the whole reason this is
    shared.** It existed in three near-identical thirty-five-line comment
    blocks across three collectors, which at twenty-six collectors means what
    gets copied is one of the three — and the copy that drops the second arm
    fails *silently*:

    * `ScopeUnavailable` is what `ScopeClient` raises when the lake **answered**
      and held nothing usable. `exc.reason` is forwarded rather than flattened
      to a literal, because `scope_unavailable` ("roster-scope published nothing
      for this week or the last"), `scope_empty` ("it published an envelope that
      resolved zero members") and a collector's own reasons such as
      `identity_unavailable` ("this pod was never pointed at player-identity")
      have three different fixes. Collapsing them costs an operator the one
      thing the envelope could have told them.

    * **Anything else.** The scope read is I/O and the lake can fail *outright*:
      `S3LakeWriter.list_keys`/`read` propagate botocore errors and `json`
      decode errors untouched, and `_parse_captured_at` raises `ValueError` on a
      timestamp it does not recognise. Without this arm every one of those
      escapes the capture coroutine entirely — **no `present: 0` envelope, no
      `collector_capture_failures_total`, just a log line from whoever
      dispatched the pass.** That is precisely the "we failed" versus "we never
      tried" distinction `collector_core.failure` exists to record. No explicit
      `reason=` here on purpose: `fail_capture` falls back to
      `CollectorMetrics.reason_for(exc)`, which classifies a decode or timestamp
      failure as `malformed` and anything else as `unknown` — both true
      statements, and neither mistakable for `scope_unavailable`.

    Never returns normally on failure: `fail_capture` writes one `present: 0`
    envelope per signal type and then re-raises, so `CaptureState` cannot
    install an empty capture over the last good one. Call this **before** the
    first upstream request — that ordering is the whole of failing closed, and
    moving it below the fetch looks harmless while silently restoring the cost
    the narrowing exists to remove.
    """
    try:
        return await fetch()
    except ScopeUnavailable as exc:
        await fail_capture(exc, reason=exc.reason, **failure_context)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        await fail_capture(exc, **failure_context)
