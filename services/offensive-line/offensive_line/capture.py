"""The capture pass: fetch -> join -> coverage -> guard -> envelope -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

--------------------------------------------------------------------------
Six feeds, two of them fatal
--------------------------------------------------------------------------

| feed | size | fatal? | what its loss costs |
|---|---|---|---|
| `play_by_play_<season>.csv.gz` | 18.22 MiB | yes | everything |
| `pbp_participation_<season>.csv` | 46.82 MiB | yes | every pressure column |
| `depth_charts_<season>.csv.gz` | 10.15 MiB | no | the starter half |
| `players.csv.gz` | 2.39 MiB | no | the starter half (the crosswalk) |
| `snap_counts_<season>.csv.gz` | 0.48 MiB | no | the starter half |
| `injuries_<season>.csv.gz` | 0.12 MiB | no | `starter_availability` |

~78.2 MiB on a changed pass, freshness re-checked across formats against the
releases API before size — see each adapter's docstring for its own table.

The fatal/optional split is the same call `defensive-front` made and for the
same reason. The two charted feeds carry every rate on the unit row, and a
`offensive_line_strength` unit row with every pressure column null is a
complete-looking row describing nothing. The other four carry the **starter**
half — who the five are, what they are called, whether they will play — and
losing that costs five of six rows per team, which coverage states loudly
(32/192, ratio 0.167) rather than hiding. Unit rows still publish, and the
spec says the unit row is what drives the projection.

**Three of the six feeds 404 for the 2026 season today** (`play_by_play`,
`pbp_participation`, `snap_counts`), verified live for this build, because the
season has not been played. That is why `CAPTURE_ENABLED` is `false` in the
Helm values: a running loop would pin `collector_coverage_ratio` at 0.0
forever for zero data.

--------------------------------------------------------------------------
Conditional GET across six feeds
--------------------------------------------------------------------------

Every feed is conditional. A single `304` must not end the pass — that would
suppress a genuinely changed feed while `mark_unchanged` advanced
`last_capture_at`, so staleness never alerts and the collector goes quiet
while looking healthy (observed on `defense-vs-position`). So
`UpstreamUnchanged` is caught **per feed** in `_fetch` and nowhere else; the
pass is unchanged only when `every_feed_unchanged` says every feed answered
`304`; and if any feed changed, the ones that did not are re-fetched
**through the same failure classifier**, never around it (observed on
`defensive-front`: an optional feed that 304'd and then failed its
unconditional re-fetch threw an uncaught `HTTPStatusError` out of the whole
capture, and that is the ordinary weekly path).

No adapter takes another adapter's result, so the related ordering hazard —
ETag-gating a small feed ahead of the large one it precedes — cannot occur
here structurally rather than by choosing an order.

--------------------------------------------------------------------------
The three things that are correctness, not style
--------------------------------------------------------------------------

**`coverage.expected` never derives from what succeeded.** `EXPECTED_FLOOR` is
192 — 32 offences x (one unit row + five starter rows), the spec's own clause
— declared independently of any fetch. **And so is the `present` predicate**:
a unit key is present when its row carries a pass-block sample, a starter key
when a canonical id was published for that slot. Neither of the two
deliberately-null YBC fields appears in either half; putting an unsourceable
field in the predicate pins the ratio at zero forever and destroys its ability
to report a truncated upstream at the same time.

**A failed capture still writes an envelope.** `fail_capture` writes one
`present: 0` envelope with a populated `errors` array, then re-raises. The
write makes the gap in the append-only lake explicit; the re-raise stops
`CaptureState` installing an empty capture over the last good one.

**The lineup guard fails the pass.** See `guard.py` for why this one raises
where `defensive-front`'s timing guard flags.
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

from .adapters import depth as depth_adapter
from .adapters import identity as identity_adapter
from .adapters import injuries as injuries_adapter
from .adapters import participation as participation_adapter
from .adapters import pbp as pbp_adapter
from .adapters import players as players_adapter
from .adapters import snaps as snaps_adapter
from .guard import STALE_UNIT_REASON, StaleUnitRow, assert_lineup_guard
from .lineups import derive_lineups
from .metrics import metrics
from .ratings import (
    RECORD_STARTER,
    RECORD_UNIT,
    STARTER_POSITIONS,
    LineTotals,
    build_rows,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "NULL_FIELD_REASON",
    "SIGNAL_TYPES",
    "STRENGTH",
    "capture_offensive_line",
    "every_feed_unchanged",
    "reset_published_digests",
]

COLLECTOR_NAME = "offensive-line"
CADENCE_CLASS = CadenceClass.WEEKLY

STRENGTH = "offensive_line_strength"
SIGNAL_TYPES = (STRENGTH,)

# **`unit-attributed` is a disclosure, not a label.** The spec: "The adapter
# must attribute pressures to a specific blocker where the upstream supports
# it and to the unit where it does not, and must say which in
# `upstream.adapter`." It does not support it — `was_pressure` is charted at
# the play with no blocker column — so this is the "does not" branch, said out
# loud in every envelope. See `adapters/participation.py`.
UPSTREAM_ADAPTER = "nflverse-offensive-line-unit-attributed"

# **192, straight from the spec's coverage clause**: "all 32 offenses, each
# contributing one `unit` row and five `starter` rows (192 rows total); a team
# with fewer than five identified starters is reported in `coverage.missing`
# rather than emitted partially."
#
# 32 is the league, fixed since the 2002 realignment, and six is the row count
# per team — neither comes from a fetch. **Both halves of the predicate move
# together**: this floor AND the `record`/`fail` calls in `_strength_envelope`.
# Changing one and leaving the other is the exact shape a mutation set that
# only attacks `expected` scores full marks against.
TEAMS_IN_LEAGUE = 32
ROWS_PER_TEAM = 1 + len(STARTER_POSITIONS)
EXPECTED_FLOOR: dict[str, int] = {STRENGTH: TEAMS_IN_LEAGUE * ROWS_PER_TEAM}

# **A September consequence worth knowing before it looks like an outage.**
# `ratings.MIN_DELTA_GAMES` is 2 on *both* sides of a with/without split, and
# the positional prior is derived from that same pass's measurements — so no
# replacement delta of any kind exists before a team's fourth game. Every team
# that changes its five in weeks 1-3 is therefore dropped wholesale with
# `lineup_changed_without_replacement_delta`, and league coverage sits well
# below 192 for the first month of a season. That is the honest reading of a
# window too short to measure anything, not a broken collector; a cross-season
# prior read back from the lake is the fix and is not built.
REASON_NO_PASS_BLOCK_SNAPS = "no_charted_pass_block_snaps_for_this_team"
REASON_STARTERS_UNIDENTIFIED = "fewer_than_five_identified_starters"
REASON_STALE_UNIT = STALE_UNIT_REASON
REASON_DEPTH_UNAVAILABLE = "depth_charts_unavailable"
REASON_PLAYERS_UNAVAILABLE = "players_unavailable"
REASON_SNAPS_UNAVAILABLE = "snap_counts_unavailable"
REASON_INJURIES_UNAVAILABLE = "injuries_unavailable"
REASON_IDENTITY_UPSTREAM_ERROR = identity_adapter.IDENTITY_UPSTREAM_ERROR
REASON_IDENTITY_UNRESOLVED = identity_adapter.IDENTITY_UNRESOLVED
REASON_REFETCH_UNCHANGED = "unconditional_refetch_answered_304"

# **The failure lattice, as data rather than as two hand-written loops.** A
# feed is fatal unless it is named here with the reason its loss carries. The
# classifier in `capture_offensive_line._attempt` is the only thing that reads
# it, so the conditional first attempt and the unconditional re-fetch cannot
# classify the same feed differently — which, on `defensive-front`, they did.
FATAL_FEEDS: tuple[str, ...] = ("pbp", "participation")
OPTIONAL_FEEDS: dict[str, str] = {
    "snaps": REASON_SNAPS_UNAVAILABLE,
    "depth": REASON_DEPTH_UNAVAILABLE,
    "players": REASON_PLAYERS_UNAVAILABLE,
    "injuries": REASON_INJURIES_UNAVAILABLE,
}


class UpstreamContractViolation(RuntimeError):
    """A `304` answered to a request that carried no validator."""


# The two spec fields with no free upstream, emitted present-and-null with a
# machine-readable reason. An unsourceable value is a null with a reason —
# never a default, never a plausible-looking zero, and never quietly dropped
# from the schema.
#
# **Symmetric with `defensive-front`, deliberately.** That collector nulls
# `yards_before_contact_allowed_per_carry` and its `_adj` for the same reason
# in the same words, and a differential where one term is real and the other
# is null is worse than one where both are null — because it looks computable.
# Pro Football Reference publishes yards before contact at **season level**,
# so it cannot be attributed to a week or to the front faced; nothing free
# publishes it per play. Deliberately NOT derived from anything:
# `adjusted_line_yards` is a *different* quantity (a weighting of total
# rushing yards, not a contact point), and substituting it would publish a
# plausible wrong number under a name a generator would trust.
NULL_YBC = (
    "no_free_per_play_yards_before_contact; PFR publishes it season-level, so "
    "it cannot be attributed to a week or to the defensive front faced. "
    "Symmetric with defensive-front's field of the same stem, which is null "
    "for the same reason"
)
NULL_FIELD_REASON: dict[str, str] = {
    "yards_before_contact_per_carry": NULL_YBC,
    "yards_before_contact_per_carry_adj": NULL_YBC,
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
    the pass before this is reached — which makes the guard a defence of an
    invariant rather than of a live code path. Inlining it would make the
    defence **unprovable**: the mutation that removes `bool(unchanged)`
    survives any suite that cannot construct the input distinguishing the two.
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
    so it cannot drift back.** `defensive-front` shipped a revision that called
    the fetchers here directly while its docstring claimed the lattice still
    applied: an optional feed that answered `304` and then failed its
    unconditional re-fetch propagated an uncaught `HTTPStatusError` out of the
    whole capture. No `fail_capture`, so no `present: 0` envelope; no
    `collector_capture_failures_total`; the only signal was
    `collector_staleness_seconds` climbing.

    That is the **ordinary weekly path**, not an exotic one — play-by-play
    changes whenever a game finishes and the roster feed routinely does not,
    so this re-fetch runs on essentially every real capture.
    """
    for name, was_unchanged in unchanged.items():
        if was_unchanged:
            await attempt(name, conditional=False)


