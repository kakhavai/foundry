"""usage-share's capture pass: fetch -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**. A collector that reaches
its upstream inside a request handler has inverted that contract.

This collector answers what the offense *gave* a player, independent of what
they converted it into — and it is the only collector that carries the
team-level denominators, without which every share is uninterpretable. So every
share published here is computed from an explicit base that travels in the same
row, never taken from whatever the vendor already divided.

Two things here are correctness, not style, and both have a fleet-wide history:

**`coverage.expected` never derives from what succeeded.** A collector that
builds its expectation from the document it just fetched reports a truncated
upstream — 100 of 2,900 records — as `expected: 100, present: 100`, ratio 1.0.
Perfectly healthy, while 96% of the league silently vanished. `EXPECTED_FLOOR`
below encodes the size the universe is KNOWN to have, independently of the
fetch, and `CoverageAccumulator` takes it as a floor that never lowers a
genuine count. `acc.expect(key)` is called on the fact that made a key owed;
`acc.record(key)` only after it actually landed. Never the other way round.

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
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter, awrite

from .adapters.upstream import (
    UPSTREAM_ADAPTER,
    TeamDenominators,
    UsageRow,
    WeekUsage,
    fetch_week_usage,
    source_ref,
)
from .metrics import TEAM_SUM_TOLERANCE, metrics

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "capture_usage_share",
]

COLLECTOR_NAME = "usage-share"
CADENCE_CLASS = CadenceClass.WEEKLY
SIGNAL_TYPES = ("player_usage_weekly",)

# ── the declared universe ─────────────────────────────────────────────────────
#
# The spec: "One row per roster-scope watchlist player whose team has completed
# its game for the scoped week, and a complete `denominators` object for every
# team that played." Both halves are counted, and the floor below is derived
# from what SHOULD exist rather than from anything a fetch returned.
#
#   LEAGUE_TEAMS                     32, a fact about the league.
#   OFFENSIVE_SCOPE_SLOTS_PER_TEAM   11 = QB<=2 + RB<=3 + WR<=4 + TE<=2, which
#                                    is roster-scope's config quota. Its full
#                                    universe is 13 per team (416 slots), but
#                                    the extra two are one kicker and one team
#                                    defense, and neither records offensive
#                                    usage — so neither is OWED a row here.
#   + 1                              the team's own `denominators` object,
#                                    which the spec names as part of complete
#                                    coverage in its own right. A team whose
#                                    denominators never arrive is a hole in
#                                    every share for that team, and counting it
#                                    makes that hole visible even when zero of
#                                    its players were retained.
#
# 32 * (11 + 1) = 384. Why the floor is not optional: `Coverage.ratio` returns
# 1.0 when `expected` is 0, so a pass that captured nothing would otherwise
# report perfect coverage. With the floor, a total outage reads 0/384 = 0.00, a
# truncated document reads 100/384 = 0.26, and a healthy week — which observes
# far more than 384 keys, because this collector captures every offensive-skill
# player the feed carries rather than only the ~352 in scope — is unaffected,
# because the floor raises a short count and never lowers a genuine one.
LEAGUE_TEAMS = 32
OFFENSIVE_SCOPE_SLOTS_PER_TEAM = 11
EXPECTED_FLOOR: dict[str, int] = {
    "player_usage_weekly": LEAGUE_TEAMS * (OFFENSIVE_SCOPE_SLOTS_PER_TEAM + 1),
}

# The shares that are counts over counts, and therefore cannot leave [0, 1]. A
# value outside it means the base excluded plays it should have counted — the
# spec's named impossible value, `snap_share` above 1.0.
#
# Two published numbers are deliberately NOT in here, and neither is an
# oversight:
#
#   air_yards_share  Air yards are negative on a target behind the line of
#                    scrimmage, so a screen-heavy back legitimately posts a
#                    share below zero. Range-checking it would refuse real
#                    rows — a check that fires on correct data is worse than
#                    no check, because it trains a reader to ignore the array.
#   wopr             1.5*target_share + 0.7*air_yards_share, which reaches 2.2
#                    at the top and goes negative with air_yards_share.
BOUNDED_SHARES = ("snap_share", "route_participation", "target_share", "carry_share")


class AmbiguousUsage(ValueError):
    """This row cannot be published without guessing, so it is not published.

    Carries a `reason` recorded verbatim against the row's coverage key. "The
    team's denominators never arrived" and "this share came out above 1.0" are
    different facts and must not collapse into one undifferentiated hole.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def player_key(row: UsageRow) -> str:
    """The coverage key for one player row.

    Namespaced, because this accumulator holds two kinds of key and a bare team
    abbreviation could otherwise collide with a player id. Stable across passes
    and unique within one: it is what appears in `coverage.missing`, so a key
    that changes between passes makes every row look newly missing.
    """
    return f"player:{row.upstream_player_id}"


