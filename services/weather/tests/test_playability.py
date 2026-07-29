import pytest

from weather.environment import Environment
from weather.playability import derive_playability

MILD = {
    "temperature_f": 68.0,
    "wind_speed_mph": 5.0,
    "wind_gust_mph": 8.0,
    "precipitation_rate_in_hr": 0.0,
    "precipitation_type": "none",
}
SEVERE = {
    "temperature_f": 18.0,
    "wind_speed_mph": 28.0,
    "wind_gust_mph": 40.0,
    "precipitation_rate_in_hr": 0.35,
    "precipitation_type": "snow",
}

SCORES = ("kicking_difficulty", "deep_pass_penalty", "ball_security_risk")


@pytest.mark.parametrize("conditions", [MILD, SEVERE])
def test_every_score_is_bounded(conditions):
    result = derive_playability(conditions, Environment.OUTDOOR)
    for name in SCORES:
        assert 0.0 <= result[name]["score"] <= 1.0, name


def test_severe_conditions_score_higher_than_mild():
    mild = derive_playability(MILD, Environment.OUTDOOR)
    severe = derive_playability(SEVERE, Environment.OUTDOOR)
    for name in SCORES:
        assert severe[name]["score"] > mild[name]["score"], name


def test_every_score_carries_the_inputs_that_produced_it():
    """A derived score with no provenance cannot be debugged when it looks wrong."""
    result = derive_playability(SEVERE, Environment.OUTDOOR)
    for name in SCORES:
        assert result[name]["inputs"], name
        for key, value in result[name]["inputs"].items():
            assert SEVERE[key] == value


def test_closed_roof_zeroes_every_score():
    """Indoors there is no weather to play through."""
    result = derive_playability(SEVERE, Environment.RETRACTABLE_CLOSED)
    for name in SCORES:
        assert result[name]["score"] == 0.0, name


def test_fixed_dome_zeroes_every_score():
    result = derive_playability(SEVERE, Environment.FIXED_DOME)
    for name in SCORES:
        assert result[name]["score"] == 0.0, name


def test_undecided_roof_scores_as_if_open():
    """Nulling would discard real information about the possible condition.
    The environment field carries the ambiguity instead."""
    undecided = derive_playability(SEVERE, Environment.RETRACTABLE_UNDECIDED)
    outdoor = derive_playability(SEVERE, Environment.OUTDOOR)
    assert undecided == outdoor


def test_kicking_difficulty_uses_wind_and_temperature():
    """Kicking formula is wind/35 + max(0, (40-temp))/120. Verify exact score."""
    # wind=5, temp=68 → 5/35 + max(0, (40-68))/120 = 0.1429 + 0 = 0.1429
    result = derive_playability(MILD, Environment.OUTDOOR)
    assert result["kicking_difficulty"]["score"] == 0.1429


def test_deep_pass_penalty_uses_gust_and_precipitation():
    """Deep pass formula is gust/45 + rate/0.6. Verify exact score."""
    # gust=8, rate=0 → 8/45 + 0/0.6 = 0.1778
    result = derive_playability(MILD, Environment.OUTDOOR)
    assert result["deep_pass_penalty"]["score"] == 0.1778


def test_ball_security_risk_uses_precipitation_and_temperature():
    """Ball security formula is rate/0.4 + max(0, (35-temp))/100. Verify exact score."""
    # rate=0, temp=68 → 0/0.4 + max(0, (35-68))/100 = 0 + 0 = 0.0
    result = derive_playability(MILD, Environment.OUTDOOR)
    assert result["ball_security_risk"]["score"] == 0.0
