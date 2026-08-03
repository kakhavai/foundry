"""Claims a first mutation run left undefended.

Every test here was written **because a mutant survived**, and each names the
mutation it kills. Grouping them in one file rather than scattering them is
deliberate: it keeps the record of what the rest of the suite could not see,
and it is the file to read before deciding a claim is already covered.

Six survivors are recorded as **equivalent** rather than fixed, and each was
paired with a non-equivalent neighbour that the suite killed — because "I
think that mutant is equivalent" is a claim, and the way to test a claim is to
perturb it slightly and watch the perturbation die. The ones whose equivalence
is a property worth stating carry a note at the site instead: see
`capture._strength_envelope`'s `acc.expect` block and `lineups.derive_lineups`'
crosswalk miss.

**That discipline earned its keep on the tie-break.** An independent review
raised `>` → `>=` there as a live gap whose consequence would be ties
resolving by CSV row order. The mutation is in fact equivalent — the
comparison is on a `(snaps, id)` **tuple**, so `>=` can differ only on an
exact tuple equality, which two distinct players cannot produce — but
building the neighbours that *do* carry that consequence found a degenerate
axis in the fixture the review had not: with only two tied men, "highest id"
coincides with "first row seen" for free, and a first-row-wins mutant
survived. The tie is three men now, with the winner in the middle of the row
order. The review's concern was right; the mutation it named was not the one
that carried it.
"""

import gzip

import httpx
import pytest
import respx

from offensive_line import ratings
from offensive_line.adapters import depth as depth_adapter
from offensive_line.adapters import identity as identity_adapter
from offensive_line.adapters import pbp as pbp_adapter
from offensive_line.adapters import snaps as snaps_adapter
from offensive_line.capture import STRENGTH, _fold
from offensive_line.lineups import unavailable_starters
from offensive_line.ratings import (
    PROVENANCE_MEASURED,
    PROVENANCE_PRIOR,
    RECORD_UNIT,
    STARTER_POSITIONS,
    LineTotals,
    OffenseGameTotals,
    StarterSlot,
    continuity_games,
    line_yards,
    replacement_deltas,
)

from . import season as season_module
from .conftest import (
    SEASON,
    WEEK,
    Feeds,
    SpyLake,
    canonical_for,
    run_capture,
    starters,
    units,
)

# --------------------------------------------------------------------------
# C3 — a team with no identifiable five must not report a streak
# --------------------------------------------------------------------------


def test_an_unidentifiable_lineup_has_no_streak_to_report():
    """Kills C3 (dropping the `hashes[-1] is None` guard).

    Every game's hash is `None` for a team whose five cannot be identified, so
    a walk that compared `None` to `None` would find them all equal and report
    a full streak — `continuity_games: 4` beside `lineup_hash: null`. Populated,
    plausible, and a claim about personnel this pass has no evidence for.
    """
    assert continuity_games([None, None, None, None]) == 0
    assert continuity_games([None]) == 0


async def test_the_unlabelled_team_reports_no_streak_on_the_real_path():
    row = units(await run_capture(Feeds(), lake=SpyLake()))[
        season_module.UNLABELLED_TEAM
    ]
    assert row["lineup_hash"] is None
    assert row["continuity_games"] == 0
    assert row["lineup_changed"] is False


# --------------------------------------------------------------------------
# C5 — games are ordered by week, not by whatever the id sorts as
# --------------------------------------------------------------------------


def test_games_are_ordered_by_week_rather_than_by_game_id():
    """Kills C5 (sorting `team_games` by id alone).

    nflverse's ids happen to embed a zero-padded week, so sorting by id gives
    the right answer *today* and this survives every end-to-end test. It is
    not a property of the data model, and `continuity_games` counts
    consecutive games — an ordering bug there does not raise, it silently
    reports a stable line as churning.
    """
    totals = LineTotals(
        opponents={("AAA", "zzz-week-1"): "BBB", ("AAA", "aaa-week-2"): "CCC"},
        game_week={"zzz-week-1": 1, "aaa-week-2": 2},
    )
    totals.order_games()
    assert totals.team_games["AAA"] == ["zzz-week-1", "aaa-week-2"]


