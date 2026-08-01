"""Play-by-play — folded into one counter set per (team, week).

**The weekly bucket is this collector's atom, and that is guard 1 made
structural rather than remembered.** The spec's named failure mode is a rate
window that straddles the change it describes, and its stated remedy is
"recompute from scratch when a revision boundary is inserted mid-season". A
collector that keeps a running season-to-date total per team has to *remember*
to invalidate it; one that only ever accumulates per week has nothing to
invalidate, because a revision's rates are a pure fold over the weeks that fall
inside it. Inserting a boundary changes which buckets are selected and nothing
else. See `rates.py`.

--------------------------------------------------------------------------
Format: `.csv.gz`, and why parquet was rejected
--------------------------------------------------------------------------

nflverse publishes this season four ways. Measured live, 2026-08-01:

| artifact | size |
|---|---|
| `play_by_play_2025.csv` | 93.41 MiB |
| `play_by_play_2025.csv.gz` | **18.22 MiB** |
| `play_by_play_2025.parquet` | 19.40 MiB |

**The parquet is larger than the gzipped CSV.** On the one upstream this
collector cannot avoid, the format that would have justified adding `pyarrow`
loses outright. `services/coaching-scheme/README.md` carries the full
decision; the short form is that parquet's win exists only on the two feeds
nflverse does not gzip, and it is not worth a 47.8 MiB wheel fleet-wide.

`gzipped=True` also buys the correctness property the plain-CSV path cannot
have: a gzip member carries a trailer, so a short body raises
`UpstreamTruncated` rather than silently yielding half a season. Half a season
of play-by-play would produce a full set of rates with every window silently
halved — `expected: 32, present: 32`, ratio 1.0, and every number wrong.

Projected to 21 of 372 columns. Measured through the shipped path: 1.6s for
48,771 rows.

--------------------------------------------------------------------------
PROE comes from nflfastR's own model, not one invented here
--------------------------------------------------------------------------

`pass_oe` is `(pass - xpass) * 100` — nflfastR's published pass-over-expected,
against a baseline already conditioned on down, distance, score and time,
which is exactly the baseline the spec's field table asks for. Building a
private one would be a worse number nobody can reproduce.

That also fixes the unit for `changepoint.py`: `pass_oe` is in **percentage
points**, so the spec's "a sustained shift of more than roughly eight points"
is a threshold of `8.0` against this series and needs no conversion.

--------------------------------------------------------------------------
`fourth_down_go_rate_over_expected` has no source and is null
--------------------------------------------------------------------------

The spec asks for it "against a win-probability-optimal baseline". nflfastR
publishes `wp` and `vegas_wp` but no fourth-down recommendation column — the
public bots that produce one are separate projects with their own models.
Emitting a rate against a baseline this collector invented would be a number
that looks like the well-known one and is not it. Null with a reason.
"""

import os
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-pbp"

UPSTREAM_URL_TEMPLATE = os.getenv(
    "PBP_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.csv.gz",
)

REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "play_id",
        "season_type",
        "week",
        "posteam",
        "play_type",
        "pass",
        "pass_oe",
        "shotgun",
        "no_huddle",
        "down",
        "qtr",
        "wp",
        "game_seconds_remaining",
        "drive",
        "aborted_play",
        "two_point_attempt",
        "punt_attempt",
        "field_goal_attempt",
        "fourth_down_converted",
        "fourth_down_failed",
    }
)

SEASON_TYPE = "REG"

# What counts as an offensive snap. Kicks, punts, extra points, timeouts and
# penalty-only rows are excluded: every rate here is a play-calling tendency,
# and a coach does not call a kickoff.
OFFENSIVE_PLAY_TYPES = frozenset({"pass", "run"})

# --------------------------------------------------------------------------
# Neutral game script. Named constants rather than literals, because a typo'd
# bound produces a plausible rate rather than an error.
#
# `qtr <= 3` drops fourth-quarter clock management, which is a function of the
# scoreboard rather than of the scheme. `0.20 <= wp <= 0.80` drops garbage
# time at both ends. Deliberately NOT restricted by down: the spec's
# `neutral_pass_rate` is a script filter, and narrowing it to early downs as
# well would silently make it a different statistic from the published figure
# a reader will compare it against.
# --------------------------------------------------------------------------
NEUTRAL_MAX_QUARTER = 3
NEUTRAL_WP_FLOOR = 0.20
NEUTRAL_WP_CEILING = 0.80

# Bounds on a usable inter-snap clock delta for `sec_per_play_neutral`. Below
# 1s is a clock that did not move (a penalty replay, an untimed down); above
# 60s spans a timeout, a review, or the change of possession the drive check
# is supposed to have caught. Both are excluded rather than clamped: a pace
# statistic built from clamped outliers is a pace statistic that cannot be
# wrong, which is worse than one with a smaller sample.
MIN_PLAY_CLOCK_DELTA = 1.0
MAX_PLAY_CLOCK_DELTA = 60.0


@dataclass
class WeeklyBucket:
    """One team's counters for one week. Raw counts, never rates.

    Rates are computed in `rates.py`, from a *selection* of these. Storing
    counts rather than per-week rates is what lets a revision's rate be the
    correctly weighted mean over its weeks rather than a mean of means —
    a mean of means overweights a team's low-snap games, and a blowout is
    exactly the week whose snap count differs most.
    """

    team_id: str
    week: int
    games: set[str] = field(default_factory=set)

    offensive_plays: int = 0
    shotgun_plays: int = 0
    no_huddle_plays: int = 0

    neutral_plays: int = 0
    neutral_passes: int = 0
    neutral_clock_seconds: float = 0.0
    neutral_clock_samples: int = 0

    proe_sum: float = 0.0
    proe_plays: int = 0

    fourth_down_decisions: int = 0
    fourth_down_gos: int = 0


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


