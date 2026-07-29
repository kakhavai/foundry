"""The envelope every collector emits, in HTTP responses and lake objects alike.

Contracted in contracts/signal-envelope/. The `coverage` block is the part worth
defending: without it a collector returning 309 of 312 rows is indistinguishable
from a healthy one, and the generator quietly trains on a hole.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

ENVELOPE_VERSION = "1"


def _rfc3339(value: datetime) -> str:
    """Serialize as RFC 3339 UTC with a `Z` suffix.

    Rejects naive datetimes rather than assuming UTC. A naive timestamp means
    'some timezone the caller forgot to state', and guessing puts a wrong
    instant into an append-only lake that is never rewritten.
    """
    if value.tzinfo is None:
        raise ValueError(f"timezone-aware datetime required, got naive: {value!r}")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Upstream:
    adapter: str
    fetched_at: datetime
    source_ref: str | None = None

    def to_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "fetched_at": _rfc3339(self.fetched_at),
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class Coverage:
    expected: int
    present: int
    missing: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        """present/expected, or 1.0 when nothing was expected.

        An empty week is complete, not broken — a bye week legitimately expects
        zero records, and 0/0 must not read as a coverage failure.
        """
        return 1.0 if self.expected == 0 else self.present / self.expected

    def to_dict(self) -> dict:
        return {
            "expected": self.expected,
            "present": self.present,
            "missing": list(self.missing),
        }


@dataclass(frozen=True)
class Envelope:
    envelope_version: str
    collector: str
    signal_type: str
    captured_at: datetime
    upstream: Upstream
    scope: dict
    coverage: Coverage
    errors: list[dict]
    signals: list[dict]

    def __post_init__(self) -> None:
        # Validate eagerly rather than at serialization time, so a bad timestamp
        # fails where it was constructed instead of deep in a lake write.
        _rfc3339(self.captured_at)
        _rfc3339(self.upstream.fetched_at)

    def to_dict(self) -> dict:
        return {
            "envelope_version": self.envelope_version,
            "collector": self.collector,
            "signal_type": self.signal_type,
            "captured_at": _rfc3339(self.captured_at),
            "upstream": self.upstream.to_dict(),
            "scope": dict(self.scope),
            "coverage": self.coverage.to_dict(),
            "errors": list(self.errors),
            "signals": list(self.signals),
        }
