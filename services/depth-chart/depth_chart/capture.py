"""depth-chart's capture pass: fetch -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**. A collector that reaches
its upstream inside a request handler has inverted that contract.

Two things here are correctness, not style, and both have a fleet-wide history:

**`coverage.expected` never derives from what succeeded.** Every one of the 160
`(team, position)` groups is declared expected up front, out of `universe.py`,
*before* the feed is contacted — so a truncated document carrying three teams
reports `expected: 160, present: 15`, not `expected: 15, present: 15`. Coverage
counts position groups rather than players, per the spec: a team publishing a
chart with a position group omitted registers as missing, and a chart listing
six receivers instead of four does not register as *more* coverage than one
listing four.

**A failed capture still writes an envelope.** `collector_core.failure.
fail_capture` writes one `present: 0` envelope per signal type with a populated
`errors` array, then re-raises. Both halves matter: the write is what makes a
gap in the append-only lake *explicit* rather than something a reader has to
infer from absence, and the re-raise is what stops `CaptureState` installing an
empty capture over the last good one.

Every lake call goes off the event loop via `awrite` — `LakeWriter` is
synchronous boto3, and the lake handed to this function raises if it is called
from the loop thread.
"""

from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.conditional import UpstreamUnchanged
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from .adapters.upstream import (
    UPSTREAM_ADAPTER,
    PositionHistory,
    fetch_depth_charts,
    source_ref,
)
from .metrics import metrics
from .stability import (
    STABILITY_WINDOW_WEEKS,
    chart_hash,
    comparisons,
    freshness,
    rank_changes,
    stability_score,
    weeks_at_rank,
)
from .universe import expected_groups

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "CHART_SIGNAL",
    "STABILITY_SIGNAL",
    "capture_depth_chart",
]

COLLECTOR_NAME = "depth-chart"
CADENCE_CLASS = CadenceClass.VOLATILE

CHART_SIGNAL = "team_depth_chart"
STABILITY_SIGNAL = "depth_chart_stability"
SIGNAL_TYPES = (CHART_SIGNAL, STABILITY_SIGNAL)

# The size the universe is KNOWN to have, declared from config in `universe.py`
# and never computed from a fetch:
#
#     32 teams x 5 configured positions (QB, RB, WR, TE, K) = 160 groups
#
# The same number for both signal types, because both are emitted at the
# `(team, position)` grain — one ordering and one stability assessment per
# group. Read `universe.expected_groups` for the derivation; changing the
# position set there changes this automatically, which is why it is computed
# from the config tuple rather than written as the literal 160.
EXPECTED_FLOOR: dict[str, int] = {
    CHART_SIGNAL: len(expected_groups()),
    STABILITY_SIGNAL: len(expected_groups()),
}

# Recorded in `coverage.missing` / `errors` when a group could not be captured.
REASON_TRUNCATED = "capture_truncated"
REASON_DEADLINE = "deadline_exceeded"


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def role_label(position: str, rank: int) -> str | None:
    """The spec's normalized role, but only where the chart determines one.

    `qb1`/`qb2` are the only labels this upstream can support honestly. Every
    other value in the spec's enum — `wr_x`, `wr_slot`, `early_down_back`,
    `passing_down_back`, `te_inline` — is an *alignment or usage* fact, and this
    feed carries neither. It does distinguish three receiver lanes numerically
    (`pos_slot` 1, 2 and 8) but never names them, so mapping lane 1 to `wr_x`
    would be a coin flip dressed as a normalization.

    `None` is therefore returned rather than guessed. The label becomes
    derivable once `usage-share` publishes alignment rates; until then an
    absent label is a visible gap and a wrong one would not be.
    """
    if position == "QB" and rank in (1, 2):
        return f"qb{rank}"
    return None


def build_chart_signal(
    history: PositionHistory, entry, *, group_hash: str, published_at: datetime
) -> dict:
    """One charted player -> one `team_depth_chart` row.

    Several spec fields are emitted as `null` rather than inferred, and each
    absence is a deliberate refusal to invent:

    - **`player_id`** — the canonical `fdy-` id is minted by `player-identity`
      anchored on *its own* upstream's record key, so there is no offline
      derivation from anything in this feed. Resolving it needs an HTTP client
      for `player-identity`, which every collector in the fleet will need and
      which therefore belongs in `collector-core` rather than as a third
      per-service copy. `gsis_id` is carried instead: it is a *published*
      crosswalk id that `player-identity` adopts as a Tier-1 link without
      scoring, so the row joins today via `external_ids.gsis` and gains a
      `player_id` the day the shared client lands.
    - **`functional_rank`** and **`disagreement`** — the spec assigns these to
      an in-repo computation over `usage-share`, which is not built. `null`
      until it is, with `rank_source: official` stating what was actually used.
    - **`is_starter`** — "opened the most recent game at the position" is a
      participation fact, not a charting one. `official_rank == 1` is the
      chart's *claim*, which is a different assertion and is already carried by
      `official_rank` itself.
    - **`roster_status`** — reconciled against `roster-transactions`, which is
      not registered yet. This feed carries no status column at all.
    """
    return {
        "team": history.team,
        "position": history.position,
        "player_id": None,
        "gsis_id": entry.gsis_id,
        "player_name": entry.player_name,
        "official_rank": entry.rank,
        "functional_rank": None,
        "rank_source": "official",
        "disagreement": None,
        "role_label": role_label(history.position, entry.rank),
        "is_starter": None,
        "roster_status": None,
        "weeks_at_rank": weeks_at_rank(history, entry.gsis_id, entry.player_name),
        "chart_published_at": _rfc3339(published_at),
        "chart_hash": group_hash,
    }


