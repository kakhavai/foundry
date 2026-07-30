"""Capture orchestration: Sleeper players -> canonical records -> envelope
-> lake, plus the standing miss queue emitted alongside it.

`/signals` and `/resolve` both serve from the cache this fills, never from
the upstream. An upstream outage therefore degrades freshness rather than
availability — which is only true because a failed capture leaves the cached
envelopes alone; see `_write_failure_and_raise` below.
"""

from datetime import UTC, datetime
from typing import NoReturn

import httpx
from collector_core.cadence import CadenceClass
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import LakeWriter

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
# `expected` is therefore `max(qualifying upstream keys, EXPECTED_ROSTER_FLOOR)`
# — the floor never *lowers* the count, so a genuine roster expansion past
# 2,900 still reports honestly.
#
# Records that fail structural validation stay counted in `expected`. We
# cannot know whether they qualified, and assuming they did not is
# derivation-from-success again, just with an extra step.
EXPECTED_ROSTER_FLOOR = 2900

# At weather's ~16 games an uncapped `errors` list is fine. At 2,900 records
# a total schema break produces 2,900 near-identical entries in every
# envelope, in memory and in every lake object. Capped with an explicit
# truncation marker so the count is never silently lost.
MAX_ERRORS = 50


def _wall_clock() -> datetime:
    """Real elapsed time, for deadline enforcement only.

    Distinct from `capture_identities`'s `now`, which is the single instant
    the whole pass *describes* and is deliberately frozen for its duration.
    """
    return datetime.now(tz=UTC)


def _capped(errors: list[dict]) -> list[dict]:
    if len(errors) <= MAX_ERRORS:
        return errors
    omitted = len(errors) - MAX_ERRORS
    return [
        *errors[:MAX_ERRORS],
        {"reason": "errors_truncated", "detail": f"{omitted} further error(s) omitted"},
    ]


class FlooredCoverage:
    """`CoverageAccumulator` with a floor on `expected`.

    A local prototype of what belongs in `collector-core`:
    `CoverageAccumulator` derives `expected` from `len(expected_keys)` and so
    structurally cannot express "expected is at least N regardless of what
    came back". Deliberately not added to the shared library in this PR —
    three collectors are landing in parallel and the library should change
    once, on purpose.

    Note what `missing` can and cannot say: it names the keys this pass knows
    about, so when the floor exceeds the observed key count, `missing` is
    short while `expected` is not. That gap is real information, so it is
    also recorded as an explicit error rather than left for a reader to
    infer from arithmetic.
    """

    def __init__(self, floor: int = 0) -> None:
        self._floor = floor
        self._expected: set[str] = set()
        self._present: set[str] = set()
        self._errors: list[dict] = []

    def expect(self, key: str) -> None:
        self._expected.add(key)

    def record(self, key: str) -> None:
        self._expected.add(key)
        self._present.add(key)

    def fail(self, key: str, reason: str) -> None:
        self._expected.add(key)
        self._errors.append({"reason": reason, "detail": key})

    @property
    def observed(self) -> int:
        return len(self._expected)

    @property
    def errors(self) -> list[dict]:
        """Uncapped. `_capped` is applied once, at envelope construction —
        capping here as well would truncate an already-truncated list and
        report the wrong omitted count."""
        if self._floor > len(self._expected):
            # First, not last, so it survives `_capped`: a shortfall against
            # the season floor is the single most important thing in this
            # list and must not be the entry that gets dropped.
            return [
                {
                    "reason": "below_expected_roster_floor",
                    "detail": (
                        f"upstream described {len(self._expected)} record(s); "
                        f"the season floor is {self._floor}"
                    ),
                },
                *self._errors,
            ]
        return list(self._errors)

    def result(self) -> Coverage:
        return Coverage(
            expected=max(len(self._expected), self._floor),
            present=len(self._present),
            missing=sorted(self._expected - self._present),
        )


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


