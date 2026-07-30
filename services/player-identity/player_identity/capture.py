"""Capture orchestration: Sleeper players -> canonical records -> envelope
-> lake, plus the standing miss queue emitted alongside it.

`/signals` and `/resolve` both serve from the cache this fills, never from
the upstream. An upstream outage therefore degrades freshness rather than
availability — which is only true because a failed capture leaves the cached
envelopes alone; see `_write_failure_and_raise` below.
"""

from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter, awrite

from .adapters.sleeper import (
    UpstreamSchemaError,
    fetch_players,
    record_schema_errors,
    validate_document,
)
from .identity import (
    ANCHOR_SOURCE,
    CROSSWALK_KEYS,
    KNOWN_POSITIONS,
    IdentityRecord,
    crosswalk_external_ids,
    mint_player_id,
    normalized_key,
    position_group,
    roster_status,
    split_suffix,
)
from .metrics import metrics
from .resolution import MissQueue, ResolutionIndex

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_ROSTER_FLOOR",
    "SIGNAL_TYPES",
    "capture_identities",
]

COLLECTOR_NAME = "player-identity"
CADENCE_CLASS = CadenceClass.SEASONAL
SIGNAL_TYPES = ("player_identity_crosswalk", "name_resolution_miss")
UPSTREAM_ADAPTER = "sleeper-players"

# `coverage.expected` means, per the Phase 8 spec: every player on any active
# roster, reserve list, or practice squad across all 32 teams for the season,
# plus every free agent transacted in the trailing 30 days. Roughly 2,900
# records.
#
# THIS FLOOR IS THE POINT. Deriving `expected` from the fetched document —
# `expected = len(whatever came back)` — reproduces the 8A coverage bug one
# level up. A *truncated* document carrying 100 records instead of 2,900
# would yield expected=100, present=100, ratio 1.0: "perfectly healthy",
# while 96% of the league silently vanished. The floor encodes what complete
# means independently of what the fetch happened to return, so:
#
#     total outage   -> 2900 / 0    -> ratio 0.00
#     truncation     -> 2900 / 100  -> ratio 0.03
#     healthy fetch  -> 2900 / 2900 -> ratio 1.00
#
# The mechanism — `CoverageAccumulator(floor=...)`, the error cap, and the
# failure-envelope path — was prototyped here during 8A and marked as
# belonging in the shared library. Wave 0 moved all three into
# `collector_core`, so this constant is now the only part that is genuinely
# player-identity's: what the number *is*.
#
# Records that fail structural validation stay counted in `expected`. We
# cannot know whether they qualified, and assuming they did not is
# derivation-from-success again, just with an extra step.
EXPECTED_ROSTER_FLOOR = 2900


def _wall_clock() -> datetime:
    """Real elapsed time, for deadline enforcement only.

    Distinct from `capture_identities`'s `now`, which is the single instant
    the whole pass *describes* and is deliberately frozen for its duration.
    """
    return datetime.now(tz=UTC)


def _qualifies(record: dict) -> tuple[bool, str]:
    """Does this upstream record fall inside `coverage.expected`?

    DOCUMENTED PROXY — read before trusting this. The spec's population is
    "every player on a roster/reserve list/practice squad, plus every free
    agent **transacted within the trailing 30 days**". The Sleeper players
    document carries no transaction dates at all, so the 30-day window is
    simply not observable from this adapter. Rather than fabricate recency,
    this uses `team is null AND status is Active` as the stand-in for a
    recently-transacted free agent: an unsigned player the upstream still
    marks Active is the closest observable thing to one who moved lately.

    It over-counts (an Active free agent who last moved in March qualifies)
    and under-counts (a player transacted last week whose status went
    Inactive does not). Both directions are stated rather than guessed at,
    per "refuse rather than guess". Swap this out the moment an adapter with
    transaction dates lands — the league transaction feed named in the
    Phase 8 candidate upstreams is exactly that.
    """
    position = (record.get("position") or "").upper()
    if position not in KNOWN_POSITIONS:
        return False, "unrostered_position"
    if record.get("team"):
        return True, ""
    if (record.get("status") or "").strip().lower() == "active":
        return True, ""
    return False, "not_on_a_roster"


