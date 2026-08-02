"""The capture pass: fetch -> resolve -> rate -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

Three orderings here are correctness rather than style.

**The roster map is fetched before the play-by-play.** It is 2.39 MiB against
18.22 MiB and it gates which players the fold accumulates at all, so a failure
of the cheap feed costs the expensive one nothing.

**Identity resolution runs between the fold and the attribution**, not after
it. The spec requires every allowed opportunity to resolve to a canonical
`player_id` *before* it is attributed to a position, and
`pbp.totals_from_fold` is the only place attribution happens. Resolving
afterwards would produce identical numbers on a healthy day and silently
publish unresolvable players' opportunities on every other one.

**`coverage.expected` never derives from what succeeded.** The universe is the
32 defenses, declared as a constant, and a defense counts present only when
every declared (position, alignment, scoring_format) split exists with
`games_sampled >= 1`. Both halves matter independently: a correct floor with a
hollowed-out `present` predicate reports a league of empty rows as complete,
and a correct predicate over a fetch-derived floor reports a two-team
truncation as a perfect week.

Every lake call goes off the event loop -- the success path through
`publish_capture`, the failure path through `fail_capture`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.conditional import UpstreamUnchanged
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from .adapters import pbp, players
from .adapters.identity import (
    IDENTITY_UNRESOLVED,
    IDENTITY_UPSTREAM_ERROR,
    IdentityFailures,
    IdentityUnavailable,
    build_identity_client,
    resolve_players,
)
from .metrics import metrics
from .ratings import Split, build_rows, declared_splits

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "capture_defense_vs_position",
]

COLLECTOR_NAME = "defense-vs-position"
CADENCE_CLASS = CadenceClass.WEEKLY
SIGNAL_TYPES = ("defense_positional_allowance",)

UPSTREAM_ADAPTER = pbp.UPSTREAM_ADAPTER

# **The size the universe is KNOWN to have, independently of any fetch.** The
# league has had 32 clubs since 2002 and one row set is owed per defense. It
# is deliberately not a count of teams seen in the document: a play-by-play
# truncated to a single game describes two defenses, which without this floor
# would report `expected: 2, present: 2`, ratio 1.0.
NFL_TEAMS = 32

EXPECTED_FLOOR: dict[str, int] = {
    "defense_positional_allowance": NFL_TEAMS,
}

# A defense that has rows but not a complete, sampled set of them. Distinct
# from a defense the feed never mentioned at all, which is never `expect`ed
# and instead shows up as a shortfall against the floor.
INCOMPLETE_SPLITS = "incomplete_splits"

# One flagged (team, position, alignment, scoring_format) split, per entry
# rather than summarised: "flag for manual review" is only actionable if an
# operator can see WHICH row to look at. The measured worst case is 92 entries
# against `CoverageAccumulator`'s 50-cap, so the list is capped with the
# library's explicit marker rather than silently shortened.
RANK_DIVERGENCE = "rank_divergence"


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_key(row: dict) -> str:
    """The coverage key for one published row: the defense it describes.

    Coverage is counted per DEFENSE, not per row, because that is what the
    spec says -- "all 32 defenses, each with a populated row for every
    (position, alignment, scoring_format) combination". A per-row key would
    make 576 the denominator and let a defense missing two of its eighteen
    splits read as 99.7% healthy.
    """
    return str(row["team_id"])


def build_signal(signal_type: str, row: dict, *, now: datetime) -> dict:
    """One rated split -> one signal row.

    Mirrored by `contracts/signal-envelope/collectors/defense-vs-position.json`,
    which `tests/test_capture_contract_conformance.py` validates the REAL
    output of this function against.
    """
    return {
        "team_id": row["team_id"],
        "position": row["position"],
        "alignment": row["alignment"],
        "scoring_format": row["scoring_format"],
        "observed_at": _rfc3339(now),
        "games_sampled": row["games_sampled"],
        "opportunities_defended": row["opportunities_defended"],
        "fantasy_points_allowed_per_game": row["fantasy_points_allowed_per_game"],
        "fantasy_points_allowed_per_game_adj": row[
            "fantasy_points_allowed_per_game_adj"
        ],
        "fantasy_points_allowed_per_opportunity": row[
            "fantasy_points_allowed_per_opportunity"
        ],
        "targets_allowed_per_game": row["targets_allowed_per_game"],
        "receptions_allowed_per_game": row["receptions_allowed_per_game"],
        "receiving_yards_allowed_per_game": row["receiving_yards_allowed_per_game"],
        "yac_allowed_per_reception": row["yac_allowed_per_reception"],
        "rush_yards_allowed_per_carry": row["rush_yards_allowed_per_carry"],
        "touchdowns_allowed_per_game": row["touchdowns_allowed_per_game"],
        "opponent_strength_index": row["opponent_strength_index"],
        "adjustment_method": row["adjustment_method"],
        "adjustment_window_weeks": row["adjustment_window_weeks"],
        "rank_divergence_flagged": row["rank_divergence_flagged"],
    }


@dataclass
class Gathered:
    """One pass's rated rows, plus what it had to drop to get them."""

    rows: list[dict]
    flagged: dict[Split, float]
    # Players with opportunities whom `player-identity` did not resolve. Their
    # opportunities are dropped, which deflates a defense's rates without
    # removing its row -- invisible to coverage by construction, which is why
    # it is reported here and on a gauge.
    unresolved: int
    seen: int


