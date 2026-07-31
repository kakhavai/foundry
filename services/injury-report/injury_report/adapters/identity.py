"""The `player-identity` seam, kept to the smallest thing that is honest.

Every row this collector emits carries a canonical `player_id`, and
`player-identity` is the collector that decides what one is. Until
`PLAYER_IDENTITY_URL` names a deployment, ids are minted deterministically from
the upstream's own stable player key.

Deterministic matters more than it looks: `designation_history` and
`practice_participation` are appended to across a week, so an id that changed
between passes would turn one player's week into three unrelated players and
the trajectory this collector exists to publish would be lost.

**It refuses rather than guesses.** A row with no upstream player key is
dropped and counted in `errors`, never hashed from a display name — the phase
doc's failure mode for the neighbouring collector is a well-formed injury item
attached to the wrong player, and a confident id derived from nothing is how
that happens.

**This is duplicated work and should not stay that way.** `roster-scope` ships
a fuller version of this seam (`roster_scope/adapters/identity.py`) with name
normalization and a provisional HTTP client. A `PlayerIdentityResolver` belongs
in `collector-core` so the fleet resolves identically rather than twenty-four
times approximately; it is deliberately not moved here, mid-wave, while other
collectors are being written against the current library.
"""

import hashlib
import os

# Empty means "no player-identity deployment to talk to". Read at call time so
# a redeploy changes behaviour without a reimport.
PLAYER_IDENTITY_URL_ENV = "PLAYER_IDENTITY_URL"


class UnresolvablePlayer(ValueError):
    """The resolver declined to name this player.

    Carries a `reason` because the caller records it verbatim: "we could not
    resolve this" and "the club filed no report" are different holes and must
    not collapse into one.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def resolve_player_id(external_id: str) -> str:
    """`fdy-<sha256(external_id)[:12]>`, or raise.

    When `PLAYER_IDENTITY_URL` is set this is where the call to
    `player-identity`'s `GET /resolve` goes; nothing outside this function
    changes when it does, because the contract is
    `str -> str | raise UnresolvablePlayer`.
    """
    if os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip():
        # Not built. Failing loudly beats silently minting stub ids against a
        # cluster that was configured to expect real ones — those ids would
        # join to nothing and every downstream lookup would miss.
        raise UnresolvablePlayer(
            "identity_resolver_unavailable",
            f"{PLAYER_IDENTITY_URL_ENV} is set but the HTTP resolver is not built",
        )
    key = (external_id or "").strip()
    if not key:
        raise UnresolvablePlayer("identity_missing_external_id")
    return f"fdy-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
