"""The upstream adapters — the only modules that know a wire format.

`player-profile` reads **three** upstreams, and they are different in kind,
different in size and different in how often they move. Keeping them here means
`capture.py` never sees a column name.

| Upstream | Size, measured 2026-07-31 | Moves |
|---|---|---|
| `players.csv` | 7.32 MB, 25,029 rows | listed weight/position, weekly-ish |
| `combine.csv` | 0.89 MB, 8,968 rows | once a year, in March |
| `snap_counts_<n>.csv` | 2.40 MB, 26,615 rows *each* | weekly, current season only |

**Conditional GET is not only a cost control here — it is half of the staleness
guard.** The spec's named failure mode is a listed weight or position that goes
stale without erroring, and its guard is a per-field `last_confirmed_at` that
must be a *measured* age. What measures it is whether the upstream document was
actually re-served: a `304` means nobody republished the listed value, so
`last_confirmed_at` must not advance. That is why every fetch below reports
`republished` alongside its rows, and why a `304` does **not** raise out of this
module the way `depth-chart`'s does — see `_stream_or_memo`.

**Memory.** `roster-scope` was `OOMKilled` at 256Mi because a 37 MB document was
held three times over. Every read here goes through `stream_csv_dicts`, and
every one filters as it parses:

* `players.csv` keeps only fantasy-relevant positions still active within
  `RECENT_SEASONS` of the scoped season — 1,326 of 25,029 rows, measured.
* `snap_counts_<season>.csv` is reduced to one `pfr_id -> int` total as it
  streams; the 26,615 per-game rows are never materialised.
* `combine.csv` is small enough to keep whole, keyed by `pfr_id`.

**The per-season snap files are immutable once a season ends**, which is what
makes a career total affordable at all: the first pass of a process downloads
~15 files (~36 MB), and every pass after that sends `If-None-Match` and gets a
`304` carrying zero bytes for all but the current season. The measured totals
are memoised per season for exactly that reason.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import httpx
from collector_core.conditional import ETAGS, ETagStore, UpstreamUnchanged
from collector_core.streaming import stream_csv_dicts

logger = logging.getLogger(__name__)

__all__ = [
    "CAREER_SNAP_FIRST_SEASON",
    "COMBINE_URL",
    "FANTASY_POSITIONS",
    "PLAYERS_URL",
    "UPSTREAM_ADAPTER",
    "CareerSnaps",
    "CombineRow",
    "CombineTable",
    "ProfileRow",
    "ProfileTable",
    "fetch_career_snaps",
    "fetch_combine",
    "fetch_profiles",
    "normalize_position",
    "reset_upstream_memo",
    "source_ref",
]

# Names the upstream in every envelope's `upstream.adapter`. One label for all
# three feeds because they are one publisher and one release train; the exact
# artifact each envelope read is in `source_ref`, which is what a consumer
# actually joins on.
UPSTREAM_ADAPTER = "nflverse-player-tables"

PLAYERS_URL = os.getenv(
    "PLAYERS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv",
)
COMBINE_URL = os.getenv(
    "COMBINE_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/combine/combine.csv",
)
# `{season}` is substituted per season. One file per season is the only shape
# nflverse publishes snap counts in; there is no career aggregate.
SNAP_COUNTS_URL = os.getenv(
    "SNAP_COUNTS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/"
    "snap_counts_{season}.csv",
)

# nflverse's snap-count coverage begins in 2012. A player whose rookie season
# precedes it has a career total that is a FLOOR rather than a total, and the
# row says so in `career_snaps_complete` instead of quietly under-reporting.
CAREER_SNAP_FIRST_SEASON = int(os.getenv("CAREER_SNAP_FIRST_SEASON", "2012"))

# How far back a player may have last appeared and still be a candidate for the
# current season's scope. Two rather than one because a player who missed all of
# last season on IR is still rosterable, and one rather than zero because
# `last_season` is only refreshed once a player records a snap.
RECENT_SEASONS = 2

# Upstream position labels collapsed into the canonical set. Deliberately the
# same offensive subset `roster_scope.rules.POSITION_ALIASES` uses, because the
# scope this collector narrows to is minted from that map: a position this
# collector recognised and roster-scope did not could never be in scope anyway,
# and one roster-scope recognised and this did not would be a silent hole.
# There is no import across the service boundary to enforce it — the two live in
# different services — so `tests/test_upstream_adapter.py` pins the pairs.
POSITION_ALIASES: dict[str, str] = {
    "QB": "QB",
    "RB": "RB",
    "HB": "RB",
    "TB": "RB",
    "WR": "WR",
    "SE": "WR",
    "FL": "WR",
    "SLOT": "WR",
    "X": "WR",
    "Z": "WR",
    "TE": "TE",
    "Y": "TE",
    "K": "K",
    "PK": "K",
}

FANTASY_POSITIONS = frozenset(POSITION_ALIASES.values())

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly with `reason=malformed`
# rather than map nulls into an append-only lake. Exactly the columns each
# mapping below reads and no more, so an unrelated column disappearing upstream
# does not fail a capture that never used it.
PLAYERS_COLUMNS = frozenset(
    {
        "gsis_id",
        "pfr_id",
        "display_name",
        "birth_date",
        "position",
        "height",
        "weight",
        "college_name",
        "jersey_number",
        "rookie_season",
        "last_season",
        "latest_team",
        "years_of_experience",
        "draft_year",
        "draft_round",
        "draft_pick",
    }
)

COMBINE_COLUMNS = frozenset(
    {"pfr_id", "forty", "bench", "vertical", "broad_jump", "cone", "shuttle"}
)

SNAP_COUNTS_COLUMNS = frozenset({"pfr_player_id", "offense_snaps"})


def normalize_position(raw: str) -> str | None:
    """An upstream position label to the canonical one, or `None`.

    `None` rather than the raw string for an unrecognised label: a position this
    collector cannot place on a curve must not reach `position_age_curve_stage`,
    which would then bucket it against whichever default the table happened to
    carry.
    """
    return POSITION_ALIASES.get((raw or "").strip().upper())


def _int(value: str | None) -> int | None:
    """A CSV cell to an int, or `None` for blank/unparseable.

    Never 0. The spec is explicit that an untested player has null combine
    fields rather than zeros because a zero forty time is numerically
    catastrophic downstream, and the same reasoning holds for a missing draft
    pick: pick 0 would read as better than pick 1.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class ProfileRow:
    """One `players.csv` row, narrowed to what a profile needs."""

    gsis_id: str
    pfr_id: str | None
    display_name: str
    team: str | None
    position: str
    jersey_number: int | None
    birth_date: date | None
    height_inches: int | None
    weight_lbs: int | None
    college: str | None
    experience_seasons: int | None
    rookie_season: int | None
    draft_year: int | None
    draft_round: int | None
    draft_overall_pick: int | None


