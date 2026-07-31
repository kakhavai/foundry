"""Coverage accounting — 'what does complete mean here?' made mechanical.

Expected keys are declared up front and present ones recorded as they land, so
`missing` is derived rather than maintained. A collector cannot report itself
complete while silently dropping rows, because the two numbers come from one
source.

Two properties here are load-bearing and neither is obvious.

**`expected` never derives from what succeeded — including one level up.**
`record` refuses a key that was not expected first, which stops the obvious
version. The subtle version is a collector that builds its expectation *from
the document it just fetched*: a truncated upstream carrying 100 of 2,900
records yields `expected: 100, present: 100`, ratio `1.0` — reported as
perfectly healthy while 96% of the league silently vanished. That is why
`floor` exists. Where the universe has a known size (32 teams, ~1,700
rostered players, 17 weeks), the floor encodes it independently of what the
fetch happened to return:

    total outage   -> 2900 / 0    -> ratio 0.00
    truncation     -> 2900 / 100  -> ratio 0.03
    healthy fetch  -> 2900 / 2900 -> ratio 1.00

The floor never *lowers* the count, so a genuine expansion past the floor
still reports honestly.

**The errors array is capped, and the truncation is visible.** A total schema
break against a 2,900-record upstream produced 2,900 near-identical entries in
an 8A prototype — in memory, in every HTTP response, and in every append-only
lake object. `cap_errors` bounds it and appends an explicit marker carrying
the omitted and total counts, because a silently truncated error list is worse
than a long one: it looks like a short list of problems.
"""

from collections.abc import Iterable

from .envelope import Coverage

# At weather's ~16 games an uncapped list is fine. At 2,900 records it is not.
# 50 is enough to see the shape of a failure (which reasons, against which
# keys) without carrying the whole cardinality of the upstream.
MAX_ERRORS = 50

# The marker `cap_errors` appends. Named rather than inlined so a consumer can
# recognize a truncated list without string-matching prose.
ERRORS_TRUNCATED = "errors_truncated"

# Recorded when the observed key count falls short of the declared floor —
# the truncated-upstream case above, stated rather than left to be inferred
# from `expected` and `missing` disagreeing.
BELOW_EXPECTED_FLOOR = "below_expected_floor"


def cap_errors(errors: Iterable[dict], *, max_errors: int = MAX_ERRORS) -> list[dict]:
    """Bound an errors array, appending a marker that states what was dropped.

    Idempotent: a list that already ends in a truncation marker is returned
    unchanged. Without that, applying the cap twice — an accumulator's own cap
    plus a collector re-capping after appending its own entries — would
    truncate an already-truncated list and report the wrong omitted count.
    """
    if max_errors < 1:
        raise ValueError(f"max_errors must be at least 1, got {max_errors}")

    entries = list(errors)
    if entries and entries[-1].get("reason") == ERRORS_TRUNCATED:
        return entries
    if len(entries) <= max_errors:
        return entries

    total = len(entries)
    omitted = total - max_errors
    return [
        *entries[:max_errors],
        {
            "reason": ERRORS_TRUNCATED,
            "detail": (f"{omitted} of {total} error(s) omitted; cap is {max_errors}"),
            "omitted": omitted,
            "total": total,
        },
    ]