def build_stability_signal(
    history: PositionHistory, *, now: datetime, group_hash: str
) -> dict:
    """One `(team, position)` group -> one `depth_chart_stability` row.

    Split from the ordering by *grain*: `rank_changes_4w` and `stability_score`
    are properties of the group, not of a player, and repeating them on every
    charted player would make a per-position fact look like a per-player one.
    `weeks_at_rank` stays on the ordering row for the mirror-image reason.

    `chart_age_days` and `is_stale` are the independent axis the spec's failure
    mode needs: a frozen preseason chart drives `stability_score` to 1.0 while
    nothing in the ordering looks wrong, so freshness has to be carried
    alongside rather than folded into the score.
    """
    fresh = freshness(history, now)
    return {
        "team": history.team,
        "position": history.position,
        "chart_published_at": _rfc3339(fresh.chart_published_at),
        "chart_hash": group_hash,
        "charted_players": len(history.current.entries),
        "weeks_observed": len(history.weeks),
        "window_weeks": STABILITY_WINDOW_WEEKS,
        "comparisons": comparisons(history),
        "rank_changes_4w": rank_changes(history),
        "stability_score": stability_score(history),
        "chart_age_days": fresh.age_days,
        "is_stale": fresh.is_stale,
    }


async def capture_depth_chart(
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
        source_ref=source_ref(season, week),
    )

    metrics.capture_attempt()
    try:
        fetched = await fetch_depth_charts(
            season, week, client=client, now=now, deadline=deadline
        )
    except UpstreamUnchanged:
        # NOT a failure. `fail_capture` below would write a `present: 0`
        # envelope over a healthy capture and count a failure that did not
        # happen. `run_capture_loop` catches this and marks the pass
        # unchanged. Must stay ABOVE the generic handler.
        raise
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Writes a `present: 0` envelope per signal type, then re-raises `exc`.
        # Never returns — do not add code after this call.
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
            source_ref=source_ref(season, week),
        )

    # Hashed once per group and shared by both signal types, so the ordering row
    # and its stability row can never disagree about which chart they describe.
    hashes = {
        key: chart_hash(history.current) for key, history in fetched.histories.items()
    }

    chart_rows: list[dict] = []
    stability_rows: list[dict] = []
    stale = 0
    accumulators = {
        signal_type: CoverageAccumulator(
            expected_groups(), floor=EXPECTED_FLOOR[signal_type]
        )
        for signal_type in SIGNAL_TYPES
    }

    for key in sorted(fetched.histories):
        history = fetched.histories[key]
        group_hash = hashes[key]
        current = history.current

        # `current.entries` is never empty by construction — the adapter only
        # creates a group when it has a row to put in it (see
        # `_GroupAccumulator.result`). No guard here, deliberately: a branch no
        # test can reach reads as a handled case while being dead code.
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing away
            # what already resolved: a truncated pass that reports itself
            # truncated is useful; one that reports itself complete is not.
            for accumulator in accumulators.values():
                accumulator.fail(key, REASON_DEADLINE)
            continue

        for entry in current.entries:
            chart_rows.append(
                build_chart_signal(
                    history,
                    entry,
                    group_hash=group_hash,
                    published_at=current.captured_at,
                )
            )
        stability_row = build_stability_signal(history, now=now, group_hash=group_hash)
        stability_rows.append(stability_row)
        if stability_row["is_stale"]:
            stale += 1

        for accumulator in accumulators.values():
            accumulator.record(key)

    if fetched.truncated:
        # Pass-level, so it is routed through the accumulator and shares the
        # one place the 50-entry error cap is applied.
        for accumulator in accumulators.values():
            accumulator.add_error(
                REASON_TRUNCATED,
                "the capture deadline elapsed while streaming the feed",
            )

    # Recorded on every pass, including zero: an absent Prometheus series and a
    # healthy one are indistinguishable in PromQL, so a gauge written only when
    # it is interesting cannot be alerted on.
    metrics.charted_players(len(chart_rows))
    metrics.stale_charts(stale)

    rows = {CHART_SIGNAL: chart_rows, STABILITY_SIGNAL: stability_rows}
    envelopes = {
        signal_type: Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=signal_type,
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=accumulators[signal_type].result(),
            errors=accumulators[signal_type].errors,
            signals=rows[signal_type],
        )
        for signal_type in SIGNAL_TYPES
    }

    return await publish_capture(envelopes, lake=lake, metrics=metrics)
