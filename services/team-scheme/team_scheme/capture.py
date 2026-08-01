"""The capture pass: three upstreams -> the window guard -> coverage -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

--------------------------------------------------------------------------
1. The failure lattice — three feeds, two different consequences
--------------------------------------------------------------------------

| feed | size | owns | its failure |
|---|---|---|---|
| `pbp` | 18.22 MiB | ten of the thirteen fields | **fatal** |
| `ftn_charting` | 7.75 MiB | play action, pre-snap motion | field-level |
| `pbp_participation` | 46.82 MiB | `personnel_rates` | field-level |

Only play-by-play ends the pass, through `fail_capture`. Without it there is
no team universe, no week, and no rate — the collector has nothing whatsoever
to say, so a `present: 0` envelope and a re-raise is the honest answer.

**That is a change from `coaching-scheme`, and it is a consequence of the
split rather than a decision.** There, a play-by-play failure was *degraded*:
`staff_assignment` published normally off a different feed, so re-raising
would have discarded a good capture. With the staff half deferred to
`coaching-staff` there is no second signal type to save, so the degraded
branch would write exactly the `present: 0` envelope `fail_capture` writes and
then swallow the exception that explains it. Note the consequence:
`play_by_play_<season>.csv.gz` does not exist until a season's first games are
played, so a preseason pass is a *loud* failed capture rather than a quiet
one. That is correct — a collector serving nothing should say so — and it is
one more reason the loop ships disabled.

The two charting feeds fail at **field level**: their fields go null with a
reason and every other rate publishes. Losing `personnel_rates` must not cost
the other twelve fields, least of all when it is the 46.82 MiB one.

This module records `collector_capture_failures_total` **itself** on the
field-level branches, and only there. That is the documented exception to "the
library owns the counter" — `docs/collectors.md` names "a degraded path that
builds its own envelopes" as the collector-owned case. Do NOT add a
`metrics.capture_failure(exc)` before `fail_capture` or `publish_capture`;
both already record it, and calling it first double-counts.

--------------------------------------------------------------------------
2. Conditional GET across three feeds, and the trap of all-or-nothing
--------------------------------------------------------------------------

Every feed sends `If-None-Match`. A `304` is a **successful** capture.

But three feeds cannot share one verdict. `officiating` re-raises the first
`UpstreamUnchanged` it sees, which ends the pass. All three feeds here are
nflverse release assets, but they are **three separate releases built by three
separate jobs** — `pbp`, `ftn_charting`, `pbp_participation` — and the
charting feeds depend on a third party delivering its week before nflverse can
load it. So the common mixed case is *play-by-play rebuilt after Sunday's
games, charting still on last week's build*, and under all-or-nothing the pass
aborts and **a whole week of new rates is discarded** to wait on a feed that
owns two fields of thirteen.

(In `coaching-scheme` this argument rested on `games.csv`, a live master file
that changes several times a week against weekly release assets. That feed is
gone with the staff half. The mechanism is unchanged and still required; the
justification is genuinely weaker than it was, and is stated here as what it
now is rather than carried across as what it was.)

So: catch `UpstreamUnchanged` per feed. If **every** feed is unchanged the
pass is unchanged and re-raises. If any feed changed, the unchanged ones are
re-fetched against a throwaway `ETagStore`, which sends no `If-None-Match`.
The shared store keeps its entry, which is still correct — the feed really is
unchanged — so the next pass 304s again normally.

The `bool(unchanged)` in that predicate is not defensive: `all({})` is `True`,
and an empty feed map would otherwise report a healthy pass as unchanged
forever. See `every_feed_unchanged`.

--------------------------------------------------------------------------
3. The digest gate, and why it waits for the write to land
--------------------------------------------------------------------------

`seasonal` polls daily; this data changes weekly. Publishing a byte-identical
envelope on the other six days fills an append-only lake with objects carrying
no information.

The digest is keyed by `(season, week, signal_type)` and recorded only for a
write `PublishResult.landed` confirms: record a digest for content the lake
never received and the next pass digests the same content, matches, raises
`UpstreamUnchanged`, and the object is never written again until the upstream
itself changes — which on a seasonal cadence is a week at best and a season at
worst.

The signal type is in the key even though there is only one today. It costs
nothing, it is what `coaching-staff` will need if these two are ever served
together, and a per-pass key would have to be rewritten rather than extended.

--------------------------------------------------------------------------
4. Coverage: one profile per team, floored at 32
--------------------------------------------------------------------------

Per the phase doc: *every team in the season grid has a profile with
`neutral_pass_rate` and `games_sampled` non-null. 32 teams is a declarable
floor independent of any fetch.*

Both clauses are scored, and they are scored separately, because they fail for
different reasons and a single combined check makes one of them unobservable:

* `games_sampled <= 0` — the team is in the document and has not played. Its
  row still publishes; "this team exists and has no games" is a fact worth
  serving.
* `neutral_pass_rate is None` — the team played and produced no *neutral*
  snap. Rare and real (a game that was a blowout from the first drive), and it
  is the clause that says the headline field is actually populated.

**The floor is where `expected` comes from, never the fetch.** A play-by-play
document truncated to eight teams must report 8/32, not 8/8. The team universe
here is read from the same document that produces the rates, which is exactly
the derivation `CoverageAccumulator(floor=...)` exists to defend against — so
the floor is doing real work, not decoration.

One failure it structurally cannot see: a document truncated in the **week**
direction. Every team is present, ratio 1.0, and every rate is computed over
three weeks instead of twelve. `team_scheme_min_games_sampled` is the gauge
for that; see `metrics.py`.
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
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from . import rates as rates_module
from .adapters import ftn as ftn_adapter
from .adapters import participation as participation_adapter
from .adapters import pbp as pbp_adapter
from .metrics import metrics

logger = logging.getLogger(__name__)

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "PROFILE",
    "SIGNAL_TYPES",
    "build_profile_row",
    "capture_team_scheme",
    "every_feed_unchanged",
    "reset_published_digests",
]

COLLECTOR_NAME = "team-scheme"
CADENCE_CLASS = CadenceClass.SEASONAL

PROFILE = "team_scheme_profile"
SIGNAL_TYPES = (PROFILE,)

UPSTREAM_ADAPTER = "nflverse-team-scheme"

# The size the universe is KNOWN to have, declared independently of any fetch:
# 32 teams, fixed since the 2002 realignment, and every one of them owes a
# profile. Not derived from the play-by-play document, because that document
# is also what a truncation would shrink — the exact case the floor exists for.
EXPECTED_FLOOR: dict[str, int] = {PROFILE: 32}

REASON_NO_GAMES_SAMPLED = "no_games_sampled_for_this_team"
REASON_NO_NEUTRAL_SNAPS = "no_neutral_script_snaps_for_this_team"
REASON_CHARTING_UNAVAILABLE = "ftn_charting_unavailable"
REASON_PARTICIPATION_UNAVAILABLE = "participation_unavailable"

# The one spec field with no free upstream, emitted as null with the reason
# attached to the row. An unsourceable value is a null with a reason — never a
# default, and never quietly dropped from the schema.
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
    live code path, and inlining it would make the defence **unprovable**: a
    mutation removing `bool(unchanged)` survived the whole `coaching-scheme`
    suite, because no test could construct the input that distinguishes the
    two. Naming it is what turns the claim into something a test can kill, and
    keeps it correct if a future change to the fetch order makes emptiness
    reachable.
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


def build_profile_row(
    team_id: str,
    season: int,
    profile: rates_module.TeamSeasonRates,
    *,
    degraded: tuple[str, ...],
) -> dict:
    """One team-season's scheme profile as a `team_scheme_profile` signal row.

    Mirrored by `contracts/signal-envelope/collectors/team-scheme.json`, which
    `tests/test_capture_contract_conformance.py` validates the REAL output of
    this function against.

    Keyed by `(team_id, season)` and by nothing else. **There is deliberately
    no `revision_id`, no `effective_from_week` and no `effective_to_week`** —
    those belonged to the staff half and are deferred to `coaching-staff`. A
    consumer joining on them is joining on a claim this collector cannot make.

    Carries no capture timestamp. Not an omission: the capture instant belongs
    on the envelope's `captured_at`, and a per-row one would make every daily
    digest unique and silently disable the unchanged-snapshot gate.
    """
    return {
        "team_id": team_id,
        "season": season,
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
        "degraded_upstreams": list(degraded),
        "null_field_reason": {"fourth_down_go_rate_over_expected": NULL_FOURTH_DOWN_OE},
    }


def _profile_envelope(
    *,
    play_buckets: dict[tuple[str, int], pbp_adapter.WeeklyBucket],
    charting_buckets: dict | None,
    personnel_buckets: dict | None,
    season: int,
    now: datetime,
    scope: dict,
    deadline: datetime | None,
) -> tuple[Envelope, list[dict]]:
    """Build `team_scheme_profile`, one row per team, applying the guard.

    Coverage is keyed by `team_id`: a team is what owes a profile, and there is
    nothing narrower to key it to now that revisions are gone.
    """
    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[PROFILE])
    rows: list[dict] = []
    refusals = 0

    degraded: list[str] = []
    if charting_buckets is None:
        degraded.append(REASON_CHARTING_UNAVAILABLE)
    if personnel_buckets is None:
        degraded.append(REASON_PARTICIPATION_UNAVAILABLE)
    degraded_tuple = tuple(degraded)

    games_sampled_per_team: list[int] = []

    for team_id in sorted({team for team, _ in play_buckets}):
        # Expected because the team APPEARS in the document and is therefore
        # owed a profile — never because building its row happened to succeed.
        acc.expect(team_id)
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            acc.fail(team_id, "deadline_exceeded")
            continue

        weeks = rates_module.weeks_in_team_season(team_id, play_buckets)
        profile = rates_module.aggregate(
            team_id,
            weeks,
            play_buckets=play_buckets,
            charting_buckets=charting_buckets,
            personnel_buckets=personnel_buckets,
        )
        try:
            # The guard, at write time, on the row about to be built.
            rates_module.assert_window_is_the_team_season(team_id, profile)
        except rates_module.WindowIsNotTheTeamSeason as exc:
            refusals += 1
            acc.fail(team_id, exc.reason)
            continue

        rows.append(
            build_profile_row(team_id, season, profile, degraded=degraded_tuple)
        )
        games_sampled_per_team.append(profile.games_sampled)

        # The phase doc's coverage sentence, both clauses, scored separately.
        if profile.games_sampled <= 0:
            # A team that has not played is expected and missing, not
            # present-with-nulls. Its row still publishes.
            acc.fail(team_id, REASON_NO_GAMES_SAMPLED)
            continue
        if profile.neutral_pass_rate is None:
            acc.fail(team_id, REASON_NO_NEUTRAL_SNAPS)
            continue
        acc.record(team_id)

    metrics.window_refusals(refusals)
    metrics.degraded_upstreams(len(degraded_tuple))
    # Zero when nothing was profiled at all, which is honest: the smallest
    # sample behind any published rate is then vacuously none.
    metrics.min_games_sampled(min(games_sampled_per_team, default=0))

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


async def capture_team_scheme(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one `team_scheme_profile` envelope.

    `week` scopes the lake partition and the envelope, and nothing else. **The
    rates are not week-scoped**: a team's profile is drawn from every week of
    its season, which is what "keyed to a team-season" means. Asking "what did
    this team do in week 9" is not a question this collector answers — it
    answers "what has this team done this season, as of week 9".
    """
    scope = {"season": season, "week": week}
    metrics.capture_attempt()

    async def _pbp(**kw):
        return await pbp_adapter.fetch_weekly_buckets(season, client=client, **kw)

    fetchers: dict[str, Callable[..., Awaitable]] = {"pbp": _pbp}
    results: dict[str, object] = {}
    unchanged: dict[str, bool] = {}

    # --- Phase 1: play-by-play, which everything else needs ----------------
    try:
        results["pbp"], unchanged["pbp"] = await _fetch(_pbp)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Fatal: with the staff half deferred there is no signal type left to
        # publish. Do NOT call `metrics.capture_failure(exc)` first — the
        # library owns that counter for a failure that ends a pass.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=pbp_adapter.UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            expected=EXPECTED_FLOOR,
            source_ref=pbp_adapter.source_ref(season),
        )

    # --- Phase 2: the charted feeds, which need pbp's play index -----------
    #
    # `results["pbp"]` is None exactly when phase 1 answered 304, in which case
    # the index arrives with the unconditional re-fetch below — and if nothing
    # else changed, the pass ends before either charted feed is read.
    play_index: dict = results["pbp"][1] if results["pbp"] is not None else {}

    charting_failure: Exception | None = None
    personnel_failure: Exception | None = None

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
    # True and the emptiness guard is otherwise untestable — see that function.
    if every_feed_unchanged(unchanged):
        raise UpstreamUnchanged(pbp_adapter.source_ref(season))

    # Some feed changed, so the 304'd ones must be read after all.
    await _refetch_unconditionally(unchanged, fetchers, results)

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
        play_buckets=play_buckets,
        charting_buckets=charting_buckets,
        personnel_buckets=personnel_buckets,
        season=season,
        now=now,
        scope=scope,
        deadline=deadline,
    )

    return await _publish_changed(
        {PROFILE: profile_envelope},
        {PROFILE: _digest(profile_rows)},
        season,
        week,
        lake,
    )


