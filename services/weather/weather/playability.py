"""Derived playability scores.

Each score carries the inputs that produced it. A bare number that looks wrong
is undebuggable; the same number with its inputs attached is a five-minute
question. The weightings are deliberately simple and are not the product's
value — the generator applies its own model on top.
"""

from .environment import IS_CLOSED_ENVIRONMENT, Environment


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _scored(score: float, inputs: dict) -> dict:
    return {"score": round(_clamp(score), 4), "inputs": inputs}


def derive_playability(forecast: dict, environment: Environment) -> dict:
    """Three 0-1 scores describing how much the weather interferes with play.

    A closed roof zeroes everything — there is no weather to play through. An
    undecided retractable scores as if open, because nulling would discard real
    information about the condition the game may be played in. The `environment`
    field carries the ambiguity instead.
    """
    if environment in IS_CLOSED_ENVIRONMENT:
        zero = {"environment": str(environment)}
        return {
            name: _scored(0.0, zero)
            for name in (
                "kicking_difficulty",
                "deep_pass_penalty",
                "ball_security_risk",
            )
        }

    wind = float(forecast["wind_speed_mph"])
    gust = float(forecast["wind_gust_mph"])
    temp = float(forecast["temperature_f"])
    rate = float(forecast["precipitation_rate_in_hr"])

    # Kicking degrades with sustained wind first and cold second.
    kicking = wind / 35.0 + max(0.0, (40.0 - temp)) / 120.0

    # Deep passing is gust-sensitive rather than mean-wind-sensitive: an
    # unpredictable ball is worse than a consistently fast one.
    deep_pass = gust / 45.0 + rate / 0.6

    # Ball security is precipitation and cold — a wet cold ball is fumbled.
    ball_security = rate / 0.4 + max(0.0, (35.0 - temp)) / 100.0

    return {
        "kicking_difficulty": _scored(
            kicking, {"wind_speed_mph": wind, "temperature_f": temp}
        ),
        "deep_pass_penalty": _scored(
            deep_pass, {"wind_gust_mph": gust, "precipitation_rate_in_hr": rate}
        ),
        "ball_security_risk": _scored(
            ball_security,
            {"precipitation_rate_in_hr": rate, "temperature_f": temp},
        ),
    }
