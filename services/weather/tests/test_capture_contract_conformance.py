"""Producer-side contract conformance: `capture_week`'s real output must
validate against `contracts/signal-envelope/collectors/weather.json`.

FINDING 4: the repo-root `tests/test_signal_envelope_conformance.py` only
validates committed static fixtures against the committed collector schema --
both hand-maintained files -- so it catches fixture drift, never producer
drift. And `contracts/responses/weather.json` pins `envelopes[].signals[]`
down to only `{game_id, team, venue_id}`, generated from `seeded_state`'s mock
rows rather than a real capture. A field rename inside `capture.py` (e.g.
`playability` -> `playability_score`) passed the entire suite -- 219 tests --
before this file existed. This closes the gap by running the real capture
path against mocked upstreams and validating its real signal rows.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
from collector_core.lake import NullLakeWriter
from jsonschema import Draft202012Validator, FormatChecker

from weather.adapters.forecast import FORECAST_URL
from weather.adapters.schedule import SCHEDULE_URL
from weather.capture import capture_week

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "signal-envelope"
    / "collectors"
    / "weather.json"
)

SCHEDULE_HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)
HOME_GAME = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)


def _hourly_payload() -> dict:
    dates = sorted({"2026-09-13", NOW.strftime("%Y-%m-%d")})
    times = [f"{d}T{h:02d}:00" for d in dates for h in range(24)]
    n = len(times)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": [68.0] * n,
            "apparent_temperature": [67.0] * n,
            "relative_humidity_2m": [62] * n,
            "wind_speed_10m": [11.0] * n,
            "wind_gusts_10m": [18.0] * n,
            "wind_direction_10m": [210] * n,
            "precipitation": [0.0] * n,
            "precipitation_probability": [10] * n,
        }
    }


@respx.mock
async def test_capture_week_output_conforms_to_the_collector_contract():
    """Runs the real producer against mocked upstreams -- not a static
    fixture -- so this actually proves the schema describes what
    `capture_week` emits today, in both signal types it produces.

    `Draft202012Validator` with a `FormatChecker` attached, matching the
    repo-root conformance suite: plain `jsonschema.validate` does not enforce
    `format`, and `forecast_valid_at` is `format: date-time`.
    """
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=f"{SCHEDULE_HEADER}\n{HOME_GAME}\n")
    )
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=_hourly_payload())
    )

    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    collector_schema = json.loads(CONTRACT.read_text())["signal_types"]

    for signal_type, envelope in result.items():
        field_schema = collector_schema[signal_type]
        validator = Draft202012Validator(field_schema, format_checker=FormatChecker())
        assert envelope.signals, f"{signal_type}: no signal rows captured to validate"
        for row in envelope.signals:
            validator.validate(row)
