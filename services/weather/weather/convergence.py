"""weather's own extra route's supporting logic: the ordered per-kickoff
forecast series with snapshot-over-snapshot deltas. Derivable from the lake,
but every consumer would otherwise reimplement it, so it lives here as a
pure function `main.py`'s `/signals/convergence` route calls.
"""


def _delta(match: dict, previous: dict) -> dict:
    return {
        key: round(match.get(key, 0) - previous.get(key, 0), 2)
        for key in ("temperature_f", "wind_speed_mph")
    }


def build_convergence_series(
    lake, collector: str, game_id: str, season: int, week: int
) -> list[dict]:
    """The ordered forecast series for one kickoff, with per-snapshot deltas.

    `list_keys` filters to this signal type by key suffix (`lake.py`), so
    every object read back here is already a `venue_forecast_kickoff`
    envelope -- no further filtering needed on read.
    """
    keys = lake.list_keys(collector, "venue_forecast_kickoff", season, week)
    series: list[dict] = []
    previous: dict | None = None
    for key in keys:
        body = lake.read(key)
        match = next((s for s in body["signals"] if s.get("game_id") == game_id), None)
        if match is None:
            continue
        series.append(
            {
                "captured_at": body["captured_at"],
                "forecast_lead_hours": match.get("forecast_lead_hours"),
                "temperature_f": match.get("temperature_f"),
                "wind_speed_mph": match.get("wind_speed_mph"),
                "bands": match.get("bands"),
                "delta": None if previous is None else _delta(match, previous),
            }
        )
        previous = match
    return series
