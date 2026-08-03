"""The capture pass: fetch -> join -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

--------------------------------------------------------------------------
Four feeds, two of them fatal
--------------------------------------------------------------------------

| feed | size | fatal? | what its loss costs |
|---|---|---|---|
| `play_by_play_<season>.csv.gz` | 18.22 MiB | yes | everything |
| `pbp_participation_<season>.csv` | 46.82 MiB | yes | 7 of 16 fields, and the guard |
| `players.csv.gz` | 2.39 MiB | no | `front_continuity_index`, `key_absences` |
| `injuries_<season>.csv.gz` | 0.12 MiB | no | `key_absences` |

~67.6 MiB on a changed pass. Participation is **fatal rather than
field-level**, which departs from `team-scheme`'s treatment of the same feed
and is deliberate: there it bought one field of thirteen, here it carries
pressure rate, its adjusted counterpart, blitz rate, pressure when blitzing,
pressure-to-sack conversion, mean release time faced and front continuity —
and the guard's whole independent variable. A `defensive_front_strength` row
with every pressure column null is a complete-looking row describing nothing,
which is worse than a `present: 0` envelope that says so.

--------------------------------------------------------------------------
Conditional GET across four feeds
--------------------------------------------------------------------------

Every feed is conditional. A single `304` must not end the pass — that would
suppress a genuinely changed feed while `mark_unchanged` advanced
`last_capture_at`, so staleness never alerts. So `UpstreamUnchanged` is caught
**per feed** in `_fetch` and nowhere else; the pass is unchanged only when
`every_feed_unchanged` says every feed answered `304`; and if any feed changed,
the ones that did not are re-fetched against a throwaway `ETagStore`.

The related ordering hazard — ETag-gating a small auxiliary feed ahead of the
large one it precedes — cannot occur here for a structural reason rather than
by choosing an order: **no adapter takes another adapter's result.**
`participation` returns `(game_id, play_id) -> RushSnap` and the join to
play-by-play's dropback index happens below, in `_fold`. `team-scheme` passes
its play index down into the participation fetch instead, which means a pass
where play-by-play answers `304` folds participation against an **empty**
index, publishes zero charted rows, and is not repaired by the unconditional
re-fetch that follows (it re-fetches play-by-play, not the fold). Reported as
a follow-up rather than patched from here.

--------------------------------------------------------------------------
The two things that are correctness, not style
--------------------------------------------------------------------------

**`coverage.expected` never derives from what succeeded.** `EXPECTED_FLOOR` is
32 — the league, fixed since the 2002 realignment — declared independently of
any fetch. **And so is the `present` predicate**: a team is present when it
has a published row with a pass-rush sample, not when some clause about the
fetch is satisfied. See `EXPECTED_FLOOR`'s comment for the spec deviation this
encodes.

**A failed capture still writes an envelope.** `fail_capture` writes one
`present: 0` envelope with a populated `errors` array, then re-raises. The
write makes the gap in the append-only lake explicit; the re-raise stops
`CaptureState` installing an empty capture over the last good one.
"""

import hashlib
import json
import logging
import statistics
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.conditional import ETagStore, UpstreamUnchanged
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from . import ratings as ratings_module
from .adapters import identity as identity_adapter
from .adapters import injuries as injuries_adapter
from .adapters import participation as participation_adapter
from .adapters import pbp as pbp_adapter
from .adapters import players as players_adapter
from .metrics import NOT_RUN, metrics
from .ratings import FrontTotals, build_rows
from .timing import TimingRegression

logger = logging.getLogger(__name__)

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "NULL_FIELD_REASON",
    "SIGNAL_TYPES",
    "STRENGTH",
    "capture_defensive_front",
    "every_feed_unchanged",
    "reset_published_digests",
]

COLLECTOR_NAME = "defensive-front"
CADENCE_CLASS = CadenceClass.WEEKLY

STRENGTH = "defensive_front_strength"
SIGNAL_TYPES = (STRENGTH,)

UPSTREAM_ADAPTER = "nflverse-defensive-front"