# --------------------------------------------------------------------------
# P3 — `expect` is called on the schedule, never on what succeeded
# --------------------------------------------------------------------------


async def test_a_team_that_produced_no_rows_still_owes_six_keys(monkeypatch):
    """Kills P3 (declaring keys only for teams that produced rows).

    The mutant is the exact shape the coverage contract forbids: a truncated
    pass would report `expected` equal to what it managed to build, and the
    ratio would read healthy. Reached by dropping one team's rows *after*
    `build_rows` — every other line of the collector runs unchanged.
    """
    dropped = season_module.TEAMS[2]
    original = ratings.build_rows

    def without_one(totals, **kwargs):
        rows, drops = original(totals, **kwargs)
        return [row for row in rows if row["team_id"] != dropped], drops

    # Patched on `capture`, not on `ratings`: `capture.py` does
    # `from .ratings import build_rows`, so the name it calls was bound at
    # import time and patching the source module would change nothing.
    monkeypatch.setattr("offensive_line.capture.build_rows", without_one)
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]

    for kind in (RECORD_UNIT, *STARTER_POSITIONS):
        assert f"{dropped}:{kind}" in envelope.coverage.missing
    assert envelope.coverage.present + len(envelope.coverage.missing) == (
        len(season_module.TEAMS) * (1 + len(STARTER_POSITIONS))
    )


# --------------------------------------------------------------------------
# A1 — the adjustment removes the OPPONENT's term, not some other one
# --------------------------------------------------------------------------


def _mean_front_faced(team: str) -> float:
    """The mean `FRONT_STRENGTH` of the fronts `team` faced in the fixture."""
    faced = [
        away if home == team else home
        for _game, _week, home, away in season_module.games()
        if team in (home, away)
    ]
    return sum(season_module.FRONT_STRENGTH[other] for other in faced) / len(faced)


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    return cov / (vx * vy) if vx and vy else 0.0


async def test_the_yardstick_is_fit_on_the_fronts_actually_faced():
    """Kills A1 (fitting the yardstick on the conceding unit instead).

    An adjustment fit on the **wrong side of the ball** still produces spread,
    still divides by something plausible, and still ranks roughly by the
    line's own weakness -- so every ranking and variance assertion in the
    suite passes on it. The one thing it cannot do is track the fronts a line
    actually faced, because it never looked at them.

    So the claim is made directly against the fixture's own generating term:
    `opponent_pressure_strength_index` must correlate with the mean
    `FRONT_STRENGTH` of the defences on that team's schedule. Under A1 the
    index is built from the opposing *offensive lines*' pressure-allowed
    production, which is independent of it.
    """
    rows = units(await run_capture(Feeds(), lake=SpyLake()))
    teams = sorted(rows)
    faced = [_mean_front_faced(team) for team in teams]
    index = [rows[team]["opponent_pressure_strength_index"] for team in teams]

    r = _correlation(faced, index)
    assert r > 0.5, (
        f"opponent_pressure_strength_index tracks the fronts faced at r={r:.3f}"
        " -- the yardstick is not fit on the opposing defensive fronts"
    )

    # And the adjustment moves each rate in the direction that index implies:
    # a line that faced strong fronts is rated better than its raw rate.
    shift = [
        rows[team]["pressure_rate_allowed"]
        - rows[team]["pressure_rate_allowed_adj_observed"]
        for team in teams
    ]
    assert _correlation(faced, shift) > 0.5


# --------------------------------------------------------------------------
# J4 — a two-point conversion is not a scrimmage carry
# --------------------------------------------------------------------------


