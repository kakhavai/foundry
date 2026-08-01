"""The capture pass: scope -> identity -> one upstream -> envelope -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

Six things here are correctness rather than style.

--------------------------------------------------------------------------
1. Narrowing happens BEFORE the upstream request
--------------------------------------------------------------------------

`fetch_scope_or_fail` resolves both narrowing seams — the membership list out of
the lake and the `player-identity` client the forward join runs through — before
`historical_contracts.csv.gz` is touched. That ordering *is* failing closed. A
pass that cannot narrow costs zero upstream calls rather than 1.13 MiB fetched
to publish nothing, and there is deliberately no unnarrowed fallback: one would
spend the whole budget precisely during the incident that took `roster-scope`
out.

--------------------------------------------------------------------------
2. `coverage.expected` is the SCOPE SLOT, never the upstream row
--------------------------------------------------------------------------

Coverage is keyed by scope member, expected up front for every individual player
the watchlist names, and failed for the ones no resolved contract row covered.
Keying it by upstream row instead would make an out-of-scope player read as a
permanent coverage regression and — far worse — would make a pass that resolved
four rows report `expected: 4, present: 4`, ratio 1.0.

`EXPECTED_FLOOR` encodes the size the universe is KNOWN to have, independently
of any fetch and independently of the scope's own size: `Coverage.ratio` returns
1.0 when `expected` is 0, so a truncated scope naming two players would
otherwise read as a perfect pass.

**`present` is the other half, and it is mutated in the same breath as the
floor.** A slot counts as present only when a resolved row produced a record
with a non-null `contract_end_season` — the phase doc's predicate. A row that
resolved but whose term the feed left blank is `fail`ed with
`contract_term_unknown`, not recorded.

--------------------------------------------------------------------------
3. The spec's coverage exclusion, and where it could not be implemented
--------------------------------------------------------------------------

The phase doc says practice-squad players and unsigned free agents are
"excluded from `expected` rather than reported missing". That cannot be done
from here: `ScopeClient` returns member ids and nothing else, and reaching past
it into the scope envelope's `membership_status` column would fork the fleet's
one narrowing seam — the thing `durability-history`, `player-profile` and
`usage-share` each explicitly refuse to do.

So a scope slot with no active contract row is expected and `fail`ed under its
own reason, `no_active_contract`, which a consumer can subtract. Note the
distinction is arithmetically invisible anyway: `EXPECTED_FLOOR` is 384 and the
scope is 384, so `Coverage.result` reports `expected: 384` whether the slot was
declared or not. The only difference is that this way the slot is *named* in
`coverage.missing` with a reason, instead of vanishing. Disclosed in the service
README.

--------------------------------------------------------------------------
4. Six fields are null because the SOURCE lacks them, not because the row does
--------------------------------------------------------------------------

`cap_hit_current_usd`, `guaranteed_remaining_usd`, `signing_bonus_proration_usd`,
`dead_money_if_cut_usd`, `tag_status` and `void_years_count` are cap-accounting
derivations the free feed does not carry. They are emitted **present and null**
with a machine-readable reason, never omitted and never fabricated — and never
derived from `apy`, which is average annual value and is not read at all
(`adapters/upstream.COLUMNS`).

`null_field_reasons` keeps that separate from the *other* kind of null: a row
whose `value`, `guaranteed` or `years` the upstream left blank. "This source
will never supply it" and "this row did not say" call for different consumer
behaviour, and an absent key cannot express either.

--------------------------------------------------------------------------
5. A `seasonal` cadence must not append an identical snapshot daily
--------------------------------------------------------------------------

Conditional GET already suppresses the *fetch* when the document has not moved.
The digest gate suppresses the *write* when the document moved but this
collector's own output did not — a contract elsewhere in the league changing
does not justify a new object for the same 384 players.

The digest is keyed by `(season, week, signal_type)` and recorded only for a
write that **landed** (`PublishResult.landed`). A digest recorded for content
the lake never received permanently suppresses the retry: the next pass digests
the same content, matches, raises `UpstreamUnchanged`, and the object is never
written again until the data itself changes — a whole season, on this cadence.

--------------------------------------------------------------------------
6. The library owns `collector_capture_failures_total` for a fatal pass
--------------------------------------------------------------------------

`fail_capture` and `publish_capture` both record it. This module never calls
`metrics.capture_failure` itself, because it has no degraded path that builds
its own envelopes — every failure here either ends the pass or is a coverage
entry.

--------------------------------------------------------------------------
The failure mode the phase doc names: a restructured deal
--------------------------------------------------------------------------

A mid-season restructure changes proration and dead money retroactively. The
lake is append-only, so an old snapshot stays correct-as-of its `captured_at`
and **must not be reconciled backward**. With the cap fields null this is latent
rather than active — but it becomes live the moment a paid feed supplies them,
so nothing here reads a prior envelope back and no reconcile-backward path is
built "for later". The only prior state this module keeps is
`_PUBLISHED_DIGESTS`, which is in-memory, forward-only, and never rewrites an
object.
"""

