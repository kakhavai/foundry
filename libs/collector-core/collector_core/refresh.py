"""Minimum-interval floor for POST /refresh.

Waiting on a timer is the wrong behaviour during breaking news or a backfill,
so force-refresh exists. The floor exists so it cannot become a way to get an
API key banned by an upstream that rate-limits.
"""

import math
import uuid
from datetime import datetime, timedelta


class RefreshGate:
    def __init__(self, min_interval: timedelta) -> None:
        self._min_interval = min_interval
        self._last_allowed_at: datetime | None = None

    def _elapsed_enough(self, now: datetime) -> bool:
        if self._last_allowed_at is None:
            return True
        return now - self._last_allowed_at >= self._min_interval

    def try_acquire(self, now: datetime) -> str | None:
        """Return a refresh_id, or None when called too soon.

        A refused attempt deliberately does not update the timestamp — otherwise
        a client polling faster than the floor would hold the gate shut forever.
        """
        if not self._elapsed_enough(now):
            return None
        self._last_allowed_at = now
        return uuid.uuid4().hex

    def retry_after(self, now: datetime) -> int:
        """Whole seconds until the next refresh is permitted. 0 when allowed."""
        if self._elapsed_enough(now):
            return 0
        remaining = self._min_interval - (now - self._last_allowed_at)
        return max(0, math.ceil(remaining.total_seconds()))