def _fold(
    fold: pbp_adapter.PbpFold,
    snaps: Mapping[tuple[str, int], participation_adapter.BlockSnap],
) -> LineTotals:
    """Join the two charted feeds into one set of totals.

    **The join is an intersection, and that is the point.** A play counts only
    when play-by-play calls it a regular-season dropback in the window AND
    participation charted a pass rush on it. 5.24% of charted pass-rush snaps
    are penalty-nullified `no_play` rows, which can carry a pressure but never
    a sack; counting them would deflate every sack rate by that much while
    every field stayed populated and plausible.

    It is also **the same intersection `defensive-front` takes**, which is what
    makes `pass_block_snaps` here and `pass_rush_snaps` there the same number
    counted from opposite sides — the precondition for differencing the two
    collectors' adjusted rates without conversion.
    """
    totals = LineTotals(
        offense_game=fold.offense_game,
        defense_game=fold.defense_game,
        opponents=fold.opponents,
        weeks=set(fold.weeks),
        game_week=dict(fold.game_week),
    )

    for key, snap in snaps.items():
        dropback = fold.dropbacks.get(key)
        if dropback is None:
            continue
        blocking = fold.offense_line(dropback.offense, dropback.game_id)
        rushing = fold.defense_line(dropback.defense, dropback.game_id)

        blocking.pass_block_snaps += 1
        rushing.pass_rush_snaps += 1
        # Attributed to the BLOCKING UNIT, never to the play outcome: a hurry
        # or a knockdown counts even though the ball came out. `was_pressure`
        # is charted independently of `sack`, and nothing here reconciles the
        # two.
        if snap.was_pressure:
            blocking.pressures_allowed += 1
            rushing.pressures += 1
        if dropback.sack:
            blocking.sacks_allowed += 1
            rushing.sacks += 1
        if snap.time_to_throw is not None:
            blocking.time_to_throw_sum += snap.time_to_throw
            blocking.time_to_throw_charted += 1

    totals.order_games()
    return totals


