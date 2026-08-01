"""Event reconstruction: per-week designations collapsed into injury events.

**An adapter's job here is event reconstruction, not row copying.** The upstream
publishes one line per player per team-week saying "Hamstring / Out". This
module turns runs of those lines into events with an onset, a resolution and a
`days_to_return`, and decides which of them re-aggravate an earlier one.

Four decisions here are correctness rather than style.

--------------------------------------------------------------------------
1. `absence_reason` is SOURCED from the designation. Never inferred.
--------------------------------------------------------------------------

This is the spec's named failure mode, and it is the whole reason this module
exists as its own file. A game missed for a suspension, a personal matter, a
healthy scratch or a late-season rest week for a seeded team, folded into
`career_games_missed_injury`, gives a player a durability problem they do not
have. It looks entirely plausible — the availability rate is a believable
number, just wrong — and it biases every downstream projection for somebody who
has never been hurt.

So `classify_absence` reads the designation and nothing else, and a game with no
designation is `undesignated`: an absence with no stated cause, counted in
`games_missed_by_reason` so it is visible, and **never** counted as an injury.
"Absent, therefore injured" is exactly the inference this forbids. Note the
consequence, which is deliberate: an injury a club simply never reported is
under-counted. Under-counting biases toward "this player is fine", which is the
safe direction; the opposite invents a problem.

The order of the checks in `classify_absence` is load-bearing. nflverse
publishes rows like `Ankle [Not Injury Related - Personal, Thursday Only]`,
where a body-part token and a non-injury phrase are in the same cell. The
non-injury phrases are tested FIRST, so that row is `personal`, not `injury`.

--------------------------------------------------------------------------
2. "Consecutive" means consecutive GAMES, not consecutive week numbers
--------------------------------------------------------------------------

A player designated in weeks 5 and 7 with a bye in week 6 has one uninterrupted
absence. Comparing week numbers splits it into two events and then flags the
second as a recurrence of the first — a fabricated re-aggravation, at the exact
26-day distance the recurrence rule is calibrated for. Runs are therefore walked
over the team's ordered completed-game list, which has no bye in it at all.

--------------------------------------------------------------------------
3. Recurrence is a documented, versioned rule, and it keys on `injury_site`
--------------------------------------------------------------------------

`adapters.upstream.UPSTREAM_ADAPTER` carries the rule and its version, per the
spec, so a rule change is distinguishable from a change in the player. The rule:
two events at the same `injury_site` are linked when the later one's
`onset_date` falls within `RECURRENCE_WINDOW_DAYS` of the earlier one's return
(`resolved_date`, falling back to its `onset_date` while unresolved).

**`injury_site`, not `body_part`, and this is a deliberate refinement of the
spec.** The spec's `body_part` enum is ten values wide and has no member for a
calf, a quad or an oblique, so all three collapse to `other` — and keying
recurrence on `body_part` would then link a Week 3 calf strain to a Week 8 quad
strain as a re-aggravation of the same tissue. That is a fabricated recurrence,
which is the same class of error as a fabricated absence. `injury_site` is the
finer normalized token (`calf`, `quadricep`, `hamstring`), it is **published on
every event**, so `is_recurrence_of` stays reproducible from the row, and
`body_part` is still emitted exactly as the spec defines it.

--------------------------------------------------------------------------
4. An event is a DESIGNATION run, not an absence run
--------------------------------------------------------------------------

A player carried as Questionable with a hamstring who plays every week still
strained a hamstring. The spec's `injury_events[].games_missed` is "games missed
attributable to this event", which is 0 for that event and not a reason to
discard it — the recurrence history is the product here, and dropping the
played-through events would hide the two strains that preceded the one that
finally cost a game.
"""

from dataclasses import dataclass
from datetime import date

from .adapters.upstream import (
    RECURRENCE_WINDOW_DAYS,
    DesignationRow,
    GameRef,
    Participation,
    PlayerRow,
    Schedule,
)

__all__ = [
    "ABSENCE_REASONS",
    "BODY_PARTS",
    "COUNTED_ABSENCE_REASON",
    "TISSUE_CLASSES",
    "Absence",
    "InjuryEvent",
    "PlayerHistory",
    "TenureGame",
    "classify_absence",
    "normalize_site",
    "reconstruct",
    "site_body_part",
    "site_tissue_class",
]

# The spec's closed enum, published verbatim on every event.
BODY_PARTS: tuple[str, ...] = (
    "hamstring",
    "knee",
    "ankle",
    "shoulder",
    "foot",
    "groin",
    "back",
    "concussion",
    "hand",
    "other",
)

