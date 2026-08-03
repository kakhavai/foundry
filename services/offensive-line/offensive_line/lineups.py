"""Two questions about the starting five, kept apart on purpose.

**Who actually played** (`derive_lineups`) is decided by snap counts and
nothing else. The depth chart contributes only the *slot label* — which of
LT/LG/C/RG/RT a man occupies — and the label mapping is resolved once per
week from the snapshot current at that week's last game, so a depth chart
republished in March cannot retroactively reorder a week-5 hash inside one
pass. The spec's warning is about the first question: "or continuity becomes a
description of the team's press releases."

**What the five will be next week** (`build_lineup_view`) is the `/lineups`
route's answer, and it is forward-looking, which is why the spec gives it a
route at all rather than leaving it to `/signals`. It reads the newest
envelope for a `(season, week)` back out of the lake — synchronously, because
`LakeWriter` is boto3, so the caller offloads the whole helper with
`asyncio.to_thread` rather than awaiting each call.
"""

from collections.abc import Mapping, Sequence

from collector_core.lake import LakeWriter

from .adapters.depth import DepthCharts
from .adapters.players import Roster
from .adapters.snaps import SnapFold
from .ratings import RECORD_STARTER, RECORD_UNIT, STARTER_POSITIONS, StarterSlot

__all__ = ["build_lineup_view", "derive_lineups", "unavailable_starters"]

# The availabilities that make a listed starter unlikely to play. `/lineups`
# surfaces them because a projected five containing a man who is out is the
# precise input the spec's failure mode corrupts.
UNAVAILABLE = frozenset({"out", "doubtful", "ir"})


def derive_lineups(
    snaps: SnapFold,
    roster: Roster,
    charts: DepthCharts,
    week_dates: Mapping[int, str],
) -> dict[tuple[str, str], list[StarterSlot]]:
    """`(team, game_id) -> the five snap-decided starters`, where identifiable.

    Per position label, the man with the most offensive snaps in that game.
    Ties broken by id rather than by iteration order: two linemen on identical
    snap counts would otherwise land in the lineup depending on which row the
    CSV parser reached first, and the hash would move between passes over
    nothing.

    Returns whatever it could identify — one slot, three, five. `lineup_hash`
    refuses anything short of five, and `build_rows` turns that into
    `coverage.missing` for the whole team, which is the spec's own clause.
    """
    best: dict[tuple[str, str], dict[str, tuple[int, str]]] = {}

    for entry in snaps.line:
        gsis_id = roster.gsis_for_pfr.get(entry.pfr_id)
        if gsis_id is None:
            # No crosswalk row: this man cannot be named, so he cannot be a
            # starter in a hash of canonical ids. Counted by the caller as a
            # missing slot rather than skipped silently.
            #
            # **A defaulting `.get(entry.pfr_id, entry.pfr_id)` here happens to
            # be harmless, and only by accident of the next line**: the label
            # lookup is keyed by `gsis_id`, so a PFR key adopted in its place
            # simply misses the depth chart and the slot goes unfilled anyway.
            # Mutation-tested and confirmed equivalent. Written explicitly all
            # the same — the equivalence is a property of the *label* lookup,
            # not of this statement, and it would evaporate the moment
            # anything downstream keyed on something other than the chart.
            continue
        label = charts.labels_at(week_dates.get(entry.week)).get((entry.team, gsis_id))
        if label is None:
            continue
        slot_key = (entry.team, entry.game_id)
        current = best.setdefault(slot_key, {}).get(label)
        candidate = (entry.offense_snaps, gsis_id)
        if current is None or candidate > current:
            best[slot_key][label] = candidate

    lineups: dict[tuple[str, str], list[StarterSlot]] = {}
    for key, by_position in best.items():
        slots: list[StarterSlot] = []
        for position in STARTER_POSITIONS:
            found = by_position.get(position)
            if found is None:
                continue
            snap_count, gsis_id = found
            slots.append(
                StarterSlot(position=position, gsis_id=gsis_id, snaps=snap_count)
            )
        lineups[key] = slots
    return lineups


def unavailable_starters(rows: Sequence[Mapping]) -> list[str]:
    """The `starter_id`s a generator should not project as playing."""
    return sorted(
        str(row["starter_id"])
        for row in rows
        if str(row.get("starter_availability", "")).lower() in UNAVAILABLE
    )


def build_lineup_view(
    lake: LakeWriter,
    collector: str,
    signal_type: str,
    season: int,
    week: int,
    team: str | None = None,
) -> list[dict]:
    """The projected starting five per team, newest capture wins.

    **Synchronous, and called through `asyncio.to_thread`.** It does one
    `list_keys` plus one `read`, and the lake `build_collector_app` hands a
    collector raises if either is called from the event loop thread — a prefix
    scan on the loop stalls every other request, including `/health`.

    Reads only the newest key in the partition. The lake is append-only and
    resolved by recency, so an older object is a superseded capture and
    merging the two would silently mix two vintages of the same week.
    """
    keys = lake.list_keys(collector, signal_type, season, week)
    if not keys:
        return []
    body = lake.read(keys[-1])
    rows = body.get("signals", [])

    units = {
        row["team_id"]: row for row in rows if row.get("record_type") == RECORD_UNIT
    }
    starters: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("record_type") != RECORD_STARTER:
            continue
        starters.setdefault(row["team_id"], []).append(row)

    view: list[dict] = []
    for team_id in sorted(set(units) | set(starters)):
        if team is not None and team_id != team:
            continue
        unit = units.get(team_id, {})
        five = sorted(
            starters.get(team_id, []),
            key=lambda row: STARTER_POSITIONS.index(row["starter_position"]),
        )
        view.append(
            {
                "team_id": team_id,
                "lineup_hash": unit.get("lineup_hash"),
                "continuity_games": unit.get("continuity_games"),
                "lineup_changed": unit.get("lineup_changed"),
                "starters": [
                    {
                        "starter_id": row["starter_id"],
                        "starter_position": row["starter_position"],
                        "starter_availability": row["starter_availability"],
                        "starter_snap_share": row["starter_snap_share"],
                        "replacement_delta_pressure_rate": row[
                            "replacement_delta_pressure_rate"
                        ],
                        "replacement_delta_provenance": row[
                            "replacement_delta_provenance"
                        ],
                    }
                    for row in five
                ],
                # Named rather than left for the caller to derive: this is the
                # whole reason the route is forward-looking. A five that is
                # complete on paper and missing a tackle on Sunday is the
                # input the spec's failure mode corrupts.
                "unavailable_starters": unavailable_starters(five),
                "captured_at": body.get("captured_at"),
            }
        )
    return view
