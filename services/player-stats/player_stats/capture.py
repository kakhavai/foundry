"""player-stats' capture pass: lake -> scope -> box scores -> envelope -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**. A collector that reaches
its upstream inside a request handler has inverted that contract.

Five things here are correctness rather than style.

**The watchlist is fetched from the lake before the upstream is touched, and
a failure to fetch it is fatal to the pass.** `adapters/scope.py` reads the
last `roster-scope` capture straight out of the lake — never `roster-scope`
over HTTP — so a `roster-scope` outage costs this collector's coverage
freshness, not `/signals`'s availability. `fetch_watchlist` raises
`ScopeUnavailable` rather than returning an empty set, and this coroutine
never falls back to an unnarrowed capture: no scope means zero calls to the
box-score feed and a `present: 0` envelope for every signal type.

**`coverage.expected` never derives from what succeeded.** `EXPECTED_FLOOR`
below is computed from `roster-scope`'s *config*, before any upstream is
contacted, and `CoverageAccumulator` takes it as a floor that never lowers a
genuine count. `acc.expect(key)` is called on the fact that made a key owed —
the watchlist naming it, or the feed recording a snap for it — never on the
mapping happening to succeed.

**An ambiguous row emits nothing and is counted as missing with a reason.** A
row whose `team` does not appear in its own `game_id` is the spec's named hard
case (a player traded the same week they played); this collector refuses to
guess which side of the trade it belongs to and records the ambiguity rather
than writing a possibly-wrong team into an append-only lake.

**A failed capture still writes an envelope.** `collector_core.failure.
fail_capture` writes one `present: 0` envelope per signal type with a populated
`errors` array, then re-raises. Both halves matter: the write makes a gap in
the lake explicit rather than something a reader infers from absence, and the
re-raise stops `CaptureState` installing an empty capture over the last good
one.

**The previous snapshot is read first, and a lake failure is fatal to the
pass.** `revision` is derived from it (see `revisions.py`), so continuing
without it would mint revision 0 over rows that already reached revision 3 and
break the monotonicity every consumer pins against. That read is also the
first `await` in this coroutine, which is what lets uvicorn finish starting
before any object-store latency is incurred — `roster-scope`'s first deploy
failed its readiness probe for exactly the lack of it.
"""

import asyncio
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture
from collector_core.scope import ScopeUnavailable

from .adapters.identity import UnresolvedPlayer, build_id_resolver
from .adapters.scope import fetch_watchlist
from .adapters.upstream import UPSTREAM_ADAPTER, BoxRow, fetch_box_rows, source_ref
from .metrics import metrics
from .revisions import final_restated, load_previous_state, next_revision
from .scoring import SCORING_RULES_VERSION, build_fantasy_points, build_rates

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "capture_player_stats",
]

COLLECTOR_NAME = "player-stats"
CADENCE_CLASS = CadenceClass.WEEKLY
SIGNAL_TYPES = ("player_box_weekly",)
BOX_SIGNAL = SIGNAL_TYPES[0]

# ── the coverage floor, and where the number comes from ──────────────────────
#
# The spec: "One row per `roster-scope` watchlist player whose team has
# completed its game for the scoped week, plus any non-watchlist player who
# recorded at least one offensive snap in those games."
#
# The floor encodes the first half, taken from `roster-scope`'s *config* rather
# than from anything fetched. Its rules are `QB<=2, RB<=3, WR<=4, TE<=2` per
# team, plus every kicker and all 32 team defenses — a 416-slot universe. A
# team defense has no box score and can never be owed a row here, so 32 of
# those slots drop out:
#
#     32 teams x (2 QB + 3 RB + 4 WR + 2 TE + 1 K) = 32 x 12 = 384
#
# It is a floor, never a cap: `CoverageAccumulator` reports `max(observed,
# floor)`, so the several hundred non-watchlist players who took a snap raise
# `expected` honestly rather than being hidden under it.
#
# What it defends against is the silent one. A truncated upstream carrying 40
# rows would otherwise report `expected: 40, present: 40`, ratio 1.0 —
# perfectly healthy, with 90% of the league's production missing. With the
# floor it reports 40/384 and `collector_coverage_ratio` alerts.
TEAM_COUNT = 32
WATCHLIST_SLOTS_PER_TEAM = 12
EXPECTED_FLOOR: dict[str, int] = {BOX_SIGNAL: TEAM_COUNT * WATCHLIST_SLOTS_PER_TEAM}


