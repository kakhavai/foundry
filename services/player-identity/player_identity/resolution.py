"""How a raw name becomes a canonical record: the index, the tiers, the
weighted score, and the standing miss queue.

`identity.py` owns what a record *is*; this module owns the matching. The
three tiers are deliberately distinct and must not be collapsed:

    Tier 1  a *published* crosswalk id (gsis, espn, yahoo, ...). Adopted, not
            matched — no scoring logic runs at all.
    Tier 2  exact match on an upstream's *own* stable id already seen and
            recorded in `external_ids` under a non-crosswalk source.
    Tier 3  weighted multi-attribute *agreement*, below.

Tiers 1 and 2 read from two separate maps rather than one, so a
non-crosswalk source can never be served as an adopted crosswalk link by
accident — the distinction survives in the data structure, not just in a
comment.

There is no LLM/AI resolver here, and there is not going to be one: the
Phase 8 spec rejects it explicitly, and every case one would be reached for
is a case where "refuse and queue the miss" is the correct answer.

Tier 3 scores agreement, not equality. A strict AND across attributes is
brittle in exactly the fields it depends on — a player traded on Tuesday
agrees with a book's Wednesday row on name, position, and number but
disagrees on team, and an equality key drops a row it should have resolved.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .identity import CROSSWALK_SOURCES

# Sum to 1.0. Name is the single heaviest attribute but deliberately not a
# majority: with team, position, and number present a row resolves without
# the name matching at all, which is how book display strings ("Hollywood
# Brown", "CMC", "Deebo") resolve with no fuzzy string handling anywhere.
WEIGHTS: dict[str, float] = {
    "normalized_key": 0.30,
    "team": 0.20,
    "jersey_number": 0.20,
    "position_group": 0.15,
    "entry_year": 0.075,
    "birth_date": 0.075,
}

THRESHOLD = 0.60
MARGIN = 0.10

# A link additionally requires this many attributes to actually agree. The
# score alone is not enough: a *sparse* canonical record (a free agent with
# no team and no jersey number) has a small denominator, so a name-only
# query can clear 0.60 on a single agreeing attribute — 0.30/0.45 = 0.667
# looks like a confident link and is nothing of the kind.
MIN_AGREEING_ATTRIBUTES = 2

# Every attribute in the key is a time-varying fact. `team` and
# `jersey_number` are the two that move mid-season, so an older `as_of`
# discounts them — linearly over the horizon, down to a floor rather than to
# zero, because a month-old team is weak evidence, not no evidence.
STALENESS_HORIZON = timedelta(days=30)
STALENESS_FLOOR = 0.25
STALE_AS_OF_FIELD = {"team": "team_as_of", "jersey_number": "jersey_as_of"}

MAX_BATCH_QUERIES = 500


def _parse_instant(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def staleness_discount(as_of: datetime | None, reference: datetime) -> float:
    """Linear decay from 1.0 at zero age to `STALENESS_FLOOR` at the horizon.

    An absent `as_of` is treated as fully stale rather than fully fresh: a
    record that will not say when its team was true has not earned the
    weight of one that will.
    """
    if as_of is None:
        return STALENESS_FLOOR
    age = (reference - as_of).total_seconds()
    if age <= 0:
        return 1.0
    fraction = age / STALENESS_HORIZON.total_seconds()
    return max(STALENESS_FLOOR, 1.0 - (1.0 - STALENESS_FLOOR) * fraction)


@dataclass(frozen=True)
class ResolveQuery:
    """One row a caller wants turned into a `player_id`."""

    raw_name: str | None = None
    normalized_key: str | None = None
    team: str | None = None
    position: str | None = None
    position_group: str | None = None
    jersey_number: int | None = None
    entry_year: int | None = None
    birth_date: str | None = None
    season: int | None = None
    source: str | None = None
    source_id: str | None = None
    # The instant the row being resolved describes. Staleness is measured
    # against this, not against now: backfilling a week-4 slate has to
    # compare against week-4 truth.
    as_of: datetime | None = None

    def value_for(self, attribute: str):
        return getattr(self, attribute, None)

    def context(self) -> dict:
        return {
            "team": self.team,
            "position": self.position,
            "jersey_number": self.jersey_number,
            "entry_year": self.entry_year,
            "birth_date": self.birth_date,
            "season": self.season,
        }


@dataclass(frozen=True)
class Scored:
    player_id: str
    row: dict
    score: float
    agreeing: tuple[str, ...]
    disagreeing: tuple[str, ...]
    # Attributes the record could be compared on but the caller did not
    # supply. Distinct from `disagreeing`: absence is the caller's gap, and
    # folding it into the disagreement label would make
    # `identity_resolution_failures_total{attribute=...}` unreadable.
    absent: tuple[str, ...]


@dataclass(frozen=True)
class Resolution:
    resolved: bool
    player_id: str | None
    link_method: str | None
    confidence: float
    margin: float | None
    candidates: tuple[Scored, ...]
    reason: str | None
    near_miss: bool = False


def score_row(row: dict, query: ResolveQuery, *, reference: datetime) -> Scored:
    """Score one canonical row against one query.

    The denominator is the weight of every attribute *the record* carries a
    non-null value for — not the intersection of record and query. That is
    deliberate, and it is what makes the two worked cases in the Phase 8
    spec come out where they do:

        traded-Tuesday row (name+position+number agree, team disagrees)
            0.65 / 0.85 = 0.765  -> resolves
        no-name book string (team+position+number agree)
            0.55 / 0.85 = 0.647  -> resolves

    Intersecting instead would score the second row 0.55/0.55 = 1.0 — a
    perfect score for a query that never supplied a name at all. A query
    that withholds an attribute the record *could* have been checked on is
    less certain, and the denominator is where that shows up.
    """
    numerator = 0.0
    denominator = 0.0
    agreeing: list[str] = []
    disagreeing: list[str] = []
    absent: list[str] = []

    for attribute, base_weight in WEIGHTS.items():
        record_value = row.get(attribute)
        if record_value is None or record_value == "":
            continue  # not comparable: the record has nothing to compare to

        weight = base_weight
        as_of_field = STALE_AS_OF_FIELD.get(attribute)
        if as_of_field is not None:
            as_of = _parse_instant(row.get(as_of_field))
            weight *= staleness_discount(as_of, reference)

        # The discount lands on the denominator as well as the numerator: a
        # stale attribute must carry less weight in *either* direction, or
        # discounting it would quietly punish records that disclose an
        # `as_of` at all.
        denominator += weight

        query_value = query.value_for(attribute)
        if query_value is None or query_value == "":
            absent.append(attribute)
        elif query_value == record_value:
            numerator += weight
            agreeing.append(attribute)
        else:
            disagreeing.append(attribute)

    score = 0.0 if denominator == 0 else numerator / denominator
    return Scored(
        player_id=row.get("player_id", ""),
        row=row,
        score=score,
        agreeing=tuple(agreeing),
        disagreeing=tuple(disagreeing),
        absent=tuple(absent),
    )


def _no_candidate() -> Resolution:
    return Resolution(
        resolved=False,
        player_id=None,
        link_method=None,
        confidence=0.0,
        margin=None,
        candidates=(),
        reason="no_candidate",
    )


class ResolutionIndex:
    """The in-memory index `/resolve` reads and `capture` replaces.

    One mutable object constructed in `main.py` and bound into `capture`
    with `functools.partial`, so the capture loop and the resolve routes
    share exactly one index without either reaching for a module-level
    global.
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._by_crosswalk: dict[tuple[str, str], str] = {}
        self._by_upstream: dict[tuple[str, str], str] = {}
        self._by_key: dict[str, list[dict]] = {}
        self._by_team_jersey: dict[tuple[str, int], list[dict]] = {}

    def __len__(self) -> int:
        return len(self._rows)

    def replace(self, rows: Iterable[dict]) -> list[dict]:
        """Swap in a freshly captured set of rows.

        Returns the injectivity violations found while indexing: each
        `external_ids.<source>.id` must map to exactly one `player_id`. A
        violation means two canonical records claim the same upstream
        identity — a bad merge — and it is the cheapest invariant in the
        whole collector to check.
        """
        self._rows = {}
        self._by_crosswalk = {}
        self._by_upstream = {}
        self._by_key = {}
        self._by_team_jersey = {}
        conflicts: list[dict] = []

        for row in rows:
            player_id = row.get("player_id")
            if not player_id:
                continue
            self._rows[player_id] = row

            key = row.get("normalized_key")
            if key:
                self._by_key.setdefault(key, []).append(row)

            team, jersey = row.get("team"), row.get("jersey_number")
            if team and jersey is not None:
                self._by_team_jersey.setdefault((team, jersey), []).append(row)

            for source, link in (row.get("external_ids") or {}).items():
                bucket = (
                    self._by_crosswalk
                    if source in CROSSWALK_SOURCES
                    else self._by_upstream
                )
                external = (source, str(link.get("id")))
                incumbent = bucket.get(external)
                if incumbent is not None and incumbent != player_id:
                    conflicts.append(
                        {
                            "source": source,
                            "external_id": external[1],
                            "player_ids": sorted([incumbent, player_id]),
                        }
                    )
                    continue
                bucket[external] = player_id

        return conflicts

    def row(self, player_id: str) -> dict | None:
        return self._rows.get(player_id)

    def _tier1(self, query: ResolveQuery) -> Resolution | None:
        """Published crosswalk id — adopted, never scored."""
        if not query.source or not query.source_id:
            return None
        if query.source not in CROSSWALK_SOURCES:
            return None
        player_id = self._by_crosswalk.get((query.source, str(query.source_id)))
        return None if player_id is None else self._exact(player_id, "crosswalk")

    def _tier2(self, query: ResolveQuery) -> Resolution | None:
        """An upstream's own stable id already linked under a non-crosswalk
        source. Distinct from Tier 1: this link was *earned* previously, and
        the map it lives in is a different one."""
        if not query.source or not query.source_id:
            return None
        if query.source in CROSSWALK_SOURCES:
            return None
        player_id = self._by_upstream.get((query.source, str(query.source_id)))
        return None if player_id is None else self._exact(player_id, "exact_id")

    def _exact(self, player_id: str, link_method: str) -> Resolution:
        row = self._rows[player_id]
        return Resolution(
            resolved=True,
            player_id=player_id,
            link_method=link_method,
            confidence=1.0,
            margin=None,
            candidates=(
                Scored(
                    player_id=player_id,
                    row=row,
                    score=1.0,
                    agreeing=(),
                    disagreeing=(),
                    absent=(),
                ),
            ),
            reason=None,
        )

    def _candidates(self, query: ResolveQuery) -> list[dict]:
        """Rows worth scoring: same match key, or same team-and-number.

        The second half is what lets a no-name book string reach a candidate
        at all — without it, Tier 3 could only ever resolve rows that
        already carried a name, which is the case it exists to handle.
        """
        seen: dict[str, dict] = {}
        if query.normalized_key:
            for row in self._by_key.get(query.normalized_key, []):
                seen[row["player_id"]] = row
        if query.team and query.jersey_number is not None:
            for row in self._by_team_jersey.get((query.team, query.jersey_number), []):
                seen[row["player_id"]] = row
        return list(seen.values())

    def resolve(
        self, query: ResolveQuery, *, now: datetime, limit: int = 5
    ) -> Resolution:
        for tier in (self._tier1, self._tier2):
            hit = tier(query)
            if hit is not None:
                return hit

        reference = query.as_of or now
        scored = sorted(
            (
                score_row(row, query, reference=reference)
                for row in self._candidates(query)
            ),
            key=lambda s: (-s.score, s.player_id),
        )[:limit]

        if not scored:
            return _no_candidate()

        best = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None
        margin = None if runner_up is None else round(best.score - runner_up.score, 6)

        if best.score < THRESHOLD:
            reason = "below_threshold"
        elif len(best.agreeing) < MIN_AGREEING_ATTRIBUTES:
            reason = "insufficient_agreeing_attributes"
        elif margin is not None and margin < MARGIN:
            # A tie goes to the miss queue, never to the higher score. This
            # is what makes the multi-attribute key safe rather than merely
            # accurate.
            reason = "ambiguous"
        else:
            reason = None

        resolved = reason is None
        return Resolution(
            resolved=resolved,
            player_id=best.player_id if resolved else None,
            link_method="attribute_score" if resolved else None,
            confidence=best.score,
            margin=margin,
            candidates=tuple(scored),
            reason=reason,
            near_miss=reason == "ambiguous",
        )


