"""coaching-scheme's binding to the shared collector metrics.

The fleet-wide series (`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library. The four below answer
"is THIS collector wrong in the way only it can be wrong", which
`collector_coverage_ratio` structurally cannot: coverage is computed against
the same revisions that drove the fetch, so a *blended* rate and a correct one
have identical coverage.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is consumed by a collection, so a value written on a capture
cadence is absent from every scrape that does not immediately follow one — and
PromQL cannot tell an absent series from a healthy idle one.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "coaching-scheme"


class CoachingSchemeMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)

        self._window_straddles = LastValueGauge(
            meter,
            "coaching_scheme_window_straddles",
            description=(
                "Revisions whose rate window was refused by guard 1 in the "
                "last pass, by collector."
            ),
        )
        self._unexplained_changepoints = LastValueGauge(
            meter,
            "coaching_scheme_unexplained_changepoints",
            description=(
                "Teams whose weekly PROE series shifted with no corresponding "
                "staff revision, in the last pass, by collector."
            ),
        )
        self._play_callers_identified = LastValueGauge(
            meter,
            "coaching_scheme_play_callers_identified",
            description=(
                "Teams with an in-force curated play-caller assertion, in the "
                "last pass, by collector."
            ),
        )
        self._staff_revisions = LastValueGauge(
            meter,
            "coaching_scheme_staff_revisions",
            description=(
                "Staff revisions the schedule feed described in the last "
                "pass, by collector."
            ),
        )

    def window_straddles(self, count: int) -> None:
        """Guard 1's firing count. **Zero is the expected value.**

        Non-zero means the collector built a rate window that does not lie
        inside the revision it was keyed to — a bug in this service, not an
        upstream problem, because `pbp.py` accumulates per week and `rates.py`
        folds a selection of those. Alert on `> 0`, not on a rate of change.
        """
        self._window_straddles.set(count, {"collector": self.collector})

    def unexplained_changepoints(self, count: int) -> None:
        """Guard 2's firing count. **Non-zero is expected on a live season.**

        The opposite reading from `window_straddles`, and the difference
        matters. `adapters/games.py` measured that nfldata's coach columns
        carry no mid-season change for 2024 or 2025 despite several real ones,
        so on a current season this gauge is where those changes appear. A
        season-long zero here alongside `staff_revisions == 32` is not a quiet
        year; it is both detectors reporting nothing, which for a league that
        fires two to four coaches a year is the suspicious state.
        """
        self._unexplained_changepoints.set(count, {"collector": self.collector})

    def play_callers_identified(self, count: int) -> None:
        """Teams with a non-expired curated play-caller assertion, 0..32.

        The curation backlog as a number. It is deliberately NOT derivable
        from `collector_coverage_ratio` alone — that ratio also moves when the
        schedule feed loses a team — and it is the metric that says whether
        the remedy is a code change or a line in `play_callers.py`.
        """
        self._play_callers_identified.set(count, {"collector": self.collector})

    def staff_revisions(self, count: int) -> None:
        """Revisions the schedule feed described, across all teams.

        Exactly 32 means one revision per team: either a genuinely stable
        season or, far more likely on a current one, the un-backfilled feed
        described in `adapters/games.py`. Read next to
        `unexplained_changepoints`.
        """
        self._staff_revisions.set(count, {"collector": self.collector})


metrics = CoachingSchemeMetrics()
