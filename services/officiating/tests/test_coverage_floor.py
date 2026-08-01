"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned. These are the
tests that fail if somebody "simplifies" `capture.py` by computing the
expectation from the rows it got back.

This collector has a sharper version of the trap than most, and it is the
subject of the first two tests: **the officials feed is post-game**. In week 3
it describes 48 games out of 272, so "games the crew feed knows about" is a
number that starts small and grows — and using it as the expectation reports
`ratio 1.0` all season while most of the season is missing.

Every test here attacks BOTH halves. Mutating only the floor scores well and
still misses a deleted `present` check, because the floor and the present
predicate fail in opposite directions: a wrong floor understates coverage on a
healthy pass, and a wrong `present` overstates it on a broken one.
"""

import httpx
import respx

from officiating.capture import (
    ASSIGNMENT,
    EXPECTED_FLOOR,
    RATES,
    SIGNAL_TYPES,
    capture_officiating,
)

from .conftest import NOW, SEASON, SpyLake, mock_upstreams, season_of


async def _capture(lake=None, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_officiating(
            SEASON,
            1,
            client=client,
            lake=lake or SpyLake(),
            now=NOW,
            **kwargs,
        )


@respx.mock
async def test_a_post_game_feed_partway_through_the_season_reports_the_gap():
    """THE failure this collector's floor exists for.

    The schedule lists all 48 games; the officials feed has published crews for
    only the first 16, because the rest have not been played. `expected` must
    stay the full season's floor, `present` must be 16, and every unplayed game
    must appear as a miss with a reason — not vanish from the universe.
    """
    schedule = season_of(crews=8, weeks=6)
    played = [g for g in schedule if g.week == 1]
    mock_upstreams(
        schedule,
        officials_response=httpx.Response(200, text=_officials_for(played)),
    )

    envelopes = await _capture()
    coverage = envelopes[ASSIGNMENT].coverage

    assert coverage.present == 8
    assert coverage.expected == EXPECTED_FLOOR[ASSIGNMENT]
    assert coverage.ratio < 0.05
    reasons = {error["reason"] for error in envelopes[ASSIGNMENT].errors}
    assert "crew_not_published" in reasons, reasons
    # The 40 unplayed games are named, not merely counted.
    assert len(coverage.missing) == 40


def _officials_for(games):
    from .conftest import officials_csv

    return officials_csv(games)


@respx.mock
async def test_a_truncated_upstream_does_not_report_full_coverage():
    """An upstream returning one crew of eight must not yield ratio 1.0."""
    schedule = season_of(crews=8, weeks=6)
    one_crew = [g for g in schedule if g.referee_id == "701"]
    mock_upstreams(
        schedule, officials_response=httpx.Response(200, text=_officials_for(one_crew))
    )

    envelopes = await _capture()

    assert envelopes[ASSIGNMENT].coverage.present == 6
    assert envelopes[ASSIGNMENT].coverage.expected == EXPECTED_FLOOR[ASSIGNMENT]
    assert envelopes[RATES].coverage.present == 1
    assert envelopes[RATES].coverage.expected == EXPECTED_FLOOR[RATES]
    for signal_type in SIGNAL_TYPES:
        reasons = {e["reason"] for e in envelopes[signal_type].errors}
        assert "below_expected_floor" in reasons, (signal_type, reasons)


@respx.mock
async def test_an_empty_upstream_reports_zero_not_one():
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a pass that captured nothing. The floor is what
    keeps `expected` non-zero here."""
    schedule = season_of()
    mock_upstreams(
        schedule, officials_response=httpx.Response(200, text=_officials_for([]))
    )

    envelopes = await _capture()

    for signal_type in SIGNAL_TYPES:
        coverage = envelopes[signal_type].coverage
        assert coverage.present == 0
        assert coverage.expected == EXPECTED_FLOOR[signal_type]
        assert coverage.ratio == 0.0


@respx.mock
async def test_expansion_past_the_floor_still_reports_honestly():
    """The floor must not CAP a genuine count, only raise a short one.

    Twenty crews is more than the league fields, but a floor that capped would
    report `expected: 17, present: 20` — a ratio above 1.0, or a silently
    dropped crew, depending on which side got clamped.
    """
    schedule = season_of(crews=20, weeks=6)
    mock_upstreams(schedule)

    envelopes = await _capture()

    assert envelopes[RATES].coverage.expected == 20
    assert envelopes[RATES].coverage.present == 20
    assert envelopes[RATES].coverage.ratio == 1.0
    assert envelopes[ASSIGNMENT].coverage.expected == EXPECTED_FLOOR[ASSIGNMENT]
    assert envelopes[ASSIGNMENT].coverage.present == 120