def _wall_clock() -> datetime:
    """Real elapsed time, for deadline enforcement only.

    Distinct from `capture_player_stats`'s `now`, which is the single instant
    the whole pass *describes* and is deliberately frozen for its duration. A
    deadline has to be checked against a clock that actually advances.
    """
    return datetime.now(tz=UTC)


def _team_matches_game(row: BoxRow) -> bool:
    """Whether the row's team is one of the two the game id names.

    The spec's hard case, stated as a predicate: "deciding a player's `team`
    for a game played the same week they changed teams, because most upstreams
    key the row to the current roster rather than the roster at kickoff". The
    feed's `game_id` (`2024_01_NYJ_SF`) carries both participants, so a `team`
    that is in neither is an upstream that has back-filled the player's
    *current* club onto a game they played for someone else.
    """
    if not row.team or not row.game_id:
        return False
    return row.team in row.game_id.split("_")


def build_signal(row: BoxRow, *, player_id: str, now: datetime) -> dict:
    """One `BoxRow` -> one `player_box_weekly` signal row.

    Mirrored field for field in `contracts/signal-envelope/collectors/
    player-stats.json`; `tests/test_capture_contract_conformance.py` validates
    the **real** output of this function against that schema, so a renamed
    field fails there rather than in the generator six weeks later.

    `revision` is not set here — `capture_player_stats` derives it against the
    previous lake snapshot once the rest of the row exists, because the
    fingerprint that decides it is computed from these very blocks.
    """
    return {
        "player_id": player_id,
        "game_id": row.game_id,
        "team": row.team,
        "opponent": row.opponent,
        "position": row.position,
        # The upstream recorded this player in this game, which is the only
        # participation claim the feed supports. It cannot see a dressed player
        # who never took a snap, so this collector never emits `played: false`
        # — a watchlist player with no row is a `coverage.missing` entry, which
        # is the spec's "distinct from a missing row" read the other way round.
        "played": True,
        # Not available from a box-score feed. Snap counts live in the
        # participation feed, which is `usage-share`'s upstream. `None` rather
        # than `0`, because a fabricated zero reads as "dressed but never
        # played" — a claim this feed cannot support.
        "offense_snaps": None,
        "passing": dict(row.passing),
        "rushing": dict(row.rushing),
        "receiving": dict(row.receiving),
        "misc": dict(row.misc),
        "rates": build_rates(row.passing, row.rushing, row.receiving),
        "fantasy_points": build_fantasy_points(
            row.passing, row.rushing, row.receiving, row.misc
        ),
        "scoring_rules_version": SCORING_RULES_VERSION,
        # The upstream publishes no certification level, and the spec forbids
        # inferring one from elapsed time. `None` is the honest answer until a
        # feed that carries finality is wired up — see `adapters/upstream.py`.
        "stat_state": None,
    }