def _entry_year(record: dict, season: int) -> int | None:
    """First season on an NFL roster, derived from `years_exp`.

    Separates a rookie from the retired veteran who shares their normalized
    key — which is exactly the collision Tier 3's `entry_year` weight is
    there for.
    """
    years = record.get("years_exp")
    if not isinstance(years, int) or years < 0:
        return None
    return season - years


def _aliases(record: dict, full_name: str, now: datetime) -> list[dict]:
    constructed = " ".join(
        part for part in (record.get("first_name"), record.get("last_name")) if part
    ).strip()
    if not constructed or constructed == full_name:
        return []
    return [
        {
            "name": constructed,
            "source": ANCHOR_SOURCE,
            "valid_from": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valid_to": None,
        }
    ]


def _to_record(
    upstream_key: str, record: dict, *, season: int, now: datetime
) -> IdentityRecord:
    full_name = (record.get("full_name") or "").strip()
    if not full_name:
        raise ValueError("record carries no full_name")

    base, suffix = split_suffix(full_name)
    position = (record.get("position") or "").upper()
    group = position_group(position)
    if group is None:
        raise ValueError(f"unmapped position {position!r}")

    jersey = record.get("number")
    if not isinstance(jersey, int):
        jersey = None

    team = record.get("team") or None

    external_ids = crosswalk_external_ids(record, now)
    # The anchor source is recorded as an ordinary `external_ids` entry too,
    # so the injectivity invariant covers it: the id this collector minted
    # from must itself map to exactly one player_id.
    external_ids[ANCHOR_SOURCE] = {
        "id": str(record.get("player_id") or upstream_key),
        "linked_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "link_method": "exact_id",
        "match_score": None,
        "match_margin": None,
    }

    return IdentityRecord(
        player_id=mint_player_id(
            ANCHOR_SOURCE, str(record.get("player_id") or upstream_key)
        ),
        full_name=full_name,
        first_name=(record.get("first_name") or base.split(" ")[0]).strip(),
        last_name=(record.get("last_name") or base.split(" ")[-1]).strip(),
        name_suffix=suffix,
        normalized_key=normalized_key(full_name),
        position=position,
        position_group=group,
        jersey_number=jersey,
        jersey_as_of=now,
        team=team,
        team_as_of=now,
        roster_status=roster_status(record.get("status"), team),
        birth_date=record.get("birth_date") or None,
        entry_year=_entry_year(record, season),
        external_ids=external_ids,
        last_verified_at=now,
        aliases=_aliases(record, full_name, now),
    )


def _envelope(
    signal_type: str,
    *,
    season: int,
    week: int,
    now: datetime,
    source_ref: str | None,
    coverage: Coverage,
    errors: list[dict],
    signals: list[dict],
) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=COLLECTOR_NAME,
        signal_type=signal_type,
        captured_at=now,
        upstream=Upstream(
            adapter=UPSTREAM_ADAPTER, fetched_at=now, source_ref=source_ref
        ),
        scope={"season": season, "week": week},
        coverage=coverage,
        errors=errors,
        signals=signals,
    )