TISSUE_CLASSES: tuple[str, ...] = ("soft_tissue", "joint", "bone", "head", "other")

# Every reason a scoped player can be absent from a game inside their tenure.
# `undesignated` is an explicit member rather than a null: "we looked at the
# injury report and it said nothing about this player" is a fact, and a null
# would be indistinguishable from "we never looked".
ABSENCE_REASONS: tuple[str, ...] = (
    "injury",
    "illness",
    "personal",
    "discipline",
    "rest",
    "non_injury_other",
    "undesignated",
)

# The ONLY reason that increments `career_games_missed_injury`. Named rather
# than inlined so the guard is one symbol a test can pin, and so widening it is
# a visible edit rather than a comparison somebody loosened.
COUNTED_ABSENCE_REASON = "injury"

# Non-injury phrases, tested BEFORE any body-part token — see the module
# docstring, section 1. Substring matches against the lowercased designation
# text, longest-intent first within each group.
_NON_INJURY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("suspend", "discipline"),
    ("coach's decision", "discipline"),
    ("coaching decision", "discipline"),
    ("coachs decision", "discipline"),
    ("not injury related - coach", "discipline"),
    ("resting player", "rest"),
    ("not injury related - resting", "rest"),
    ("healthy scratch", "rest"),
    ("personal", "personal"),
    ("jury duty", "personal"),
    ("did not travel", "personal"),
    ("bereavement", "personal"),
    ("illness", "illness"),
    ("non-football illness", "illness"),
    # The catch-all for the family, AFTER every specific member of it. nflverse
    # publishes a bare `Not injury related - other`, which is a real designation
    # naming a real non-injury cause this collector cannot classify further.
    ("not injury related", "non_injury_other"),
)

# `injury_site -> (body_part, tissue_class)`. The keys are the normalized site
# tokens; `body_part` is the spec's closed enum and `tissue_class` is
# independent of it, which is the point: a calf is `other` to the spec and
# unambiguously `soft_tissue` to a model, and the two fields exist so both can
# be true at once.
_SITES: dict[str, tuple[str, str]] = {
    "hamstring": ("hamstring", "soft_tissue"),
    "groin": ("groin", "soft_tissue"),
    "knee": ("knee", "joint"),
    "ankle": ("ankle", "joint"),
    "shoulder": ("shoulder", "joint"),
    "foot": ("foot", "joint"),
    "toe": ("foot", "joint"),
    "heel": ("foot", "joint"),
    "back": ("back", "soft_tissue"),
    "concussion": ("concussion", "head"),
    "head": ("concussion", "head"),
    "hand": ("hand", "joint"),
    "finger": ("hand", "joint"),
    "thumb": ("hand", "joint"),
    "wrist": ("hand", "joint"),
    "calf": ("other", "soft_tissue"),
    "quadricep": ("other", "soft_tissue"),
    "thigh": ("other", "soft_tissue"),
    "glute": ("other", "soft_tissue"),
    "oblique": ("other", "soft_tissue"),
    "abdomen": ("other", "soft_tissue"),
    "pectoral": ("other", "soft_tissue"),
    "biceps": ("other", "soft_tissue"),
    "triceps": ("other", "soft_tissue"),
    "forearm": ("other", "soft_tissue"),
    "achilles": ("other", "soft_tissue"),
    "neck": ("other", "soft_tissue"),
    "hip": ("other", "joint"),
    "pelvis": ("other", "joint"),
    "elbow": ("other", "joint"),
    "rib": ("other", "bone"),
    "chest": ("other", "bone"),
    "collarbone": ("other", "bone"),
    "fibula": ("other", "bone"),
    "tibia": ("other", "bone"),
    "shin": ("other", "bone"),
    "hernia": ("other", "other"),
    "cramps": ("other", "other"),
    "eye": ("other", "other"),
    "throat": ("other", "other"),
    "kidney": ("other", "other"),
}

# Upstream spellings folded onto a canonical site token before the lookup.
# Ordered longest-first at match time so `quadricep` wins over `quad`.
_SITE_ALIASES: dict[str, str] = {
    "quad": "quadricep",
    "quadriceps": "quadricep",
    "hamstrings": "hamstring",
    "ribs": "rib",
    "feet": "foot",
    "hips": "hip",
    "bicep": "biceps",
    "tricep": "triceps",
    "abdominal": "abdomen",
    "achilles tendon": "achilles",
    "clavicle": "collarbone",
    "concussion protocol": "concussion",
    "lower back": "back",
}

# The site published when the designation names no recognised one. A real
# category, not a null: the club said "Out" and named nothing this collector
# could place, which is different from there being no designation at all.
UNSPECIFIED_SITE = "unspecified"