@dataclass(frozen=True)
class CombineRow:
    """One `combine.csv` row. Every member may be null — most are.

    Measured against the 1,326 recent fantasy-position players: 749 have a
    combine row at all, and within those, `forty` is present for 565, `vertical`
    555, `broad_jump` 535, `shuttle` 324, `cone` 294, `bench` 272. An absent
    measurement is null and is explicitly NOT a coverage miss, per the spec.
    """

    forty_yard: float | None
    vertical_inches: float | None
    broad_jump_inches: float | None
    three_cone: float | None
    shuttle: float | None
    bench_reps: int | None


@dataclass(frozen=True)
class ProfileTable:
    rows: tuple[ProfileRow, ...]
    # False when the fetch was answered `304` and these rows came from this
    # process's memo. The staleness guard reads it: a document nobody
    # republished has not re-confirmed anything.
    republished: bool


@dataclass(frozen=True)
class CombineTable:
    by_pfr_id: dict[str, CombineRow]
    republished: bool


@dataclass
class CareerSnaps:
    """Career offensive snaps per `pfr_id`, plus what the total is missing."""

    totals: dict[str, int] = field(default_factory=dict)
    # Seasons this pass could not read at all — a fetch failure or the deadline.
    # A total built over a hole is reported rather than published as complete.
    seasons_missing: tuple[int, ...] = ()
    # Seasons this pass DID read. Empty means the sweep produced nothing, which
    # is a different fact from "every player took zero snaps" — see `total_for`.
    seasons_read: tuple[int, ...] = ()
    # The earliest season any file contributed. A player who was already in the
    # league before it has a truncated total.
    first_season: int = CAREER_SNAP_FIRST_SEASON

    def total_for(self, pfr_id: str | None) -> int | None:
        """Career snaps, or `None` where a number cannot be defended.

        `None`, never 0, in two cases, and both matter for the same reason: a
        zero flows straight into `career_snap_load_percentile` as a genuine
        bottom-of-cohort reading, and a ten-year veteran reported at zero career
        snaps is the exact "well-formed record that is silently wrong" this
        collector exists to prevent.

        * **No `pfr_id`.** "This player has no join key" and "this player took
          zero snaps" are different facts.
        * **No season was read at all.** A sweep that failed outright knows
          nothing about anybody. Found by mutation testing: an empty
          `CareerSnaps` used to answer 0 for every player who *had* a join key,
          so a total snap-feed outage published a league of rookies.

        A player absent from seasons that WERE read is a genuine 0 — that is a
        measurement, not an absence.
        """
        if not pfr_id or not self.seasons_read:
            return None
        return self.totals.get(pfr_id, 0)