async def capture_identities(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
    misses: MissQueue,
    index: ResolutionIndex,
    roster_floor: int = EXPECTED_ROSTER_FLOOR,
) -> dict[str, Envelope]:
    """Rebuild the canonical crosswalk and emit the standing miss queue.

    `misses` and `index` are bound by `main.py` with `functools.partial`:
    the library fixes this function's positional signature, and both objects
    are shared with the resolve routes, so neither can be a module-level
    global nor an extra positional argument.
    """
    metrics.capture_attempt()
    try:
        payload, source_ref = await fetch_players(client)
        validate_document(payload, CROSSWALK_KEYS)
    except Exception as exc:  # noqa: BLE001 — total-outage path, classified here
        metrics.capture_failure(exc)
        reason = (
            "schema"
            if isinstance(exc, UpstreamSchemaError)
            else metrics.reason_for(exc)
        )
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope={"season": season, "week": week},
            lake=lake,
            metrics=metrics,
            reason=reason,
            # Only the crosswalk has a roster floor. The miss queue's
            # population *is* whatever is in the queue, so it has no a-priori
            # size — but it still floors to 1 inside `fail_capture`, because
            # `expected: 0` would make a total outage report ratio 1.0.
            expected={"player_identity_crosswalk": roster_floor},
        )

    crosswalk = CoverageAccumulator(floor=roster_floor)
    rows: list[dict] = []

    items = list(payload.items())
    for position, (upstream_key, record) in enumerate(items):
        if deadline is not None and _wall_clock() >= deadline:
            for remaining_key, _ in items[position:]:
                crosswalk.fail(remaining_key, "deadline_exceeded")
            break

        schema_errors = record_schema_errors(record)
        if schema_errors:
            # Structurally invalid: it stays inside `expected` because we
            # cannot know whether it qualified. This is the check that
            # catches `number` -> `jersey`, which would otherwise write
            # jersey_number: null for every player and look like data.
            crosswalk.fail(upstream_key, "schema")
            continue

        qualified, why = _qualifies(record)
        if not qualified:
            # Genuinely outside the declared population — not a miss, not an
            # error, simply not expected. It is not added to `expected` at
            # all, which is why the floor below is what keeps the ratio
            # honest.
            continue

        # Declared expected on the fact that made it *qualify*, before the
        # mapping is attempted -- never on the mapping succeeding.
        # `CoverageAccumulator.record` refuses an undeclared key precisely so
        # `expected` cannot grow because something worked.
        crosswalk.expect(upstream_key)
        try:
            rows.append(
                _to_record(upstream_key, record, season=season, now=now).to_signal()
            )
        except ValueError as exc:
            metrics.capture_failure(exc)
            crosswalk.fail(upstream_key, "malformed")
            continue
        crosswalk.record(upstream_key)

    for conflict in index.replace(rows):
        metrics.merge_conflict(conflict["source"])
        # Into the accumulator rather than a side list, so the error cap is
        # applied in exactly one place.
        crosswalk.add_error(
            "merge_conflict",
            f"{conflict['source']}:{conflict['external_id']} claimed by "
            f"{', '.join(conflict['player_ids'])}",
        )

    miss_rows = misses.rows()
    miss_coverage = CoverageAccumulator()
    for row in miss_rows:
        # `expect` then `record`, in two calls, because this is the one place
        # where deriving the expectation from what is present is correct: the
        # miss queue's population is local state, not an upstream fetch that
        # could have come back truncated. Spelled out rather than hidden in a
        # convenience method so it stays visibly the exception.
        key = f"{row['source']}:{row['raw_name']}"
        miss_coverage.expect(key)
        miss_coverage.record(key)

    envelopes = {
        "player_identity_crosswalk": _envelope(
            "player_identity_crosswalk",
            season=season,
            week=week,
            now=now,
            source_ref=source_ref,
            coverage=crosswalk.result(),
            errors=crosswalk.errors,
            signals=rows,
        ),
        "name_resolution_miss": _envelope(
            "name_resolution_miss",
            season=season,
            week=week,
            now=now,
            source_ref=source_ref,
            coverage=miss_coverage.result(),
            errors=miss_coverage.errors,
            signals=miss_rows,
        ),
    }

    for signal_type, envelope in envelopes.items():
        try:
            # `awrite`, not `lake.write`: boto3 is synchronous and blocking it
            # on the event loop gates readiness on object-store latency.
            await awrite(lake, envelope)
        except Exception as exc:  # noqa: BLE001 — total-outage path (lake down)
            metrics.capture_failure(exc)
            raise
        metrics.coverage(signal_type, envelope.coverage.ratio)

    return envelopes