def _write_failure_and_raise(
    exc: BaseException,
    *,
    season: int,
    week: int,
    now: datetime,
    lake: LakeWriter,
    reason: str,
    roster_floor: int,
) -> NoReturn:
    """Write a failure envelope for every signal type, then re-raise.

    Two halves, both required.

    *Write*, because the Phase 8 contract says a failed capture still writes
    an envelope: `present: 0` with populated `errors` is a positive record
    that the collector tried and could not, which is not the same thing as
    an absent object and must not look like one in an append-only lake.

    *Re-raise*, because `run_capture_loop` catches and leaves `state`
    untouched. Returning the failure envelopes normally instead would hand
    them to `CaptureState.apply_capture`, which would install them over the
    last good capture — turning an upstream outage into a loss of
    *availability* on `/signals` and `/resolve`, when the whole point of
    capturing to a cache is that an outage costs only freshness.

    (weather's `capture_week` re-raises without writing, so it satisfies the
    second half and not the first. Left alone deliberately — it is somebody
    else's PR, noted as a follow-up.)
    """
    detail = f"{type(exc).__name__}: {exc}"
    empty = Coverage(expected=roster_floor, present=0, missing=[])
    errors = [{"reason": reason, "detail": detail[:500]}]
    for signal_type in SIGNAL_TYPES:
        envelope = _envelope(
            signal_type,
            season=season,
            week=week,
            now=now,
            source_ref=None,
            # The miss queue has no roster floor — its population *is*
            # whatever is in the queue — so a failure reports 0/0 there.
            coverage=empty
            if signal_type == "player_identity_crosswalk"
            else Coverage(expected=0, present=0, missing=[]),
            errors=errors,
            signals=[],
        )
        try:
            lake.write(envelope)
        except Exception:  # noqa: BLE001 — the original failure is what matters
            pass
        metrics.coverage(signal_type, envelope.coverage.ratio)
    raise exc


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
        _write_failure_and_raise(
            exc,
            season=season,
            week=week,
            now=now,
            lake=lake,
            reason=reason,
            roster_floor=roster_floor,
        )

    crosswalk = FlooredCoverage(floor=roster_floor)
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

        try:
            rows.append(
                _to_record(upstream_key, record, season=season, now=now).to_signal()
            )
        except ValueError as exc:
            metrics.capture_failure(exc)
            crosswalk.fail(upstream_key, "malformed")
            continue
        crosswalk.record(upstream_key)

    conflicts = index.replace(rows)
    crosswalk_errors = list(crosswalk.errors)
    for conflict in conflicts:
        metrics.merge_conflict(conflict["source"])
        crosswalk_errors.append(
            {
                "reason": "merge_conflict",
                "detail": (
                    f"{conflict['source']}:{conflict['external_id']} claimed by "
                    f"{', '.join(conflict['player_ids'])}"
                ),
            }
        )

    miss_rows = misses.rows()
    miss_coverage = FlooredCoverage()
    for row in miss_rows:
        miss_coverage.record(f"{row['source']}:{row['raw_name']}")

    envelopes = {
        "player_identity_crosswalk": _envelope(
            "player_identity_crosswalk",
            season=season,
            week=week,
            now=now,
            source_ref=source_ref,
            coverage=crosswalk.result(),
            errors=_capped(crosswalk_errors),
            signals=rows,
        ),
        "name_resolution_miss": _envelope(
            "name_resolution_miss",
            season=season,
            week=week,
            now=now,
            source_ref=source_ref,
            coverage=miss_coverage.result(),
            errors=_capped(miss_coverage.errors),
            signals=miss_rows,
        ),
    }

    for signal_type, envelope in envelopes.items():
        try:
            lake.write(envelope)
        except Exception as exc:  # noqa: BLE001 — total-outage path (lake down)
            metrics.capture_failure(exc)
            raise
        metrics.coverage(signal_type, envelope.coverage.ratio)

    return envelopes
