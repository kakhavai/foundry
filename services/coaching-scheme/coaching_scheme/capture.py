"""The capture pass: four upstreams -> two guards -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

--------------------------------------------------------------------------
1. The failure lattice — four feeds, four different consequences
--------------------------------------------------------------------------

| feed | size | owns | its failure |
|---|---|---|---|
| `games.csv` | 0.49 MiB | the revision timeline | **fatal** |
| `pbp` | 18.22 MiB | nine rate fields, the changepoint series | degraded |
| `ftn_charting` | 7.75 MiB | play action, pre-snap motion | field-level |
| `pbp_participation` | 46.82 MiB | `personnel_rates` | field-level |

Only `games.csv` ends the pass, through `fail_capture`. Without the grid there
is no team universe, no revision and no week span, so neither signal type has
anything to say.

A play-by-play failure is **degraded**, following `officiating`:
`staff_assignment` publishes normally — who the head coach is does not depend
on how his offense played — and `team_scheme_profile` gets a hand-built
`present: 0` envelope. Routing it through `fail_capture` would re-raise and
discard a perfectly good staff capture already in memory, over an upstream
that signal type does not use. `play_by_play_<season>.csv.gz` also does not
exist until a season's first games are played, so this branch is the normal
one for months.

The two charting feeds fail at **field level**: their fields go null with a
reason and every other rate publishes. Losing `personnel_rates` must not cost
the other thirteen fields, least of all when it is the 46.82 MiB one.

This module records `collector_capture_failures_total` **itself** on the
degraded and field-level branches, and only there. That is the documented
exception to "the library owns the counter" — `docs/collectors.md` names "a
degraded path that builds its own envelopes" as the collector-owned case. Do
NOT add a `metrics.capture_failure(exc)` before `fail_capture` or
`publish_capture`; both already record it, and calling it first double-counts.

--------------------------------------------------------------------------
2. Conditional GET across four feeds, and the trap of all-or-nothing
--------------------------------------------------------------------------

Every feed sends `If-None-Match`. A `304` is a **successful** capture.

But four feeds cannot share one verdict. `officiating` re-raises the first
`UpstreamUnchanged` it sees, which ends the pass — and on this collector that
would be wrong in a way that costs exactly the signal it exists for.
`games.csv` is a live master file that changes for odds and scores several
times a week; the three nflverse release assets change once, on the weekly
build. So the common mixed case is *games.csv changed, the rest 304'd* — and
under all-or-nothing the pass aborts and **a mid-season coaching change is
discarded**, which is the one event that lands off-cycle.

So: catch `UpstreamUnchanged` per feed. If **every** feed is unchanged the
pass is unchanged and re-raises. If any feed changed, the unchanged ones are
re-fetched against a throwaway `ETagStore`, which sends no `If-None-Match`.
The shared store keeps its entry, which is still correct — the feed really is
unchanged — so the next pass 304s again normally.

The `and unchanged` in that condition is not defensive: `all([])` is `True`,
and an empty feed list would otherwise report a healthy pass as unchanged
forever.

--------------------------------------------------------------------------
3. The digest gate, and why it waits for the write to land
--------------------------------------------------------------------------

`seasonal` polls daily; this data changes weekly. Publishing a byte-identical
envelope on the other six days fills an append-only lake with objects carrying
no information.

Digests are per **signal type** and keyed by `(season, week, signal_type)`.
Recorded only for a write `PublishResult.landed` confirms: record a digest for
content the lake never received and the next pass digests the same content,
matches, raises `UpstreamUnchanged`, and the object is never written again
until the upstream itself changes — which on a seasonal cadence is a week at
best and a season at worst.

--------------------------------------------------------------------------
4. Coverage, per the spec, and the play-caller decision it encodes
--------------------------------------------------------------------------

`staff_assignment` follows the spec's definition **exactly**: a team is
present when it has a revision covering the requested week AND both
`play_caller_id` and `play_caller_role` are non-null. With the register in
`play_callers.py` shipping empty that is 0/32, every team named in
`coverage.missing` with a reason, plus one priority error pointing at the
file. See that module for the full argument — including why defaulting the
play-caller to the head coach was refused outright.

`team_scheme_profile` is keyed by **revision**, not by team, because a
revision is what owes a profile — and its universe is fully sourceable, so it
reaches 1.0 on a healthy pass. That split is what keeps a curation gap in one
signal type from reading as an outage in the collector.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.conditional import ETagStore, UpstreamUnchanged
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture, failure_envelopes
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from . import changepoint as changepoint_module
from . import play_callers
from . import rates as rates_module
from .adapters import ftn as ftn_adapter
from .adapters import games as games_adapter
from .adapters import participation as participation_adapter
from .adapters import pbp as pbp_adapter
from .metrics import metrics
from .revisions import Revision, build_revisions

logger = logging.getLogger(__name__)

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "PROFILE",
    "SIGNAL_TYPES",
    "STAFF",
    "build_profile_row",
    "build_staff_row",
    "capture_coaching_scheme",
    "every_feed_unchanged",
    "reset_published_digests",
]

COLLECTOR_NAME = "coaching-scheme"
CADENCE_CLASS = CadenceClass.SEASONAL

STAFF = "staff_assignment"
PROFILE = "team_scheme_profile"
SIGNAL_TYPES = (STAFF, PROFILE)

UPSTREAM_ADAPTER = "nflverse-coaching-scheme"

# The size each universe is KNOWN to have, declared independently of any fetch.
#
#   staff_assignment      32 teams. Keyed by TEAM, per the spec's coverage
#                         sentence ("every team has at least one revision
#                         covering the requested week"). Fixed since the 2002
#                         realignment.
#   team_scheme_profile   32 revisions. Keyed by REVISION: every team has at
#                         least one, so 32 is the floor and a season with
#                         mid-year changes legitimately exceeds it. A floor
#                         never caps a genuine count.
#
# Neither number comes from the fetch. That is the whole point: a games.csv
# truncated to eight teams must report 8/32, not 8/8.
EXPECTED_FLOOR: dict[str, int] = {
    STAFF: 32,
    PROFILE: 32,
}

REASON_NO_REVISION = "no_revision_covering_week"
REASON_PBP_UNAVAILABLE = "play_by_play_unavailable"
REASON_CHARTING_UNAVAILABLE = "ftn_charting_unavailable"
REASON_PARTICIPATION_UNAVAILABLE = "participation_unavailable"
REASON_NO_GAMES_SAMPLED = "no_games_sampled_in_revision"
REASON_PLAY_CALLER_REGISTER_EMPTY = "play_caller_register_has_no_entry_for_this_season"

# Spec fields with no free upstream, emitted as null with the reason attached
# to the row. An untested value is a null with a reason — never a default, and
# never quietly dropped from the schema.
NULL_COORDINATORS = "no_free_feed_names_coordinators_only_head_coaches"
NULL_CHANGE_REPORTED_AT = "no_feed_carries_a_staff_change_publication_instant"
NULL_FOURTH_DOWN_OE = "no_free_win_probability_optimal_fourth_down_baseline"

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
    """A stable sha256 over anything JSON-serialisable."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def every_feed_unchanged(unchanged: Mapping[str, bool]) -> bool:
    """True only when at least one feed was attempted and every one 304'd.

    **Extracted so the emptiness case is reachable by a test.** `all({})` is
    `True`, so without the `bool(unchanged)` a pass that attempted no feed at
    all would report itself unchanged: `last_capture_at` advances,
    `collector_upstream_unchanged_total` increments, no envelope is written,
    and nothing in the metrics says why.

    Today the caller cannot produce an empty map — a `games.csv` failure ends
    the pass before this is reached, so `unchanged` always carries at least
    that feed. That makes the guard a defence of an invariant rather than of a
    live code path, and inlining it would make the defence **unprovable**: a
    mutation removing `bool(unchanged)` survived the whole suite, because no
    test could construct the input that distinguishes the two. Naming it is
    what turns the claim into something a test can kill, and keeps it correct
    if a future change to the fetch order makes emptiness reachable.
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


def build_staff_row(
    revision: Revision,
    *,
    assertion: play_callers.PlayCallerAssertion | None,
    play_caller_reason: str | None,
) -> dict:
    """One revision as a `staff_assignment` signal row.

    Mirrored by `contracts/signal-envelope/collectors/coaching-scheme.json`,
    which `tests/test_capture_contract_conformance.py` validates the REAL
    output of this function against.

    Carries no capture timestamp. Not an omission: the capture instant belongs
    on the envelope's `captured_at`, and a per-row one would make every daily
    digest unique and silently disable the unchanged-snapshot gate.
    """
    return {
        "team_id": revision.team_id,
        "season": revision.season,
        "revision_id": revision.revision_id,
        "effective_from_week": revision.effective_from_week,
        "effective_to_week": revision.effective_to_week,
        "head_coach_id": revision.head_coach_id,
        # Published alongside the id because the id is a slug OF the name —
        # a re-spelling upstream mints a new id, and the name is what lets a
        # consumer tell that apart from a real change. See revisions.py.
        "head_coach_name": revision.head_coach_name,
        "offensive_coordinator_id": None,
        "defensive_coordinator_id": None,
        "play_caller_id": assertion.play_caller_id if assertion else None,
        "play_caller_role": assertion.play_caller_role if assertion else None,
        "play_caller_source": assertion.source if assertion else None,
        "play_caller_missing_reason": play_caller_reason,
        "change_event": revision.change_event,
        "change_reported_at": None,
        "null_field_reason": {
            "offensive_coordinator_id": NULL_COORDINATORS,
            "defensive_coordinator_id": NULL_COORDINATORS,
            "change_reported_at": NULL_CHANGE_REPORTED_AT,
        },
    }


def build_profile_row(
    revision: Revision,
    profile: rates_module.RevisionRates,
    *,
    unexplained: changepoint_module.Changepoint | None,
    degraded: tuple[str, ...],
) -> dict:
    """One revision's scheme profile as a `team_scheme_profile` signal row.

    `changepoint_unexplained` is the spec's "surface it; do not silently
    correct it". The rates on both sides of an unexplained shift are wrong,
    and they are published anyway — marked. Dropping them would leave a
    consumer with a missing profile and no reason, which is the silent
    correction the spec forbids.
    """
    return {
        "team_id": revision.team_id,
        "season": revision.season,
        "revision_id": revision.revision_id,
        "effective_from_week": revision.effective_from_week,
        "effective_to_week": revision.effective_to_week,
        "games_sampled": profile.games_sampled,
        "sampled_weeks": list(profile.sampled_weeks),
        "neutral_pass_rate": profile.neutral_pass_rate,
        "pass_rate_over_expected": profile.pass_rate_over_expected,
        "sec_per_play_neutral": profile.sec_per_play_neutral,
        "no_huddle_rate": profile.no_huddle_rate,
        "personnel_rates": profile.personnel_rates,
        "shotgun_rate": profile.shotgun_rate,
        "play_action_rate": profile.play_action_rate,
        "pre_snap_motion_rate": profile.pre_snap_motion_rate,
        "fourth_down_go_rate": profile.fourth_down_go_rate,
        "fourth_down_go_rate_over_expected": None,
        "changepoint_unexplained": unexplained is not None,
        "changepoint_week": unexplained.week if unexplained else None,
        "changepoint_shift": unexplained.shift if unexplained else None,
        "degraded_upstreams": list(degraded),
        "null_field_reason": {
            "fourth_down_go_rate_over_expected": NULL_FOURTH_DOWN_OE,
        },
    }


def _staff_envelope(
    revisions: dict[str, list[Revision]],
    *,
    season: int,
    week: int,
    now: datetime,
    scope: dict,
    deadline: datetime | None,
) -> tuple[Envelope, list[dict]]:
    """Build `staff_assignment` over every team the grid describes.

    Rows are per revision — the whole timeline, which is what
    `GET /teams/{team_id}/revisions` serves. **Coverage is per team**, because
    that is what the spec's sentence counts.
    """
    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[STAFF])
    rows: list[dict] = []
    identified = 0

    for team_id, team_revisions in sorted(revisions.items()):
        # Expected because the team EXISTS in the grid and is therefore owed —
        # never because building its row happened to succeed.
        acc.expect(team_id)
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            acc.fail(team_id, "deadline_exceeded")
            continue

        covering = next((r for r in team_revisions if r.covers(week)), None)
        for revision in team_revisions:
            assertion, reason = play_callers.resolve(team_id, season, week)
            rows.append(
                build_staff_row(
                    revision, assertion=assertion, play_caller_reason=reason
                )
            )

        if covering is None:
            acc.fail(team_id, REASON_NO_REVISION)
            continue
        assertion, reason = play_callers.resolve(team_id, season, week)
        if assertion is None:
            # The spec's predicate, unmodified: a null play_caller_id is NOT
            # present, and `unknown` counts only as a *role* beside a real id.
            acc.fail(team_id, reason or play_callers.REASON_UNKNOWN)
            continue
        identified += 1
        acc.record(team_id)

    metrics.play_callers_identified(identified)
    metrics.staff_revisions(sum(len(v) for v in revisions.values()))

    if identified < len(revisions):
        # `add_priority_error`, not `add_error`: `errors` is capped at 50 and
        # the cap is applied when the property is read, so on an empty
        # register this entry would be queued behind 32 routine per-team
        # failures and could be the one dropped. It is also the only entry
        # that says where the fix goes.
        acc.add_priority_error(
            REASON_PLAY_CALLER_REGISTER_EMPTY,
            f"{len(revisions) - identified} of {len(revisions)} team(s) have no "
            f"in-force play-caller assertion for season {season} week {week}; "
            "curate services/coaching-scheme/coaching_scheme/play_callers.py",
        )

    return (
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=STAFF,
            captured_at=now,
            upstream=Upstream(
                adapter=games_adapter.UPSTREAM_ADAPTER,
                fetched_at=now,
                source_ref=games_adapter.source_ref(),
            ),
            scope=dict(scope),
            coverage=acc.result(),
            errors=acc.errors,
            signals=rows,
        ),
        rows,
    )


def _profile_envelope(
    revisions: dict[str, list[Revision]],
    *,
    play_buckets: dict,
    charting_buckets: dict | None,
    personnel_buckets: dict | None,
    proe_series: dict[str, list[tuple[int, float]]],
    season: int,
    now: datetime,
    scope: dict,
) -> tuple[Envelope, list[dict]]:
    """Build `team_scheme_profile`, applying both guards.

    Coverage is keyed by `revision_id`: a revision is the thing that owes a
    profile, and keying by team would let one revision of three stand in for
    the other two.
    """
    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[PROFILE])
    rows: list[dict] = []
    straddles = 0
    unexplained_count = 0

    degraded: list[str] = []
    if charting_buckets is None:
        degraded.append(REASON_CHARTING_UNAVAILABLE)
    if personnel_buckets is None:
        degraded.append(REASON_PARTICIPATION_UNAVAILABLE)
    degraded_tuple = tuple(degraded)

    for team_id, team_revisions in sorted(revisions.items()):
        observed_weeks = [week for (t, week) in play_buckets if t == team_id]

        # Guard 2 runs per TEAM, once, on a series built from play-by-play
        # alone — see changepoint.py on why detection cannot see a revision.
        found = changepoint_module.detect(proe_series.get(team_id, []))
        unexplained = None
        if found is not None and not changepoint_module.explains(found, team_revisions):
            unexplained = found
            unexplained_count += 1
            acc.add_priority_error(
                changepoint_module.REASON_UNEXPLAINED_CHANGEPOINT,
                f"{team_id} PROE shifted {found.shift:+.1f} points at week "
                f"{found.week} ({found.before_mean:.1f} -> {found.after_mean:.1f}) "
                "with no staff revision within "
                f"{changepoint_module.REVISION_MATCH_TOLERANCE_WEEKS} week(s); "
                "the rates on BOTH sides of it are suspect",
            )

        for revision in team_revisions:
            acc.expect(revision.revision_id)
            weeks = rates_module.weeks_in_revision(revision, observed_weeks)
            profile = rates_module.aggregate(
                revision,
                weeks,
                play_buckets=play_buckets,
                charting_buckets=charting_buckets,
                personnel_buckets=personnel_buckets,
            )
            try:
                # Guard 1, at write time, on the row about to be built.
                rates_module.assert_window_within_revision(
                    revision, profile.sampled_weeks, profile.games_sampled
                )
            except rates_module.WindowStraddlesRevision as exc:
                straddles += 1
                acc.fail(revision.revision_id, exc.reason)
                continue
            rows.append(
                build_profile_row(
                    revision, profile, unexplained=unexplained, degraded=degraded_tuple
                )
            )
            if profile.games_sampled <= 0:
                # A revision that has not played yet is expected and missing,
                # not present-with-nulls. Its row still publishes: "this
                # regime exists and has no games" is a fact worth serving.
                acc.fail(revision.revision_id, REASON_NO_GAMES_SAMPLED)
                continue
            acc.record(revision.revision_id)

    metrics.window_straddles(straddles)
    metrics.unexplained_changepoints(unexplained_count)

    return (
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=PROFILE,
            captured_at=now,
            upstream=Upstream(
                adapter=pbp_adapter.UPSTREAM_ADAPTER,
                fetched_at=now,
                source_ref=pbp_adapter.source_ref(season),
            ),
            scope=dict(scope),
            coverage=acc.result(),
            errors=acc.errors,
            signals=rows,
        ),
        rows,
    )


async def _refetch_unconditionally(
    unchanged: dict[str, bool],
    fetchers: dict[str, Callable[..., Awaitable]],
    results: dict[str, object],
) -> None:
    """Re-fetch the feeds that 304'd, when some other feed did change.

    Against a **throwaway** `ETagStore`, so no `If-None-Match` is sent and the
    shared store keeps its (still-correct) entry for the next pass. A failure
    here is left to the caller's handler exactly as a first-attempt failure
    would be — the feed's own place in the failure lattice does not change
    because this is its second request.
    """
    for name, was_unchanged in unchanged.items():
        if not was_unchanged:
            continue
        results[name] = await fetchers[name](etag_store=ETagStore())


async def capture_coaching_scheme(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one envelope per signal type.

    `week` scopes the lake partition and the `staff_assignment` coverage
    question ("does every team have a revision covering *this* week"). The
    rates are not week-scoped: a revision's profile is drawn from every week
    inside that revision, which is the entire point of guard 1.
    """
    scope = {"season": season, "week": week}
    metrics.capture_attempt()

    async def _games(**kw):
        return await games_adapter.fetch_team_week_coaches(season, client=client, **kw)

    async def _pbp(**kw):
        return await pbp_adapter.fetch_weekly_buckets(season, client=client, **kw)

    # --- Phase 1: the two feeds that do not need the play index ------------
    fetchers: dict[str, Callable[..., Awaitable]] = {"games": _games, "pbp": _pbp}
    results: dict[str, object] = {}
    unchanged: dict[str, bool] = {}

    try:
        results["games"], unchanged["games"] = await _fetch(_games)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Fatal. Do NOT call `metrics.capture_failure(exc)` first: the library
        # owns that counter for a failure that ends a pass.
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
            source_ref=games_adapter.source_ref(),
        )

    pbp_failure: Exception | None = None
    try:
        results["pbp"], unchanged["pbp"] = await _fetch(_pbp)
    except Exception as exc:  # noqa: BLE001 — degraded, not fatal
        pbp_failure = exc
        results["pbp"], unchanged["pbp"] = None, False

    # --- Phase 2: the charted feeds, which need pbp's play index -----------
    play_index: dict = {}
    if pbp_failure is None and results["pbp"] is not None:
        play_index = results["pbp"][1]

    charting_failure: Exception | None = None
    personnel_failure: Exception | None = None

    if pbp_failure is None:

        async def _ftn(**kw):
            return await ftn_adapter.fetch_charting_buckets(
                season, client=client, play_index=play_index, **kw
            )

        async def _participation(**kw):
            return await participation_adapter.fetch_personnel_buckets(
                season, client=client, play_index=play_index, **kw
            )

        fetchers["ftn"] = _ftn
        fetchers["participation"] = _participation
        try:
            results["ftn"], unchanged["ftn"] = await _fetch(_ftn)
        except Exception as exc:  # noqa: BLE001 — field-level, not fatal
            charting_failure = exc
            results["ftn"], unchanged["ftn"] = None, False
        try:
            results["participation"], unchanged["participation"] = await _fetch(
                _participation
            )
        except Exception as exc:  # noqa: BLE001 — field-level, not fatal
            personnel_failure = exc
            results["participation"], unchanged["participation"] = None, False

    # --- Every feed unchanged? Then so is the pass. ------------------------
    #
    # Through `every_feed_unchanged` rather than inline, because `all({})` is
    # True and the emptiness guard is otherwise untestable — see that
    # function.
    if every_feed_unchanged(unchanged):
        raise UpstreamUnchanged(games_adapter.source_ref())

    # Some feed changed, so the 304'd ones must be read after all.
    await _refetch_unconditionally(unchanged, fetchers, results)

    coach_rows = results["games"]
    revisions = build_revisions(coach_rows, season=season)

    staff_envelope, staff_rows = _staff_envelope(
        revisions,
        season=season,
        week=week,
        now=now,
        scope=scope,
        deadline=deadline,
    )

    if pbp_failure is not None:
        return await _capture_without_play_by_play(
            pbp_failure,
            staff_envelope=staff_envelope,
            staff_rows=staff_rows,
            season=season,
            week=week,
            now=now,
            scope=scope,
            lake=lake,
        )

    play_buckets, _index = results["pbp"]
    charting_buckets = results.get("ftn")
    personnel_buckets = results.get("participation")

    if charting_failure is not None:
        # Recorded HERE because the library cannot see it: neither
        # `fail_capture` nor a failed `publish_capture` write runs for a feed
        # whose loss costs two fields rather than the pass.
        metrics.capture_failure(charting_failure, reason=REASON_CHARTING_UNAVAILABLE)
    if personnel_failure is not None:
        metrics.capture_failure(
            personnel_failure, reason=REASON_PARTICIPATION_UNAVAILABLE
        )

    profile_envelope, profile_rows = _profile_envelope(
        revisions,
        play_buckets=play_buckets,
        charting_buckets=charting_buckets,
        personnel_buckets=personnel_buckets,
        proe_series=pbp_adapter.weekly_proe_series(play_buckets),
        season=season,
        now=now,
        scope=scope,
    )

    envelopes = {STAFF: staff_envelope, PROFILE: profile_envelope}
    digests = {STAFF: _digest(staff_rows), PROFILE: _digest(profile_rows)}
    return await _publish_changed(envelopes, digests, season, week, lake)


