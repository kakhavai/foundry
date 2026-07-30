"""The scope configuration: which slots the scope *demands*.

Deliberately separate from `scope.py`. This module is config and nothing
else — the 32 teams, the positional quotas, the two blanket rules, the
manual overrides, and the derived slot key set. `scope.py` is resolution:
turning these declarations into filled slots against a live depth chart.

Widening the universe to `WR<=5` is one edit here, and every downstream
collector inherits it, because every downstream collector fetches
`GET /scope/players` before it pulls anything.

The separation is what keeps `coverage.expected` honest. `expected_slots()`
is computed from this file alone, *before* any upstream is contacted, so a
depth chart that yields three receivers under a `WR<=4` rule contributes one
`coverage.missing` entry rather than quietly shrinking the denominator.
"""

from dataclasses import dataclass

# Canonical team abbreviations. All 32, always — the scope is defined by
# config, not by whichever teams an upstream happened to return today.
TEAMS: tuple[str, ...] = (
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
)  # fmt: skip

# Upstream team labels that are not the canonical abbreviation. Relocations
# and the two Los Angeles clubs are the whole list; an unknown label is
# dropped rather than guessed at, and its slots then read as missing.
TEAM_ALIASES: dict[str, str] = {
    "JAC": "JAX",
    "LA": "LAR",
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
    "WSH": "WAS",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
}

# Upstream position labels collapsed into canonical groups. Depth charts are
# published for media obligations, not for us: the same receiver is `WR` on
# one chart, `SE`/`FL`/`X`/`Z`/`SLOT` on another. Collapsing them is
# mandatory per the Phase 8 spec's adapter notes.
POSITION_ALIASES: dict[str, str] = {
    "QB": "QB",
    "RB": "RB", "HB": "RB", "TB": "RB",
    "WR": "WR", "SE": "WR", "FL": "WR", "SLOT": "WR", "X": "WR", "Z": "WR",
    "TE": "TE", "Y": "TE",
    "K": "K", "PK": "K",
}  # fmt: skip


@dataclass(frozen=True)
class DepthRule:
    """One config rule: how many slots a position is worth, per team."""

    rule_id: str
    position: str
    max_depth: int
    # A `team_defense` slot has no human behind it, so it is filled from
    # config rather than from the depth chart. That is why a total upstream
    # outage still yields 32 present slots rather than zero.
    entity_type: str = "player"


# The positional quotas. `QB<=2, RB<=3, WR<=4, TE<=2` per team.
POSITION_RULES: tuple[DepthRule, ...] = (
    DepthRule("qb_depth_le_2", "QB", 2),
    DepthRule("rb_depth_le_3", "RB", 3),
    DepthRule("wr_depth_le_4", "WR", 4),
    DepthRule("te_depth_le_2", "TE", 2),
)

# Every kicker and all 32 team defenses — blanket rules, one slot per team.
KICKER_RULE = DepthRule("all_kickers", "K", 1)
TEAM_DEFENSE_RULE = DepthRule("all_team_defenses", "DST", 1, entity_type="team_defense")

ALL_RULES: tuple[DepthRule, ...] = (
    *POSITION_RULES,
    KICKER_RULE,
    TEAM_DEFENSE_RULE,
)

# How many weeks a player who falls out of the rule window keeps being
# fetched. Depth charts churn on Tuesday and revert on Friday; a player out
# of scope for three days leaves a hole in a partially captured week that a
# real-time upstream can never backfill.
GRACE_WEEKS = 2

# `depth_source_captured_at` must be under this for every team. One team's
# frozen chart is invisible in an aggregate freshness average, so this is
# checked per team and surfaced as a count.
MAX_DEPTH_CHART_AGE_HOURS = 48


@dataclass(frozen=True)
class ManualOverride:
    """Config pinning a named player into a specific slot regardless of what
    the depth chart says. `is_manual_override`/`override_reason` on the
    emitted row exist for exactly this, and `reason` is mandatory."""

    team: str
    rule_id: str
    rank: int
    player_name: str
    reason: str

    @property
    def slot_key(self) -> str:
        return slot_key(self.team, self.rule_id, self.rank)


# Pin-in overrides. Empty in the committed config — an override is an
# operator action recorded in a PR diff, never a default.
MANUAL_OVERRIDES: tuple[ManualOverride, ...] = ()

# Pin-out: normalized names the chart may list but the scope must not admit
# (season-ending injury an upstream has not caught up with, say). Keyed by
# `adapters.identity.normalize_name` output. Empty for the same reason.
EXCLUDED_PLAYERS: dict[str, str] = {}


def canonical_team(raw: str) -> str | None:
    """Canonical abbreviation, or None when the label is not a known team."""
    label = (raw or "").strip().upper()
    label = TEAM_ALIASES.get(label, label)
    return label if label in TEAMS else None


def canonical_position(raw: str) -> str | None:
    """Canonical position group, or None for a label outside the scope's
    rules. Returning None (rather than passing the raw label through) is what
    keeps a linebacker off a `WR<=4` slot."""
    return POSITION_ALIASES.get((raw or "").strip().upper())


def slot_key(team: str, rule_id: str, rank: int) -> str:
    """`<team>:<rule_id>:<rank>` — the coverage accounting unit.

    Rank is part of the key, not just the position: `WR3` going missing is a
    different fact from `WR1` going missing, and a key that omitted the rank
    would let a chart returning four copies of the same receiver read as
    complete.
    """
    return f"{team}:{rule_id}:{rank}"


def expected_slots() -> tuple[str, ...]:
    """Every slot the config demands, derived from this file alone.

    32 teams x (QB 2 + RB 3 + WR 4 + TE 2 + K 1 + DST 1) = 32 x 13 = **416**.

    The Phase 8 spec's prose says "roughly 350"; its own quotas arithmetically
    give 416, and the quotas are the normative part — the spec states
    `coverage.expected` is "the number of slots the config demands - sum over
    32 teams of each positional quota, plus one kicker and one team defense
    per team". 416 is that sum. The discrepancy is called out in the PR body
    rather than silently reconciled by fudging a quota.

    Built before any upstream is contacted. This is the single rule that
    makes the collector's characteristic failure visible: `expected` must
    never be derived from what succeeded, or a stale chart that omits the
    correct player reports 100% coverage across the entire downstream fleet.
    """
    return tuple(
        slot_key(team, rule.rule_id, rank)
        for team in TEAMS
        for rule in ALL_RULES
        for rank in range(1, rule.max_depth + 1)
    )


def rule_by_id(rule_id: str) -> DepthRule | None:
    for rule in ALL_RULES:
        if rule.rule_id == rule_id:
            return rule
    return None
