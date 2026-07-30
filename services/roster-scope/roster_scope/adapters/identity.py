"""The `player-identity` seam.

roster-scope's spec is unambiguous: every slot is filled by a resolved
`player_id`, never a raw name, and *an unresolvable name is counted as a
missing slot rather than skipped*. A skipped row would shrink the numerator
and the denominator together and read as perfect coverage.

`player-identity` is being built in parallel and is not callable yet, so this
module is a seam rather than a client:

- `StubPlayerIdentityResolver` is the default. It mints a deterministic
  `fdy-` id from the attributes it was given, and **refuses rather than
  guesses** when those attributes cannot identify anybody.
- `HttpPlayerIdentityResolver` calls the real collector. Its request and
  response shape is **PROVISIONAL** — see the class docstring. It has not
  been agreed with the team building `player-identity`.
- `build_resolver` picks between them on `PLAYER_IDENTITY_URL`, exactly the
  way weather's `SCHEDULE_URL` is env-overridable.

Swapping the stub for the real thing is therefore a ConfigMap edit plus
whatever the agreed shape turns out to be inside one method — no call site in
`scope.py` or `capture.py` changes.
"""

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

# Empty means "no player-identity deployment to talk to", which is the state
# of the world until that collector ships. Read at call time, not import
# time, so a test (or a redeploy) can change it without reimporting.
PLAYER_IDENTITY_URL_ENV = "PLAYER_IDENTITY_URL"

# Name suffixes held out of the match key. `player-identity` carries these in
# its own `name_suffix` field for the same reason: `Odell Beckham Jr` and
# `Odell Beckham` are one human, and letting the suffix into the key makes
# them two.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

# Apostrophes are *elided*, not replaced with a space: `Ja'Marr` is one token
# and splitting it into `ja marr` would stop it matching a feed that writes
# `JaMarr`. Every other punctuation mark becomes a separator, so `A.J.` and
# `Amon-Ra` split into tokens the way a human reads them.
_ELIDED = re.compile(r"['‘’ʼ]")
_PUNCTUATION = re.compile(r"[^a-z0-9 ]")
_WHITESPACE = re.compile(r"\s+")

# A normalized key shorter than this cannot identify anybody. Two characters
# is the floor, not a tuning knob: initials-only rows ("A", "") are exactly
# the upstream garbage this refuses to turn into a confident id.
MIN_NORMALIZED_KEY_LENGTH = 2


def normalize_name(raw: str) -> str:
    """Lowercased, diacritics-folded, punctuation-stripped, suffix-removed.

    Mirrors `player-identity`'s `normalized_key` field. Lives here rather
    than in `scope.py` because normalization is an identity concern —
    `scope.py` imports it for co-listing order so that the two agree by
    construction rather than by two implementations staying in step.
    """
    elided = _ELIDED.sub("", raw or "")
    folded = unicodedata.normalize("NFKD", elided)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", ascii_only)).strip()
    parts = [p for p in cleaned.split(" ") if p and p not in _SUFFIXES]
    return " ".join(parts)


