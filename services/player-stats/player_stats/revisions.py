"""Restatement tracking: where `revision` comes from, and what `/revisions` serves.

The spec's failure mode for this collector is not an outage. It is a stat
correction issued days after a game — a reception rescored as a lateral, a
fumble reassigned — silently changing a value the append-only lake already
captured. Two snapshots for the same `(game_id, player_id)` then disagree and
nothing in the envelope says which one wins.

`revision` is the answer, and this collector has to **derive** it: the candidate
upstream publishes no revision marker at all (see `adapters/upstream.py`), and
the spec's adapter note asks for one "if one exists". So the number is computed
against the lake instead:

    unchanged counting stats -> the same revision as last pass
    changed counting stats   -> last pass's revision + 1
    never seen before        -> revision 0

which makes it monotonic per `(game_id, player_id)` **by construction** rather
than by an assertion somebody has to remember. That is also what makes the
spec's second guard meaningful: a row once emitted as `stat_state: final`
cannot change without the revision incrementing, because the same fingerprint
comparison drives both.

The property that actually needs defending is the *other* direction, and it is
the one the spec names outright: "alert on `player_stats_restatements_total`
spiking outside the normal Monday-to-Wednesday window, which usually means an
adapter is re-emitting unchanged rows as new revisions". `_fingerprint` is what
stops that — an unchanged row must produce a byte-identical fingerprint, so a
capture that changes nothing increments nothing.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime

from collector_core.envelope import ENVELOPE_VERSION
from collector_core.lake import LakeWriter
from fastapi import HTTPException

# The blocks a restatement can touch. `rates` and `fantasy_points` are derived
# from these, so including them would be double-counting; `revision` itself
# obviously cannot be part of what decides `revision`.
FINGERPRINTED_BLOCKS: tuple[str, ...] = ("passing", "rushing", "receiving", "misc")

# How many lake snapshots `/revisions` will read for one partition. A weekly
# collector writes one object per pass, so this is generous — but a dispatched
# `POST /refresh` writes one too, and an unbounded scan would let an operator
# turn one HTTP request into thousands of object reads.
MAX_SNAPSHOTS = 200


class SinceFormatError(ValueError):
    """`?since=` was not an RFC 3339 timestamp."""


def parse_since(value: str | None) -> datetime | None:
    """`?since=` to an aware datetime, or `None` for "everything".

    Rejects rather than ignores: a caller who mistypes a timestamp and gets the
    whole history back reads it as "nothing has been restated in the window",
    which is the opposite of the truth.
    """
    if value is None or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SinceFormatError(f"{value!r} is not an RFC 3339 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fingerprint(signal: dict) -> str:
    """A stable digest of one row's counting stats.

    Byte-stability is the whole contract: `json.dumps(..., sort_keys=True)` so
    a dict whose key order changed does not read as a restatement.
    """
    payload = {block: signal.get(block) for block in FINGERPRINTED_BLOCKS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def row_key(signal: dict) -> str:
    """`(game_id, player_id)`, flattened. The unit a revision is counted per."""
    return f"{signal.get('game_id')}|{signal.get('player_id')}"


def load_previous_state(
    lake: LakeWriter, collector: str, signal_type: str, season: int, week: int
) -> dict[str, dict]:
    """`{row_key: {revision, fingerprint, stat_state}}` from the newest snapshot.

    **Synchronous, and deliberately so** — `LakeWriter` is boto3 and the lake
    every collector is handed raises if it is touched from the event loop.
    Callers hand this whole function to `asyncio.to_thread` in one go.

    Only the newest object is read. Revisions accumulate forward, so the latest
    snapshot already carries the highest revision for every key it names, and
    reading the partition's whole history would cost one object read per
    capture that ever ran.

    An absent partition is an empty dict — a first capture, not a failure. A
    lake that *raises* propagates: `capture.py` turns that into a failure
    envelope, because minting revision 0 over rows that already reached
    revision 3 would break the monotonicity every consumer pins against.
    """
    keys = lake.list_keys(collector, signal_type, season, week, ENVELOPE_VERSION)
    if not keys:
        return {}
    body = lake.read(keys[-1])
    return {
        row_key(signal): {
            "revision": int(signal.get("revision") or 0),
            "fingerprint": fingerprint(signal),
            "stat_state": signal.get("stat_state"),
        }
        for signal in body.get("signals") or []
    }


def next_revision(previous: dict[str, dict], signal: dict) -> tuple[int, bool]:
    """`(revision, restated)` for one freshly built row.

    `restated` is True only when a key that was already in the lake changed its
    counting stats. A first sighting is not a restatement, and neither is an
    identical re-capture — which is the behaviour
    `player_stats_restatements_total` is alerted on.
    """
    prior = previous.get(row_key(signal))
    if prior is None:
        return 0, False
    if prior["fingerprint"] == fingerprint(signal):
        return int(prior["revision"]), False
    return int(prior["revision"]) + 1, True


def final_restated(previous: dict[str, dict], signal: dict) -> bool:
    """Whether a row the upstream had certified `final` has changed anyway.

    The spec's hard assertion, kept as an explicit predicate rather than an
    inline condition so it can be tested directly. Dormant while the upstream
    publishes no finality (`stat_state` is `None` — see
    `adapters/upstream.py`), and live the moment one does.
    """
    prior = previous.get(row_key(signal))
    if prior is None or prior.get("stat_state") != "final":
        return False
    return prior["fingerprint"] != fingerprint(signal)


def build_revision_series(
    lake: LakeWriter,
    collector: str,
    signal_type: str,
    season: int,
    week: int,
    since: datetime | None = None,
) -> list[dict]:
    """`GET /revisions` — the `(game_id, player_id, revision)` tuples restated.

    Synchronous for the same reason as `load_previous_state`, and offloaded
    whole by the route rather than one lake call at a time: a prefix scan plus
    a read per snapshot on the event loop would stall every other request,
    including `/health`, for its duration.

    Walks the partition oldest to newest and emits a tuple each time a key's
    revision *increases*. A key's first appearance is not emitted — the
    generator asked what changed, not what exists, and `/signals` already
    answers the latter.
    """
    keys = lake.list_keys(collector, signal_type, season, week, ENVELOPE_VERSION)[
        -MAX_SNAPSHOTS:
    ]
    seen: dict[str, int] = {}
    series: list[dict] = []
    for key in keys:
        body = lake.read(key)
        captured_at = body.get("captured_at")
        for signal in body.get("signals") or []:
            identity = row_key(signal)
            revision = int(signal.get("revision") or 0)
            if identity in seen and revision > seen[identity]:
                series.append(
                    {
                        "game_id": signal.get("game_id"),
                        "player_id": signal.get("player_id"),
                        "revision": revision,
                        "captured_at": captured_at,
                    }
                )
            seen[identity] = max(revision, seen.get(identity, revision))

    if since is None:
        return series
    return [entry for entry in series if _at_or_after(entry["captured_at"], since)]


async def revisions_view(
    spec, *, since: str | None, season: int | None, week: int | None
) -> dict:
    """`GET /revisions`, whole. The route itself is a call and a return.

    `season`/`week` default to the scope the capture loop runs on
    (`CollectorSpec.default_scope`, itself read from `CAPTURE_SEASON`/
    `CAPTURE_WEEK`), so the common call is a bare `GET /revisions?since=...`
    and an operator cannot accidentally ask a different week than the one the
    collector is capturing.

    `build_revision_series` is offloaded **whole** rather than one lake call at
    a time: it does a prefix scan plus one read per snapshot, and running that
    on the event loop would stall every other request — including `/health` —
    for its duration.
    """
    try:
        cutoff = parse_since(since)
    except SinceFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    season = spec.default_scope["season"] if season is None else season
    week = spec.default_scope["week"] if week is None else week
    # Taken off the spec rather than imported from `capture.py`: this collector
    # declares exactly one signal type, and reading it here keeps the
    # capture -> revisions dependency one-way.
    signal_type = spec.signal_types[0]
    series = await asyncio.to_thread(
        build_revision_series, spec.lake, spec.name, signal_type, season, week, cutoff
    )
    return {
        "season": season,
        "week": week,
        "since": since,
        "revisions": series,
        "count": len(series),
    }


def _at_or_after(captured_at: str | None, since: datetime) -> bool:
    """A snapshot with an unparseable `captured_at` is kept.

    Dropping it would hide a restatement behind a formatting problem, and a
    `since` filter that silently loses entries reads as "nothing changed".
    """
    try:
        parsed = parse_since(captured_at)
    except SinceFormatError:
        return True
    return parsed is None or parsed >= since