class CoverageAccumulator:
    """Accumulates one signal type's coverage over a capture pass.

    `expected_keys` declares the universe up front where it is known ahead of
    the fetch (weather's scheduled games, roster-scope's 416 config slots).
    Where it is only discoverable as the document is read, declare keys with
    `expect` as they arrive and set `floor` to the size the universe is known
    to have — see the module docstring for why that is not optional.
    """

    def __init__(
        self,
        expected_keys: Iterable[str] = (),
        *,
        floor: int = 0,
        max_errors: int = MAX_ERRORS,
    ) -> None:
        if floor < 0:
            raise ValueError(f"floor must not be negative, got {floor}")
        self._expected: set[str] = set(expected_keys)
        self._present: set[str] = set()
        self._errors: list[dict] = []
        self._floor = floor
        self._max_errors = max_errors

    def expect(self, key: str) -> None:
        """Declare a key part of the expected universe. Idempotent.

        For collectors whose universe is only knowable as the upstream
        document is read. Call it on the fact that made the key qualify, never
        on the success of capturing it — the latter is exactly the derivation
        the floor exists to defend against.
        """
        self._expected.add(key)

    def record(self, key: str) -> None:
        """Mark a key captured. Idempotent.

        Refuses a key that was never expected: `expected` must not grow
        because something succeeded.
        """
        if key not in self._expected:
            raise KeyError(f"{key!r} is not in the expected set")
        self._present.add(key)

    def fail(self, key: str, reason: str) -> None:
        """Record why a key could not be captured. It stays missing.

        Unlike `record`, this declares the key expected: a failure is evidence
        the key *was* owed, which is the opposite of deriving the expectation
        from a success.
        """
        self._expected.add(key)
        self._errors.append({"reason": reason, "detail": key})

    def add_error(self, reason: str, detail: str = "") -> None:
        """Record a pass-level problem that is not tied to one missing key —
        a merge conflict, an upstream fetch that failed wholesale, a rank
        violation. Routed through the accumulator rather than a collector's
        own side list so there is exactly one place the cap is applied.
        """
        self._errors.append({"reason": reason, "detail": detail})

    def add_priority_error(self, reason: str, detail: str = "") -> None:
        """Record a pass-level problem that must SURVIVE the cap.

        Same entry shape as `add_error`, inserted at the front rather than
        appended — the identical reasoning `errors` already applies to
        `below_expected_floor` ("First, not last, so it survives capping").
        `cap_errors` keeps the first `MAX_ERRORS` entries and drops the tail,
        so an entry that explains why a whole pass published nothing must not
        be queued behind a few hundred routine per-key failures. A week where
        half a league's feed breaks can produce well over the cap in ordinary
        entries; appending would silently delete the one entry a reader needs.

        Use it sparingly, and only for the entry that explains the pass. It is
        the public form of an insert `injury-report` previously did by reaching
        into `_errors` from `services/` — a private attribute of this class,
        which at twenty-six collectors is exactly the kind of thing that gets
        copied. The library already owned this concern for
        `below_expected_floor`; it now owns it for collectors too.

        Ordering against `below_expected_floor`: `errors` prepends the floor
        shortfall ahead of everything in `_errors`, so a priority error lands
        second when a shortfall is also present and first otherwise. Both
        survive the cap, which is the property that matters.
        """
        self._errors.insert(0, {"reason": reason, "detail": detail})

    @property
    def observed(self) -> int:
        """How many keys this pass actually knows about, before the floor."""
        return len(self._expected)

    @property
    def errors(self) -> list[dict]:
        """The capped errors array, floor shortfall first.

        First, not last, so it survives capping: a shortfall against the
        declared floor is the single most important entry in this list and
        must not be the one that gets dropped.
        """
        shortfall: list[dict] = []
        if self._floor > len(self._expected):
            shortfall = [
                {
                    "reason": BELOW_EXPECTED_FLOOR,
                    "detail": (
                        f"upstream described {len(self._expected)} key(s); "
                        f"the declared floor is {self._floor}"
                    ),
                }
            ]
        return cap_errors([*shortfall, *self._errors], max_errors=self._max_errors)

    def result(self) -> Coverage:
        """`expected` is the observed universe or the declared floor,
        whichever is larger. `missing` names only the keys this pass knows
        about, so when the floor exceeds the observed count `missing` is
        short while `expected` is not — that gap is real information, and it
        is also stated outright in `errors`.
        """
        return Coverage(
            expected=max(len(self._expected), self._floor),
            present=len(self._present),
            missing=sorted(self._expected - self._present),
        )