# **32, and this is a disclosed deviation from the spec's coverage clause.**
#
# The spec says `coverage.expected` is "all 32 defenses x the three declared
# `unit` values (96 rows); a team is present only when `overall`, `interior`
# and `edge` are all populated". Only `overall` is sourceable — see
# `ratings.UNITS` — so that predicate would make coverage **0.0 forever**, and
# a ratio pinned at zero cannot report anything else either: a truncated
# upstream, a dead join and a half-empty week would all read exactly the same
# as a healthy pass. The clause would swallow the metric it belongs to.
#
# So: 32 rows, one per defence, floored independently of any fetch. **Both
# halves move together** — the floor here AND the `present` predicate in
# `_strength_envelope`, which records a team on having a published row with a
# pass-rush sample. Changing one and leaving the other is the exact shape a
# mutation set that only attacks `expected` scores full marks against.
EXPECTED_FLOOR: dict[str, int] = {STRENGTH: 32}

REASON_NO_PASS_RUSH_SNAPS = "no_charted_pass_rush_snaps_for_this_team"
REASON_PLAYERS_UNAVAILABLE = "players_unavailable"
REASON_INJURIES_UNAVAILABLE = "injuries_unavailable"
REASON_IDENTITY_UPSTREAM_ERROR = identity_adapter.IDENTITY_UPSTREAM_ERROR
REASON_IDENTITY_UNRESOLVED = identity_adapter.IDENTITY_UNRESOLVED
REASON_TIMING_CONFOUND = "timing_confound_detected"
REASON_TIMING_GUARD_NOT_RUN = "timing_guard_could_not_run"
REASON_REFETCH_UNCHANGED = "unconditional_refetch_answered_304"

# **The failure lattice, as data rather than as two hand-written loops.** A
# feed is fatal unless it is named here with the reason its loss carries. The
# classifier in `capture_defensive_front._attempt` is the only thing that reads
# it, so the conditional first attempt and the unconditional re-fetch cannot
# classify the same feed differently — which they used to.
FATAL_FEEDS: tuple[str, ...] = ("pbp", "participation")
OPTIONAL_FEEDS: dict[str, str] = {
    "players": REASON_PLAYERS_UNAVAILABLE,
    "injuries": REASON_INJURIES_UNAVAILABLE,
}


class UpstreamContractViolation(RuntimeError):
    """A `304` answered to a request that carried no validator."""


# The two spec fields with no free upstream, emitted present-and-null with a
# machine-readable reason. An unsourceable value is a null with a reason —
# never a default, and never quietly dropped from the schema.
#
# Pro Football Reference publishes yards before contact at **season level and
# on the offence's side of the ball**, so it cannot be attributed to the
# defence that was faced; nothing free publishes it per play. It is a
# tackling-depth measurement and there is nothing free to derive it from —
# `adjusted_line_yards_allowed` is a *different* quantity (a weighting of
# total rushing yards, not a contact point), and substituting it would publish
# a plausible wrong number under a name a generator would trust.
NULL_YBC = (
    "no_free_per_play_yards_before_contact; PFR publishes it season-level and "
    "offense-side, so it cannot be attributed to the opposing defense"
)
NULL_FIELD_REASON: dict[str, str] = {
    "yards_before_contact_allowed_per_carry": NULL_YBC,
    "yards_before_contact_allowed_per_carry_adj": NULL_YBC,
}

# `(season, week, signal_type) -> the digest THIS process last published AND
# saw land in the lake`. In memory rather than read back from the lake: a pod
# restart then costs exactly one redundant snapshot, where reading the lake
# would make a restart find a matching digest, raise `UpstreamUnchanged`
# against an EMPTY cache, and serve nothing from `/signals`.
_PUBLISHED_DIGESTS: dict[tuple[int, int, str], str] = {}


def reset_published_digests() -> None:
    """Forget what this process has published. For tests only."""
    _PUBLISHED_DIGESTS.clear()