class UnresolvablePlayer(Exception):
    """The resolver declined to name this player.

    Carries a `reason` because the caller records it verbatim in
    `coverage.missing`/`errors` — "we could not resolve this" and "the chart
    was one receiver short" must not collapse into one undifferentiated hole.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PlayerRef:
    """What roster-scope knows about a chart row before it has an id.

    Team and position are not optional garnish: with them present, the real
    `player-identity` resolves a row *without the name matching at all*,
    which is how book display strings and nicknames resolve. They are also
    what the stub refuses to proceed without.
    """

    name: str
    team: str
    position: str
    jersey_number: int | None = None


@runtime_checkable
class PlayerIdentityResolver(Protocol):
    async def resolve(self, ref: PlayerRef) -> str:
        """Return the canonical `player_id`, or raise `UnresolvablePlayer`."""
        ...


class StubPlayerIdentityResolver:
    """Deterministic stand-in until `player-identity` is callable.

    `fdy-<sha256(normalized_name|team|position)[:12]>`. Deterministic so the
    same chart resolves to the same ids across captures — otherwise every
    capture would mint a new universe and `scope_change_event` would be pure
    noise.

    It refuses rather than guesses. A blank team or position, or a name that
    normalizes below `MIN_NORMALIZED_KEY_LENGTH`, raises rather than hashing
    whatever it was handed: a stable id derived from nothing is worse than an
    absent one, because it looks resolved to every downstream collector.
    """

    async def resolve(self, ref: PlayerRef) -> str:
        if not (ref.team or "").strip() or not (ref.position or "").strip():
            raise UnresolvablePlayer(
                "identity_missing_attributes", f"{ref.name!r} has no team/position"
            )
        key = normalize_name(ref.name)
        if len(key) < MIN_NORMALIZED_KEY_LENGTH:
            raise UnresolvablePlayer("identity_unresolvable_name", repr(ref.name))
        digest = hashlib.sha256(
            f"{key}|{ref.team.strip().upper()}|{ref.position.strip().upper()}".encode()
        ).hexdigest()
        return f"fdy-{digest[:12]}"


class HttpPlayerIdentityResolver:
    """Calls the real `player-identity` collector.

    **PROVISIONAL — NOT AGREED WITH THE `player-identity` AUTHOR.**

    The shape assumed here is:

        GET {base}/resolve?name=&team=&position=&jersey_number=
            Authorization: Bearer <COLLECTOR_TOKEN>
        200 {"candidates": [{"player_id": "fdy-...", "confidence": 0.97}, ...]}

    with the highest-confidence candidate taken, and an empty `candidates`
    list meaning "this went to the miss queue". The Phase 8 spec names
    `GET /resolve` and describes ranked candidates with a `confidence` in
    `[0,1]`, so the route is right; the parameter names, the envelope key,
    and the confidence floor are this module's guess and must be reconciled
    before `PLAYER_IDENTITY_URL` is ever set in a values file.

    Everything outside this class is insulated from that: the contract with
    `scope.py` is `resolve(PlayerRef) -> str | raise UnresolvablePlayer`.
    """

    # A candidate below this is a tie or a coin-flip, and the spec is explicit
    # that a tie goes to the miss queue rather than to the higher score.
    MIN_CONFIDENCE = 0.5

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def resolve(self, ref: PlayerRef) -> str:
        params = {"name": ref.name, "team": ref.team, "position": ref.position}
        if ref.jersey_number is not None:
            params["jersey_number"] = str(ref.jersey_number)
        headers = {}
        token = os.getenv("COLLECTOR_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = await self._client.get(
                f"{self._base_url}/resolve", params=params, headers=headers
            )
            resp.raise_for_status()
            candidates = resp.json().get("candidates") or []
        except Exception as exc:  # noqa: BLE001 — every failure is one missing slot
            raise UnresolvablePlayer("identity_upstream_error", str(exc)) from exc

        best = max(candidates, key=lambda c: c.get("confidence", 0.0), default=None)
        if best is None or best.get("confidence", 0.0) < self.MIN_CONFIDENCE:
            raise UnresolvablePlayer("identity_no_confident_match", ref.name)
        player_id = best.get("player_id")
        if not player_id:
            raise UnresolvablePlayer("identity_malformed_response", ref.name)
        return str(player_id)


def build_resolver(client: httpx.AsyncClient) -> PlayerIdentityResolver:
    """HTTP iff `PLAYER_IDENTITY_URL` is set; the stub otherwise.

    The stub is the default deliberately. `player-identity` does not exist
    yet, and a collector that refused to capture until it did would block the
    whole of 8A on a service it only depends on conceptually.
    """
    base_url = os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip()
    if base_url:
        return HttpPlayerIdentityResolver(client, base_url)
    return StubPlayerIdentityResolver()