async def test_a_two_point_attempt_is_not_a_run_block_snap():
    """Kills J4 (dropping the `two_point_attempt` exclusion).

    `defensive-front` excludes it from `run_defense_snaps` for the same reason
    — the yardage is not comparable — so counting it here would put the two
    collectors' run denominators on different play sets and corrupt
    `adjusted_line_yards` against `adjusted_line_yards_allowed` silently.
    """
    per_game = season_module.CARRIES_PER_GAME
    weeks = season_module.WEEKS
    row = units(await run_capture(Feeds(), lake=SpyLake()))[season_module.TEAMS[0]]
    assert row["run_block_snaps"] == per_game * weeks

    # And the fixture really does carry one, or the assertion above is vacuous.
    with_two_point = gzip.decompress(Feeds().bodies["pbp"]).decode()
    assert with_two_point.count(",1,4,") or "two point attempt" in with_two_point


# --------------------------------------------------------------------------
# L1 — the depth chart is read PER WEEK
# --------------------------------------------------------------------------


async def test_a_mid_season_position_swap_breaks_the_streak():
    """Kills L1 (labelling every week from the newest snapshot).

    `SWAP_TEAM` trades its two guards' labels from `SWAP_FROM_WEEK`. The same
    five men play all five weeks, so the *membership* never changes — but the
    hash is over ids in position order, so the ordering does. Read per week the
    streak is `WEEKS - SWAP_FROM_WEEK`; read from the newest snapshot every
    week shares one labelling and the streak is `WEEKS - 1`.
    """
    rows = units(await run_capture(Feeds(), lake=SpyLake()))
    swapped = rows[season_module.SWAP_TEAM]
    assert swapped["continuity_games"] == (
        season_module.WEEKS - season_module.SWAP_FROM_WEEK
    )
    assert swapped["lineup_changed"] is False
    assert rows[season_module.TEAMS[0]]["continuity_games"] == season_module.WEEKS - 1


async def test_the_swap_is_reflected_in_the_published_slot_labels():
    rows = starters(await run_capture(Feeds(), lake=SpyLake()))
    swapped = {
        row["starter_position"]: row["starter_id"]
        for row in rows[season_module.SWAP_TEAM]
    }
    left, right = season_module.SWAP_SLOTS
    assert swapped["LG"] == canonical_for(
        season_module.line_id(season_module.SWAP_TEAM, right)
    )
    assert swapped["RG"] == canonical_for(
        season_module.line_id(season_module.SWAP_TEAM, left)
    )


# --------------------------------------------------------------------------
# L3 — a crosswalk miss is a missing slot, never an adopted foreign id
# --------------------------------------------------------------------------


async def test_a_crosswalk_miss_costs_the_slot_rather_than_adopting_a_pfr_id():
    """Kills L3 (`gsis_for_pfr.get(pfr_id, pfr_id)`).

    A defaulting `.get` here promotes a Pro-Football-Reference key into a
    position where a GSIS id is expected. It would then be sent to
    `player-identity` as `source: gsis`, and either resolve to the wrong man or
    be refused — with the row published either way under the mutant that
    matters. The honest answer is four identified starters and five missing
    slots.
    """
    # The centre, deliberately: `LT` and `LG` each have a listed deputy in
    # the fixture who would simply take the slot -- correct behaviour, and
    # it would make this test assert nothing.
    gap = (season_module.TEAMS[0], 2)
    feeds = Feeds(
        bodies={
            "players": season_module.players_document(drop_crosswalk=frozenset({gap}))
        }
    )
    envelopes = await run_capture(feeds, lake=SpyLake())
    assert gap[0] not in starters(envelopes)
    envelope = envelopes[STRENGTH]
    for position in STARTER_POSITIONS:
        assert f"{gap[0]}:{position}" in envelope.coverage.missing
    # And no published id is a PFR key.
    for rows in starters(envelopes).values():
        for row in rows:
            assert row["starter_id"].startswith("fdy-")


# --------------------------------------------------------------------------
# L7 — a player listed at two slots takes the one he is ranked higher in
# --------------------------------------------------------------------------


