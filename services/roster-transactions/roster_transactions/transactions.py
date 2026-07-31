"""The transaction vocabulary, the derived id, and row validation.

Separate from `capture.py` (which orchestrates) and from `adapters/upstream.py`
(which knows the wire) because this is the part with actual rules in it, and
rules deserve tests that need neither HTTP nor a lake.

Three things here are contract, not convenience:

**`transaction_id` is derived from content, never passed through.** The same
move appears under different ids on different feeds, and re-appears under a new
id on the *same* feed when it is upgraded from `reported` to `official`.
Deriving the id from `(player_id, transaction_type, effective_at, to_team)`
makes the upgrade land on the same key, which is what lets a consumer recognise
it as a correction rather than counting two moves where one happened.

**The type vocabulary is closed, and an unrecognised value is loud.** Bucketing
an unknown vendor verb into the nearest known one is how a `ps_elevation`
becomes a `signing`, and nothing errors — the roster the generator reconstructs
is simply wrong. An unmapped verb raises, the row is dropped, and the interval
it belonged to is recorded as not covered.

**`announced_at` and `effective_at` stay separate.** The gap between them is
exactly the window in which every depth chart in the platform is wrong.
Collapsing them would erase the one fact this collector exists to record.
"""

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

__all__ = [
    "CONFIDENCE_LEVELS",
    "DEPARTURE_TYPES",
    "SIGNING_CLASS_TYPES",
    "TRANSACTION_TYPES",
    "TransactionSchemaError",
    "UnknownTransactionType",
    "duplicate_signing_count",
    "normalize",
    "parse_timestamp",
    "transaction_id",
]


class TransactionSchemaError(ValueError):
    """A row could not be mapped onto the normalized shape.

    Subclasses `ValueError` so `CollectorMetrics.reason_for` classifies it as
    `malformed`, per the Phase 8 failure-handling contract: an upstream that
    renames or drops a field must fail loudly rather than write nulls into an
    append-only lake nobody rewrites.
    """


class UnknownTransactionType(TransactionSchemaError):
    """The upstream used a verb outside the closed vocabulary."""


# The fixed vocabulary from the phase doc. Anything outside it is refused
# rather than bucketed — see the module docstring.
TRANSACTION_TYPES: frozenset[str] = frozenset(
    {
        "signing",
        "waiver_claim",
        "waiver_release",
        "release",
        "trade",
        "ps_signing",
        "ps_elevation",
        "ir_placement",
        "ir_designated_return",
        "activation",
        "suspension",
        "reinstatement",
        "retirement",
    }
)

CONFIDENCE_LEVELS: frozenset[str] = frozenset({"reported", "official"})

# The types that put a player ON a roster, for the duplicate-signing check the
# phase doc names as the assertion that catches double-counted moves.
SIGNING_CLASS_TYPES: frozenset[str] = frozenset(
    {"signing", "waiver_claim", "ps_signing"}
)

# The types that take a player OFF one. An intervening departure is what makes
# a second signing legitimate rather than a duplicate.
DEPARTURE_TYPES: frozenset[str] = frozenset(
    {"waiver_release", "release", "trade", "retirement", "ir_placement", "suspension"}
)

# The phase doc's window: two signing-class rows for the same
# `(player_id, to_team)` inside this span, with nothing in between taking the
# player off that roster, is the reported-then-official duplicate.
DUPLICATE_SIGNING_WINDOW_HOURS = 72

# Bounds a single field's contribution to an error detail, so one pathological
# row cannot write a multi-megabyte lake object.
MAX_DETAIL_CHARS = 200


def parse_timestamp(value: str | None, field: str) -> datetime:
    """An RFC 3339 timestamp, or a loud failure.

    Naive input is refused rather than assumed UTC: guessing a timezone puts a
    wrong instant into an append-only lake, and for this collector the instant
    *is* the signal.
    """
    if not value:
        raise TransactionSchemaError(f"{field} is required and was empty")
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TransactionSchemaError(
            f"{field} is not an RFC 3339 timestamp: {text[:MAX_DETAIL_CHARS]!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise TransactionSchemaError(
            f"{field} has no timezone offset: {text[:MAX_DETAIL_CHARS]!r}"
        )
    return parsed.astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def transaction_id(
    player_id: str,
    transaction_type: str,
    effective_at: datetime,
    to_team: str | None,
) -> str:
    """The stable key for one move, derived from its content.

    Identical across re-publishes and across feeds, which is the whole point —
    see the module docstring. The separator is a character that cannot occur in
    any of the four components, so `("a|b", "c")` and `("a", "b|c")` cannot
    collide.
    """
    material = "\x1f".join(
        [player_id, transaction_type, _rfc3339(effective_at), to_team or ""]
    )
    return "rtx-" + hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _optional_team(value: str | None) -> str | None:
    """An empty team is `None`, not `""`.

    `from_team` is genuinely absent for a free-agent signing and `to_team` for
    a release; an empty string would validate against a `string` schema and
    read downstream as a team code nobody recognises.
    """
    text = (value or "").strip().upper()
    return text or None


def _optional_int(value: str | None, field: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise TransactionSchemaError(
            f"{field} is not an integer: {text[:MAX_DETAIL_CHARS]!r}"
        ) from exc


def _bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "t"}