async def _publish_changed(
    envelopes: dict[str, Envelope],
    digests: dict[str, str],
    season: int,
    week: int,
    lake: LakeWriter,
) -> dict[str, Envelope]:
    """Write only what changed, and record only what landed.

    PER SIGNAL TYPE, not per pass: one digest spanning both would append a
    byte-identical staff envelope every time a single rate moved, which after
    every week's games is all 32 of them.
    """
    changed = {
        signal_type: envelope
        for signal_type, envelope in envelopes.items()
        if _PUBLISHED_DIGESTS.get((season, week, signal_type)) != digests[signal_type]
    }
    if not changed:
        raise UpstreamUnchanged(games_adapter.source_ref(), source_ref=digests[STAFF])

    published = await publish_capture(changed, lake=lake, metrics=metrics)

    # Gated on the write LANDING, per signal type. Iterating `published`
    # rather than `envelopes` is required, not stylistic: `landed` raises for a
    # signal type this call did not publish, and that raise costs the whole
    # pass after every write has already happened.
    for signal_type in published:
        if published.landed(signal_type):
            _PUBLISHED_DIGESTS[(season, week, signal_type)] = digests[signal_type]

    # An unchanged envelope is not written, but its coverage gauge still is —
    # `publish_capture` only records the ones it was handed, and a gauge that
    # went quiet whenever the data was stable would read as a stopped
    # collector.
    for signal_type, envelope in envelopes.items():
        if signal_type not in changed:
            metrics.coverage(signal_type, envelope.coverage.ratio)

    return envelopes