def _digest(payload: object) -> str:
    """A stable sha256 over anything JSON-serialisable.

    `hashlib`, never `hash()`. Python salts `hash()` on `str` per process, so a
    digest built from it would differ between two pods and between one pod's
    restarts — every pass would look changed and the gate would silently do
    nothing.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def every_feed_unchanged(unchanged: Mapping[str, bool]) -> bool:
    """True only when at least one feed was attempted and every one 304'd.

    **Extracted so the emptiness case is reachable by a test.** `all({})` is
    `True`, so without the `bool(unchanged)` a pass that attempted no feed at
    all would report itself unchanged: `last_capture_at` advances,
    `collector_upstream_unchanged_total` increments, no envelope is written,
    and nothing in the metrics says why.

    Today the caller cannot produce an empty map — a play-by-play failure ends
    the pass before this is reached, so `unchanged` always carries at least
    that feed. That makes the guard a defence of an invariant rather than of a
    live code path, and inlining it would make the defence **unprovable**: the
    mutation that removes `bool(unchanged)` survives any suite that cannot
    construct the input distinguishing the two.
    """
    return bool(unchanged) and all(unchanged.values())


async def _fetch(fn: Callable[..., Awaitable], **kwargs) -> tuple[object | None, bool]:
    """Run one adapter fetch, reporting a `304` as `(None, True)`.

    `UpstreamUnchanged` is caught here and **nowhere else**, so it can never
    reach `fail_capture` — which would write a `present: 0` envelope over a
    perfectly healthy capture. Every other exception propagates to the
    caller's own handler, which knows whether that feed is fatal.
    """
    try:
        return await fn(**kwargs), False
    except UpstreamUnchanged:
        return None, True


async def _refetch_unconditionally(
    unchanged: Mapping[str, bool],
    attempt: Callable[..., Awaitable[None]],
) -> None:
    """Re-fetch the feeds that 304'd, when some other feed did change.

    Against a **throwaway** `ETagStore`, so no `If-None-Match` is sent and the
    shared store keeps its (still-correct) entry for the next pass.

    **Through `attempt`, which is the same failure lattice the first request
    went through — and it takes a callable rather than the fetchers precisely
    so it cannot drift back.** An earlier revision called the fetchers here
    directly while this docstring claimed the lattice still applied. It did
    not: an optional feed that answered `304` and then failed its
    unconditional re-fetch propagated an uncaught `HTTPStatusError` out of the
    whole capture. No `fail_capture`, so no `present: 0` envelope; no
    `collector_capture_failures_total`; the only signal was
    `collector_staleness_seconds` climbing.

    That is the **ordinary weekly path**, not an exotic one — play-by-play
    changes whenever a game finishes and the roster and injury feeds routinely
    do not, so this re-fetch runs on essentially every real capture and a
    transient 5xx cost the entire pass instead of two fields.
    """
    for name, was_unchanged in unchanged.items():
        if was_unchanged:
            await attempt(name, conditional=False)


def _recent_weeks(weeks: set[int]) -> frozenset[int]:
    """The last `FRONT_WINDOW_WEEKS` sampled weeks — "who is playing now".

    Taken from the weeks actually sampled rather than counted back from the
    requested week, so a bye or an unplayed week does not silently shrink the
    window.
    """
    return frozenset(sorted(weeks)[-ratings_module.FRONT_WINDOW_WEEKS :])


def _fold(
    fold: pbp_adapter.PbpFold,
    snaps: Mapping[tuple[str, int], participation_adapter.RushSnap],
    front: Mapping[str, players_adapter.PlayerRef],
) -> FrontTotals:
    """Join the two large feeds into one set of totals.

    **The join is an intersection, and that is the point.** A play counts only
    when play-by-play calls it a regular-season dropback in the window AND
    participation charted a pass rush on it. 5.24% of charted pass-rush snaps
    are penalty-nullified `no_play` rows, which can carry a pressure but never
    a sack; counting them would deflate `pressure_to_sack_rate` by that much
    while every field stayed populated and plausible.

    `front` narrows the eleven defenders on each snap to the front. When it is
    empty — the roster feed was unavailable — no front snaps are recorded and
    `front_continuity_index` is null, rather than counting the secondary as
    part of the front.
    """
    totals = FrontTotals(
        defense=fold.defense,
        offense_game=fold.offense_game,
        opponents=fold.opponents,
        weeks=set(fold.weeks),
    )
    recent_weeks = _recent_weeks(fold.weeks)

    for key, snap in snaps.items():
        dropback = fold.dropbacks.get(key)
        if dropback is None:
            continue
        line = fold.defense_line(dropback.defense)
        allowed = fold.offense_line(dropback.offense, dropback.game_id)

        line.pass_rush_snaps += 1
        allowed.pass_rush_snaps += 1
        # Attributed to the RUSHING UNIT, never to the play outcome: a hurry
        # or a knockdown counts even though the ball came out. `was_pressure`
        # is charted independently of `sack`, and nothing here reconciles the
        # two — that is the spec's requirement made structural.
        if snap.was_pressure:
            line.pressures += 1
            allowed.pressures_allowed += 1
        if dropback.sack:
            line.sacks += 1
            allowed.sacks_allowed += 1
        if snap.rushers >= ratings_module.BLITZ_RUSHERS:
            line.blitzes += 1
            if snap.was_pressure:
                line.blitz_pressures += 1
        if snap.time_to_throw is not None:
            line.time_to_throw_sum += snap.time_to_throw
            line.time_to_throw_charted += 1

        if not front:
            continue
        window = totals.front_snaps.setdefault(dropback.defense, {})
        recent = (
            totals.recent_front_snaps.setdefault(dropback.defense, {})
            if dropback.week in recent_weeks
            else None
        )
        for player in snap.defenders:
            if player not in front:
                continue
            window[player] = window.get(player, 0) + 1
            if recent is not None:
                recent[player] = recent.get(player, 0) + 1

    return totals


async def _key_absences(
    season: int,
    absences: list[injuries_adapter.Absence],
    front: Mapping[str, players_adapter.PlayerRef],
    *,
    client: httpx.AsyncClient,
    acc: CoverageAccumulator,
) -> dict[str, list[str]]:
    """`team -> canonical ids`, or `{}` with the reason filed.

    Identity failure never fails the pass: fifteen other fields do not depend
    on it. It is filed as a coverage error and counted, so a `player-identity`
    outage is distinguishable from a genuinely healthy week — the two produce
    the same empty field otherwise.
    """
    if not absences or not front:
        return {}
    try:
        identity = identity_adapter.build_identity_client(client)
    except identity_adapter.IdentityUnavailable as exc:
        acc.add_error(exc.reason, "PLAYER_IDENTITY_URL is unset; key_absences is empty")
        return {}

    failures = identity_adapter.IdentityFailures()
    by_team, unresolved = await identity_adapter.resolve_absences(
        absences, season=season, front=front, identity=identity, failures=failures
    )
    if failures.rows:
        # Summarised, not per row — one dead seam would otherwise fill the
        # 50-entry error cap by itself and push every other reason off it.
        acc.add_error(REASON_IDENTITY_UPSTREAM_ERROR, failures.detail())
    if unresolved:
        acc.add_error(
            REASON_IDENTITY_UNRESOLVED,
            f"{unresolved} absent front starter(s) player-identity did not resolve",
        )
    metrics.absences_unresolved(unresolved)
    return by_team


def _record_guard(
    acc: CoverageAccumulator, regression: TimingRegression | None
) -> None:
    """File and record the timing guard's verdict, including "did not run".

    A guard that could not run must not be reported as one that passed, and
    `defensive_front_timing_guard_ran` is the series that separates them.
    """
    if regression is None:
        acc.add_error(
            REASON_TIMING_GUARD_NOT_RUN,
            "fewer than 4 comparable defenses, or no spread in release timing",
        )
        metrics.timing_guard(ran=False, slope=NOT_RUN, t_statistic=NOT_RUN)
        return
    metrics.timing_guard(
        ran=True,
        slope=regression.slope,
        t_statistic=regression.t_statistic,
        flagged=regression.flagged,
    )
    if regression.flagged:
        # `add_priority_error`, because this entry EXPLAINS the pass: the
        # adjusted pressure rate still tracks the timing it was supposed to be
        # comparable across. Inserted at the front so the 50-entry cap cannot
        # delete it.
        acc.add_priority_error(
            REASON_TIMING_CONFOUND,
            f"pressure_rate_generated_adj still tracks mean_time_to_throw_faced: "
            f"slope={regression.slope:+.5f}/s "
            f"95% CI [{regression.ci_low:+.5f}, {regression.ci_high:+.5f}] "
            f"t={regression.t_statistic:+.3f} on {regression.degrees_of_freedom} df "
            f"(p={regression.p_value:.4f}, n={regression.teams})",
        )


def _strength_envelope(
    totals: FrontTotals,
    *,
    acc: CoverageAccumulator,
    absences: Mapping[str, list[str]],
    degraded: tuple[str, ...],
    season: int,
    now: datetime,
    scope: Mapping,
    deadline: datetime | None,
) -> tuple[Envelope, list[dict]]:
    """Build the one envelope, its coverage, and this pass's guard verdict."""
    rows, regression = build_rows(
        totals,
        absences=absences,
        degraded=degraded,
        null_field_reason=NULL_FIELD_REASON,
    )

    published: list[dict] = []
    for row in rows:
        team = row["team_id"]
        # Expected because the team APPEARS IN THE SCHEDULE this pass read —
        # never because building its row happened to succeed.
        acc.expect(team)
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing
            # away what already resolved.
            acc.fail(team, "deadline_exceeded")
            continue
        published.append(row)
        # **The `present` half of the coverage predicate.** A team is present
        # when there is a pass-rush sample behind its rates; a row of nulls is
        # not a captured defence. This moves with `EXPECTED_FLOOR` — see its
        # comment.
        if row["pass_rush_snaps"]:
            acc.record(team)
        else:
            acc.fail(team, REASON_NO_PASS_RUSH_SNAPS)

    _record_guard(acc, regression)
    for column in ("pressure_rate_generated_adj", "sack_rate_generated_adj"):
        values = [row[column] for row in published if row[column] is not None]
        metrics.adjusted_variance(
            column, statistics.pvariance(values) if len(values) > 1 else 0.0
        )
    metrics.rows_captured(len(published))
    metrics.degraded_upstreams(len(degraded))

    return (
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=STRENGTH,
            captured_at=now,
            upstream=Upstream(
                adapter=UPSTREAM_ADAPTER,
                fetched_at=now,
                source_ref=pbp_adapter.source_ref(season),
            ),
            scope=dict(scope),
            coverage=acc.result(),
            errors=acc.errors,
            signals=published,
        ),
        published,
    )