async def _gather(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    failures: IdentityFailures,
) -> Gathered:
    """Everything between the first byte and the rated rows.

    Split out so the failure envelope's `try`/`except` in
    `capture_defense_vs_position` wraps one call rather than a page of
    orchestration -- the shape that makes it obvious no upstream call escapes
    it.
    """
    identity = build_identity_client(client)
    positions = await players.fetch_positions(client=client)
    fold = await pbp.fetch_fold(season, week, client=client, positions=positions)
    resolved = await resolve_players(
        fold.players,
        season=season,
        positions=positions,
        identity=identity,
        failures=failures,
    )
    metrics.players_resolved(len(resolved), len(fold.players))
    totals = pbp.totals_from_fold(fold, positions=positions, resolved=resolved)
    rows, flagged = build_rows(totals)
    return Gathered(
        rows=rows,
        flagged=flagged,
        unresolved=len(fold.players) - len(resolved),
        seen=len(fold.players),
    )


async def capture_defense_vs_position(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one envelope per signal type."""
    scope = {"season": season, "week": week}
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=pbp.source_ref(season),
    )
    failures = IdentityFailures()
    failure_context = dict(
        collector=COLLECTOR_NAME,
        signal_types=SIGNAL_TYPES,
        adapter=UPSTREAM_ADAPTER,
        now=now,
        scope=scope,
        lake=lake,
        metrics=metrics,
        expected=EXPECTED_FLOOR,
        source_ref=pbp.source_ref(season),
    )

    metrics.capture_attempt()
    try:
        gathered = await _gather(season, week, client=client, failures=failures)
    except UpstreamUnchanged:
        # A 304 is a SUCCESSFUL capture, not a failure, and this arm must stay
        # ABOVE the generic handler. `run_capture_loop`/`_run_capture` catch
        # it and call `mark_unchanged`, which advances `last_capture_at` and
        # records `collector_upstream_unchanged_total` without touching the
        # stored envelopes -- so `/signals` keeps serving the previous
        # capture's rows. Routed into `fail_capture` instead, an unchanged
        # upstream would write `present: 0` over a perfectly healthy capture.
        raise
    except IdentityUnavailable as exc:
        # Fails closed with its own reason rather than the classifier's
        # `unknown`: "this pod was never pointed at player-identity" and "the
        # capture crashed" have different fixes, and `reason` reaches
        # `collector_capture_failures_total` as a label as well as the
        # envelope.
        await fail_capture(exc, reason=exc.reason, **failure_context)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Records `collector_capture_failures_total`, writes a `present: 0`
        # envelope per signal type, then re-raises. Never returns. Do NOT call
        # `metrics.capture_failure(exc)` first: the library owns that counter
        # for a failure that ends a pass, and doing both double-counts.
        await fail_capture(exc, **failure_context)

    expected_splits = declared_splits()
    envelopes: dict[str, Envelope] = {}
    for signal_type in SIGNAL_TYPES:
        acc = CoverageAccumulator(floor=EXPECTED_FLOOR[signal_type])
        signals: list[dict] = []
        # `team -> how many of its declared splits carry a real sample`. A row
        # that exists with `games_sampled == 0` is a placeholder, not a datum,
        # and must not count toward presence.
        populated: dict[str, int] = {}
        teams: set[str] = set()

        for row in gathered.rows:
            key = _row_key(row)
            teams.add(key)
            # Declared because the defense EXISTS and is therefore owed a full
            # set of rows -- never because building one below succeeded.
            acc.expect(key)
            if deadline is not None and datetime.now(tz=UTC) >= deadline:
                # Over budget. Record the rest as missing rather than throwing
                # away what already resolved.
                acc.fail(key, "deadline_exceeded")
                continue
            try:
                signals.append(build_signal(signal_type, row, now=now))
            except Exception as exc:  # noqa: BLE001 — one bad row is not a pass
                acc.fail(key, metrics.reason_for(exc))
                continue
            if row["games_sampled"] >= 1:
                populated[key] = populated.get(key, 0) + 1

        for team in sorted(teams):
            if populated.get(team, 0) >= expected_splits:
                acc.record(team)
            else:
                acc.add_error(
                    INCOMPLETE_SPLITS,
                    f"{team}: {populated.get(team, 0)}/{expected_splits} "
                    "split(s) sampled",
                )

        for split, gap in sorted(
            gathered.flagged.items(),
            key=lambda item: (-item[1], item[0].team_id, item[0].position),
        ):
            acc.add_error(
                RANK_DIVERGENCE,
                f"{split.team_id}/{split.position}/{split.alignment}/"
                f"{split.scoring_format}: per-game and per-opportunity ranks "
                f"differ by {gap:g} places",
            )

        if gathered.unresolved:
            # Summarised, and a PRIORITY error: it explains the whole pass
            # when it is large, and a few hundred routine `rank_divergence`
            # entries would otherwise push it past the 50-entry cap. One
            # entry, never one per player.
            acc.add_priority_error(
                IDENTITY_UNRESOLVED,
                f"{gathered.unresolved} of {gathered.seen} player(s) with "
                "opportunities were not resolved; their opportunities are "
                "excluded from every rate",
            )
        if failures.rows:
            # A failed REQUEST is a different fact from a refusal, and only
            # this one is a `player-identity` incident.
            acc.add_priority_error(IDENTITY_UPSTREAM_ERROR, failures.detail())

        metrics.rows_captured(len(signals))
        metrics.rank_divergences(len(gathered.flagged))
        envelopes[signal_type] = Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=signal_type,
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=acc.result(),
            errors=acc.errors,
            signals=signals,
        )

    # Writes every envelope off the event loop, records each coverage gauge,
    # records `collector_capture_failures_total` if a write fails -- and
    # returns the envelopes ANYWAY. An object-store outage must not cost
    # `/signals` a capture that is already built and correct. Do not replace
    # this with a hand-written `awrite` loop.
    return await publish_capture(envelopes, lake=lake, metrics=metrics)
