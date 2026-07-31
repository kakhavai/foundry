"""Resolution: config rules plus a depth chart become a versioned membership
list, a change stream, and the route views over both.

`rules.py` declares what the scope demands. This module decides what it
actually got, and — importantly — records the difference rather than hiding
it. Every path through here that cannot fill a slot calls
`CoverageAccumulator.fail`; nothing is ever skipped, because a skipped slot
shrinks numerator and denominator together and reads as perfect health.

Three decisions here are load-bearing and none are obvious:

1. **Co-listed players are ordered by normalized name, not by the upstream's
   text order.** A chart writing `Player A OR Player B` is the upstream
   explicitly declining to order them. Preserving its text order manufactures
   a spurious `rank_changed` event every time the chart cosmetically flips to
   `Player B OR Player A`.

2. **The distinct-`depth_rank` invariant applies to `active` rows only.**
   Grace rows carry their last known rank, which necessarily collides with
   the active row that replaced them.

3. **The version ledger lives in the lake, not in memory.** A restarted pod
   that reset to version 1 would break the immutable-additive model the
   entire fleet pins against.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from collector_core.coverage import CoverageAccumulator
from collector_core.lake import LakeWriter
from fastapi import HTTPException

from .adapters.depth_chart import DepthChart
from .adapters.identity import (
    PlayerIdentityResolver,
    PlayerRef,
    UnresolvablePlayer,
    normalize_name,
)
from .rules import (
    ALL_RULES,
    EXCLUDED_PLAYERS,
    GRACE_WEEKS,
    MANUAL_OVERRIDES,
    MATCHUP_RULES,
    MAX_DEPTH_CHART_AGE_HOURS,
    TEAMS,
    canonical_position,
    slot_key,
)

MEMBERSHIP_SIGNAL = "scope_membership_weekly"
CHANGE_SIGNAL = "scope_change_event"

# Uppercase ` OR ` and `/` only. No IGNORECASE: a real surname can contain
# "or" (Moore, Thornton), and folding case here would shred it into two
# players that resolve to nothing.
_CO_LISTING = re.compile(r" OR |/")

# `scope_version` when the ledger could not be read. Not 1 — minting version 1
# on a lake outage would collide with a real version 1 that already exists and
# break the "immutable and additive" guarantee consumers pin against.
NO_VERSION = 0


def _rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class LedgerUnavailable(Exception):
    """The lake could not be read, so no version can be minted."""


@dataclass(frozen=True)
class Candidate:
    name: str
    # Dropped for co-listed rows: which of `A OR B` owns the number the chart
    # printed is genuinely ambiguous, and guessing is worse than absent.
    jersey_number: int | None


@dataclass(frozen=True)
class PreviousScope:
    """The last membership envelope found in the lake, and where it came from."""

    version: int
    rows: tuple[dict, ...]
    week: int | None


def split_co_listed(raw: str) -> list[str]:
    """`"A OR B"` / `"A/B"` -> `["A", "B"]`, in normalized-name order.

    Ordering here rather than at the call site so the ordering rule has one
    home. Breaking a co-listing into a deterministic total order — rather
    than dropping one of the pair — is mandatory per the Phase 8 adapter
    notes.
    """
    parts = [p.strip() for p in _CO_LISTING.split(raw or "")]
    names = [p for p in parts if p]
    if len(names) <= 1:
        return names
    return sorted(names, key=normalize_name)


def ordered_candidates(
    chart: DepthChart | None, position: str
) -> tuple[Candidate, ...]:
    """The chart's players at one canonical position, in rank order.

    Positions are collapsed here (`SE`/`FL`/`X`/`Z` -> `WR`), co-listings are
    expanded in place, and pinned-out players are removed before ranks are
    assigned — so removing a player promotes everyone below them rather than
    leaving a hole.
    """
    if chart is None:
        return ()
    candidates: list[Candidate] = []
    rows = [r for r in chart.rows if canonical_position(r.position_raw) == position]
    for row in sorted(rows, key=lambda r: r.depth_order):
        names = split_co_listed(row.name_raw)
        co_listed = len(names) > 1
        for name in names:
            if normalize_name(name) in EXCLUDED_PLAYERS:
                continue
            candidates.append(Candidate(name, None if co_listed else row.jersey_number))
    return tuple(candidates)


def load_previous_scope(
    lake: LakeWriter, collector: str, season: int, week: int
) -> PreviousScope:
    """The newest membership envelope for `(season, week)`, else `week - 1`.

    At most two `list_keys` calls. The `week - 1` fallback is what keeps
    versions season-monotonic across a week rollover and — the part that
    actually matters — carries grace state over the boundary, so a player who
    fell out in week 4 is still in grace in week 5 rather than silently
    re-entering as a brand new row.

    Raises `LedgerUnavailable` on any lake error. The caller must not treat
    that as "no previous scope": those two states differ by an entire version
    history.
    """
    try:
        for candidate_week in (week, week - 1):
            if candidate_week < 1:
                continue
            keys = lake.list_keys(collector, MEMBERSHIP_SIGNAL, season, candidate_week)
            if not keys:
                continue
            # Keys sort by captured_at first (see collector_core.lake.lake_key),
            # so the last key is the newest capture for the partition.
            body = lake.read(keys[-1])
            version = int(body.get("scope", {}).get("scope_version", 0))
            return PreviousScope(
                version=version,
                rows=tuple(body.get("signals", ())),
                week=candidate_week,
            )
    except Exception as exc:  # noqa: BLE001 — classified by the caller
        raise LedgerUnavailable(str(exc)) from exc
    return PreviousScope(version=0, rows=(), week=None)


def count_stale_depth_charts(
    charts: dict[str, DepthChart],
    now: datetime,
    *,
    max_age_hours: int = MAX_DEPTH_CHART_AGE_HOURS,
) -> int:
    """Teams whose chart is missing or older than the freshness invariant.

    A count, not an average. One team's frozen chart moves a 32-team average
    by 3% and is invisible; it moves this gauge from 0 to 1.
    """
    cutoff = now - timedelta(hours=max_age_hours)
    stale = 0
    for team in TEAMS:
        chart = charts.get(team)
        if chart is None or chart.captured_at < cutoff:
            stale += 1
    return stale


def distinct_rank_violations(rows: list[dict]) -> list[dict]:
    """`depth_rank` must be distinct within every `(team, position)`.

    A duplicate means the adapter collapsed a co-listing instead of breaking
    it. **Active rows only** — grace rows deliberately carry their last known
    rank, which collides with whoever replaced them, and folding those in
    would make the invariant fire on correct behaviour.
    """
    seen: dict[tuple[str, str, int], str] = {}
    violations: list[dict] = []
    for row in rows:
        if row.get("membership_status") != "active":
            continue
        key = (row["team"], row["position"], row["depth_rank"])
        if key in seen:
            violations.append(
                {
                    "reason": "duplicate_depth_rank",
                    "detail": f"{key[0]}:{key[1]}:{key[2]}",
                }
            )
            continue
        seen[key] = row["player_id"]
    return violations


def reconcile_missed_producers(
    usage_rows: list[dict], membership_rows: list[dict]
) -> list[str]:
    """`player_id`s that produced while the scope said to stop fetching them.

    The one assertion external to scope, and therefore the only one that can
    catch "the scope is wrong and reports itself as complete". A nonzero
    result means a real producer was outside the universe every downstream
    collector fetches for.

    **The producer half of this is deferred.** Realized usage comes from
    `usage-share`, an 8B collector that does not exist yet, so nothing calls
    this with a non-empty `usage_rows` today. The function is implemented and
    unit-tested rather than stubbed, and `capture` records `0` every pass so
    the series exists to alert on — see `metrics.missed_producers`.
    """
    excluded = {
        row["player_id"]
        for row in membership_rows
        if row.get("membership_status") == "excluded"
    }
    produced = {
        row["player_id"]
        for row in usage_rows
        if row.get("player_id") in excluded
        and any(
            float(row.get(field) or 0) > 0
            for field in ("snap_share", "targets", "carries")
        )
    }
    return sorted(produced)


async def resolve_membership(
    *,
    charts: dict[str, DepthChart],
    resolver: PlayerIdentityResolver,
    previous: PreviousScope,
    season: int,
    week: int,
    version: int,
    acc: CoverageAccumulator,
    deadline: datetime | None = None,
    clock=_utc_now,
) -> list[dict]:
    """Fill every config slot, then carry forward whoever fell out.

    A total chart outage is **not** a special case here: `charts == {}` flows
    through this same loop, every human slot fails with a classified reason,
    and the 32 config-derived `team_defense` slots still fill. The result is
    a ratio of 32/416 ~= 0.077 — low, and visibly so, which is the point. A
    special-cased outage branch would have to reimplement the accounting and
    would inevitably drift from it.
    """
    overrides = {o.slot_key: o for o in MANUAL_OVERRIDES}
    previous_by_id = {
        row["player_id"]: row for row in previous.rows if row.get("player_id")
    }
    rows: list[dict] = []
    seen_ids: set[str] = set()
    truncated = False

    for team in TEAMS:
        chart = charts.get(team)
        chart_at = _rfc3339(chart.captured_at) if chart is not None else None
        by_position = {
            rule.position: ordered_candidates(chart, rule.position)
            for rule in ALL_RULES
            if rule.entity_type == "player"
        }

        # Checked between teams rather than by wrapping the pass in a timeout:
        # cancelling the coroutine would discard everything already resolved,
        # where this preserves the partial capture and accounts for the rest.
        if deadline is not None and not truncated and clock() >= deadline:
            truncated = True

        for rule in ALL_RULES:
            for rank in range(1, rule.max_depth + 1):
                key = slot_key(team, rule.rule_id, rank)

                if truncated:
                    acc.fail(key, "deadline_exceeded")
                    continue

                if rule.entity_type == "team_defense":
                    # No human behind it, so it comes from config and cannot
                    # fail on an upstream outage.
                    row = _build_row(
                        player_id=f"fdy-dst-{team.lower()}",
                        entity_type="team_defense",
                        rule_id=rule.rule_id,
                        team=team,
                        position=rule.position,
                        rank=rank,
                        version=version,
                        chart_at=chart_at,
                        previous_by_id=previous_by_id,
                    )
                    rows.append(row)
                    seen_ids.add(row["player_id"])
                    acc.record(key)
                    continue

                override = overrides.get(key)
                if override is not None:
                    ref = PlayerRef(override.player_name, team, rule.position)
                else:
                    candidates = by_position.get(rule.position, ())
                    if chart is None:
                        acc.fail(key, "depth_chart_unavailable")
                        continue
                    if len(candidates) < rank:
                        # The spec's worked example: a team whose chart yields
                        # only three receivers under `WR<=4` contributes one
                        # entry to coverage.missing.
                        acc.fail(key, "depth_chart_short")
                        continue
                    candidate = candidates[rank - 1]
                    ref = PlayerRef(
                        candidate.name, team, rule.position, candidate.jersey_number
                    )

                try:
                    player_id = await resolver.resolve(ref)
                except UnresolvablePlayer as exc:
                    # Never a skipped row: an unresolvable name is a *missing
                    # slot*, so the denominator holds and the hole is visible.
                    acc.fail(key, exc.reason)
                    continue

                row = _build_row(
                    player_id=player_id,
                    entity_type="player",
                    rule_id=rule.rule_id,
                    team=team,
                    position=rule.position,
                    rank=rank,
                    version=version,
                    chart_at=chart_at,
                    previous_by_id=previous_by_id,
                    override=override,
                )
                rows.append(row)
                seen_ids.add(player_id)
                acc.record(key)

    rows.extend(_carry_forward(previous_by_id, seen_ids, week=week, version=version))
    return rows


def _build_row(
    *,
    player_id: str,
    entity_type: str,
    rule_id: str,
    team: str,
    position: str,
    rank: int,
    version: int,
    chart_at: str | None,
    previous_by_id: dict[str, dict],
    override=None,
) -> dict:
    prior = previous_by_id.get(player_id, {})
    return {
        "player_id": player_id,
        "entity_type": entity_type,
        "scope_version": version,
        "membership_status": "active",
        "rule_id": rule_id,
        "team": team,
        "position": position,
        "depth_rank": rank,
        "previous_depth_rank": prior.get("depth_rank"),
        "depth_source_captured_at": chart_at,
        # A player already in scope keeps the version they entered at; a new
        # one enters at this version. That is what makes "how long have we
        # been watching them" answerable without replaying the whole lake.
        "added_at_version": prior.get("added_at_version", version),
        "grace_expires_week": None,
        "is_manual_override": override is not None,
        "override_reason": None if override is None else override.reason,
    }


def _carry_forward(
    previous_by_id: dict[str, dict], seen_ids: set[str], *, week: int, version: int
) -> list[dict]:
    """Players who fell out of the rule window this version.

    They do not disappear. `active` -> `grace` with `grace_expires_week` two
    weeks out, and collectors keep fetching for them, because depth charts
    churn on Tuesday and revert on Friday and a three-day hole in a partially
    captured week can never be backfilled from a real-time upstream.

    An `excluded` row is emitted exactly once — in the version where the
    transition happens — and then dropped. Emitting it forever would grow the
    envelope without bound, and `GET /scope/diff` is where "why did this
    player's data stop" is answered.
    """
    carried: list[dict] = []
    for player_id, prior in previous_by_id.items():
        if player_id in seen_ids:
            continue
        status = prior.get("membership_status")
        if status == "excluded":
            continue

        if status == "grace":
            expires = prior.get("grace_expires_week")
            if expires is not None and week > int(expires):
                new_status, new_expires = "excluded", None
            else:
                new_status, new_expires = "grace", expires
        else:
            new_status, new_expires = "grace", week + GRACE_WEEKS

        carried.append(
            {
                **prior,
                "scope_version": version,
                "membership_status": new_status,
                "previous_depth_rank": prior.get("depth_rank"),
                "grace_expires_week": new_expires,
            }
        )
    return carried


def build_change_events(
    previous: PreviousScope, rows: list[dict], *, version: int, occurred_at: datetime
) -> list[dict]:
    """The transitions between the previous version and this one.

    `trigger` is `manual` for an override-filled slot and `depth_chart`
    otherwise. Finer triggers (`transaction`, `injury_designation`) need the
    8B collectors that publish those events; claiming them from a depth-chart
    diff alone would be a guess dressed as provenance.
    """
    previous_by_id = {
        row["player_id"]: row for row in previous.rows if row.get("player_id")
    }
    stamp = _rfc3339(occurred_at)
    events: list[dict] = []

    for row in rows:
        player_id = row["player_id"]
        prior = previous_by_id.get(player_id)
        status = row["membership_status"]
        trigger = "manual" if row.get("is_manual_override") else "depth_chart"

        if prior is None:
            if status == "active":
                transition, from_rank = "entered", None
            else:
                continue
        elif status == "grace" and prior.get("membership_status") != "grace":
            transition, from_rank = "entered_grace", prior.get("depth_rank")
        elif status == "excluded":
            transition, from_rank = "excluded", prior.get("depth_rank")
        elif status == "active" and prior.get("membership_status") != "active":
            transition, from_rank = "entered", prior.get("depth_rank")
        elif status == "active" and prior.get("depth_rank") != row["depth_rank"]:
            transition, from_rank = "rank_changed", prior.get("depth_rank")
        else:
            continue

        events.append(
            {
                "scope_version": version,
                "player_id": player_id,
                "transition": transition,
                "from_depth_rank": from_rank,
                "to_depth_rank": row["depth_rank"] if status == "active" else None,
                "trigger": trigger,
                "occurred_at": stamp,
            }
        )
    return events


# --------------------------------------------------------------------------
# Row filtering and route views
# --------------------------------------------------------------------------

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "player_id",
    "team",
    "position",
    "rule_id",
    "membership_status",
    "scope_version",
)

_ROW_FILTERS = (
    "player_id",
    "team",
    "position",
    "rule_id",
    "membership_status",
    "scope_version",
)


def signal_matches(row: dict, params) -> bool:
    """Collector-specific row filtering for `GET /signals`.

    Compared as strings because query parameters are strings; `scope_version`
    is an integer in the row, so `str()` on both sides keeps `?scope_version=3`
    working without a per-field cast table.
    """
    for field in _ROW_FILTERS:
        if field in params and str(row.get(field)) != str(params[field]):
            return False
    return True


def _read_version_from_lake(spec, scope_version: int, season: int, week: int):
    """The blocking half of the lookup, run in a worker thread by the caller."""
    keys = spec.lake.list_keys(spec.name, MEMBERSHIP_SIGNAL, season, week)
    for key in reversed(keys):
        body = spec.lake.read(key)
        if int(body.get("scope", {}).get("scope_version", -1)) == scope_version:
            return list(body.get("signals", []))
    return None


async def _membership_rows(
    spec, scope_version: int | None
) -> tuple[int | None, list[dict]]:
    """Rows for a version: from memory for the latest, from the lake otherwise.

    The lake branch goes through `asyncio.to_thread` for the same reason
    `capture.py` does — boto3 is synchronous, and a slow object store must not
    freeze the event loop and take `/health` down with it.
    """
    envelope = spec.state.envelopes.get(MEMBERSHIP_SIGNAL)
    latest = None if envelope is None else envelope.scope.get("scope_version")

    if scope_version is None or (latest is not None and scope_version == latest):
        return latest, list(envelope.signals) if envelope is not None else []

    season = spec.default_scope["season"]
    week = spec.default_scope["week"]
    try:
        rows = await asyncio.to_thread(
            _read_version_from_lake, spec, scope_version, season, week
        )
    except Exception:  # noqa: BLE001 — an unreadable lake is a 404, not a 500
        rows = None
    if rows is not None:
        return scope_version, rows
    raise HTTPException(
        status_code=404, detail=f"no resolved scope for version {scope_version}"
    )


async def scope_players_view(
    spec, *, scope_version: int | None, status: str | None
) -> dict:
    """`GET /scope/players` — the list every other collector calls for."""
    version, rows = await _membership_rows(spec, scope_version)
    if status is not None:
        rows = [r for r in rows if r.get("membership_status") == status]
    return {
        "scope_version": version,
        "players": rows,
        "count": len(rows),
    }


def scope_rules_view(spec) -> dict:
    """`GET /scope/rules` — the config *as applied*, for both rule sets.

    Including which rules produced zero slots, which is the whole reason this
    route exists: a rule silently matching nothing is otherwise indis-
    tinguishable from a rule that is working. That reasoning applies exactly
    as much to `MATCHUP_RULES` (Task 6's opposing-player scope) as it does to
    `ALL_RULES` — a matchup rule silently matching nothing is invisible
    without this, same as a player-scope one — so both are reported here.

    Kept as **two separate lists** (`rules`, `matchup_rules`) rather than one
    merged one. The two scopes have independent `CoverageAccumulator`s for
    the same reason `capture.py` and `matchups.py`'s docstrings give: a
    matchup-scope outage must not read as a player-scope problem or vice
    versa, and merging their `produced_zero_slots` signal into one list would
    make "which scope is actually unhealthy" a string-matching exercise on
    `rule_id` instead of a field on the response.
    """
    envelope = spec.state.envelopes.get(MEMBERSHIP_SIGNAL)
    rows = [] if envelope is None else envelope.signals
    filled: dict[str, int] = {}
    for row in rows:
        if row.get("membership_status") == "active":
            filled[row["rule_id"]] = filled.get(row["rule_id"], 0) + 1

    # Deferred: `matchups.py` imports `split_co_listed` from this module at
    # its own top level, so importing `matchups.py` from here at module level
    # would be circular. Both modules are fully loaded by the time any route
    # handler runs, so a call-time import is safe.
    from .matchups import MATCHUP_SIGNAL

    matchup_envelope = spec.state.envelopes.get(MATCHUP_SIGNAL)
    matchup_rows = [] if matchup_envelope is None else matchup_envelope.signals
    matchup_filled: dict[str, int] = {}
    for row in matchup_rows:
        matchup_filled[row["rule_id"]] = matchup_filled.get(row["rule_id"], 0) + 1

    version = None if envelope is None else envelope.scope.get("scope_version")
    return {
        "scope_version": version,
        "teams": len(TEAMS),
        "grace_weeks": GRACE_WEEKS,
        "manual_overrides": [
            {"slot": o.slot_key, "player_name": o.player_name, "reason": o.reason}
            for o in MANUAL_OVERRIDES
        ],
        "excluded_players": dict(EXCLUDED_PLAYERS),
        "rules": [
            {
                "rule_id": rule.rule_id,
                "position": rule.position,
                "max_depth": rule.max_depth,
                "entity_type": rule.entity_type,
                "slots_expected": len(TEAMS) * rule.max_depth,
                "slots_filled": filled.get(rule.rule_id, 0),
                "produced_zero_slots": filled.get(rule.rule_id, 0) == 0,
            }
            for rule in ALL_RULES
        ],
        "matchup_rules": [
            {
                "rule_id": rule.rule_id,
                "position": rule.position,
                "max_depth": rule.max_depth,
                "slots_expected": len(TEAMS) * rule.max_depth,
                "slots_filled": matchup_filled.get(rule.rule_id, 0),
                "produced_zero_slots": matchup_filled.get(rule.rule_id, 0) == 0,
            }
            for rule in MATCHUP_RULES
        ],
    }


def _read_change_events(spec, season: int, week: int) -> list[dict]:
    """The blocking half, run in a worker thread by the caller."""
    events: list[dict] = []
    for key in spec.lake.list_keys(spec.name, CHANGE_SIGNAL, season, week):
        events.extend(spec.lake.read(key).get("signals", []))
    return events


async def scope_diff_view(spec, *, version_from: int, version_to: int) -> dict:
    """`GET /scope/diff` — transitions in `(from, to]`.

    Half-open deliberately: a diff from 4 to 6 answers "what changed since I
    pinned version 4", which must not re-report the events that produced
    version 4 itself.
    """
    if version_to < version_from:
        raise HTTPException(
            status_code=422, detail="`to` must be greater than or equal to `from`"
        )

    season = spec.default_scope["season"]
    week = spec.default_scope["week"]
    try:
        events = await asyncio.to_thread(_read_change_events, spec, season, week)
    except Exception:  # noqa: BLE001 — fall back to what this process captured
        events = []
    if not events:
        envelope = spec.state.envelopes.get(CHANGE_SIGNAL)
        events = [] if envelope is None else list(envelope.signals)

    selected = [
        event
        for event in events
        if version_from < int(event.get("scope_version", 0)) <= version_to
    ]
    selected.sort(key=lambda e: (e.get("scope_version", 0), e.get("player_id", "")))
    return {
        "from": version_from,
        "to": version_to,
        "transitions": selected,
        "count": len(selected),
    }