@dataclass
class Miss:
    raw_name: str
    source: str | None
    context: dict
    best_candidate_player_id: str | None
    match_score: float | None
    match_margin: float | None
    disagreeing_attributes: list[str]
    reason: str | None
    first_seen_at: str
    last_seen_at: str
    occurrence_count: int = 1

    def to_signal(self) -> dict:
        return {
            "raw_name": self.raw_name,
            "source": self.source,
            "context": dict(self.context),
            "best_candidate_player_id": self.best_candidate_player_id,
            "match_score": self.match_score,
            "match_margin": self.match_margin,
            "disagreeing_attributes": list(self.disagreeing_attributes),
            "reason": self.reason,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "occurrence_count": self.occurrence_count,
        }


class MissQueue:
    """The standing record of names that would not resolve.

    Shared between the resolve routes (which populate it) and `capture`
    (which emits it as the `name_resolution_miss` signal), so a name that
    fails 400 times a week is visible in the lake rather than dropped.
    """

    def __init__(self) -> None:
        self._misses: dict[tuple[str, str], Miss] = {}

    def __len__(self) -> int:
        return len(self._misses)

    def clear(self) -> None:
        self._misses = {}

    def record(
        self, query: ResolveQuery, resolution: Resolution, *, now: datetime
    ) -> Miss:
        raw_name = query.raw_name or ""
        key = (raw_name.casefold(), (query.source or "").casefold())
        stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        best = resolution.candidates[0] if resolution.candidates else None

        existing = self._misses.get(key)
        if existing is not None:
            existing.occurrence_count += 1
            existing.last_seen_at = stamp
            existing.context = query.context()
            existing.best_candidate_player_id = best.player_id if best else None
            existing.match_score = round(best.score, 6) if best else None
            existing.match_margin = resolution.margin
            existing.disagreeing_attributes = list(best.disagreeing) if best else []
            existing.reason = resolution.reason
            return existing

        miss = Miss(
            raw_name=raw_name,
            source=query.source,
            context=query.context(),
            best_candidate_player_id=best.player_id if best else None,
            match_score=round(best.score, 6) if best else None,
            match_margin=resolution.margin,
            disagreeing_attributes=list(best.disagreeing) if best else [],
            reason=resolution.reason,
            first_seen_at=stamp,
            last_seen_at=stamp,
        )
        self._misses[key] = miss
        return miss

    def rows(self) -> list[dict]:
        """Ordered by `occurrence_count` descending, per the contract."""
        ordered = sorted(
            self._misses.values(),
            key=lambda m: (-m.occurrence_count, m.first_seen_at, m.raw_name),
        )
        return [m.to_signal() for m in ordered]


def candidate_payload(scored: Scored, link_method: str) -> dict:
    """One ranked candidate as served by `/resolve`."""
    row = scored.row
    return {
        "player_id": scored.player_id,
        "full_name": row.get("full_name"),
        "team": row.get("team"),
        "position": row.get("position"),
        "jersey_number": row.get("jersey_number"),
        # `confidence` is the score itself, already bounded to [0, 1] by
        # construction (agreeing weight over comparable weight).
        "confidence": round(scored.score, 6),
        "link_method": link_method,
        "agreeing_attributes": list(scored.agreeing),
        "disagreeing_attributes": list(scored.disagreeing),
        "absent_attributes": list(scored.absent),
    }
