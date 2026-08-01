"""team-scheme's binding to the shared collector metrics.

The fleet-wide series (`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library. The three below answer
"is THIS collector wrong in the way only it can be wrong", which
`collector_coverage_ratio` structurally cannot — every one of them describes a
failure in which coverage reads 1.0.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is consumed by a collection, so a value written on a capture
cadence is absent from every scrape that does not immediately follow one — and
PromQL cannot tell an absent series from a healthy idle one.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "team-scheme"


class TeamSchemeMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)

        self._window_refusals = LastValueGauge(
            meter,
            "team_scheme_window_refusals",
            description=(
                "Teams whose rate window was refused by the team-season guard "
                "in the last pass, by collector."
            ),
        )
        self._min_games_sampled = LastValueGauge(
            meter,
            "team_scheme_min_games_sampled",
            description=(
                "The smallest games_sampled behind any published profile in "
                "the last pass, by collector."
            ),
        )
        self._degraded_upstreams = LastValueGauge(
            meter,
            "team_scheme_degraded_upstreams",
            description=(
                "Optional upstreams missing in the last pass (0, 1 or 2), by collector."
            ),
        )
        self._rate_outliers = LastValueGauge(
            meter,
            "team_scheme_rate_outliers",
            description=(
                "Published rates sitting further outside the other teams' "
                "range than that range is wide, in the last pass, by collector."
            ),
        )

    def window_refusals(self, count: int) -> None:
        """The guard's firing count. **Zero is the expected value.**

        Non-zero means the collector built a rate window that is not one
        team's regular season — a bug in this service, not an upstream
        problem, because `pbp.py` accumulates per `(team, week)` and
        `rates.py` folds a selection of those. Alert on `> 0`, not on a rate of
        change.
        """
        self._window_refusals.set(count, {"collector": self.collector})

    def min_games_sampled(self, count: int) -> None:
        """The smallest sample behind any published profile.

        **The failure coverage cannot see.** A play-by-play document truncated
        in the *week* direction — half a season, an interrupted release build
        — still carries all 32 teams, so every team is present and
        `collector_coverage_ratio` reads 1.0 while every rate is computed over
        three weeks instead of twelve. Every number is wrong and nothing in
        the coverage accounting moves.

        Read it against the week: by week 10 it should sit near 9 (a team has
        had one bye). A value far below the week number with coverage at 1.0
        is the truncation signature. 0 with rows published means at least one
        team is in the document and has not played, which is normal in week 1
        and suspicious in week 12.
        """
        self._min_games_sampled.set(count, {"collector": self.collector})

    def degraded_upstreams(self, count: int) -> None:
        """How many of the two optional feeds were missing, 0..2.

        Also invisible to coverage: `personnel_rates`, `play_action_rate` and
        `pre_snap_motion_rate` going null costs no coverage at all, because a
        team is counted present on `neutral_pass_rate`. That is the right
        coverage predicate — the phase doc's — and it means the loss of the
        46.82 MiB participation feed would otherwise be a perfectly healthy
        pass with three fields quietly nulled on every row.

        A sustained 1 or 2 is a feed that stopped publishing, not a blip:
        `collector_capture_failures_total` counts each occurrence, this says
        whether the current state is degraded.
        """
        self._degraded_upstreams.set(count, {"collector": self.collector})

    def rate_outliers(self, count: int) -> None:
        """Published rates that sit outside the shape of their own pass.

        **The third failure coverage cannot see, and the one no mutation can
        express.** An implausible value is a perfectly present team with a
        perfectly valid row: coverage reads 1.0, the schema validates, the
        four-decimal contract holds, and the number is wrong. Live 2025
        publishes `no_huddle_rate = 0.6223` for WAS against a 0.0748 league
        median — arithmetically correct here, and almost certainly an
        artefact in nflfastR's `no_huddle` column for that team-season.

        **A non-zero value here does not mean the collector is broken**, and
        that is the difference between this gauge and `window_refusals`. It
        means one team's number is unlike the other thirty-one's and a human
        should look at which. The row publishes either way — see
        `rates.flag_dispersion_outliers` for why this reports rather than
        refuses. Alert on a *sustained* non-zero, not on a single pass.
        """
        self._rate_outliers.set(count, {"collector": self.collector})


metrics = TeamSchemeMetrics()