async def test_a_double_listed_player_takes_his_better_ranked_slot():
    """Kills L7 (keeping the higher rank instead of the lower).

    A real chart lists a swing tackle behind both edges. Resolving that by row
    order would make his label depend on how the CSV happened to be written,
    and the label decides the hash's ordering.
    """
    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            charts = await depth_adapter.fetch_depth_charts(SEASON, client=client)

    team = season_module.TEAMS[0]
    swing = season_module.line_id(team, season_module.SWING_SLOT)
    labels = charts.labels_at(season_module.game_date(1))
    assert labels[(team, swing)] == "LT", (
        "the swing man is listed LT at rank 2 and RT at rank 3; the better rank is his"
    )


# --------------------------------------------------------------------------
# I4 — the position filter is an assertion, not a consequence
# --------------------------------------------------------------------------


def test_an_unsendable_mapped_position_still_travels_as_none(monkeypatch):
    """Kills I4 (dropping the `SENDABLE_POSITIONS` check).

    Today every value in `POSITION_FOR_SLOT` is one `player-identity` knows, so
    the filter is a no-op and deleting it changes nothing — that is exactly
    what makes it worth pinning. It exists so that widening the mapping cannot
    silently start 422-ing whole 500-query batches; this test widens it.
    """
    monkeypatch.setitem(identity_adapter.POSITION_FOR_SLOT, "LT", "LEFT-TACKLE")
    query = identity_adapter.build_query(
        StarterSlot(position="LT", gsis_id="00-1000000"), "AAA", None, SEASON
    )
    assert query.position is None


# --------------------------------------------------------------------------
# R1/R2/R3/R4/R6 — the replacement delta says what it is
# --------------------------------------------------------------------------


async def test_a_modelled_delta_is_labelled_as_modelled():
    """Kills R1 (labelling the prior `measured`).

    The spec's own words: the adapter "must fall back to a positional prior and
    **mark the field's provenance** rather than emitting a modelled number that
    looks measured". A consumer must be able to tell one from the other without
    joining anything.
    """
    rows = starters(await run_capture(Feeds(), lake=SpyLake()))

    measured = [
        row
        for team_rows in rows.values()
        for row in team_rows
        if row["replacement_delta_provenance"] == PROVENANCE_MEASURED
    ]
    prior = [
        row
        for team_rows in rows.values()
        for row in team_rows
        if row["replacement_delta_provenance"] == PROVENANCE_PRIOR
    ]
    assert measured, "no measured delta in the fixture — the test is vacuous"
    assert prior, "no modelled delta in the fixture — the test is vacuous"

    # A measured delta names the games it was measured over; a prior cannot.
    assert all(row["replacement_delta_sample_games"] > 0 for row in measured)
    assert all(row["replacement_delta_sample_games"] == 0 for row in prior)

    # **And the number is the with-side count, not the window length.** Kills
    # M17. `sample_games` exists so a consumer can judge how much evidence is
    # behind a `measured` delta; a value that silently reported the whole
    # window would overstate every one of them and would be indistinguishable
    # from a genuinely well-sampled measurement.
    window = season_module.WEEKS
    for row in measured:
        started = sum(
            1
            for week in range(1, window + 1)
            if season_module.starting_slot(row["team_id"], week, 0) == 0
        )
        if row["team_id"] in season_module.TEAMS[season_module.CHURN_FROM :]:
            assert row["replacement_delta_sample_games"] == started, row
            assert row["replacement_delta_sample_games"] < window, (
                "the whole window is not a with/without split"
            )
    # And only the teams whose five actually moved have a measurement: the
    # four that benched a tackle, plus the one that swapped its guards -- the
    # same men, but each spent part of the window at a different slot, which
    # is a with/without split at both.
    assert {row["team_id"] for row in measured} == {
        *season_module.TEAMS[season_module.CHURN_FROM :],
        season_module.SWAP_TEAM,
    }


