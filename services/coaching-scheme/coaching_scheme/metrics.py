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
        """Guard 2's firing count. **Pinned at 0: the guard ships disabled.**

        Do not read this as "no regime changes were detected". Read it as "no
        detection was attempted". `changepoint.py` carries the measurement: a
        weekly-PROE mean-shift test fires on 65% of real team-seasons and 55%
        of *week-shuffled* ones, and the oracle test -- handed the true week
        for free -- cannot separate a real head-coach change (mean |shift|
        4.83 pts) from an arbitrary week (4.01) at n = 12, p = 0.18. Any
        effect is smaller than roughly six points and below what that sample
        can resolve, so the guard is off.

        An earlier revision of this docstring said the opposite — that a
        season-long zero here was "the suspicious state". That reading assumed
        the detector worked. It does not, and the gauge is kept only so the
        series stays present in PromQL, where an absent series and a healthy
        idle one are indistinguishable.

        **When this can become informative again:** only after guard 2 is
        re-enabled over a series with demonstrated power. Alerting on it today
        would alert on a constant.
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
        described in `adapters/games.py`.

        **This is currently the collector's only regime-change signal, and it
        is known to under-report.** The intended cross-check —
        `unexplained_changepoints` — is disabled, so a 32 here is not
        corroborated by anything. Treat it as "the feed says nothing changed",
        not as "nothing changed".
        """
        self._staff_revisions.set(count, {"collector": self.collector})


metrics = CoachingSchemeMetrics()