import hashlib
import json
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.conditional import UpstreamUnchanged
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture
from collector_core.scope import fetch_scope_or_fail

from . import derive
from .adapters.scope import (
    IDENTITY_UPSTREAM_ERROR,
    IdentityFailures,
    build_identity_client,
    fetch_scope,
    individual_players,
    resolve_in_scope,
)
from .adapters.upstream import (
    UPSTREAM_ADAPTER,
    ContractRow,
    canonical_team,
    fetch_active_contracts,
    source_ref,
)
from .metrics import metrics

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "CONTRACT_STATUS",
    "EXPECTED_FLOOR",
    "ROW_NULLABLE_FIELDS",
    "SIGNAL_TYPES",
    "UNSOURCED_FIELDS",
    "build_signal",
    "capture_player_contract",
    "player_key",
    "reset_published_digests",
]

COLLECTOR_NAME = "player-contract"
CADENCE_CLASS = CadenceClass.SEASONAL

CONTRACT_STATUS = "player_contract_status"
SIGNAL_TYPES = (CONTRACT_STATUS,)

# ── the declared universe ────────────────────────────────────────────────────
#
# `roster-scope`'s config is 32 teams x (QB 2 + RB 3 + WR 4 + TE 2 + K 1 + DST 1)
# = 416 slots. Twelve of those thirteen per-team slots are individual players;
# the thirteenth is a team defense, which cannot hold a contract and is excluded
# by `individual_players`. So:
#
#     32 * 12 = 384
#
# A fact about the league's config, decided before any fetch and independent of
# how many members the scope actually resolved. An expectation taken from the
# scope would make a truncated scope read as a perfect pass, and one taken from
# the document would make a truncated upstream read the same way.
LEAGUE_TEAMS = 32
INDIVIDUAL_SCOPE_SLOTS_PER_TEAM = 12
EXPECTED_FLOOR: dict[str, int] = dict.fromkeys(
    SIGNAL_TYPES, LEAGUE_TEAMS * INDIVIDUAL_SCOPE_SLOTS_PER_TEAM
)

# Coverage failure reasons. Named constants rather than literals, because each
# implies a different operator action and a test asserting on a typo'd string
# passes just as happily as one asserting on the real thing.
REASON_NO_ACTIVE_CONTRACT = "no_active_contract"
REASON_CONTRACT_TERM_UNKNOWN = "contract_term_unknown"
REASON_DEADLINE = "deadline_exceeded"
REASON_MALFORMED_ROWS = "upstream_rows_malformed"
REASON_DUPLICATE_ACTIVE = "duplicate_active_contract"
# The one that explains a whole pass: every published record describes a deal
# whose final season already passed. `collector_coverage_ratio` cannot see it —
# an expired contract is a PRESENT record — so it is filed as a priority error
# that survives the 50-entry cap.
REASON_CONTRACT_EXPIRED = "contract_end_season_precedes_capture_season"

# Why a field is null. Two values, and the distinction is the point: a consumer
# retrying against a paid feed cares about the first and can do nothing about
# the second.
UNSOURCED = "unsourced_by_upstream"
ABSENT_IN_ROW = "absent_in_upstream_row"

# The six cap-accounting fields the free feed does not carry, in the phase doc's
# order. Emitted present-and-null on every row, with `UNSOURCED` recorded for
# each in `null_field_reasons`.
UNSOURCED_FIELDS: tuple[str, ...] = (
    "cap_hit_current_usd",
    "guaranteed_remaining_usd",
    "signing_bonus_proration_usd",
    "dead_money_if_cut_usd",
    "tag_status",
    "void_years_count",
)

# The fields that can be null because a particular ROW was blank. Kept as a
# tuple next to the one above so the two classes stay visibly different things
# rather than one dict a later edit can quietly merge.
ROW_NULLABLE_FIELDS: tuple[str, ...] = (
    "team",
    "contract_start_season",
    "contract_end_season",
    "is_contract_year",
    "seasons_remaining",
    "total_value_usd",
    "guaranteed_total_usd",
)

# `(season, week, signal_type) -> the digest THIS process last published AND saw
# land in the lake`. In memory rather than read back from the lake: a restart
# that found a matching digest would raise `UpstreamUnchanged` against an EMPTY
# `CaptureState` and serve nothing until the data next changed.
_PUBLISHED_DIGESTS: dict[tuple[int, int, str], str] = {}