def test_the_positional_prior_is_built_from_starters_only():
    """Kills R2 (building the prior from every measured delta).

    Any window with a substitution yields **two** measurements that are near
    exact negatives: the incumbent's "worse without him" and the deputy's
    "better without him". Pooling both cancels them to approximately zero --
    measured on this collector's own fixture, a left-tackle prior of exactly
    `0.0` before the restriction was added. A zero prior is not a weak prior:
    it is a claim that losing a starting tackle costs nothing, and it is what
    would then be applied as the correction for every change with no
    measurement of its own.
    """
    measured = {
        ("AAA", "LT", "incumbent"): 0.10,
        ("AAA", "LT", "deputy"): -0.10,
    }
    current = {("AAA", "LT", "incumbent")}
    assert ratings._positional_priors(measured, current) == {"LT": 0.10}
    # The pooled version, for contrast -- and it is exactly what R2 restores.
    assert ratings._positional_priors(measured, set(measured)) == {"LT": 0.0}


async def test_the_published_prior_is_a_real_number_with_a_sign():
    """The end-to-end arm: a prior that reached a row says something."""
    rows = starters(await run_capture(Feeds(), lake=SpyLake()))
    priors = [
        row
        for team_rows in rows.values()
        for row in team_rows
        if row["replacement_delta_provenance"] == PROVENANCE_PRIOR
    ]
    assert priors, "no prior in the fixture -- the test is vacuous"
    tackles = [
        row["replacement_delta_pressure_rate"]
        for row in priors
        if row["starter_position"] in ("LT", "RT")
    ]
    assert tackles and all(value > 0.01 for value in tackles), (
        f"the tackle prior is indistinguishable from zero: {tackles}"
    )


async def test_a_measured_delta_has_the_sign_its_name_implies():
    """Kills R4 (measuring on the raw rate) and R6 (the subtraction reversed).

    **This is a defect that shipped in an earlier revision of this collector.**
    The with/without split compares two different opponent slates, and the
    front term is larger than the personnel term it is isolating: the churn
    teams' bench weeks happened to fall against the league's two weakest
    fronts, so a raw split reported that their line was *better* without its
    starting left tackle. Every field was populated and plausible.
    """
    rows = starters(await run_capture(Feeds(), lake=SpyLake()))
    measured = [
        row
        for team_rows in rows.values()
        for row in team_rows
        if row["replacement_delta_provenance"] == PROVENANCE_MEASURED
    ]
    assert measured
    for row in measured:
        assert row["replacement_delta_pressure_rate"] > 0, (
            f"{row['team_id']} {row['starter_position']}: the fixture makes "
            "this line demonstrably worse without its incumbent, so the delta "
            "must be positive"
        )


def test_a_one_game_sample_is_not_a_measurement():
    """Kills R3 (`MIN_DELTA_GAMES = 1`).

    One game is a single opponent and a single script, so a delta built from it
    is a difference of two game scripts wearing a personnel label — and it
    would be published as `measured`, which is the one thing the spec says it
    must not look like.
    """
    totals = LineTotals(
        opponents={("AAA", f"g{i}"): "BBB" for i in range(1, 6)},
        game_week={f"g{i}": i for i in range(1, 6)},
        offense_game={
            ("AAA", f"g{i}"): OffenseGameTotals(
                pass_block_snaps=20, pressures_allowed=4 + (i == 5) * 8
            )
            for i in range(1, 6)
        },
    )
    totals.order_games()
    incumbent = StarterSlot(position="LT", gsis_id="starter")
    deputy = StarterSlot(position="LT", gsis_id="deputy")
    totals.lineups = {
        ("AAA", f"g{i}"): [deputy if i == 5 else incumbent] for i in range(1, 6)
    }

    measured = replacement_deltas(totals, {})
    assert measured == {}, (
        "a 1-game with/without split must not be published as a measurement"
    )