# ── process-lifetime memos ───────────────────────────────────────────────────
#
# Keyed the same way the `ETagStore` is, and for the same reason: a `304` says
# "byte-identical to what you already read", which is only useful to a process
# that still holds what it read. A restart costs exactly one full download per
# key. See `_stream_or_memo` for what happens if the two ever disagree.
_PROFILE_MEMO: dict[int, tuple[ProfileRow, ...]] = {}
_COMBINE_MEMO: dict[str, CombineRow] = {}
_COMBINE_LOADED = False
_SNAP_MEMO: dict[int, dict[str, int]] = {}


def reset_upstream_memo(etag_store: ETagStore = ETAGS) -> None:
    """Forget every memo and every stored ETag. For tests only.

    Both halves, together: clearing one without the other produces exactly the
    inconsistent state `_stream_or_memo` has to defend against, and a test that
    creates it is testing the defence rather than the behaviour it meant to.
    """
    global _COMBINE_LOADED
    _PROFILE_MEMO.clear()
    _COMBINE_MEMO.clear()
    _COMBINE_LOADED = False
    _SNAP_MEMO.clear()
    etag_store.clear()


def source_ref(season: int, week: int) -> str:
    """The exact upstream artifacts this pass read, recorded in the envelope.

    All three, space-free and comma-joined, because a profile row is a join
    across all three and a consumer tracing a corrected birth date needs to know
    which documents produced it. The spec asks for exactly this: "the adapter
    must record which upstream supplied the date in `upstream.source_ref` so a
    correction can be traced."

    `week` is accepted and unused: no upstream here varies by week. It is in the
    signature because every collector's `source_ref` has it and a caller passing
    the scope through should not have to remember which ones care.
    """
    del week
    snaps = SNAP_COUNTS_URL.format(season=season)
    return ",".join((PLAYERS_URL, COMBINE_URL, snaps))


async def _read_rows(
    client: httpx.AsyncClient,
    url: str,
    *,
    required_columns: frozenset[str],
    memo_present: bool,
    etag_store: ETagStore,
):
    """Stream `url` conditionally. `(rows, republished)`; `None` means memo.

    **`UpstreamUnchanged` is caught here rather than re-raised, and that is a
    departure from `depth-chart`'s route-1 pattern on purpose.** A collector
    with one upstream can let a `304` abort the pass, because there is nothing
    else to do. This one has three, on three different republication cadences,
    and — more importantly — a `304` is the *input* to the staleness guard
    rather than a reason to stop: the pass still has to recompute how long it
    has been since anybody re-confirmed a listed weight, and it cannot do that
    from a pass that never ran. `capture.py` keeps the "publish nothing when
    nothing changed" half through a content digest instead, which is strictly
    better here because it also catches a republication that changed no field
    this collector reads.

    The one inconsistent state worth defending: an ETag stored with no memo
    behind it. It cannot arise from this module — the memo is written on every
    successful read — but it can arise from a test that clears one and not the
    other, or from a second `ETagStore` shared between processes. Rather than
    serve nothing, the stored ETag is dropped and the document is re-requested
    unconditionally, which costs one download and self-heals.
    """
    stream = stream_csv_dicts(
        client,
        url,
        required_columns=required_columns,
        etag_key=url,
        etag_store=etag_store,
    )
    try:
        rows = [row async for row in stream]
    except UpstreamUnchanged:
        if memo_present:
            return None, False
        # An ETag with nothing behind it. Drop it and re-read unconditionally
        # rather than serve nothing — see `_stream_or_memo`.
        logger.warning("%s returned 304 with no memo behind it; re-reading", url)
        etag_store.set(url, None)
        rows = [
            row
            async for row in stream_csv_dicts(
                client,
                url,
                required_columns=required_columns,
                etag_key=url,
                etag_store=etag_store,
            )
        ]
    return rows, True