async def _publish_changed(
    envelopes: dict[str, Envelope],
    digests: dict[str, str],
    season: int,
    week: int,
    lake: LakeWriter,
) -> dict[str, Envelope]:
    """Write only what changed, and record only what landed.

    `coaching-scheme` also re-recorded the coverage gauge for an envelope the
    gate suppressed, because `publish_capture` only records the ones it is
    handed and a gauge that went quiet whenever the data was stable would read
    as a stopped collector. **That loop is deliberately absent here and is not
    an oversight:** with one signal type, `changed` is either empty (and this
    raises) or the whole of `envelopes`, so the loop body is unreachable and
    an unreachable branch is worse than none — nothing exercises it and
    nothing would notice it rotting. Restore it in the same commit that adds
    a second signal type, not before.
    """
    changed = {
        signal_type: envelope
        for signal_type, envelope in envelopes.items()
        if _PUBLISHED_DIGESTS.get((season, week, signal_type)) != digests[signal_type]
    }
    if not changed:
        raise UpstreamUnchanged(
            pbp_adapter.source_ref(season), source_ref=digests[PROFILE]
        )

    published = await publish_capture(changed, lake=lake, metrics=metrics)

    # Gated on the write LANDING, per signal type. Iterating `published`
    # rather than `envelopes` is required, not stylistic: `landed` raises for a
    # signal type this call did not publish, and that raise costs the whole
    # pass after every write has already happened.
    for signal_type in published:
        if published.landed(signal_type):
            _PUBLISHED_DIGESTS[(season, week, signal_type)] = digests[signal_type]

    return envelopes
