"""`GET /signals/diff`'s supporting logic: what changed between two captures.

The spec asks for this route by name — *"the ordering changes between two
captures, so a consumer can react to a promotion without diffing full snapshots
itself"*. Derivable from the lake by any consumer, but every consumer would
otherwise reimplement it, and a promotion is exactly the event a generator wants
to react to within minutes.

A pure synchronous function over a `LakeWriter`, mirroring weather's
`build_convergence_series`. `main.py` offloads the whole call with
`asyncio.to_thread` rather than awaiting each lake operation individually: this
does one `list_keys` plus two `read`s, and running a prefix scan on the event
loop would stall every other request — including `/health` — for its duration.
The `EventLoopGuardedLake` every collector is handed turns forgetting that from
an invisible stall into an immediate error.
"""

from dataclasses import dataclass


class SnapshotNotFound(LookupError):
    """No `team_depth_chart` object in this partition carries that captured_at.

    A distinct exception rather than an empty diff: "nothing changed" and "you
    asked about a capture that never happened" are different answers, and
    collapsing them would let a typo'd timestamp read as a quiet week.
    """


@dataclass(frozen=True)
class _Row:
    player: str
    rank: int


def _ordering(body: dict) -> dict[str, dict[str, _Row]]:
    """`{team:position: {player_key: _Row}}` from one envelope body.

    Keyed on `gsis_id` where the feed supplied one and on the name otherwise, so
    a vendor correcting a spelling does not read as one departure plus one
    arrival.
    """
    grouped: dict[str, dict[str, _Row]] = {}
    for row in body.get("signals", []):
        group = f"{row.get('team')}:{row.get('position')}"
        key = row.get("gsis_id") or row.get("player_name") or ""
        if not key:
            continue
        grouped.setdefault(group, {})[key] = _Row(
            player=row.get("player_name") or key,
            rank=row.get("official_rank"),
        )
    return grouped


def _group_changes(before: dict[str, _Row], after: dict[str, _Row]) -> list[dict]:
    changes: list[dict] = []
    for key in sorted(set(before) | set(after)):
        was, now = before.get(key), after.get(key)
        if was is None:
            changes.append(
                {
                    "change": "added",
                    "player_key": key,
                    "player_name": now.player,
                    "from_rank": None,
                    "to_rank": now.rank,
                }
            )
        elif now is None:
            changes.append(
                {
                    "change": "removed",
                    "player_key": key,
                    "player_name": was.player,
                    "from_rank": was.rank,
                    "to_rank": None,
                }
            )
        elif was.rank != now.rank:
            changes.append(
                {
                    # Named by direction rather than left as a delta: a
                    # *promotion* is the event a consumer is watching for, and
                    # making it read off `change` saves every consumer
                    # re-deriving the sign convention.
                    "change": (
                        "promoted" if (now.rank or 0) < (was.rank or 0) else "demoted"
                    ),
                    "player_key": key,
                    "player_name": now.player,
                    "from_rank": was.rank,
                    "to_rank": now.rank,
                }
            )
    return changes


def build_diff(
    lake,
    collector: str,
    signal_type: str,
    season: int,
    week: int,
    *,
    from_captured_at: str,
    to_captured_at: str,
) -> dict:
    """Ordering changes between two `team_depth_chart` captures.

    `list_keys` filters to this signal type by key suffix (`collector_core.
    lake.lake_key`), so every object considered here is already the right signal
    type — no further filtering needed on read. Only the two named objects are
    read, never the whole partition.
    """
    keys = lake.list_keys(collector, signal_type, season, week)
    wanted = {from_captured_at: None, to_captured_at: None}
    for key in keys:
        # `<captured_at>-<signal_type>.json` — the instant is the key's leading
        # segment, so matching on it needs no read.
        stamp = key.rsplit("/", 1)[-1].split("-" + signal_type, 1)[0]
        if stamp in wanted and wanted[stamp] is None:
            wanted[stamp] = key

    missing = sorted(stamp for stamp, key in wanted.items() if key is None)
    if missing:
        raise SnapshotNotFound(
            f"no {signal_type} snapshot at {', '.join(missing)} in "
            f"season={season} week={week}"
        )

    before = _ordering(lake.read(wanted[from_captured_at]))
    after = _ordering(lake.read(wanted[to_captured_at]))

    groups: list[dict] = []
    for group in sorted(set(before) | set(after)):
        changes = _group_changes(before.get(group, {}), after.get(group, {}))
        if not changes:
            continue
        team, _, position = group.partition(":")
        groups.append({"team": team, "position": position, "changes": changes})

    return {
        "signal_type": signal_type,
        "scope": {"season": season, "week": week},
        "from": from_captured_at,
        "to": to_captured_at,
        "groups": groups,
        "count": sum(len(group["changes"]) for group in groups),
    }