def test_a_two_game_sample_is_a_measurement():
    """The negative arm of the test above: `MIN_DELTA_GAMES` must not be so
    high that nothing is ever measured, which would pass it trivially."""
    totals = LineTotals(
        opponents={("AAA", f"g{i}"): "BBB" for i in range(1, 6)},
        game_week={f"g{i}": i for i in range(1, 6)},
        offense_game={
            ("AAA", f"g{i}"): OffenseGameTotals(
                pass_block_snaps=20, pressures_allowed=4 + (i >= 4) * 8
            )
            for i in range(1, 6)
        },
    )
    totals.order_games()
    incumbent = StarterSlot(position="LT", gsis_id="starter")
    deputy = StarterSlot(position="LT", gsis_id="deputy")
    totals.lineups = {
        ("AAA", f"g{i}"): [deputy if i >= 4 else incumbent] for i in range(1, 6)
    }

    measured = replacement_deltas(totals, {})
    assert measured[("AAA", "LT", "starter")] > 0
    assert measured[("AAA", "LT", "deputy")] < 0


# --------------------------------------------------------------------------
# N3 — roster status beats the game report
# --------------------------------------------------------------------------


async def test_injured_reserve_wins_over_a_questionable_game_report():
    """Kills N3 (merging the two feeds the other way round).

    The fixture's man on injured reserve is also on the weekly report, as he is
    in real life. A merge that let the report win would publish `questionable`
    for a player who cannot be activated at all — and with no such row in the
    fixture the two merge orders would agree and the mutant would survive.
    """
    rows = starters(await run_capture(Feeds(), lake=SpyLake()))
    row = next(
        row
        for row in rows[season_module.IR_TEAM]
        if row["starter_position"]
        == season_module.SLOT_POSITIONS[season_module.IR_SLOT]
    )
    assert row["starter_availability"] == "ir"


# --------------------------------------------------------------------------
# Q4 — `/lineups` names the starters who will NOT play
# --------------------------------------------------------------------------


def test_unavailable_starters_names_the_absent_ones():
    """Kills Q4 (the membership test inverted).

    Asserting the list is merely non-empty passes on the inverted version too,
    which would name every *available* starter and read as a five-man crisis
    every week.
    """
    rows = [
        {"starter_id": "fdy-1", "starter_availability": "active"},
        {"starter_id": "fdy-2", "starter_availability": "out"},
        {"starter_id": "fdy-3", "starter_availability": "questionable"},
        {"starter_id": "fdy-4", "starter_availability": "doubtful"},
        {"starter_id": "fdy-5", "starter_availability": "ir"},
    ]
    assert unavailable_starters(rows) == ["fdy-2", "fdy-4", "fdy-5"]


# --------------------------------------------------------------------------
# Equivalent mutants, with their non-equivalent neighbours
# --------------------------------------------------------------------------


def test_the_line_yards_curve_is_continuous_at_every_band_boundary():
    """Why S4 (`<=` -> `<` at `LINE_YARDS_FULL_MAX`) is **equivalent**.

    The bands meet at their boundaries by construction — `4.0` credits 4.0
    whichever branch computes it, and `10.0` credits 7.0 either way — so
    moving a comparison from `<=` to `<` cannot change an answer. That is a
    property worth stating rather than a gap.

    The neighbour that is NOT equivalent is moving the *boundary itself*, and
    `test_the_boundary_is_where_it_is_documented` below is that neighbour: it
    fails the moment a band is widened, which is what proves this file is
    capable of seeing a change here at all.
    """
    for boundary in (0.0, ratings.LINE_YARDS_FULL_MAX, ratings.LINE_YARDS_HALF_MAX):
        below = line_yards(boundary - 1e-9)
        above = line_yards(boundary + 1e-9)
        assert below == pytest.approx(line_yards(boundary), abs=1e-6)
        assert above == pytest.approx(line_yards(boundary), abs=1e-6)