def _num(raw: str) -> float | None:
    """A numeric cell, or `None` for the feed's blank / `NA`.

    Returning `None` rather than 0.0 matters for every field here: a missing
    `pass_oe` must not be averaged in as a zero, and a missing `wp` must not
    be read as a 0% win probability that then fails the neutral filter for the
    wrong reason.
    """
    raw = raw.strip()
    if not raw or raw == "NA":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def is_neutral(qtr: float | None, wp: float | None) -> bool:
    """The neutral-script predicate, as one testable function.

    A play with no `wp` is **not** neutral. Admitting it would put the
    league's least-documented plays into the statistic the collector's
    changepoint test runs on.
    """
    if qtr is None or wp is None:
        return False
    return qtr <= NEUTRAL_MAX_QUARTER and NEUTRAL_WP_FLOOR <= wp <= NEUTRAL_WP_CEILING


async def fetch_weekly_buckets(
    season: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> tuple[dict[tuple[str, int], WeeklyBucket], dict[tuple[str, int], tuple[str, int]]]:
    """One season's play-by-play, as `(team, week) -> WeeklyBucket`.

    Also returns the **offensive-play index**, `(game_id, play_id) ->
    (team_id, week)`, over pass/run plays only. `ftn.py` and
    `participation.py` both need it: neither of those feeds says which side
    had the ball on a charted play, and neither should re-derive it.

    Folded as the rows stream past, so peak memory is one chunk, one row, ~550
    buckets and ~32k index entries — never the 93.4 MiB document.

    Raises on an upstream failure. `play_by_play_<season>.csv.gz` does not
    exist until a season's first games are played, so this is the *normal*
    state for months and `capture` treats it as a degraded pass rather than a
    failed one — `staff_assignment` does not need it.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)

    buckets: dict[tuple[str, int], WeeklyBucket] = {}
    index: dict[tuple[str, int], tuple[str, int]] = {}
    # `(game_id, drive) -> game_seconds_remaining of the previous snap`, for
    # the pace delta. Reset implicitly by keying on the drive: a delta across
    # a change of possession is not an inter-snap interval.
    previous_clock: dict[tuple[str, str], float] = {}

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=url,
        **kwargs,
    ):
        if row["season_type"].strip().upper() != SEASON_TYPE:
            continue
        team = row["posteam"].strip()
        if not team:
            continue
        week_raw = row["week"].strip()
        if not week_raw.isdigit():
            continue
        week = int(week_raw)
        game_id = row["game_id"].strip()

        key = (team, week)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = WeeklyBucket(team_id=team, week=week)

        down = _num(row["down"])
        play_type = row["play_type"].strip()
        is_offensive = (
            play_type in OFFENSIVE_PLAY_TYPES
            and row["aborted_play"].strip() != "1"
            and row["two_point_attempt"].strip() != "1"
        )

        # Fourth-down decisions are counted over the whole down, not only over
        # offensive plays: a punt IS the decision, and a denominator of
        # attempts-only would report every team at 1.0.
        if down == 4.0:
            punt = row["punt_attempt"].strip() == "1"
            field_goal = row["field_goal_attempt"].strip() == "1"
            went = (
                is_offensive
                or row["fourth_down_converted"].strip() == "1"
                or (row["fourth_down_failed"].strip() == "1")
            )
            if punt or field_goal or went:
                bucket.fourth_down_decisions += 1
                if went and not punt and not field_goal:
                    bucket.fourth_down_gos += 1

        if not is_offensive:
            continue

        play_id_raw = row["play_id"].strip()
        if game_id and play_id_raw.replace(".", "").isdigit():
            index[(game_id, int(float(play_id_raw)))] = (team, week)

        if game_id:
            bucket.games.add(game_id)
        bucket.offensive_plays += 1
        if row["shotgun"].strip() == "1":
            bucket.shotgun_plays += 1
        if row["no_huddle"].strip() == "1":
            bucket.no_huddle_plays += 1

        # PROE over every offensive snap the model scored, NOT only neutral
        # ones: `xpass` already conditions on score and time, so filtering by
        # script as well would subtract the same effect twice.
        pass_oe = _num(row["pass_oe"])
        if pass_oe is not None:
            bucket.proe_sum += pass_oe
            bucket.proe_plays += 1

        if not is_neutral(_num(row["qtr"]), _num(row["wp"])):
            continue
        bucket.neutral_plays += 1
        if row["pass"].strip() == "1":
            bucket.neutral_passes += 1

        clock = _num(row["game_seconds_remaining"])
        drive_key = (game_id, row["drive"].strip())
        if clock is not None:
            prior = previous_clock.get(drive_key)
            if prior is not None:
                delta = prior - clock
                if MIN_PLAY_CLOCK_DELTA <= delta <= MAX_PLAY_CLOCK_DELTA:
                    bucket.neutral_clock_seconds += delta
                    bucket.neutral_clock_samples += 1
            previous_clock[drive_key] = clock

    return buckets, index


def weekly_proe_series(
    buckets: dict[tuple[str, int], WeeklyBucket],
) -> dict[str, list[tuple[int, float]]]:
    """`team -> [(week, mean PROE), ...]`, week-ascending.

    The series `changepoint.py` runs on, and it is built here — from
    play-by-play alone — precisely so that it is *independent of the staff
    feed*, which is the spec's requirement for that test. A week with no
    scored play contributes nothing rather than a zero.
    """
    series: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for (team, week), bucket in buckets.items():
        if bucket.proe_plays <= 0:
            continue
        series[team].append((week, bucket.proe_sum / bucket.proe_plays))
    return {team: sorted(points) for team, points in series.items()}