async def _capture_without_play_by_play(
    exc: Exception,
    *,
    staff_envelope: Envelope,
    staff_rows: list[dict],
    season: int,
    week: int,
    now: datetime,
    scope: dict,
    lake: LakeWriter,
) -> dict[str, Envelope]:
    """The degraded path: play-by-play is unreachable, the schedule feed is not.

    `staff_assignment` publishes normally. `team_scheme_profile` gets a
    `present: 0` envelope built by hand rather than by `fail_capture`, because
    `fail_capture` re-raises and would discard the staff capture this process
    already holds in memory.

    **Only the staff digest is recorded, and never the profile one.** The
    profile envelope here is a failure envelope; letting it claim "already
    published" would suppress the first healthy pass that actually computes
    rates — permanently, until the schedule feed happened to change. The staff
    digest is safe to record because its content on this branch is *identical*
    to what a healthy pass would produce: `staff_assignment` does not read
    play-by-play at all, so there is no degraded variant of it to suppress a
    healthy one with. That asymmetry is the whole reason the two are treated
    differently, and it is a stronger argument than `officiating`'s equivalent
    (which rests on a `games_sampled: 0` marker rather than on the branch
    being irrelevant to the signal type).
    """
    logger.warning(
        "coaching-scheme: play-by-play is unavailable (%s: %s); publishing "
        "staff_assignment and a present:0 team_scheme_profile",
        type(exc).__name__,
        exc,
    )
    # Recorded here because the library cannot see this one: neither
    # `fail_capture` nor a failed `publish_capture` write runs on this branch.
    metrics.capture_failure(exc, reason=REASON_PBP_UNAVAILABLE)
    metrics.window_straddles(0)
    metrics.unexplained_changepoints(0)

    staff_digest = _digest(staff_rows)
    if _PUBLISHED_DIGESTS.get((season, week, STAFF)) == staff_digest:
        raise UpstreamUnchanged(games_adapter.source_ref(), source_ref=staff_digest)

    profile_envelope = failure_envelopes(
        exc,
        collector=COLLECTOR_NAME,
        signal_types=(PROFILE,),
        adapter=pbp_adapter.UPSTREAM_ADAPTER,
        now=now,
        scope=scope,
        reason=REASON_PBP_UNAVAILABLE,
        expected=EXPECTED_FLOOR,
        source_ref=pbp_adapter.source_ref(season),
    )[PROFILE]

    published = await publish_capture(
        {STAFF: staff_envelope, PROFILE: profile_envelope}, lake=lake, metrics=metrics
    )
    if published.landed(STAFF):
        _PUBLISHED_DIGESTS[(season, week, STAFF)] = staff_digest
    return published