@respx.mock
async def test_a_game_with_no_referee_is_a_miss_not_a_dropped_row():
    """Without a white hat there is no crew key, and inventing one from the
    umpire would produce a crew that exists in no other week. The game stays
    expected, with its own reason."""
    schedule = season_of(crews=8, weeks=1)
    # Strip the referee from one game by giving it a crew of six non-referees.
    target = schedule[0]
    target.positions = ("Umpire", "Down Judge", "Line Judge")
    mock_upstreams(
        schedule,
        officials_response=httpx.Response(
            200, text=_no_referee_officials(schedule, target)
        ),
    )

    envelopes = await _capture()
    coverage = envelopes[ASSIGNMENT].coverage

    assert target.game_id in coverage.missing
    assert coverage.present == 7
    reasons = {e["reason"] for e in envelopes[ASSIGNMENT].errors}
    assert "no_referee_in_crew" in reasons, reasons


def _no_referee_officials(schedule, target):
    """`officials_csv`, minus the Referee row of one game."""
    from .conftest import officials_csv

    text = officials_csv(schedule)
    kept = [
        line
        for line in text.splitlines()
        if not (line.startswith(target.legacy_game_id) and ",Referee," in line)
    ]
    return "\n".join(kept) + "\n"


@respx.mock
async def test_a_crew_with_no_play_by_play_stays_expected_in_the_rates_universe():
    """A crew whose games play-by-play does not carry cannot have rates — but
    it is still a crew the season fielded, so it is expected and missing rather
    than absent."""
    schedule = season_of(crews=8, weeks=6)
    for game in schedule:
        if game.referee_id == "703":
            game.in_pbp = False
    mock_upstreams(schedule)

    envelopes = await _capture()
    coverage = envelopes[RATES].coverage

    assert f"{SEASON}-ref703" in coverage.missing
    assert coverage.present == 7
    assert coverage.expected == EXPECTED_FLOOR[RATES]
    reasons = {e["reason"] for e in envelopes[RATES].errors}
    assert "no_games_sampled" in reasons, reasons


@respx.mock
async def test_a_game_with_no_offensive_snaps_is_excluded_from_the_rate_window():
    """The zero-snap guard, and removing it does not merely skew a rate.

    `seconds_per_play` is `3600 / offensive_plays`, and that division happens
    inside `_rates_envelope` — which runs **after** both `try/except` blocks in
    `capture_officiating`. So a zero-snap game does not produce a degraded pass
    or a `fail_capture`: it raises `ZeroDivisionError` into
    `run_capture_loop`'s blanket handler and **the entire pass is discarded,
    including the `game_crew_assignment` capture that already succeeded in
    memory**. That is the availability inversion `publish_capture` exists to
    prevent, reintroduced by a division.

    Real inputs that produce it: play-by-play published for a game still in
    progress, or a game whose only recorded rows are special teams
    (`play_type` outside `{pass, run}`).

    The correct behaviour is to drop that one game from the window — a game
    play-by-play has an empty record for is not a game — so the crew reports
    `games_sampled: 5` where its peers report 6, and everything else survives.
    """
    schedule = season_of(crews=8, weeks=6)
    for game in schedule:
        if game.referee_id == "704" and game.week == 2:
            game.offensive_plays = 0
    mock_upstreams(schedule)

    envelopes = await _capture()

    rates = {row["crew_id"]: row for row in envelopes[RATES].signals}
    assert rates[f"{SEASON}-ref704"]["games_sampled"] == 5
    assert rates[f"{SEASON}-ref705"]["games_sampled"] == 6, "peers are unaffected"
    # The pass survived in full — both signal types, not a degraded one.
    assert envelopes[ASSIGNMENT].coverage.present == 48
    assert envelopes[RATES].coverage.present == 8
    # And the rate that would have divided by zero is finite.
    served = rates[f"{SEASON}-ref704"]["seconds_per_play"]
    assert served["raw"] is not None and served["raw"] > 0


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())


def test_the_floors_are_the_leagues_real_shape():
    """Stated as numbers rather than derived, which is the point — but they
    still have to be the RIGHT numbers, and a floor nobody checks drifts into
    being whatever made the tests pass."""
    assert EXPECTED_FLOOR[ASSIGNMENT] == 272, "17 weeks x 16 games"
    assert EXPECTED_FLOOR[RATES] == 17, "the league has fielded 17 crews since 2015"
