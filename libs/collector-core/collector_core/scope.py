"""Reading the published scope, so a collector fetches only what matters.

Deliberately reads the LAKE, never `roster-scope` over HTTP. The lake is
append-only and already written by every scope capture, so the last good
scope survives a `player-identity` outage -- which is what stops one service
being a fleet-wide stop. `roster-scope`'s HTTP routes exist for the
out-of-repo generator and for operators; collectors do not use them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from .lake import alist_keys, aread

SCOPE_COLLECTOR = "roster-scope"


class ScopeUnavailable(Exception):
    """No usable scope. The caller must write a `present: 0` envelope and
    make ZERO upstream calls -- an unnarrowed fallback would blow the vendor
    budget precisely during an incident."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Scope:
    members: frozenset[str]
    captured_at: datetime
    signal_type: str

    def age_seconds(self, now: datetime) -> float:
        return (now - self.captured_at).total_seconds()


def _parse_captured_at(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class ScopeClient:
    def __init__(self, lake) -> None:
        self._lake = lake

    async def fetch(self, signal_type: str, season: int, week: int) -> Scope:
        keys = await alist_keys(self._lake, SCOPE_COLLECTOR, signal_type, season, week)
        if not keys:
            raise ScopeUnavailable("scope_unavailable")

        # `list_keys` returns captured_at order, so the newest is last.
        envelope = await aread(self._lake, keys[-1])
        members = frozenset(
            row["player_id"] for row in envelope["signals"] if row.get("player_id")
        )
        if not members:
            raise ScopeUnavailable("scope_empty")

        return Scope(
            members=members,
            captured_at=_parse_captured_at(envelope["captured_at"]),
            signal_type=signal_type,
        )