def _availability(
    roster: players_adapter.Roster, reported: Mapping[str, str]
) -> dict[str, str]:
    """`gsis_id -> availability`, roster status winning over the game report.

    A man on injured reserve is not merely doubtful, and the two feeds answer
    different questions: `injuries` is the weekly game report and cannot
    produce `ir` at all (verified — its `report_status` column carries only
    `Out`, `Questionable`, `Doubtful` and blank), while `players.status`
    carries `RES`/`PUP` and is the only free source of the designation the
    spec's failure mode is literally named after.
    """
    merged = dict(reported)
    for gsis_id, reference in roster.by_gsis.items():
        if reference.on_ir:
            merged[gsis_id] = injuries_adapter.AVAILABILITY_IR
    return merged


async def _canonical_ids(
    season: int,
    totals: LineTotals,
    roster: players_adapter.Roster,
    *,
    client: httpx.AsyncClient,
    acc: CoverageAccumulator,
) -> dict[str, str]:
    """`gsis_id -> canonical id` for every current starter, or `{}` with a reason.

    Identity failure never fails the pass: every unit column is keyed by team
    and does not depend on it. It is filed as a coverage error and counted, so
    a `player-identity` outage is distinguishable from a genuinely healthy
    week — the two produce the same empty field otherwise.
    """
    wanted: list[tuple[str, object]] = []
    for team, games in totals.team_games.items():
        if not games:
            continue
        for slot in totals.lineups.get((team, games[-1]), []):
            wanted.append((team, slot))
    if not wanted:
        return {}

    try:
        identity = identity_adapter.build_identity_client(client)
    except identity_adapter.IdentityUnavailable as exc:
        acc.add_error(
            exc.reason, "PLAYER_IDENTITY_URL is unset; no starter row can be published"
        )
        return {}

    failures = identity_adapter.IdentityFailures()
    canonical, unresolved = await identity_adapter.resolve_starters(
        wanted,
        season=season,
        roster=roster.by_gsis,
        identity=identity,
        failures=failures,
    )
    if failures.rows:
        # Summarised, not per row — one dead seam would otherwise fill the
        # 50-entry error cap by itself and push every other reason off it.
        acc.add_error(REASON_IDENTITY_UPSTREAM_ERROR, failures.detail())
    if unresolved:
        acc.add_error(
            REASON_IDENTITY_UNRESOLVED,
            f"{unresolved} starter(s) player-identity did not resolve",
        )
    metrics.starters_unresolved(unresolved)
    return canonical