def denominators_key(team: str) -> str:
    return f"denominators:{team}"


def _share(numerator: float, denominator: float, *, field: str) -> float:
    """One share, or a refusal.

    `0/0` is 0.0 — a player with no targets on a team with no targets has a
    zero share, not an unknown one. A *nonzero* numerator over a zero
    denominator is the inconsistency the spec's rejection rule exists for: the
    base is provably wrong, and dividing anyway would produce an infinity or a
    plausible-looking number from a denominator nobody can defend.
    """
    if denominator == 0:
        if numerator == 0:
            return 0.0
        raise AmbiguousUsage(
            "denominator_inconsistent",
            f"{field}: numerator {numerator} over a zero base",
        )
    return round(numerator / denominator, 6)


def build_signal(
    row: UsageRow, denominators: TeamDenominators | None, *, now: datetime
) -> dict:
    """One upstream row plus its team's bases -> one published usage row.

    This collector's actual product. The shape is mirrored in
    `contracts/signal-envelope/collectors/usage-share.json`, and
    `tests/test_capture_contract_conformance.py` validates the REAL output of
    this function against that schema — so a renamed field fails there rather
    than in the generator six weeks later.

    Raises `AmbiguousUsage` rather than emitting a null-filled row where the
    upstream is genuinely ambiguous. The caller counts the refusal in
    `coverage.missing` with the reason, which is what makes the gap explicit.
    """
    if denominators is None:
        # The spec is unambiguous: a share arriving without its base is
        # rejected rather than stored.
        raise AmbiguousUsage("missing_denominators", row.team)

    target_share = _share(row.targets, denominators.targets, field="target_share")
    air_yards_share = _share(
        row.air_yards, denominators.air_yards, field="air_yards_share"
    )
    carry_share = _share(row.carries, denominators.carries, field="carry_share")

    signal = {
        "player_id": row.upstream_player_id,
        # NOT in the phase doc's field table. Added deliberately: that table
        # says `player_id` is "the canonical id from player-identity", and this
        # collector cannot produce one yet — roster-scope's membership rows
        # carry no name and no external id, so there is nothing to join a
        # GSIS-keyed feed onto. Emitting the upstream id under a field
        # documented as canonical, with no way for a consumer to tell, is
        # exactly the silent gap the envelope's coverage block exists to
        # prevent. Flips to `player_identity` when that join exists.
        "player_id_source": "upstream_gsis",
        "game_id": row.game_id,
        "team": row.team,
        # null, not 0.0 — this feed carries no snap counts, and "we cannot see
        # snaps" is a different fact from "this player took none". See the
        # adapter's module docstring.
        "snap_share": None,
        "route_participation": None,
        "target_share": target_share,
        "air_yards_share": air_yards_share,
        "wopr": round(1.5 * target_share + 0.7 * air_yards_share, 6),
        "carry_share": carry_share,
        # Situational splits need play-by-play, which this feed is not. Present
        # with null members rather than absent, so a consumer never has to tell
        # "this collector does not supply it" apart from "the key is missing".
        "redzone": {"carries": None, "targets": None, "snap_share": None},
        "goal_line": {"carries": None, "targets": None},
        "two_minute": {"snaps": None, "route_participation": None},
        "alignment": {
            "slot_rate": None,
            "wide_rate": None,
            "inline_rate": None,
            "backfield_rate": None,
        },
        "denominators": denominators.to_dict(),
        # `derived`, never `charted`: every share above is inferred from
        # counting stats rather than read off a charting feed.
        "usage_source": "derived",
    }

    for field in BOUNDED_SHARES:
        value = signal[field]
        if value is not None and not 0.0 <= value <= 1.0:
            metrics.invalid_share(field)
            raise AmbiguousUsage("share_out_of_range", f"{field}={value}")
    return signal