# Matched longest-first so a longer alias cannot be shadowed by a shorter token
# it contains.
_SITE_LOOKUP: tuple[tuple[str, str], ...] = tuple(
    sorted(
        [(alias, site) for alias, site in _SITE_ALIASES.items()]
        + [(site, site) for site in _SITES],
        key=lambda pair: -len(pair[0]),
    )
)


def normalize_site(text: str) -> str:
    """The canonical injury site named by a designation cell.

    `UNSPECIFIED_SITE` when nothing is recognised, never the raw string: a site
    this collector cannot place must not become its own recurrence key, or every
    idiosyncratic club spelling would look like a distinct body part that never
    recurs.

    Laterality is discarded — `Left Hamstring` and `Right Hamstring` are both
    `hamstring`. Keeping it would be more precise and is not supported by the
    data: the same club writes `Hamstring` one week and `Left Hamstring` the
    next for the same injury, so a lateral key would split one event in two.
    """
    lowered = (text or "").strip().lower()
    if not lowered:
        return UNSPECIFIED_SITE
    for token, site in _SITE_LOOKUP:
        if token in lowered:
            return site
    return UNSPECIFIED_SITE


def site_body_part(site: str) -> str:
    """The spec's `body_part` enum value for a normalized site."""
    return _SITES.get(site, ("other", "other"))[0]


def site_tissue_class(site: str) -> str:
    """The spec's `tissue_class` enum value for a normalized site."""
    return _SITES.get(site, ("other", "other"))[1]


def _designation_text(designation: DesignationRow) -> str:
    """The designation's own stated cause, report first then practice.

    Both are read because a club that lists no game status still files a
    practice report naming the body part, and that practice line is the only
    place the cause appears for a player who was limited all week and then sat.
    """
    for value in (
        designation.report_primary_injury,
        designation.practice_primary_injury,
        designation.report_secondary_injury,
        designation.practice_secondary_injury,
    ):
        if value.strip():
            return value.strip()
    return ""


def classify_absence(designation: DesignationRow | None) -> str:
    """Why a game was missed, **read off the designation**.

    `None` — no injury-report line for that team-week — is `undesignated`. That
    is the guard: there is nothing to source a reason from, so no reason is
    invented, and `undesignated` is never counted as an injury.

    A designation whose text names a non-injury cause is that cause. A
    designation that names an injury, or names nothing while carrying a
    game status of `Out`/`Doubtful`, is `injury` — a club listing a player Out
    on the *injury report* has designated them out for injury-report reasons,
    and the non-injury cases are precisely the ones that carry explicit "Not
    injury related" text (verified against `injuries_2024.csv`: 3 of 6,215 rows
    carry `Out`/`Doubtful` with no cause text, against 50 that name a non-injury
    cause outright).

    A designation with no text and no game status is a practice-report line and
    stays `undesignated`: being limited in Wednesday practice is not a stated
    reason for missing Sunday's game.
    """
    if designation is None:
        return "undesignated"

    text = _designation_text(designation).lower()
    # FIRST — see the module docstring, section 1.
    for pattern, reason in _NON_INJURY_PATTERNS:
        if pattern in text:
            return reason

    if text:
        return "injury"
    if designation.report_status.strip().lower() in {"out", "doubtful"}:
        return "injury"
    return "undesignated"


# ── the reconstructed shapes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class TenureGame:
    """One team game inside a player's observed tenure span.

    The single ordered list this whole module walks: absence counting, event
    runs, return resolution and the post-return trajectory all read it, so none
    of them can disagree about which games a player was around for.
    """

    game: GameRef
    played: bool
    snap_pct: float | None
    designation: DesignationRow | None
    absence_reason: str | None
    injury_site: str | None


@dataclass(frozen=True)
class Absence:
    """One game a scoped player did not play, and the reason for it.

    `absence_reason` is required and never null — the spec's guard, made
    structural. A consumer can therefore add up the injury-attributed misses
    itself and get the same number this collector published.
    """

    season: int
    week: int
    game_id: str
    absence_reason: str
    injury_site: str | None
    body_part: str | None


@dataclass(frozen=True)
class InjuryEvent:
    event_id: str
    body_part: str
    injury_site: str
    tissue_class: str
    onset_date: date
    onset_season: int
    onset_week: int
    games_missed: int
    days_to_return: int | None
    resolved_date: date | None
    is_recurrence_of: str | None

    @property
    def resolved(self) -> bool:
        return self.days_to_return is not None

    def anchor(self) -> date:
        """The date the recurrence window is measured from — see the rule."""
        return self.resolved_date or self.onset_date


