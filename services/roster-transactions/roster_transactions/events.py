"""`GET /events` — the cursor-paged event stream, minus the HTTP.

This collector is event-shaped rather than snapshot-shaped: every other
collector's consumer wants "the current state of week N", and this one's wants
"everything since the last thing I saw". Re-reading a week to find three new
rows is what a cursor exists to avoid, so the phase doc gives this collector one
route beyond the standard five.

The route body in `main.py` is a call and a return; the ordering, the cursor
encoding and the validation live here so they can be tested without a client.

**Ordering is `(announced_at, transaction_id)`, and the tiebreak is not
optional.** Several moves routinely share an announcement timestamp — a trade
is two rows at the same instant — and a cursor over a non-total order either
re-delivers rows or skips them, silently, depending on which way the sort
happened to fall that pass. `transaction_id` is content-derived and unique, so
the pair is a total order.

**`announced_at`, not `effective_at`.** A consumer resuming from a cursor is
asking "what has been *reported* since I last looked". Ordering by when moves
take effect would make a Thursday signing announced on Monday appear *after*
rows the consumer had already seen, i.e. behind its own cursor, where it would
never be delivered at all.

**Paging is over the cached week, and that bound is real.** `CaptureState`
holds the scoped week's envelope and nothing older, so a cursor survives
restarts of the *consumer* but not a change of scoped week. A consumer crossing
a week boundary advances `season`/`week` rather than expecting the cursor to
carry it, and the append-only lake — one object per pass — is what holds the
history. Serving from the cache is also why this route touches no lake and
needs no `to_thread`.
"""

from collections.abc import Iterable, Mapping

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "InvalidCursor",
    "cursor_for",
    "page",
    "sort_key",
]

DEFAULT_LIMIT = 100

# A page is served from memory and serialized whole, so the ceiling is about
# response size rather than query cost. 500 mirrors `player-identity`'s batch
# resolve cap, so a consumer does not have to remember two different numbers.
MAX_LIMIT = 500

# Cannot occur in an RFC 3339 timestamp or in a `rtx-` id, so splitting is
# unambiguous.
CURSOR_SEPARATOR = "|"


class InvalidCursor(ValueError):
    """The `since` cursor was not one this collector issued."""


def sort_key(row: Mapping) -> tuple[str, str]:
    """The total order pages advance through. See the module docstring."""
    return (str(row.get("announced_at") or ""), str(row.get("transaction_id") or ""))


def cursor_for(row: Mapping) -> str:
    """The opaque cursor a consumer sends back to resume after `row`."""
    announced, transaction = sort_key(row)
    return f"{announced}{CURSOR_SEPARATOR}{transaction}"


def _parse(cursor: str) -> tuple[str, str]:
    announced, separator, transaction = cursor.partition(CURSOR_SEPARATOR)
    if not separator or not announced or not transaction:
        raise InvalidCursor(
            f"cursor {cursor!r} is not '<announced_at>{CURSOR_SEPARATOR}"
            f"<transaction_id>'"
        )
    return announced, transaction


def page(
    rows: Iterable[Mapping],
    *,
    since: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """One page of events, strictly after `since`.

    `next_cursor` is `None` only when the page exhausted the stream, so a
    consumer polls until it gets one rather than guessing from a short page —
    a page can be short because `limit` happened to land on the boundary.
    """
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")

    ordered = sorted(rows, key=sort_key)
    if since is not None:
        after = _parse(since)
        ordered = [row for row in ordered if sort_key(row) > after]

    window = ordered[:limit]
    exhausted = len(window) == len(ordered)
    return {
        "events": window,
        "count": len(window),
        "next_cursor": None if exhausted or not window else cursor_for(window[-1]),
    }
