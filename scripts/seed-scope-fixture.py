#!/usr/bin/env python3
"""Seed one `scope_membership_weekly` and one `scope_matchup_weekly` envelope.

`roster-scope` ships `CAPTURE_ENABLED=false` (its upstream is a ~37 MB weekly
document, and the Kind cluster is recreated on every CI run — the same load
decision `player-identity` makes for the same reason). So no scope envelope
ever reaches the lake in CI, and every narrowed collector (`usage-share`,
`player-stats`, `injury-report`) then *correctly* fails closed: no scope means
no fetch and zero upstream calls. That is right behaviour, and it will read as
a broken integration test unless something puts a scope in the lake for those
collectors to read.

Two alternatives were rejected: enabling `roster-scope`'s capture in CI (pulls
the real 37 MB document on every push), and having the smoke test POST
`/refresh` (a dispatched refresh reaches the real upstream regardless of
`CAPTURE_ENABLED` — see `helm/values/roster-scope/values.yaml`). This script is
the third option: write the same two envelope shapes `roster-scope` itself
would write, without running any of its resolution logic.

Envelopes are built through `collector_core.envelope` (the same dataclasses
every collector uses), not hand-assembled JSON, so a schema change is caught
here automatically instead of drifting from `roster_scope.scope`/`matchups.py`
silently. `--lake` writes through `collector_core.lake` the same way.

## The member-id decision

Members here are deterministic FIXTURES, never sampled from a real feed:

* **Team defenses use roster-scope's own real, hash-free scheme** —
  `fdy-dst-<team lowercased>` for all 32 teams, exactly matching
  `roster_scope.scope.resolve_membership`'s `team_defense` branch. This costs
  nothing to get exactly right (no hash to reproduce) and cannot drift.
* **Player slots use a hash of an obviously-synthetic anchor** — the literal
  string `"fixture"` plus the slot's own description — in the same
  `fdy-<12 hex>` SHAPE every other collector's id takes, so it passes any
  format check, but it is never derived from a real name or a real upstream
  key, so it cannot collide with (or be mistaken for) a real
  `player-identity` crosswalk hash or another collector's own stub id.

This deliberately does NOT chase any narrowed collector's own id-minting
scheme to try to make rows "really" match in CI. Two facts rule that out:

* `usage-share` and `player-stats` resolve every upstream row FORWARD through
  `player-identity`'s live `/resolve` — but `player-identity` ships
  `CAPTURE_ENABLED=false` too, so its crosswalk is empty in CI. No id this
  script could pick changes that: with an empty crosswalk, every `/resolve`
  call returns unresolved, so no row from either collector can ever land in
  scope no matter what this fixture contains.
* `injury-report` is the one narrowed collector whose capture actually runs
  automatically in CI, and it mints its own ids independently of
  `player-identity` when `PLAYER_IDENTITY_URL` is empty (which it is) — a
  known, documented gap, not something this script should paper over by
  reaching into `injury_report.adapters.identity`'s private stub formula.

So this fixture proves the fail-CLOSED/fail-OPEN boundary — without it, every
narrowed collector correctly reports `scope_unavailable`; with it, capture
runs the narrow-and-filter code path instead of short-circuiting before it —
rather than proving any particular row gets published. See the Task 6 report
for the full reasoning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream

COLLECTOR = "roster-scope"
MEMBERSHIP_SIGNAL = "scope_membership_weekly"
MATCHUP_SIGNAL = "scope_matchup_weekly"
UPSTREAM_ADAPTER = "seed-scope-fixture"

# The 32 real team abbreviations, copied from `roster_scope.rules.TEAMS`
# rather than imported — this platform script does not depend on one
# collector's package, only on the shared `collector_core` library every
# collector already depends on.
TEAMS: tuple[str, ...] = (
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
)  # fmt: skip

# One representative slot per positional rule, seeded for the first two teams
# only. This is a FIXTURE: a real roster-scope capture resolves all 416
# membership slots (608 matchup slots); this script exists to give narrowed
# collectors something to read in CI, not to reproduce the whole universe.
_SAMPLE_MEMBERSHIP_SLOTS: tuple[tuple[str, str, int], ...] = (
    ("qb_depth_le_2", "QB", 1),
    ("qb_depth_le_2", "QB", 2),
    ("rb_depth_le_3", "RB", 1),
    ("wr_depth_le_4", "WR", 1),
    ("te_depth_le_2", "TE", 1),
    ("all_kickers", "K", 1),
)

_SAMPLE_MATCHUP_SLOTS: tuple[tuple[str, str, int], ...] = (
    ("cb_matchup_le_4", "CB", 1),
    ("s_matchup_le_3", "S", 1),
    ("lb_matchup_le_3", "LB", 1),
    ("dl_matchup_le_4", "DL", 1),
    ("ol_matchup_le_5", "OL", 1),
)


def _fixture_player_id(*parts: str) -> str:
    """A well-formed `fdy-<12 hex>` id that cannot collide with a real one.

    Every real `fdy-` id in the fleet is a hash of an anchor rooted in an
    actual upstream key. This is a hash of the literal string `"fixture"`
    plus the slot's own description, so it is deterministic for a given slot
    but, by construction, can never equal the hash of anything a real feed
    or crosswalk could produce.
    """
    digest = hashlib.sha1(":".join(("fixture", *parts)).encode()).hexdigest()
    return f"fdy-{digest[:12]}"


def _membership_rows() -> list[dict]:
    rows: list[dict] = []
    for team in TEAMS:
        # roster-scope's own real scheme for a team-defense slot — no human
        # behind it, so it comes from config rather than a resolver. See
        # roster_scope.scope.resolve_membership's team_defense branch.
        rows.append(
            {
                "player_id": f"fdy-dst-{team.lower()}",
                "entity_type": "team_defense",
                "scope_version": 1,
                "membership_status": "active",
                "rule_id": "all_team_defenses",
                "team": team,
                "position": "DST",
                "depth_rank": 1,
                "previous_depth_rank": None,
                "depth_source_captured_at": None,
                "added_at_version": 1,
                "grace_expires_week": None,
                "is_manual_override": False,
                "override_reason": None,
            }
        )

    for team in TEAMS[:2]:
        for rule_id, position, rank in _SAMPLE_MEMBERSHIP_SLOTS:
            rows.append(
                {
                    "player_id": _fixture_player_id(team, rule_id, str(rank)),
                    "entity_type": "player",
                    "scope_version": 1,
                    "membership_status": "active",
                    "rule_id": rule_id,
                    "team": team,
                    "position": position,
                    "depth_rank": rank,
                    "previous_depth_rank": None,
                    "depth_source_captured_at": None,
                    "added_at_version": 1,
                    "grace_expires_week": None,
                    "is_manual_override": False,
                    "override_reason": None,
                }
            )
    return rows


def _matchup_rows() -> list[dict]:
    rows: list[dict] = []
    for team in TEAMS[:2]:
        for rule_id, position, rank in _SAMPLE_MATCHUP_SLOTS:
            rows.append(
                {
                    "player_id": _fixture_player_id(
                        "matchup", team, rule_id, str(rank)
                    ),
                    "slot_key": f"{team}:{rule_id}:{rank}",
                    "rule_id": rule_id,
                    "team": team,
                    "position": position,
                    "depth_rank": rank,
                }
            )
    return rows


def build_envelopes(season: int, week: int, *, now: datetime) -> dict[str, Envelope]:
    """Both scope envelopes for `(season, week)`, built through the same
    `collector_core.envelope` dataclasses every collector's own capture uses.
    """
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref="scripts/seed-scope-fixture.py",
    )
    scope = {"season": season, "week": week, "scope_version": 1}

    membership_rows = _membership_rows()
    matchup_rows = _matchup_rows()

    return {
        MEMBERSHIP_SIGNAL: Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR,
            signal_type=MEMBERSHIP_SIGNAL,
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=Coverage(
                expected=len(membership_rows),
                present=len(membership_rows),
                missing=[],
            ),
            errors=[],
            signals=membership_rows,
        ),
        MATCHUP_SIGNAL: Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR,
            signal_type=MATCHUP_SIGNAL,
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=Coverage(
                expected=len(matchup_rows), present=len(matchup_rows), missing=[]
            ),
            errors=[],
            signals=matchup_rows,
        ),
    }


def write_to_disk(envelopes: dict[str, Envelope], out_dir: Path) -> None:
    """One flat JSON file per signal type. Disk mode needs nothing beyond
    `collector_core.envelope` — no `boto3`, so it stays usable wherever that
    is not installed (this is also what `tests/test_seed_scope_fixture.py`
    exercises, via a subprocess with no `--lake`-specific dependency)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for signal_type, envelope in envelopes.items():
        path = out_dir / f"{signal_type}.json"
        path.write_text(json.dumps(envelope.to_dict(), sort_keys=True, indent=2))
        print(f"wrote {path}")