@dataclass(frozen=True)
class PlayerHistory:
    player_id: str
    position: str
    tenure: tuple[TenureGame, ...]
    absences: tuple[Absence, ...]
    events: tuple[InjuryEvent, ...]
    games_possible: int
    games_missed_injury: int
    designated_games: int
    observation_first_season: int
    complete: bool

    def missed_by_reason(self) -> dict[str, int]:
        """Every absence, grouped by its stated reason.

        Published rather than kept internal: it is what lets a reader see that
        the eleven games this player missed were three injuries and eight
        suspension games, which is the whole difference between a durability
        problem and a disciplinary one.
        """
        counts = dict.fromkeys(ABSENCE_REASONS, 0)
        for absence in self.absences:
            counts[absence.absence_reason] += 1
        return counts


def _teams_by_season(season_weeks: dict[int, dict[int, str]]) -> dict[int, str]:
    """The club a player was with, per season, from what the feeds observed.

    Whichever team appears most often across that season's designation and
    snap-count rows, ties broken alphabetically so a pass is reproducible.
    Mid-season trades are therefore attributed to the club the player spent
    longer at, which under-counts tenure games at the other club — an
    under-count of `career_games_possible` biases `availability_rate` UPWARD,
    toward "this player is fine", which is the safe direction. Splitting a
    season across two clubs is a genuine improvement and is deliberately left
    undone rather than approximated.
    """
    teams: dict[int, str] = {}
    for season, weeks in season_weeks.items():
        tally: dict[str, int] = {}
        for team in weeks.values():
            if team:
                tally[team] = tally.get(team, 0) + 1
        if tally:
            teams[season] = max(sorted(tally), key=lambda t: tally[t])
    return teams


def _build_tenure(
    player: PlayerRow,
    *,
    seasons: list[int],
    schedule: Schedule,
    designations: list[DesignationRow],
    participation: Participation,
) -> tuple[TenureGame, ...]:
    """The ordered games inside the player's observed tenure span.

    **Bounded by what was observed, not by the whole season.** A player signed in
    week 10 was not around for weeks 1-9, and counting those as games he could
    have played inflates `career_games_possible` — which lowers
    `availability_rate` and manufactures the durability problem this collector's
    named failure mode is about. So each season contributes only the games
    between the player's first and last observed week in it, inclusive.

    A season in which nothing was observed at all contributes nothing. That
    under-counts a player who was on a roster all year and neither played a snap
    nor made an injury report, which is a rounding case for the scoped
    positions and again errs toward "fine".
    """
    by_week: dict[tuple[int, int], DesignationRow] = {}
    season_weeks: dict[int, dict[int, str]] = {season: {} for season in seasons}
    for designation in designations:
        if designation.season not in season_weeks:
            continue
        by_week.setdefault((designation.season, designation.week), designation)
        season_weeks[designation.season][designation.week] = designation.team

    played: dict[tuple[int, int], float] = {}
    if player.pfr_id:
        for (pfr_id, season, week), pct in participation.snap_pct.items():
            if pfr_id != player.pfr_id or season not in season_weeks:
                continue
            played[(season, week)] = pct
            season_weeks[season].setdefault(
                week, participation.team_of.get((pfr_id, season, week), "")
            )

    teams = _teams_by_season(season_weeks)

    games: list[TenureGame] = []
    for season in sorted(seasons):
        weeks = season_weeks.get(season) or {}
        if not weeks:
            continue
        team = teams.get(season) or (player.team if season == max(seasons) else None)
        if not team:
            continue
        first, last = min(weeks), max(weeks)
        for game in schedule.games(season, team):
            if not (first <= game.week <= last):
                continue
            designation = by_week.get((season, game.week))
            snap_pct = played.get((season, game.week))
            was_played = snap_pct is not None
            reason = None if was_played else classify_absence(designation)
            site = (
                normalize_site(_designation_text(designation))
                if designation is not None
                else None
            )
            games.append(
                TenureGame(
                    game=game,
                    played=was_played,
                    snap_pct=snap_pct,
                    designation=designation,
                    absence_reason=reason,
                    injury_site=site,
                )
            )
    games.sort(key=lambda g: (g.game.gameday, g.game.season, g.game.week))
    return tuple(games)


def _designation_site(entry: TenureGame) -> str | None:
    """The injury site this game's designation names, or `None` if it is not an
    injury designation at all.

    An event run is a run of INJURY designations at one site. A `personal` or
    `rest` designation ends a run rather than extending it, which is what stops
    a suspension in the middle of a hamstring absence merging two events into
    one long one.
    """
    if entry.designation is None:
        return None
    if classify_absence(entry.designation) != COUNTED_ABSENCE_REASON:
        return None
    return entry.injury_site


