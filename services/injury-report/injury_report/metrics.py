"""injury-report's binding to the shared collector metrics.

A subclass, because this collector has two failure modes `collector_coverage_
ratio` cannot see on its own, and both are named in the phase doc:

**Silent under-coverage, per practice day.** The doc asks for
`injury_report_teams_published / teams_with_games` tracked *per practice day*,
because a club's feed breaking on Friday is invisible in a week-level ratio
that Wednesday and Thursday already filled. Two gauges rather than one
pre-divided ratio: PromQL divides them just as easily, and the counts
themselves are what tell an operator whether Friday is short by one club or by
twenty.

**Rows this collector declined to map.** An unrecognised designation, an
unresolvable player, a club filing for a week it has no game in — each one is
a row that emitted nothing. They are recorded in `errors` too, but that array
is capped at 50 and lives inside an envelope; a counter is what can be alerted
on and graphed by reason.

**A pass where narrowing dropped every candidate row.** `coverage.ratio` is
team-keyed here (see `capture.py`), so it cannot distinguish "narrowing
excluded every player this pass resolved" from "a genuinely quiet week" —
both read as a healthy ratio with `player_injury_status.signals == []`. That
is exactly the "we failed" vs "we never tried" conflation
`collector_core.failure` exists to prevent one level up, so it gets its own
counter rather than folding into `_unmapped_rows`: the rows this counts were
resolved fine, they were excluded by scope, which is a materially different
condition from a row this collector could not map at all.

Every one of these is recorded on **every** pass, including zero. An absent
Prometheus series and a healthy one are indistinguishable in PromQL, so a gauge
written only when it is interesting cannot be alerted on.

These belong here rather than in `collector-core`: a metric only one service
records must not grow into the shared library.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "injury-report"


class InjuryReportMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._teams_published = LastValueGauge(
            meter,
            "injury_report_teams_published",
            description=(
                "Clubs that filed an injury report, by collector and practice day."
            ),
        )
        self._teams_with_games = LastValueGauge(
            meter,
            "injury_report_teams_with_games",
            description=(
                "Clubs owing an injury report — those with a scheduled game — "
                "by collector and practice day."
            ),
        )
        self._unmapped_rows = meter.create_counter(
            "injury_report_unmapped_rows",
            description=(
                "Upstream rows that produced no signal, by collector and reason."
            ),
        )
        self._scope_dropped_everything = meter.create_counter(
            "injury_report_scope_dropped_everything_total",
            description=(
                "Passes where every resolved player_injury_status row was "
                "excluded by the roster-scope membership/matchup union, by "
                "collector. Distinct from a quiet week: rows were resolved "
                "and offered, and narrowing dropped all of them."
            ),
        )

    def filings(self, practice_day: str, *, published: int, with_games: int) -> None:
        """Record one practice day's filing count against what was owed.

        Both numbers, every pass. `published` alone cannot distinguish a
        bye-heavy week from a broken feed.
        """
        labels = {"collector": self.collector, "practice_day": practice_day}
        self._teams_published.set(published, labels)
        self._teams_with_games.set(with_games, labels)

    def unmapped_row(self, reason: str) -> None:
        """A row this collector declined to map, by reason.

        Declining is the correct behaviour — a guessed injury designation is
        worse than a declared gap — but it must be visible, or "we understand
        less of this feed every week" looks exactly like "this feed got
        quieter".
        """
        self._unmapped_rows.add(1, {"collector": self.collector, "reason": reason})

    def scope_dropped_everything(self) -> None:
        """A pass where narrowing excluded every resolved `player_injury_status`
        row.

        Recorded once per pass it happens, never once per dropped row —
        narrowing dropping *most* rows is the whole point and must not alarm.
        This fires only on the all-or-nothing case: rows were resolved and
        offered, and the membership/matchup union kept none of them. See
        `capture.py`'s guard.
        """
        self._scope_dropped_everything.add(1, {"collector": self.collector})


metrics = InjuryReportMetrics()