def _keep_profile(row: dict, season: int) -> bool:
    """Kept or discarded as the row goes past — never materialised first.

    25,029 rows in, 1,326 out (measured 2026-07-31). Doing this after the fact
    is the `roster-scope` OOM in miniature.
    """
    if normalize_position(row.get("position", "")) is None:
        return False
    last_season = _int(row.get("last_season"))
    if last_season is None:
        return False
    return last_season >= season - RECENT_SEASONS


def _to_profile(row: dict) -> ProfileRow | None:
    gsis_id = _text(row.get("gsis_id"))
    position = normalize_position(row.get("position", ""))
    if not gsis_id or position is None:
        # No join key to `player-identity`, or a position this collector cannot
        # place. Dropped rather than emitted with a guessed id — the shortfall
        # shows up against `EXPECTED_FLOOR`.
        return None
    return ProfileRow(
        gsis_id=gsis_id,
        pfr_id=_text(row.get("pfr_id")),
        display_name=_text(row.get("display_name")) or gsis_id,
        team=_text(row.get("latest_team")),
        position=position,
        jersey_number=_int(row.get("jersey_number")),
        birth_date=_date(row.get("birth_date")),
        height_inches=_int(row.get("height")),
        weight_lbs=_int(row.get("weight")),
        college=_text(row.get("college_name")),
        experience_seasons=_int(row.get("years_of_experience")),
        rookie_season=_int(row.get("rookie_season")),
        draft_year=_int(row.get("draft_year")),
        draft_round=_int(row.get("draft_round")),
        # nflverse's `draft_pick` is the OVERALL selection number, not the
        # within-round one: round 5 pick 143 is the 143rd player taken.
        # Verified against the live table before this mapping was written.
        draft_overall_pick=_int(row.get("draft_pick")),
    )


async def fetch_profiles(
    season: int,
    *,
    client: httpx.AsyncClient,
    etag_store: ETagStore = ETAGS,
) -> ProfileTable:
    """Every recently-active fantasy-position player, from `players.csv`.

    Raises on an upstream failure rather than returning an empty table, so the
    caller can turn the exception into a `present: 0` envelope. An empty list
    would instead be recorded as a successful capture of nothing.
    """
    rows, republished = await _read_rows(
        client,
        PLAYERS_URL,
        required_columns=PLAYERS_COLUMNS,
        memo_present=season in _PROFILE_MEMO,
        etag_store=etag_store,
    )
    if rows is None:
        return ProfileTable(rows=_PROFILE_MEMO[season], republished=False)

    profiles = tuple(
        profile
        for profile in (_to_profile(row) for row in rows if _keep_profile(row, season))
        if profile is not None
    )
    _PROFILE_MEMO[season] = profiles
    return ProfileTable(rows=profiles, republished=republished)