def _onset_date(run: list[TenureGame]) -> date:
    """The first date the injury was recorded.

    The earliest `date_modified` on the run's own report lines — a real,
    upstream-published date, which is exactly what the spec's "first date the
    injury was recorded" asks for. It falls back to the first game's `gameday`
    only when every line in the run has an unparseable timestamp, so a null
    never reaches the published row.
    """
    reported = [
        entry.designation.reported_at
        for entry in run
        if entry.designation is not None and entry.designation.reported_at is not None
    ]
    return min(reported) if reported else run[0].game.gameday


def _find_events(
    tenure: tuple[TenureGame, ...],
    *,
    player_id: str,
    recurrence_window_days: int,
) -> tuple[InjuryEvent, ...]:
    """Collapse designation runs into events, then link the recurrences.

    Runs are walked over `tenure`'s INDEX, so a bye week cannot split one
    absence into two — see the module docstring, section 2.
    """
    events: list[InjuryEvent] = []
    index = 0
    while index < len(tenure):
        site = _designation_site(tenure[index])
        if site is None:
            index += 1
            continue

        end = index
        while end + 1 < len(tenure) and _designation_site(tenure[end + 1]) == site:
            end += 1
        run = list(tenure[index : end + 1])

        onset = _onset_date(run)
        games_missed = sum(
            1 for entry in run if entry.absence_reason == COUNTED_ABSENCE_REASON
        )

        resolved_date: date | None = None
        for entry in tenure[end + 1 :]:
            if entry.played:
                resolved_date = entry.game.gameday
                break

        days_to_return = None if resolved_date is None else (resolved_date - onset).days

        # `is_recurrence_of`: the most recent EARLIER event at the same site
        # whose return is within the window of this onset. Scanned in reverse
        # so the nearest prior event wins — chaining three hamstrings should
        # link each to the one before it, not all three to the first.
        recurrence_of = None
        for prior in reversed(events):
            if prior.injury_site != site:
                continue
            gap = (onset - prior.anchor()).days
            if 0 <= gap <= recurrence_window_days:
                recurrence_of = prior.event_id
            break

        first = run[0].game
        events.append(
            InjuryEvent(
                event_id=f"{player_id}:{first.season}-W{first.week:02d}:{site}",
                body_part=site_body_part(site),
                injury_site=site,
                tissue_class=site_tissue_class(site),
                onset_date=onset,
                onset_season=first.season,
                onset_week=first.week,
                games_missed=games_missed,
                days_to_return=days_to_return,
                resolved_date=resolved_date,
                is_recurrence_of=recurrence_of,
            )
        )
        index = end + 1
    return tuple(events)


def reconstruct(
    player: PlayerRow,
    player_id: str,
    *,
    seasons: list[int],
    schedule: Schedule,
    designations: list[DesignationRow],
    participation: Participation,
    complete: bool,
    recurrence_window_days: int = RECURRENCE_WINDOW_DAYS,
) -> PlayerHistory:
    """One player's whole reconstructed durability history.

    Returns a record for every player, including one who has never been hurt:
    **a clean history is data**, and the spec says so outright — "a record with
    zero injury events and `sample_size_events = 0` is present and complete, not
    missing". Treating it as a coverage hole would mark every healthy player in
    the league as missing.
    """
    tenure = _build_tenure(
        player,
        seasons=seasons,
        schedule=schedule,
        designations=designations,
        participation=participation,
    )

    absences: list[Absence] = []
    designated_games = 0
    games_missed_injury = 0
    for entry in tenure:
        if entry.designation is not None:
            designated_games += 1
        if entry.played:
            continue
        reason = entry.absence_reason or "undesignated"
        if reason == COUNTED_ABSENCE_REASON:
            games_missed_injury += 1
        site = entry.injury_site if reason == COUNTED_ABSENCE_REASON else None
        absences.append(
            Absence(
                season=entry.game.season,
                week=entry.game.week,
                game_id=entry.game.game_id,
                absence_reason=reason,
                injury_site=site,
                body_part=None if site is None else site_body_part(site),
            )
        )

    return PlayerHistory(
        player_id=player_id,
        position=player.position,
        tenure=tenure,
        absences=tuple(absences),
        events=_find_events(
            tenure,
            player_id=player_id,
            recurrence_window_days=recurrence_window_days,
        ),
        games_possible=len(tenure),
        games_missed_injury=games_missed_injury,
        designated_games=designated_games,
        observation_first_season=min(seasons),
        complete=complete,
    )