async def capture_player_stats(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into the `player_box_weekly` envelope."""
    scope = {"season": season, "week": week}
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER, fetched_at=now, source_ref=source_ref(season, week)
    )

    def fail(exc: BaseException, reason: str | None = None):
        return fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            reason=reason,
            expected=EXPECTED_FLOOR,
            source_ref=source_ref(season, week),
        )

    # `to_thread` rather than a direct call, and deliberately the very first
    # thing this coroutine does: it is both the offload AND the `await` that
    # lets uvicorn finish starting before any lake latency is incurred.
    try:
        previous = await asyncio.to_thread(
            load_previous_state, lake, COLLECTOR_NAME, BOX_SIGNAL, season, week
        )
    except Exception as exc:  # noqa: BLE001 — written, classified, re-raised
        # Never returns. Continuing would mint revision 0 over rows already at
        # a higher revision, which breaks the monotonicity guarantee.
        await fail(exc, reason="previous_snapshot_unavailable")

    metrics.capture_attempt()

    # BEFORE the upstream fetch, deliberately, and this ordering is the whole
    # of failing closed: a pass that cannot narrow costs zero upstream calls
    # rather than an ~8.3 MB season CSV fetched to publish nothing.
    try:
        watchlist = await fetch_watchlist(lake, season, week)
    except ScopeUnavailable as exc:
        # `exc.reason` rather than a literal: `scope_unavailable` and
        # `scope_empty` have two different fixes, and collapsing them costs
        # an operator the one thing the envelope could have told them. Never
        # returns — see `fail`.
        await fail(exc, reason=exc.reason)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # The scope read is I/O, and `ScopeUnavailable` is only what
        # `ScopeClient` raises when the lake answered and had nothing usable.
        # The lake can also fail outright: `S3LakeWriter.list_keys`/`read`
        # propagate botocore errors and `json` decode errors untouched, and
        # `ScopeClient._parse_captured_at` raises `ValueError` on a timestamp
        # it does not recognise. Without this arm every one of those escapes
        # this coroutine entirely — no `present: 0` envelope, no
        # `collector_capture_failures_total`, just a log line. No explicit
        # `reason=`: `fail_capture` falls back to
        # `CollectorMetrics.reason_for(exc)`, which classifies a decode or
        # timestamp failure as `malformed` and anything else as `unknown` —
        # both true, and neither mistakable for `scope_unavailable`.
        await fail(exc)

    try:
        rows, row_errors = await fetch_box_rows(season, week, client=client, now=now)
    except Exception as exc:  # noqa: BLE001 — written, classified, re-raised
        await fail(exc)

    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[BOX_SIGNAL])
    for error in row_errors:
        acc.add_error(error["reason"], error.get("detail", ""))
    # Owed because `roster-scope` names them, not because a row turned up.
    for player_id in watchlist:
        acc.expect(player_id)

    resolver = build_id_resolver(client)
    signals: list[dict] = []
    seen: set[str] = set()
    identity_misses = 0
    restatements = 0

    for index, row in enumerate(rows):
        if deadline is not None and _wall_clock() >= deadline:
            # Over budget. Recorded once with a count rather than once per
            # remaining row: time only moves forward, so there is nothing left
            # to attempt, and 400 identical entries would push every other
            # error past the cap.
            acc.add_error(
                "deadline_exceeded", f"{len(rows) - index} of {len(rows)} rows unread"
            )
            break

        try:
            player_id = await resolver.resolve(row.upstream_player_id)
        except UnresolvedPlayer as exc:
            # No canonical key exists, so this cannot be a `fail` against one.
            # It is still the failure the phase doc calls the most likely to go
            # unnoticed for a season, hence the dedicated metric.
            identity_misses += 1
            acc.add_error(exc.reason, row.upstream_player_id)
            continue

        if player_id in seen:
            acc.add_error("duplicate_row", f"{player_id} in {row.game_id}")
            continue
        seen.add(player_id)

        # Owed because the feed recorded this player in a completed game.
        acc.expect(player_id)

        if not _team_matches_game(row):
            acc.fail(player_id, "team_game_mismatch")
            continue

        try:
            signal = build_signal(row, player_id=player_id, now=now)
        except Exception as exc:  # noqa: BLE001 — one bad row is not a pass
            acc.fail(player_id, metrics.reason_for(exc))
            continue

        if final_restated(previous, signal):
            acc.add_error("final_row_restated", f"{row.game_id}|{player_id}")
        signal["revision"], restated = next_revision(previous, signal)
        restatements += int(restated)

        signals.append(signal)
        acc.record(player_id)

    owed_without_a_row = len(watchlist - seen)
    if owed_without_a_row:
        # Named individually in `coverage.missing` already; summarised here so
        # a bye week does not push every other error past the 50-entry cap.
        acc.add_error(
            "no_box_row",
            f"{owed_without_a_row} watchlist player(s) have no row for "
            f"season={season} week={week}",
        )

    metrics.restatements(restatements)
    metrics.identity_misses(identity_misses)

    envelope = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=COLLECTOR_NAME,
        signal_type=BOX_SIGNAL,
        captured_at=now,
        upstream=upstream,
        scope=scope,
        coverage=acc.result(),
        errors=acc.errors,
        signals=signals,
    )
    return await publish_capture({BOX_SIGNAL: envelope}, lake=lake, metrics=metrics)