async def capture_defensive_front(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one `defensive_front_strength` envelope.

    `week` is the last week sampled, not the only one: a front is rated over
    every regular-season week up to and including it, which is what the
    opponent adjustment needs a schedule for. `key_absences` alone looks
    forward, to `week + 1`.
    """
    scope = {"season": season, "week": week}
    metrics.capture_attempt()

    async def _pbp(**kw):
        return await pbp_adapter.fetch_pbp(season, week, client=client, **kw)

    async def _participation(**kw):
        return await participation_adapter.fetch_rush_snaps(season, client=client, **kw)

    async def _players(**kw):
        return await players_adapter.fetch_front_players(client=client, **kw)

    async def _injuries(**kw):
        return await injuries_adapter.fetch_absences(season, week, client=client, **kw)

    fetchers: dict[str, Callable[..., Awaitable]] = {
        "pbp": _pbp,
        "participation": _participation,
        "players": _players,
        "injuries": _injuries,
    }
    results: dict[str, object] = {}
    unchanged: dict[str, bool] = {}
    degraded: list[str] = []
    optional_failures: dict[str, Exception] = {}

    async def _attempt(name: str, *, conditional: bool = True) -> None:
        """One feed's fetch, with the failure lattice applied.

        **The single place a feed's failure is classified**, used by the
        conditional first attempt AND by the unconditional re-fetch. Having
        two call sites and one classifier is the whole point: the re-fetch
        used to call the fetchers directly, and an optional feed that 304'd
        and then failed ended the entire pass. See
        `_refetch_unconditionally`.

        A fatal feed's failure goes to `fail_capture`, which never returns; an
        optional one is degraded. A feed cannot be degraded twice: failing the
        conditional attempt sets `unchanged[name] = False`, so it is never
        re-fetched. That is an invariant of the control flow rather than a
        defended one — a dedup guard here would be unreachable code no test
        could exercise.
        """
        kwargs = {} if conditional else {"etag_store": ETagStore()}
        try:
            value, was_unchanged = await _fetch(fetchers[name], **kwargs)
        except Exception as exc:  # noqa: BLE001 — classified per feed
            reason = OPTIONAL_FEEDS.get(name)
            if reason is None:
                # Do NOT call `metrics.capture_failure(exc)` first: the library
                # owns that counter for a failure that ends a pass, and calling
                # it here double-counts.
                await fail_capture(
                    exc,
                    collector=COLLECTOR_NAME,
                    signal_types=SIGNAL_TYPES,
                    adapter=UPSTREAM_ADAPTER,
                    now=now,
                    scope=scope,
                    lake=lake,
                    metrics=metrics,
                    expected=EXPECTED_FLOOR,
                    source_ref=pbp_adapter.source_ref(season),
                )
            results[name], unchanged[name] = None, False
            optional_failures[reason] = exc
            degraded.append(reason)
            return

        if not conditional and was_unchanged:
            # A `304` to a request that carried no `If-None-Match` is an
            # upstream contract violation, and we still have no body. Treat it
            # as the feed being unavailable rather than letting a `None` reach
            # the fold, where a fatal feed would raise `AttributeError` outside
            # every handler.
            await _attempt_unavailable(name)
            return

        results[name] = value
        if conditional:
            # Only the conditional attempt informs `every_feed_unchanged`; a
            # re-fetch sends no validator and so cannot speak to it.
            unchanged[name] = was_unchanged

    async def _attempt_unavailable(name: str) -> None:
        reason = OPTIONAL_FEEDS.get(name)
        exc = UpstreamContractViolation(
            f"{name} answered 304 to an unconditional request"
        )
        if reason is None:
            await fail_capture(
                exc,
                collector=COLLECTOR_NAME,
                signal_types=SIGNAL_TYPES,
                adapter=UPSTREAM_ADAPTER,
                now=now,
                scope=scope,
                lake=lake,
                metrics=metrics,
                reason=REASON_REFETCH_UNCHANGED,
                expected=EXPECTED_FLOOR,
                source_ref=pbp_adapter.source_ref(season),
            )
        results[name] = None
        optional_failures[reason] = exc
        degraded.append(reason)

    # --- The fatal feeds first, then the optional ones ---------------------
    for name in (*FATAL_FEEDS, *OPTIONAL_FEEDS):
        await _attempt(name)

    # --- Every feed unchanged? Then so is the pass. ------------------------
    #
    # Through `every_feed_unchanged` rather than inline, because `all({})` is
    # True and the emptiness guard is otherwise untestable — see that function.
    if every_feed_unchanged(unchanged):
        raise UpstreamUnchanged(pbp_adapter.source_ref(season))

    # Some feed changed, so the 304'd ones must be read after all — through
    # the same classifier, not around it.
    await _refetch_unconditionally(unchanged, _attempt)

    for reason, exc in optional_failures.items():
        # Recorded HERE because the library cannot see it: neither
        # `fail_capture` nor a failed `publish_capture` write runs for a feed
        # whose loss costs a field rather than the pass.
        metrics.capture_failure(exc, reason=reason)

    fold: pbp_adapter.PbpFold = results["pbp"]
    front = results["players"] or {}
    absences = results["injuries"] or []
    totals = _fold(fold, results["participation"], front)

    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[STRENGTH])
    key_absences = await _key_absences(season, absences, front, client=client, acc=acc)

    envelope, rows = _strength_envelope(
        totals,
        acc=acc,
        absences=key_absences,
        degraded=tuple(degraded),
        season=season,
        now=now,
        scope=scope,
        deadline=deadline,
    )

    return await _publish_changed(
        {STRENGTH: envelope}, {STRENGTH: _digest(rows)}, season, week, lake
    )


async def _publish_changed(
    envelopes: dict[str, Envelope],
    digests: dict[str, str],
    season: int,
    week: int,
    lake: LakeWriter,
) -> dict[str, Envelope]:
    """Write only what changed, and record only what LANDED.

    The digest gate suppresses a byte-identical append. Recording a digest for
    content the lake never received would make the next pass digest the same
    content, match, raise `UpstreamUnchanged`, and never write the object
    again until the upstream data itself changed — on a weekly cadence,
    potentially the rest of a season. It takes **two passes** to see: pass 1
    with a failing lake writes nothing, pass 2 with a healthy lake raises
    `UpstreamUnchanged` and still writes nothing.

    Gated per signal type, and **iterating `published` rather than
    `envelopes`**: `PublishResult.landed` raises for a signal type this call
    did not publish, and that raise costs the whole pass after every write has
    already happened.
    """
    changed = {
        signal_type: envelope
        for signal_type, envelope in envelopes.items()
        if _PUBLISHED_DIGESTS.get((season, week, signal_type)) != digests[signal_type]
    }
    if not changed:
        raise UpstreamUnchanged(
            pbp_adapter.source_ref(season), source_ref=digests[STRENGTH]
        )

    published = await publish_capture(changed, lake=lake, metrics=metrics)

    for signal_type in published:
        if published.landed(signal_type):
            _PUBLISHED_DIGESTS[(season, week, signal_type)] = digests[signal_type]

    return envelopes