async def fetch_combine(
    *,
    client: httpx.AsyncClient,
    etag_store: ETagStore = ETAGS,
) -> CombineTable:
    """Combine measurements keyed by `pfr_id`.

    Keyed by `pfr_id` because that is the only id `combine.csv` and
    `players.csv` share — `combine.csv` carries no GSIS id at all. 1,204 of the
    1,326 recent fantasy-position players carry a `pfr_id`; the other 122
    (overwhelmingly undrafted rookies) get an all-null `athleticism` object,
    which is the spec's explicitly-optional case rather than a miss.
    """
    global _COMBINE_LOADED
    rows, republished = await _read_rows(
        client,
        COMBINE_URL,
        required_columns=COMBINE_COLUMNS,
        memo_present=_COMBINE_LOADED,
        etag_store=etag_store,
    )
    if rows is None:
        return CombineTable(by_pfr_id=dict(_COMBINE_MEMO), republished=False)

    table: dict[str, CombineRow] = {}
    for row in rows:
        pfr_id = _text(row.get("pfr_id"))
        if not pfr_id:
            continue
        table[pfr_id] = CombineRow(
            forty_yard=_float(row.get("forty")),
            vertical_inches=_float(row.get("vertical")),
            broad_jump_inches=_float(row.get("broad_jump")),
            three_cone=_float(row.get("cone")),
            shuttle=_float(row.get("shuttle")),
            bench_reps=_int(row.get("bench")),
        )
    _COMBINE_MEMO.clear()
    _COMBINE_MEMO.update(table)
    _COMBINE_LOADED = True
    return CombineTable(by_pfr_id=table, republished=republished)


async def _season_snaps(
    season: int,
    *,
    client: httpx.AsyncClient,
    etag_store: ETagStore,
) -> dict[str, int]:
    """One season's offensive snaps per `pfr_id`, summed as it streams.

    26,615 per-game rows in, ~2,192 totals out. The per-game rows are never
    materialised, which is what keeps a fifteen-season sweep flat in memory.

    The file already contains regular season plus every postseason round
    (`REG`, `WC`, `DIV`, `CON`, `SB`) and no preseason, which is exactly the
    spec's `career_offensive_snaps` definition, so no `game_type` filter is
    applied. Filtering to `REG` would silently drop the postseason snaps the
    field is defined to include.
    """
    url = SNAP_COUNTS_URL.format(season=season)
    rows, _ = await _read_rows(
        client,
        url,
        required_columns=SNAP_COUNTS_COLUMNS,
        memo_present=season in _SNAP_MEMO,
        etag_store=etag_store,
    )
    if rows is None:
        return _SNAP_MEMO[season]

    totals: dict[str, int] = {}
    for row in rows:
        pfr_id = _text(row.get("pfr_player_id"))
        if not pfr_id:
            continue
        snaps = _int(row.get("offense_snaps"))
        if snaps:
            totals[pfr_id] = totals.get(pfr_id, 0) + snaps
    _SNAP_MEMO[season] = totals
    return totals


async def fetch_career_snaps(
    season: int,
    *,
    client: httpx.AsyncClient,
    deadline: datetime | None = None,
    etag_store: ETagStore = ETAGS,
) -> CareerSnaps:
    """Cumulative offensive snaps per `pfr_id`, `CAREER_SNAP_FIRST_SEASON`..`season`.

    **A season that cannot be read is recorded, not skipped.** A career total
    silently missing three seasons is a well-formed number that is simply wrong,
    which is the same class of failure this whole collector's staleness guard
    exists for. `seasons_missing` travels to the envelope's `errors`, and every
    affected row reports `career_snaps_complete: false`.

    Newest season first so that a deadline truncates the *oldest* seasons — the
    ones contributing least to a currently-scoped player's total — rather than
    the current one, which is the only season that moves.
    """
    career = CareerSnaps(first_season=CAREER_SNAP_FIRST_SEASON)
    missing: list[int] = []
    read: list[int] = []
    for one_season in range(season, CAREER_SNAP_FIRST_SEASON - 1, -1):
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            missing.extend(range(CAREER_SNAP_FIRST_SEASON, one_season + 1))
            break
        try:
            totals = await _season_snaps(
                one_season, client=client, etag_store=etag_store
            )
        except Exception as exc:  # noqa: BLE001 — one season is not the pass
            # Deliberately not `fail_capture`: three of four signal types do not
            # read this feed at all, and a 2014 file 404ing must not discard a
            # perfectly good biographical capture. The caller records
            # `collector_capture_failures_total` once for the degraded pass.
            logger.warning("snap counts for %s unavailable: %s", one_season, exc)
            missing.append(one_season)
            continue
        read.append(one_season)
        for pfr_id, snaps in totals.items():
            career.totals[pfr_id] = career.totals.get(pfr_id, 0) + snaps
    career.seasons_missing = tuple(sorted(missing))
    career.seasons_read = tuple(sorted(read))
    return career
