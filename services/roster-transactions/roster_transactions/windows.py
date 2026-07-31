"""Polling windows — this collector's coverage universe.

**Why this module exists at all.** Every other collector in the fleet counts
*things*: 32 teams, 272 games, 416 scope slots, ~2,900 rostered players. This
one counts **time**. Transactions are an event stream, not a fixed roster, so
"how many should exist" has no answer — a quiet Tuesday legitimately has zero,
and the naive `expected = len(rows_i_got)` would report ratio `1.0` for a feed
that returned nothing at all. That is the single most tempting and most wrong
thing this collector could do, and the phase doc says so outright:

    "Coverage is over polling windows, not players: every 15-minute interval in
    the scoped week must have been polled and acknowledged by the upstream, so
    `expected` counts intervals and `missing` names the ones with no successful
    fetch. A quiet Tuesday with zero transactions is full coverage; a failed
    poll during a quiet Tuesday is not."

So the universe is the set of 15-minute intervals in the scoped week, derived
from **the calendar and the clock** — two sources the upstream cannot influence.
An upstream that returns nothing, returns half a week, or falls over entirely
cannot shrink `expected`, because `expected` was never computed from it.

**Only fully-elapsed intervals are expected.** An interval that has started but
not finished cannot have been acknowledged by anyone, so counting it would put
every single pass permanently one interval short (671/672 ≈ 0.9985) and make a
`< 1.0` alert useless. `elapsed_interval_keys` therefore takes intervals whose
*end* is at or before `now`.

**A week nobody has polled reads 0.0, not 1.0.** At the very start of a scoped
week — or for a week scoped in the future — zero intervals have elapsed, and
`Coverage.ratio` returns `1.0` when `expected` is 0. That is correct for a bye
week and catastrophic here: it would mean a collector pointed at a week it has
never once polled reports perfect coverage. `capture.py` floors `expected` to
`MIN_EXPECTED_INTERVALS` for exactly that reason, so the honest reading of
"nothing about this week has been covered" is 0.0. The cost is that the first
fifteen minutes of a scoped week read 0.0; that is the deliberate trade, and it
is the right way round.

**`expected: 1, present: 0` with signals still emitted is that case, not a bug.**
It has been reported as one, so: coverage and `signals` measure different axes
and are not required to agree. Coverage answers *how much of this week's polling
time has been covered*; `signals` answers *what did the feed hand this pass*.
The deployed defaults (`CAPTURE_SEASON=2026`, `CAPTURE_WEEK=1`) scope a week
opening 2026-09-01, so before that date nothing has elapsed, `expected` reads
the floor, and `present` cannot be anything but 0 — while the placeholder feed,
whose rows are timestamped relative to the scoped week's own start, emits three
rows anyway. The same shape occurs against a real upstream: a move announced
three minutes ago sits in an interval that has not finished elapsing, so it is a
signal without being coverage. The envelope says so itself — `errors` carries
`below_expected_floor`. Pinned by
`tests/test_coverage_floor.py::test_a_future_week_reports_zero_coverage_while_still_emitting_signals`.

**What this cannot assert, stated rather than implied.** A single envelope
describes a single pass. Whether interval T was *itself* polled successfully at
time T is a property of the history of passes, and no one pass can see it —
this collector holds no durable cross-pass state on the event-loop-safe path.
What each envelope asserts is narrower and true: *of the intervals of this week
that have elapsed, these are the ones this pass's upstream acknowledged
covering.* The cross-pass question is answered by the append-only lake (one
object per pass, each naming its own covered intervals) plus
`collector_staleness_seconds`, not by any single coverage block.
"""

from datetime import UTC, datetime, timedelta

__all__ = [
    "INTERVAL",
    "INTERVALS_PER_WEEK",
    "MIN_EXPECTED_INTERVALS",
    "covered_interval_keys",
    "elapsed_interval_keys",
    "interval_key",
    "week_window",
]