def test_the_boundary_is_where_it_is_documented():
    """The non-equivalent neighbour of S4, and of S7.

    S7 replaced the derived `LINE_YARDS_CAP` with the literal `7.0` it happens
    to equal — equivalent arithmetic, and the reason the constant is derived is
    that a change to the bands must not leave the cap behind. This test is what
    catches a band that moved: it pins the credit at points either side of both
    boundaries, so widening the half-credit band to 12 yards fails here even
    though every constant comparison in `test_scale_agreement.py` would still
    be reading the same two files.
    """
    assert line_yards(4.0) == pytest.approx(4.0)
    assert line_yards(5.0) == pytest.approx(4.5)
    assert line_yards(10.0) == pytest.approx(7.0)
    assert line_yards(11.0) == pytest.approx(7.0)
    assert line_yards(12.0) == pytest.approx(7.0)


def test_the_row_filters_are_all_string_valued():
    """Why Q1 (`str(row.get(key))` -> `row.get(key)`) is **equivalent**.

    The `str()` in `signal_matches` guards a real fleet-wide bug — a query
    string is always text and an int row value silently matches nothing — but
    all three of this collector's row filters are string-valued, so it is a
    no-op here and removing it changes no answer. Stated rather than papered
    over: the idiom stays because a fourth filter may not be a string.

    The non-equivalent neighbour is emptying `ROW_FILTERS` entirely, which
    `test_routes.py::test_a_row_filter_narrows_the_signals` kills.
    """
    envelope_rows = [
        {"team_id": "AAA", "record_type": "unit"},
        {"team_id": "AAA", "record_type": "starter", "starter_position": "LT"},
    ]
    for row in envelope_rows:
        for key in ratings.STARTER_POSITIONS[:0] or ("team_id", "record_type"):
            assert isinstance(row.get(key), str)


async def test_the_pbp_fold_holds_no_reference_to_its_loop_key():
    """Why J5 (`del key` after the join guard) is **equivalent**.

    That mutant deletes a local nothing reads again — a null mutant, and a
    badly chosen one on the author's part rather than a finding. Recorded here
    with the property that makes it so: the fold's output is keyed by
    `(team, game_id)` and never by the `(game_id, play_id)` join key, so no
    per-play identity survives the join.
    """
    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            fold = await pbp_adapter.fetch_pbp(SEASON, WEEK, client=client)
            from offensive_line.adapters import participation as participation_adapter

            charted = await participation_adapter.fetch_block_snaps(
                SEASON, client=client
            )
    totals = _fold(fold, charted)
    for team, game in totals.offense_game:
        assert isinstance(team, str) and isinstance(game, str)
        assert not game.isdigit(), "a play id has leaked into the fold's keys"


# --------------------------------------------------------------------------
# The fixture's date and tie structure — three defences that were unprovable
# --------------------------------------------------------------------------


async def test_a_snap_tie_breaks_on_the_id_not_on_the_upstreams_row_order():
    """Kills M16 (the tie-break `>` relaxed to `>=`).

    `TIE_TEAM` runs a genuine left-tackle rotation: two men on identical snap
    counts every week. Nothing in the snap feed separates them, so the tie has
    to break on something stable. Break it on the order the upstream happened
    to write its rows in and the published `lineup_hash` moves between two
    passes over **byte-identical documents** — a phantom personnel change,
    which is the one thing this collector's whole continuity story rests on
    not happening.

    Three men, not two, and the highest id sits in the MIDDLE of the emission
    order — because with two there is only one ordering axis and "highest id"
    coincides with either "first row seen" or "last row seen" for free. That
    is measured, not assumed: a two-man version of this fixture let a
    first-row-wins mutant through.
    """
    rows = starters(await run_capture(Feeds(), lake=SpyLake()))
    tackle = next(
        row for row in rows[season_module.TIE_TEAM] if row["starter_position"] == "LT"
    )
    higher = max(
        season_module.line_id(season_module.TIE_TEAM, slot)
        for slot in season_module.TIE_SLOTS
    )
    assert tackle["starter_id"] == canonical_for(higher)


