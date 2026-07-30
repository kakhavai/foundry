"""Sleeper players adapter — the upstream, and its schema assertions.

Sleeper's players document doubles as the Tier-1 published crosswalk: each
record carries `gsis_id`, `espn_id`, `yahoo_id` and friends alongside
Sleeper's own key, so adopting it gets the crosswalk and the roster in one
fetch.

Validation happens at **two levels**, and both are load-bearing:

1. **Document level** — the payload is a top-level object, and every
   declared crosswalk source key appears on at least one record. A rename of
   `gsis_id` upstream is otherwise completely silent: every record still
   parses, every field still maps, and every Tier-1 link simply disappears.
2. **Per record** — the structural keys are *present*, though they may be
   null. This is the one that catches `number` being renamed to `jersey`,
   which `record.get("number")` would happily read as `None` and write into
   the lake as `jersey_number: null` for all ~2,900 players.

A validation failure fails the capture loudly with `reason=schema` rather
than mapping nulls into an append-only lake that is never rewritten.
"""

import os

import httpx

PLAYERS_URL = os.getenv("PLAYERS_URL", "https://api.sleeper.app/v1/players/nfl")

# Present-though-nullable. Absence means the upstream's shape moved.
REQUIRED_RECORD_KEYS = frozenset(
    {
        "player_id",
        "first_name",
        "last_name",
        "full_name",
        "position",
        "team",
        "number",
        "status",
        "birth_date",
        "years_exp",
    }
)


class UpstreamSchemaError(ValueError):
    """The upstream's shape moved.

    Subclasses `ValueError` so `CollectorMetrics.reason_for` classifies it as
    `malformed`; `capture` labels the envelope error `schema` specifically,
    which is the narrower fact an operator needs.
    """


async def fetch_players(client: httpx.AsyncClient) -> tuple[dict, str | None]:
    """Fetch the players document. Returns `(payload, source_ref)`.

    `source_ref` is the response ETag — the upstream's own opaque cursor for
    this version of the document, which is exactly what the envelope's
    `upstream.source_ref` is for. `None` when the upstream sends no ETag,
    rather than a fabricated stand-in.
    """
    response = await client.get(PLAYERS_URL)
    response.raise_for_status()
    return response.json(), response.headers.get("etag")


def validate_document(payload, crosswalk_keys: dict[str, str]) -> None:
    """Document-level assertions. Raises `UpstreamSchemaError`."""
    if not isinstance(payload, dict) or not payload:
        raise UpstreamSchemaError(
            "players document must be a non-empty top-level object, got "
            f"{type(payload).__name__}"
        )

    records = [r for r in payload.values() if isinstance(r, dict)]
    if not records:
        raise UpstreamSchemaError("players document contains no record objects")

    absent = sorted(
        key
        for key in crosswalk_keys
        if not any(record.get(key) not in (None, "") for record in records)
    )
    if absent:
        raise UpstreamSchemaError(
            "crosswalk source key(s) absent from every record — a rename "
            f"upstream would silently drop every Tier-1 link: {', '.join(absent)}"
        )


def record_schema_errors(record) -> list[str]:
    """Per-record structural check. Returns the missing keys, or `[]`."""
    if not isinstance(record, dict):
        return ["<record is not an object>"]
    return sorted(REQUIRED_RECORD_KEYS - set(record))