def write_to_lake(envelopes: dict[str, Envelope]) -> None:
    """Both envelopes into the real lake, keyed the same way a genuine
    roster-scope capture would key them (`collector_core.lake.lake_key`).

    Imported here, not at module level, so `--out` mode never needs `boto3`
    importable at all — see the module and `write_to_disk` docstrings.
    """
    import os

    from collector_core.lake import S3LakeWriter

    bucket = os.getenv("LAKE_BUCKET", "").strip()
    if not bucket:
        # NOT the same silent fallback `collector_core.lake.
        # build_lake_writer_from_env` uses for a collector (a NullLakeWriter,
        # so a collector without a lake still starts). This script's entire
        # job is to put a scope in a REAL lake; discarding the write silently
        # would be the exact "reads as broken, for a reason nobody can see"
        # failure this script exists to prevent, just moved one step earlier.
        print(
            "LAKE_BUCKET is empty -- refusing to silently discard the seed. "
            "Set LAKE_BUCKET/LAKE_ENDPOINT_URL (see the 'Seed a scope "
            "fixture' step in .github/workflows/integration-test.yml).",
            file=sys.stderr,
        )
        raise SystemExit(2)

    endpoint = os.getenv("LAKE_ENDPOINT_URL") or None
    import boto3

    client = boto3.client("s3", endpoint_url=endpoint)
    lake = S3LakeWriter(bucket, client)
    for envelope in envelopes.values():
        key = lake.write(envelope)
        print(f"wrote {key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--out",
        type=Path,
        help="Write both envelopes as JSON files under this directory.",
    )
    target.add_argument(
        "--lake",
        action="store_true",
        help="Write both envelopes into the lake (LAKE_BUCKET/LAKE_ENDPOINT_URL).",
    )
    args = parser.parse_args(argv)

    now = datetime.now(tz=UTC)
    envelopes = build_envelopes(args.season, args.week, now=now)

    if args.lake:
        write_to_lake(envelopes)
    else:
        write_to_disk(envelopes, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