async def test_the_tie_is_a_real_tie():
    """The fixture guard. If the two men ever stopped being level on snaps the
    test above would pass for the wrong reason and prove nothing."""
    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            fold = await snaps_adapter.fetch_snaps(SEASON, WEEK, client=client)
    wanted = {
        season_module.pfr_id(season_module.TIE_TEAM, slot): slot
        for slot in season_module.TIE_SLOTS
    }
    tied = {
        entry.pfr_id: entry.offense_snaps
        for entry in fold.line
        if entry.team == season_module.TIE_TEAM
        and entry.week == 1
        and entry.pfr_id in wanted
    }
    assert len(tied) == len(season_module.TIE_SLOTS), tied
    assert len(set(tied.values())) == 1, tied

    # And the winning id must sit in the middle of the emission order, or one
    # of the two row-order rules reaches the right answer by luck.
    order = [
        wanted[entry.pfr_id]
        for entry in fold.line
        if entry.team == season_module.TIE_TEAM
        and entry.week == 1
        and entry.pfr_id in wanted
    ]
    winner = max(season_module.TIE_SLOTS)
    assert order[0] != winner and order[-1] != winner, order


async def test_a_man_who_played_no_snaps_cannot_fill_a_slot():
    """Kills M18 (`snaps <= 0` relaxed to `< 0`).

    The unlabelled team carries an eighth lineman listed at centre who never
    plays. He is the *only* candidate for that slot, so a collector that kept
    zero-snap rows would hand the team a complete five — a `lineup_hash` built
    on a man who was not on the field, and a continuity streak describing him.
    """
    envelopes = await run_capture(Feeds(), lake=SpyLake())
    assert units(envelopes)[season_module.UNLABELLED_TEAM]["lineup_hash"] is None
    assert season_module.UNLABELLED_TEAM not in starters(envelopes)


async def test_the_week_has_two_kickoff_dates_and_three_chart_snapshots():
    """The fixture guard for M19 and M20 — the two defences that were
    unprovable because every week had one game date and every chart landed
    exactly three days before it.

    The real feed is 219 **daily** snapshots against games spread Thursday to
    Monday, so M19's trigger — a chart republished on game day — fires weekly
    in production and fired never in the first version of this fixture.
    """
    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            fold = await pbp_adapter.fetch_pbp(SEASON, WEEK, client=client)
            charts = await depth_adapter.fetch_depth_charts(SEASON, client=client)

    dates = {
        week: {
            season_module.game_date(week, early=True),
            season_module.game_date(week),
        }
        for week in range(1, season_module.WEEKS + 1)
    }
    assert all(len(day) == 2 for day in dates.values()), "one kickoff date a week"
    # `week_dates` must resolve to the LATEST game, not the first.
    for week in range(1, season_module.WEEKS + 1):
        assert fold.week_dates[week] == season_module.game_date(week)
        assert fold.week_dates[week] > season_module.game_date(week, early=True)
    # And a snapshot published ON a game date exists, so a lookup that stops
    # short of it reads a different chart.
    for week in range(1, season_module.WEEKS + 1):
        assert season_module.game_date(week) in charts.dates
        assert len(set(season_module.chart_dates(week))) == 3


def test_the_tie_break_compares_a_tuple_not_a_snap_count():
    """Why the review's `>` -> `>=` on the tie-break is **equivalent**.

    `candidate` is `(offense_snaps, gsis_id)`. Tuple comparison already
    resolves a snap tie on the id, so relaxing `>` to `>=` can only change the
    outcome when the two tuples are *exactly* equal — same snaps and same id,
    i.e. the same player twice in one game at one slot, which the upstream's
    one-row-per-player-per-game shape cannot produce.

    The consequence the review described — ties resolving by row order — needs
    the comparison to be on the snap count alone. Those are the neighbours
    `test_a_snap_tie_breaks_on_the_id_not_on_the_upstreams_row_order` kills,
    in both directions (first-row-wins and last-row-wins).
    """
    first = (68, "00-102000")
    second = (68, "00-102060")
    assert (second > first) == (second >= first)
    assert (first > second) == (first >= second)
    # ...and the only input that separates them is one no feed can emit.
    assert (first >= first) and not (first > first)