def team_sum_drift(usage: WeekUsage) -> dict[str, float]:
    """Per team, how far the upstream's own target shares summed from 1.0.

    An independent check, and deliberately so. The shares this collector
    publishes are divided by a denominator it summed itself, so they add to 1.0
    by construction — asserting on those would be vacuous. The feed's own
    `target_share` column is computed against the *vendor's* denominator, so a
    sum that misses 1.0 means the vendor's rows and the vendor's base disagree,
    which is the spec's named failure: shares that look entirely normal while
    every one of them is two to three points wrong.

    A team with no targets at all is skipped rather than reported as drift 1.0
    — that is a team whose rows never arrived, which the coverage block already
    states, not a denominator that is wrong.
    """
    return {
        team: round(abs(denominators.upstream_target_share_sum - 1.0), 6)
        for team, denominators in usage.denominators.items()
        if denominators.targets > 0
    }


async def capture_usage_share(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one `player_usage_weekly` envelope."""
    scope = {"season": season, "week": week}
    signal_type = SIGNAL_TYPES[0]
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=source_ref(season, week),
    )

    metrics.capture_attempt()
    try:
        usage = await fetch_week_usage(
            season, week, client=client, now=now, deadline=deadline
        )
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        metrics.capture_failure(exc)
        # Recorded even on the failure path: an absent Prometheus series and a
        # healthy one are indistinguishable, so the gauges must not simply stop.
        metrics.rows_captured(0)
        metrics.team_sum_drift(0.0)
        # Writes a `present: 0` envelope per signal type, then re-raises `exc`.
        # Never returns — do not add code after this call. `expected=` is what
        # floors it to 384 rather than 1, so a total outage reads ratio 0.00.
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

    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[signal_type])
    if usage.truncated:
        acc.add_error("deadline_exceeded", "upstream stream truncated mid-document")

    # Half one of the spec's coverage definition: a complete `denominators`
    # object for every team that played. Expected because the team appeared in
    # the week at all — never because building it happened to work.
    for team, denominators in sorted(usage.denominators.items()):
        key = denominators_key(team)
        acc.expect(key)
        if denominators.dropbacks == 0 and denominators.targets == 0:
            # A team with neither a pass attempt nor a target did not play an
            # offensive snap this week. Its bases are not "complete but zero",
            # they are absent, and every share taken against them would be a
            # division this collector cannot defend.
            acc.fail(key, "empty_denominators")
            continue
        acc.record(key)

    # Half two: one row per player whose team completed its game.
    signals: list[dict] = []
    for row in usage.rows:
        key = player_key(row)
        # Declared because the row EXISTS and is therefore owed — never because
        # building it below happened to succeed.
        acc.expect(key)
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing away
            # what already resolved: a truncated pass that reports itself
            # truncated is useful; one that reports itself complete is not.
            acc.fail(key, "deadline_exceeded")
            continue
        try:
            signals.append(build_signal(row, usage.denominators.get(row.team), now=now))
        except AmbiguousUsage as exc:
            acc.fail(key, exc.reason)
            continue
        except Exception as exc:  # noqa: BLE001 — one bad row is not a pass
            acc.fail(key, metrics.reason_for(exc))
            continue
        acc.record(key)

    drift = team_sum_drift(usage)
    for team, value in sorted(drift.items()):
        if value > TEAM_SUM_TOLERANCE:
            acc.add_error("team_sum_drift", f"{team}: target shares sum off by {value}")

    metrics.rows_captured(len(signals))
    # Recorded every pass including a clean 0.0, and taken over all teams so one
    # team's broken denominator is not averaged away by thirty-one healthy ones.
    metrics.team_sum_drift(max(drift.values(), default=0.0))

    envelope = Envelope(
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
    await awrite(lake, envelope)
    metrics.coverage(signal_type, envelope.coverage.ratio)
    return {signal_type: envelope}