def _strength_envelope(
    totals: LineTotals,
    *,
    acc: CoverageAccumulator,
    availability: Mapping[str, str],
    canonical_ids: Mapping[str, str],
    degraded: tuple[str, ...],
    season: int,
    now: datetime,
    scope: Mapping,
    deadline: datetime | None,
) -> tuple[Envelope, list[dict]]:
    """Build the one envelope and its coverage, guarding before it is returned."""
    rows, dropped = build_rows(
        totals,
        availability=availability,
        canonical_ids=canonical_ids,
        degraded=degraded,
        null_field_reason=NULL_FIELD_REASON,
    )

    # **The guard runs over exactly what is about to be published**, before a
    # single row reaches an envelope. Raising here is what routes it into
    # `fail_capture` in the caller. See `guard.py`.
    assert_lineup_guard(rows)

    by_team: dict[str, list[dict]] = {}
    for row in rows:
        by_team.setdefault(row["team_id"], []).append(row)

    published: list[dict] = []
    for team in sorted(totals.teams()):
        # Expected because the team APPEARS IN THE SCHEDULE this pass read —
        # never because building its rows happened to succeed. Six keys per
        # team, which is what makes the floor and the observed universe the
        # same shape.
        #
        # **Belt and braces, and knowably so.** `CoverageAccumulator.fail`
        # declares a key expected as well, and every branch below either
        # records or fails all six — so deleting these two lines changes no
        # output today. Mutation-tested and confirmed equivalent; kept because
        # the invariant is "the schedule decides what is owed", and a later
        # branch that neither records nor fails would otherwise derive
        # `expected` from what succeeded without anything saying so.
        unit_key = f"{team}:{RECORD_UNIT}"
        acc.expect(unit_key)
        for position in STARTER_POSITIONS:
            acc.expect(f"{team}:{position}")

        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing
            # away what already resolved.
            acc.fail(unit_key, "deadline_exceeded")
            for position in STARTER_POSITIONS:
                acc.fail(f"{team}:{position}", "deadline_exceeded")
            continue

        if team in dropped:
            # The whole team, unit row included — an uncorrectable lineup
            # change. Six failures with one reason, rather than a stale row
            # that would validate, difference cleanly and be wrong.
            acc.fail(unit_key, dropped[team])
            for position in STARTER_POSITIONS:
                acc.fail(f"{team}:{position}", dropped[team])
            continue

        team_rows = by_team.get(team, [])
        unit_row = next(
            (row for row in team_rows if row["record_type"] == RECORD_UNIT), None
        )
        if unit_row is None:
            acc.fail(unit_key, REASON_NO_PASS_BLOCK_SNAPS)
        else:
            published.append(unit_row)
            # **The `present` half of the coverage predicate.** A unit is
            # present when there is a pass-block sample behind its rates; a row
            # of nulls is not a captured line. Neither null YBC field is
            # consulted — see `EXPECTED_FLOOR`.
            if unit_row["pass_block_snaps"]:
                acc.record(unit_key)
            else:
                acc.fail(unit_key, REASON_NO_PASS_BLOCK_SNAPS)

        starter_rows = [
            row for row in team_rows if row["record_type"] == RECORD_STARTER
        ]
        published.extend(starter_rows)
        filled = {row["starter_position"] for row in starter_rows}
        for position in STARTER_POSITIONS:
            key = f"{team}:{position}"
            if position in filled:
                acc.record(key)
            else:
                acc.fail(key, REASON_STARTERS_UNIDENTIFIED)

    unit_rows = [row for row in published if row["record_type"] == RECORD_UNIT]
    for column in ("pressure_rate_allowed_adj", "sack_rate_allowed_adj"):
        values = [row[column] for row in unit_rows if row[column] is not None]
        metrics.adjusted_variance(
            column, statistics.pvariance(values) if len(values) > 1 else 0.0
        )
    metrics.lineup_changes(sum(1 for row in unit_rows if row["lineup_changed"]))
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


