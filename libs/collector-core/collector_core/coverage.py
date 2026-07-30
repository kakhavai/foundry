"""Coverage accounting — 'what does complete mean here?' made mechanical.

Expected keys are declared up front and present ones recorded as they land, so
`missing` is derived rather than maintained. A collector cannot report itself
complete while silently dropping rows, because the two numbers come from one
source.
"""

from collections.abc import Iterable

from .envelope import Coverage


class CoverageAccumulator:
    def __init__(self, expected_keys: Iterable[str]) -> None:
        self._expected: set[str] = set(expected_keys)
        self._present: set[str] = set()
        self._errors: list[dict] = []

    def record(self, key: str) -> None:
        """Mark a key captured. Idempotent."""
        if key not in self._expected:
            raise KeyError(f"{key!r} is not in the expected set")
        self._present.add(key)

    def fail(self, key: str, reason: str) -> None:
        """Record why a key could not be captured. It stays missing."""
        self._errors.append({"reason": reason, "detail": key})

    @property
    def errors(self) -> list[dict]:
        return list(self._errors)

    def result(self) -> Coverage:
        return Coverage(
            expected=len(self._expected),
            present=len(self._present),
            missing=sorted(self._expected - self._present),
        )
