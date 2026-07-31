"""The `player-identity` seam.

roster-scope's spec is unambiguous: every slot is filled by a resolved
`player_id`, never a raw name, and *an unresolvable name is counted as a
missing slot rather than skipped*. A skipped row would shrink the numerator
and the denominator together and read as perfect coverage.

Two implementations behind one Protocol:

- `StubPlayerIdentityResolver` is the default, selected when
  `PLAYER_IDENTITY_URL` is empty. It mints a deterministic `fdy-` id from the
  attributes it was given, and **refuses rather than guesses** when those
  attributes cannot identify anybody.
- `HttpPlayerIdentityResolver` calls the real collector, whose `GET /resolve`
  it is now reconciled against — see the class docstring. It **obeys that
  endpoint's `resolved` flag** and does no ranking of its own.
- `build_resolver` picks between them on `PLAYER_IDENTITY_URL`, exactly the
  way weather's `SCHEDULE_URL` is env-overridable.

Both refuse in the same shape, and that is the point of the seam: whichever is
selected, an identity that cannot be established becomes an
`UnresolvablePlayer` carrying a reason, never a plausible-looking id. Turning
the HTTP resolver on is a ConfigMap edit — no call site in `scope.py` or
`capture.py` changes.
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


# `player-identity` names four: `no_candidate`, `below_threshold`,
# `insufficient_agreeing_attributes`, `ambiguous`. They are carried through
# verbatim rather than collapsed, because "we could not tell two players apart"
# and "nobody by that name" are different operational problems. Sanitised
# anyway: the value arrives from a network peer and lands in an append-only
# errors array, so it must not be able to widen that vocabulary arbitrarily.
_REASON_ALLOWED = re.compile(r"[^a-z0-9_]+")
MAX_UPSTREAM_REASON_LENGTH = 40


def _upstream_reason(raw) -> str:
    cleaned = _REASON_ALLOWED.sub("_", str(raw or "").lower()).strip("_")
    return cleaned[:MAX_UPSTREAM_REASON_LENGTH] or "unknown"


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

    Reconciled against that service's actual `GET /resolve` — see
    `services/player-identity/player_identity/{main,api,resolution}.py`:

        GET {base}/resolve?name=&team=&position=&jersey_number=
            Authorization: Bearer <COLLECTOR_TOKEN>
        200 {"resolved": true,  "player_id": "fdy-...", "reason": null,
             "link_method": "attribute_score", "confidence": 0.77,
             "candidates": [...]}
        200 {"resolved": false, "player_id": null, "reason": "ambiguous",
             "link_method": null, "confidence": 0.97, "candidates": [...]}

    **`resolved` is the answer. `candidates` is the working.** That distinction
    is the whole contract, and inverting it is how this class was wrong: it
    used to ignore `resolved` entirely, re-rank `candidates` against a local
    0.5 floor, and take the winner.

    `player-identity` populates `candidates` *precisely when it has decided not
    to resolve* — a `resolved: false` response carries the rows it would not
    choose between, and it has already filed the query in its own standing
    name-resolution miss queue. So the old code adopted an identity the
    collector that owns identity deliberately refused, and did it hardest in
    the cases that matter most: `ambiguous` means two records scored within
    `MARGIN` of each other, both typically well above 0.9, and picking the
    higher one is exactly the tie-break `player-identity` exists to refuse.
    `insufficient_agreeing_attributes` means a sparse record cleared the
    threshold on a single agreeing attribute (0.30/0.45 = 0.667) — confident
    looking, and nothing of the kind.

    A wrong `player_id` is not a recoverable error. It propagates into an
    append-only lake that is never rewritten, and every downstream collector
    reads it as settled.

    **There is no local confidence floor, deliberately.** `player-identity`
    owns `THRESHOLD`, `MARGIN` and `MIN_AGREEING_ATTRIBUTES`. A second, weaker
    floor here could never *tighten* the decision — a resolved
    `attribute_score` is already >= 0.60 and an adopted crosswalk or exact-id
    link is 1.0, both above 0.5 — so its only possible effect was to loosen a
    refusal. Deleting it is the fix; tuning it would have kept the inversion
    and moved the threshold.

    The refusal is made **visible**, not swallowed: the upstream's own reason
    is carried into `UnresolvablePlayer.reason`, which `scope.py` records with
    `acc.fail(...)`, so the slot lands in `coverage.missing` with a reason
    naming why identity declined. Never a skipped row — a skipped row shrinks
    numerator and denominator together and reads as perfect coverage.

    Everything outside this class is insulated: the contract with `scope.py` is
    `resolve(PlayerRef) -> str | raise UnresolvablePlayer`.
    """

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
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 — every failure is one missing slot
            raise UnresolvablePlayer("identity_upstream_error", str(exc)) from exc

        # Anything that is not explicitly `resolved: true` is a refusal. An
        # absent field is not permission — a body that has lost the flag is a
        # body this collector cannot reason about.
        if body.get("resolved") is not True:
            raise UnresolvablePlayer(
                f"identity_unresolved_{_upstream_reason(body.get('reason'))}",
                ref.name,
            )

        player_id = body.get("player_id")
        if not player_id:
            # `resolved: true` with no id is a contract violation, not a licence
            # to go looking in `candidates` for one.
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