def reset_published_digests() -> None:
    """Forget what this process has published. For tests only."""
    _PUBLISHED_DIGESTS.clear()


def player_key(player_id: str) -> str:
    """The coverage key for one scope slot.

    Prefixed so it can never be mistaken for a bare id, and canonical rather
    than the upstream's display name: `coverage.missing` and
    `signals[].player_id` are the two halves of one answer to "what was owed and
    what arrived", and a consumer can only join them if they name players in the
    same namespace.
    """
    return f"player:{player_id}"


def _digest(payload: object) -> str:
    """A stable sha256 over anything JSON-serialisable."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_signal(row: ContractRow, *, player_id: str, season: int) -> dict:
    """One resolved contract row as a `player_contract_status` signal row.

    `player_id` is passed in rather than read off the row: the upstream's
    display name is the join's *input*, and letting this function reach for it
    is how a fallback to a non-canonical id gets reintroduced by accident. The
    row does not carry an `fdy-` id at all, which is the structural half of the
    same guarantee.

    `otc_player_id` is published deliberately, and it is **not** an identity. It
    is OverTheCap's own stable key, the only per-player identifier this feed
    has, and carrying it buys two things a name cannot: a mis-join is traceable
    from a lake object a season later, and `player-identity` can adopt `otc` as
    a crosswalk source later without re-reading the whole archive. It is named
    for its source so it can never be mistaken for `player_id`, and the schema
    pins `player_id` to `^fdy-` so an attempt to swap the two fails the
    conformance test rather than the generator.

    Money is emitted as the feed's **nominal** whole-dollar integers. Not
    re-scaled, and not the `inflated_*` variants — see `adapters/upstream.py`.
    """
    end_season = derive.contract_end_season(row.year_signed, row.years)

    signal: dict = {
        "player_id": player_id,
        "team": canonical_team(row.otc_team),
        "contract_start_season": row.year_signed,
        "contract_end_season": end_season,
        "is_contract_year": derive.is_contract_year(season, end_season),
        "seasons_remaining": derive.seasons_remaining(season, end_season),
        "total_value_usd": row.total_value_usd,
        "guaranteed_total_usd": row.guaranteed_total_usd,
        "otc_player_id": row.otc_id or None,
    }
    # Present and null, every row, always. `dict.fromkeys` rather than six
    # literal lines so a field added to `UNSOURCED_FIELDS` cannot be declared in
    # one place and forgotten in the other.
    signal.update(dict.fromkeys(UNSOURCED_FIELDS))

    reasons = dict.fromkeys(UNSOURCED_FIELDS, UNSOURCED)
    for name in ROW_NULLABLE_FIELDS:
        if signal[name] is None:
            reasons[name] = ABSENT_IN_ROW
    signal["null_field_reasons"] = reasons
    return signal


async def capture_player_contract(
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
    # Seeded before anything can fail and overwritten with the real numbers
    # below. An absent Prometheus series and a healthy one are
    # indistinguishable, so these gauges must not simply stop on a failure path.
    metrics.expired_contract_records(0)
    metrics.unresolved_scope_slots(0)

    failure_context = dict(
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

    async def _narrowing_seams():
        """Both seams narrowing needs, resolved together.

        `build_identity_client` is synchronous and raises `ScopeUnavailable`
        when `PLAYER_IDENTITY_URL` is empty, which is why `fetch_scope_or_fail`
        takes a callable rather than an awaitable.
        """
        return build_identity_client(client), await fetch_scope(lake, season, week)

    # BEFORE the upstream request. That ordering IS failing closed — see the
    # module docstring. `fetch_scope_or_fail` owns both refusal arms.
    identity, membership = await fetch_scope_or_fail(
        _narrowing_seams, **failure_context
    )
    owed = individual_players(membership)

    try:
        read = await fetch_active_contracts(season, week, client=client)
    except UpstreamUnchanged:
        # Forward cover only. Re-raised ABOVE the generic handler so a future
        # change there can never route an unchanged upstream into
        # `fail_capture`, which would write `present: 0` over a healthy capture.
        raise
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Writes a `present: 0` envelope per signal type, then re-raises. Never
        # returns — and do NOT call `metrics.capture_failure(exc)` first: the
        # library owns that counter for a failure that ends a pass.
        await fail_capture(exc, **failure_context)

    identity_failures = IdentityFailures()
    resolved = await resolve_in_scope(
        read.rows,
        season=season,
        scope_members=owed,
        identity=identity,
        failures=identity_failures,
    )

    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[CONTRACT_STATUS])
    # Every individual scope member is owed a contract record — declared up
    # front, because the watchlist naming them is what makes them owed. Never
    # because the fetch below happened to return them.
    for player_id in sorted(owed):
        acc.expect(player_key(player_id))

    signals: list[dict] = []
    covered: set[str] = set()
    expired = 0

    for row, player_id in resolved:
        key = player_key(player_id)
        if player_id in covered:
            # Two active rows resolving to one player. `_dedupe` collapses them
            # per `otc_id` upstream of here, so this only fires when two
            # DIFFERENT OverTheCap records name the same person — in which case
            # the first wins and the second is a duplicate, not a second deal.
            continue
        covered.add(player_id)

        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing away
            # what already resolved: a truncated pass that reports itself
            # truncated is useful; one that reports itself complete is not.
            acc.fail(key, REASON_DEADLINE)
            continue

        signal = build_signal(row, player_id=player_id, season=season)
        signals.append(signal)

        # The phase doc's predicate, and the OTHER half of the coverage mutation
        # surface: a record whose term the feed left blank does not answer "when
        # does this deal end", which is the question.
        if signal["contract_end_season"] is None:
            acc.fail(key, REASON_CONTRACT_TERM_UNKNOWN)
        else:
            acc.record(key)
            if signal["seasons_remaining"] < 0:
                expired += 1

    # A scope slot no resolved contract row covered. This is where narrowing
    # becomes visible: an unresolved, out-of-feed or practice-squad player is a
    # HOLE against the watchlist, not a row that quietly disappeared from both
    # numerator and denominator. See the module docstring, section 3.
    for player_id in sorted(owed - covered):
        acc.fail(player_key(player_id), REASON_NO_ACTIVE_CONTRACT)

    if identity_failures.rows:
        # One summarised entry, never one per row: a `player-identity` outage
        # against a 2,908-row feed would otherwise fill the 50-entry cap by
        # itself and push every other reason off the list.
        acc.add_error(IDENTITY_UPSTREAM_ERROR, identity_failures.detail())
    if read.malformed:
        acc.add_error(
            REASON_MALFORMED_ROWS,
            f"{read.malformed} active row(s) had no player name and were dropped",
        )
    if read.duplicate_active:
        acc.add_error(
            REASON_DUPLICATE_ACTIVE,
            f"{read.duplicate_active} extra active row(s) for a player already "
            "seen; the newest-signed row was kept",
        )
    if expired:
        # Priority, not ordinary: this is the entry that explains the whole pass
        # when the upstream document has gone stale, and it must not queue
        # behind a few hundred routine per-slot failures.
        acc.add_priority_error(
            REASON_CONTRACT_EXPIRED,
            f"{expired} of {len(signals)} published record(s) describe a deal "
            f"whose final season precedes {season}; the upstream document may "
            "predate the capture season",
        )

    metrics.expired_contract_records(expired)
    metrics.unresolved_scope_slots(len(owed - covered))

    envelope = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=COLLECTOR_NAME,
        signal_type=CONTRACT_STATUS,
        captured_at=now,
        upstream=upstream,
        scope=scope,
        coverage=acc.result(),
        errors=acc.errors,
        signals=signals,
    )
    envelopes = {CONTRACT_STATUS: envelope}

    # Keyed by `(season, week, signal_type)`. The week is not decoration: a
    # `POST /refresh {"week": 5}` backfill against an already-captured week 3
    # must not be suppressed by week 3's digest, and a gate keyed to a literal
    # week would do exactly that while every single-week test stayed green.
    digest = _digest(signals)
    if _PUBLISHED_DIGESTS.get((season, week, CONTRACT_STATUS)) == digest:
        # Recorded before raising: an unchanged pass is a HEALTHY one, and a
        # coverage gauge that went quiet whenever the data was stable would read
        # exactly like a collector that had stopped.
        metrics.coverage(CONTRACT_STATUS, envelope.coverage.ratio)
        raise UpstreamUnchanged(source_ref(season, week), source_ref=digest)

    # The shared tail: writes the envelope off the event loop, records its
    # coverage gauge, records `collector_capture_failures_total` if the write
    # fails — then returns the envelopes ANYWAY, because the capture succeeded
    # and only its archival copy did not.
    published = await publish_capture(envelopes, lake=lake, metrics=metrics)

    # Gated on the write LANDING, and iterating `published` rather than the
    # local dict so the two cannot disagree. See the module docstring,
    # section 5, and `collector_core.publish.PublishResult`.
    for signal_type in published:
        if published.landed(signal_type):
            _PUBLISHED_DIGESTS[(season, week, signal_type)] = digest

    return published
