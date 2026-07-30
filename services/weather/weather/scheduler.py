"""weather's one genuinely weather-shaped scheduling piece.

The loop itself, the escalation decision, staleness recording, and surviving
a failed capture are fleet machinery -- identical for every collector -- and
live in `collector_core.scheduler`, wired in by `collector_core.app`.
`next_kickoff` is the one weather-shaped piece: it reads `forecast_valid_at`
out of weather's own `venue_forecast_kickoff` signals, which no other
collector has, so it stays here and is handed to the shared app builder as a
`next_event_at` callable rather than being baked into the library.
"""

from datetime import UTC, datetime, timedelta

from collector_core.routes import CaptureState

# How long after kickoff a game still counts as in progress. Games run roughly
# three hours; beyond that the dense cadence has nothing left to observe.
GAME_DURATION = timedelta(hours=4)


def next_kickoff(state: CaptureState, now: datetime) -> datetime | None:
    """The soonest kickoff still worth watching -- upcoming, or in progress.

    A game in progress returns a past timestamp, which the shared loop reads
    as a negative delta and keeps escalated. That is deliberate: the window
    closes at the final whistle, not at kickoff.
    """
    envelope = state.envelopes.get("venue_forecast_kickoff")
    if envelope is None:
        return None

    candidates: list[datetime] = []
    for signal in envelope.signals:
        raw = signal.get("forecast_valid_at")
        if not raw:
            continue
        kickoff = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if kickoff + GAME_DURATION >= now:
            candidates.append(kickoff)
    return min(candidates) if candidates else None