# The cadence class is `volatile` — a 15-minute base interval — so the polling
# window and the loop's tick are the same unit by construction. They must stay
# that way: an interval shorter than the tick could never be acknowledged by a
# single pass, and one longer would hide a missed tick inside a covered
# interval.
INTERVAL = timedelta(minutes=15)

# 7 days x 24 hours x 4 intervals. The full-week universe, for reference and for
# the bound in `elapsed_interval_keys` — a completed week has exactly this many
# intervals and no pass may ever claim more.
INTERVALS_PER_WEEK = 7 * 24 * 4

# The floor applied when nothing has elapsed yet. At least 1, because
# `Coverage.ratio` reads 1.0 at `expected == 0` — see the module docstring.
MIN_EXPECTED_INTERVALS = 1

# The coverage key prefix, so a reader of `coverage.missing` can tell an
# interval key from the row keys `capture.py` adds for unplaceable rows.
INTERVAL_PREFIX = "interval:"


def _first_thursday_of_september(season: int) -> datetime:
    """The season's opening Thursday, derived rather than tabulated.

    A per-season lookup table would be a second thing to remember to update
    every August, and the failure mode of forgetting is a silently wrong week
    boundary rather than an error.
    """
    september_first = datetime(season, 9, 1, tzinfo=UTC)
    # Monday is 0, so Thursday is 3.
    return september_first + timedelta(days=(3 - september_first.weekday()) % 7)


def week_window(season: int, week: int) -> tuple[datetime, datetime]:
    """The half-open `[start, end)` instant range one scoped week covers.

    Anchored to **Tuesday**, not to kickoff: the league's transaction week runs
    Tuesday through Monday, and the moves that matter most for a given week —
    a Tuesday waiver claim, a Wednesday practice-squad signing — happen days
    before anybody plays. Anchoring to game day would file them under the
    previous week and leave the week they actually affect looking quiet.
    """
    if week < 1:
        raise ValueError(f"week must be at least 1, got {week}")
    week_one_start = _first_thursday_of_september(season) - timedelta(days=2)
    start = week_one_start + timedelta(weeks=week - 1)
    return start, start + timedelta(weeks=1)


def interval_key(start: datetime) -> str:
    """The coverage key for the interval beginning at `start`.

    Stable across passes by construction — it is derived from the clock, not
    from anything the upstream said — which matters because an unstable key
    makes every interval look newly missing on every pass.
    """
    return f"{INTERVAL_PREFIX}{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def _interval_starts(
    window_start: datetime, window_end: datetime, through: datetime
) -> list[datetime]:
    """Interval starts in `[window_start, window_end)` that END at or before
    `through`. An interval still in progress is not yet anybody's to have
    covered."""
    starts: list[datetime] = []
    cursor = window_start
    while cursor + INTERVAL <= min(through, window_end):
        starts.append(cursor)
        cursor += INTERVAL
    return starts


def elapsed_interval_keys(season: int, week: int, now: datetime) -> list[str]:
    """Every interval of the scoped week that has fully elapsed as of `now`.

    This is `coverage.expected`'s key set, and nothing the upstream returns
    reaches it. A total outage, a truncated feed and a quiet week all produce
    the same expectation — which is the entire point.
    """
    start, end = week_window(season, week)
    return [interval_key(s) for s in _interval_starts(start, end, now)]


def covered_interval_keys(
    season: int, week: int, now: datetime, covers_through: datetime
) -> list[str]:
    """The elapsed intervals the upstream acknowledged covering.

    `covers_through` is the upstream's own declared watermark, not something
    inferred from the rows it returned. Inferring it from rows is the trap this
    collector is most exposed to: a quiet Tuesday returns no rows, and a
    watermark derived from row timestamps would read as "covered nothing" and
    drive the ratio to zero on a week where the collector did its job
    perfectly. The phase doc's word is *acknowledged*, and an acknowledgement
    has to be stated by the upstream.
    """
    start, end = week_window(season, week)
    horizon = min(now, covers_through)
    return [interval_key(s) for s in _interval_starts(start, end, horizon)]
