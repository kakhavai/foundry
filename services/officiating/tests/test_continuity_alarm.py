"""The continuity alarm — the second guard, and its four arms.

The spec: *"Alarm separately when `crew_continuity_pct` drops below 0.6 for an
assignment whose rates are being served."* Two conditions, joined, and both
halves are load-bearing:

- alarming on low continuity **alone** fires for crews making no claim about
  anyone, which trains an operator to ignore it;
- alarming on served rates **alone** fires for the healthy majority.

**A guard whose two arms look alike needs a fixture per arm, not one per
guard.** So there are four tests below, one per cell of the 2x2, and the two
that must stay silent are as important as the one that must fire — a guard that
only ever fires is indistinguishable from `return True`.

These run through the real capture path rather than calling the alarm
directly, because the join is the thing under test and it only exists once both
signal types have been built.
"""

import httpx
import respx

from officiating.capture import ASSIGNMENT, RATES, capture_officiating
from officiating.crews import CONTINUITY_ALARM_THRESHOLD

from .conftest import (
    DEFAULT_PENALTIES,
    NOW,
    SEASON,
    SpyLake,
    mock_upstreams,
    officials_csv,
    season_of,
)

ALARM = "low_continuity_with_served_rates"


async def _capture(**kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_officiating(
            SEASON, 1, client=client, lake=SpyLake(), now=NOW, **kwargs
        )


def _reassemble_one_crew(games, referee_id: str, week: int):
    """Replace every non-referee official on one crew's one game.

    A wholly different supporting cast on a single game is the churn the spec
    describes: the crew_id and the white hat are unchanged, so the rates follow
    the assignment, and six of the seven people they describe are elsewhere.
    """
    for game in games:
        if game.referee_id == referee_id and game.week == week:
            game.members = tuple(
                (f"9{index}{referee_id}", f"Substitute {index}", position)
                for index, position in enumerate(
                    (
                        "Umpire",
                        "Down Judge",
                        "Line Judge",
                        "Field Judge",
                        "Side Judge",
                        "Back Judge",
                    )
                )
            )
            return game
    raise AssertionError("no such game in the fixture")


@respx.mock
async def test_arm_one_low_continuity_with_served_rates_alarms():
    """The cell that must fire."""
    games = season_of(crews=8, weeks=6)
    churned = _reassemble_one_crew(games, "703", week=6)
    mock_upstreams(games)

    envelopes = await _capture()

    row = _row_for(envelopes[ASSIGNMENT], churned.game_id)
    assert row["crew_continuity_pct"] < CONTINUITY_ALARM_THRESHOLD
    assert _serves_any_rate(envelopes[RATES], f"{SEASON}-ref703")

    alarms = [e for e in envelopes[ASSIGNMENT].errors if e["reason"] == ALARM]
    assert len(alarms) == 1, envelopes[ASSIGNMENT].errors
    assert churned.game_id in alarms[0]["detail"]


@respx.mock
async def test_arm_two_high_continuity_with_served_rates_stays_silent():
    """The healthy majority. A guard that fires here is a guard nobody reads."""
    games = season_of(crews=8, weeks=6)
    mock_upstreams(games)

    envelopes = await _capture()

    assert all(
        row["crew_continuity_pct"] >= CONTINUITY_ALARM_THRESHOLD
        for row in envelopes[ASSIGNMENT].signals
    )
    assert len(envelopes[ASSIGNMENT].signals) == 48, "an empty loop asserts nothing"
    assert [e for e in envelopes[ASSIGNMENT].errors if e["reason"] == ALARM] == []


@respx.mock
async def test_arm_three_low_continuity_with_every_rate_refused_stays_silent():
    """The cell the join exists for, and the only one that can prove it.

    Every crew here calls exactly the same penalties in exactly the same
    volume, so the between-crew variance and the within-crew variance are both
    zero: nothing is shrinkable and no correlation is measurable, and every one
    of the eight rates is refused. The rates envelope is still fully populated
    — 8 crews, ratio 1.0 — and the crew is still churned.

    So the alarm must stay silent for a reason nothing else in this file can
    supply: not because continuity is high, not because the envelope is empty,
    but because the crew is making no claim about anyone. **This is the arm
    that separates the implemented guard from `if continuity < 0.6`.**
    """
    games = season_of(crews=8, weeks=6)
    for game in games:
        # Identical everywhere -> every rate refused. See `crew_penalties`.
        game.penalties = DEFAULT_PENALTIES
        game.offensive_plays = 120
    churned = _reassemble_one_crew(games, "703", week=6)
    mock_upstreams(games)

    envelopes = await _capture()

    row = _row_for(envelopes[ASSIGNMENT], churned.game_id)
    assert row["crew_continuity_pct"] < CONTINUITY_ALARM_THRESHOLD, (
        "the fixture must still be churned, or this arm proves nothing"
    )
    assert len(envelopes[RATES].signals) == 8, "the rates were computed, not skipped"
    assert not _serves_any_rate(envelopes[RATES], f"{SEASON}-ref703"), (
        "the fixture must actually refuse every rate, or this arm proves nothing"
    )
    assert [e for e in envelopes[ASSIGNMENT].errors if e["reason"] == ALARM] == []


@respx.mock
async def test_arm_four_high_continuity_with_no_rates_at_all_stays_silent():
    """The degraded pass. Play-by-play is unavailable, so there is no sampled
    window at all — and with no window, continuity is 1.0 by construction
    rather than by measurement, which is stated here so nobody later reads this
    arm as evidence that churn was checked."""
    games = season_of(crews=8, weeks=6)
    _reassemble_one_crew(games, "703", week=6)
    mock_upstreams(games, pbp_response=httpx.Response(404))

    envelopes = await _capture()

    assert envelopes[RATES].signals == []
    assert all(
        row["crew_continuity_pct"] == 1.0 for row in envelopes[ASSIGNMENT].signals
    )
    assert len(envelopes[ASSIGNMENT].signals) == 48
    assert [e for e in envelopes[ASSIGNMENT].errors if e["reason"] == ALARM] == []


@respx.mock
async def test_the_alarm_survives_the_error_cap():
    """`add_priority_error`'s reasoning, made concrete.

    On a post-game feed partway through the season there are hundreds of
    routine `crew_not_published` entries, and the errors array is capped at 50.
    An alarm appended to the tail would be silently deleted — a guard that
    fires into a truncated list is a guard that does not fire.
    """
    # A full schedule, but only the first two weeks played — the post-game
    # feed's normal state, and the one that produces hundreds of routine
    # `crew_not_published` entries for the alarm to be buried under.
    schedule = season_of(crews=8, weeks=12)
    played = [g for g in schedule if g.week <= 2]
    churned = _reassemble_one_crew(played, "703", week=2)
    mock_upstreams(
        schedule, officials_response=httpx.Response(200, text=officials_csv(played))
    )

    envelopes = await _capture()
    errors = envelopes[ASSIGNMENT].errors

    assert len(errors) <= 51, "capped, plus the truncation marker"
    assert any(e["reason"] == "errors_truncated" for e in errors), (
        "the fixture must actually overflow the cap, or this proves nothing"
    )
    # Second, not first, and that is the library's documented ordering:
    # `CoverageAccumulator.errors` prepends the floor shortfall ahead of
    # everything in `_errors`, so a priority error lands behind it when both
    # are present. Both survive the cap, which is the property that matters —
    # and asserting index 0 would pin an ordering the library is free to change
    # while missing the thing actually under test.
    alarms = [index for index, e in enumerate(errors) if e["reason"] == ALARM]
    assert alarms, "the alarm was capped away — the whole point of this test"
    assert max(alarms) < 3, errors[:5]
    assert any(churned.game_id in errors[index]["detail"] for index in alarms)
    # Both of the crew's two games alarm, and that is right rather than a
    # duplicate: over a two-game window, a total substitution on one of them
    # leaves BOTH assignments describing a group that only half held together.
    assert len(alarms) == 2
    # The routine entries the alarm had to survive being queued behind.
    assert sum(1 for e in errors if e["reason"] == "crew_not_published") > 40


def _row_for(envelope, game_id: str) -> dict:
    for row in envelope.signals:
        if row["game_id"] == game_id:
            return row
    raise AssertionError(f"{game_id} not in the capture")


def _serves_any_rate(envelope, crew_id: str) -> bool:
    for row in envelope.signals:
        if row["crew_id"] != crew_id:
            continue
        return any(
            isinstance(value, dict) and value.get("served") for value in row.values()
        )
    return False