def _return_window(raw: Mapping[str, str]) -> dict[str, str] | None:
    """`{opens_at, must_activate_by}` for an IR designated-to-return, else None.

    Both halves or neither: a return window with only one end is not a window,
    and emitting it half-populated would let a consumer compute a deadline from
    a missing value.
    """
    opens = (raw.get("return_window_opens_at") or "").strip()
    must = (raw.get("return_window_must_activate_by") or "").strip()
    if not opens and not must:
        return None
    if not (opens and must):
        raise TransactionSchemaError(
            "return_window needs both opens_at and must_activate_by, got "
            f"opens_at={opens[:MAX_DETAIL_CHARS]!r} "
            f"must_activate_by={must[:MAX_DETAIL_CHARS]!r}"
        )
    return {
        "opens_at": _rfc3339(parse_timestamp(opens, "return_window.opens_at")),
        "must_activate_by": _rfc3339(
            parse_timestamp(must, "return_window.must_activate_by")
        ),
    }


def normalize(raw: Mapping[str, str]) -> dict:
    """One upstream row -> one normalized `roster_transaction` signal row.

    Validation happens **before** any mapping, so a renamed or re-valued field
    fails here with a `malformed` classification rather than writing nulls that
    parse cleanly and mean nothing.
    """
    transaction_type = (raw.get("transaction_type") or "").strip().lower()
    if transaction_type not in TRANSACTION_TYPES:
        raise UnknownTransactionType(
            f"transaction_type {transaction_type[:MAX_DETAIL_CHARS]!r} is not in the "
            f"closed vocabulary ({len(TRANSACTION_TYPES)} values)"
        )

    player_id = (raw.get("player_id") or "").strip()
    if not player_id:
        raise TransactionSchemaError("player_id is required and was empty")

    confidence = (raw.get("confidence") or "").strip().lower()
    if confidence not in CONFIDENCE_LEVELS:
        raise TransactionSchemaError(
            f"confidence {confidence[:MAX_DETAIL_CHARS]!r} is not one of "
            f"{sorted(CONFIDENCE_LEVELS)}"
        )

    announced_at = parse_timestamp(raw.get("announced_at"), "announced_at")
    effective_at = parse_timestamp(raw.get("effective_at"), "effective_at")
    to_team = _optional_team(raw.get("to_team"))

    is_void = _bool(raw.get("is_void"))
    void_reason = (raw.get("void_reason") or "").strip() or None
    if is_void and not void_reason:
        # An append-only lake cannot delete a rescinded move, so the retraction
        # row is the only record of *why*. An unexplained void is not usable.
        raise TransactionSchemaError("is_void is true but void_reason is empty")

    return {
        "transaction_id": transaction_id(
            player_id, transaction_type, effective_at, to_team
        ),
        "transaction_type": transaction_type,
        "player_id": player_id,
        "position": (raw.get("position") or "").strip().upper() or None,
        "from_team": _optional_team(raw.get("from_team")),
        "to_team": to_team,
        "announced_at": _rfc3339(announced_at),
        "effective_at": _rfc3339(effective_at),
        "eligible_from_week": _optional_int(
            raw.get("eligible_from_week"), "eligible_from_week"
        ),
        "return_window": _return_window(raw),
        "elevation_count_season": _optional_int(
            raw.get("elevation_count_season"), "elevation_count_season"
        ),
        "confidence": confidence,
        "is_void": is_void,
        "void_reason": void_reason,
        "supersedes": (raw.get("supersedes") or "").strip() or None,
        "source_ref": (raw.get("source_ref") or "").strip() or None,
    }


def duplicate_signing_count(rows: list[dict]) -> int:
    """`(player_id, to_team)` pairs that took two signing-class rows inside the
    window with no intervening departure.

    The phase doc's named failure mode: the same move arrives once as
    `reported` on Monday and once as `official` on Tuesday with a different
    `effective_at`, and the generator counts two signings where one occurred.
    Nothing errors — the reconstructed roster is simply wrong — so this is a
    metric rather than an exception, and it is recorded on every pass including
    zero so it can be alerted on.

    Deliberately keyed on `(player_id, to_team)` rather than `transaction_id`:
    the ids differ precisely *because* the `effective_at` moved, which is why
    plain deduplication cannot see this and a reconciliation check can.
    """
    window = DUPLICATE_SIGNING_WINDOW_HOURS * 3600
    by_player: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("is_void"):
            continue
        by_player.setdefault(row["player_id"], []).append(row)

    duplicates = 0
    for player_rows in by_player.values():
        ordered = sorted(player_rows, key=lambda r: r["effective_at"])
        last_signing: dict[str, datetime] = {}
        for row in ordered:
            effective = parse_timestamp(row["effective_at"], "effective_at")
            kind = row["transaction_type"]
            if kind in DEPARTURE_TYPES:
                # A departure clears the team the player left, and a trade
                # clears the team joined too — either way the next signing for
                # that team is legitimate.
                last_signing.pop(row.get("from_team") or "", None)
                last_signing.pop(row.get("to_team") or "", None)
                continue
            if kind not in SIGNING_CLASS_TYPES:
                continue
            team = row.get("to_team")
            if team is None:
                continue
            previous = last_signing.get(team)
            if previous is not None and (effective - previous).total_seconds() <= window:
                duplicates += 1
            last_signing[team] = effective
    return duplicates