async def capture_offensive_line(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one `offensive_line_strength` envelope.

    `week` is the last week sampled, not the only one: a line is rated over
    every regular-season week up to and including it, which is what the
    opponent adjustment needs a schedule for. `starter_availability` alone
    looks forward, to `week + 1`.

    **Every dependency is injected** — clock, network, storage and time budget
    — and nothing below reaches FastAPI, the gateway or Kubernetes. Issue #95
    converts leaf collectors to `CronJob`s, and it is cheap only because every
    capture entrypoint in the fleet shares this signature.
    """
    scope = {"season": season, "week": week}
    metrics.capture_attempt()

    async def _pbp(**kw):
        return await pbp_adapter.fetch_pbp(season, week, client=client, **kw)

    async def _participation(**kw):
        return await participation_adapter.fetch_block_snaps(
            season, client=client, **kw
        )

    async def _snaps(**kw):
        return await snaps_adapter.fetch_snaps(season, week, client=client, **kw)

    async def _depth(**kw):
        return await depth_adapter.fetch_depth_charts(season, client=client, **kw)

    async def _players(**kw):
        return await players_adapter.fetch_line_players(client=client, **kw)

    async def _injuries(**kw):
        return await injuries_adapter.fetch_availability(
            season, week, client=client, **kw
        )

    fetchers: dict[str, Callable[..., Awaitable]] = {
        "pbp": _pbp,
        "participation": _participation,
        "snaps": _snaps,
        "depth": _depth,
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
        conditional first attempt AND by the unconditional re-fetch. Having two
        call sites and one classifier is the whole point.

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
        # whose loss costs rows rather than the pass.
        metrics.capture_failure(exc, reason=reason)

    fold: pbp_adapter.PbpFold = results["pbp"]
    totals = _fold(fold, results["participation"])

    roster = results["players"] or players_adapter.Roster()
    charts = results["depth"] or depth_adapter.DepthCharts()
    snap_fold = results["snaps"] or snaps_adapter.SnapFold(
        line=[], team_offense_snaps={}
    )
    totals.lineups = derive_lineups(snap_fold, roster, charts, fold.week_dates)
    totals.team_offense_snaps = dict(snap_fold.team_offense_snaps)

    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[STRENGTH])
    canonical_ids = await _canonical_ids(season, totals, roster, client=client, acc=acc)

    try:
        envelope, rows = _strength_envelope(
            totals,
            acc=acc,
            availability=_availability(roster, results["injuries"] or {}),
            canonical_ids=canonical_ids,
            degraded=tuple(degraded),
            season=season,
            now=now,
            scope=scope,
            deadline=deadline,
        )
    except StaleUnitRow as exc:
        # The spec: "a changed hash with unchanged adjusted metrics is a stale
        # unit and must fail rather than publish". `reason` reaches Prometheus
        # as well as the envelope, so this is alertable as itself rather than
        # landing in `unknown` beside genuine crashes.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            reason=REASON_STALE_UNIT,
            expected=EXPECTED_FLOOR,
            source_ref=pbp_adapter.source_ref(season),
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
