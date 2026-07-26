from dataclasses import dataclass, field

from foundry.triage.models.incident import Incident


@dataclass
class MetricAnomaly:
    """One metric scored against its baseline. `unit` is "" for ratios (error_rate),
    "ms" for latency, "" for counts — kept as a field so the JSON shape is uniform
    rather than branching key names per metric."""

    metric: str
    current: float
    baseline: float
    score: float
    unit: str = ""

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "current": self.current,
            "baseline": self.baseline,
            "score": self.score,
            "unit": self.unit,
        }


@dataclass
class DeployEvent:
    sha: str
    minutes_before_anomaly: float
    touched_paths: list[str]
    score: float

    def to_dict(self) -> dict:
        return {
            "sha": self.sha,
            "minutes_before_anomaly": self.minutes_before_anomaly,
            "touched_paths": self.touched_paths,
            "score": self.score,
        }


@dataclass
class Suspect:
    name: str
    score: float
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "score": self.score, "evidence": self.evidence}


@dataclass
class EvidenceBundle:
    """The structured output of the Detection Engine and the only input to the
    narrator. log_anomalies/trace_anomalies are present but empty until Milestone 2."""

    incident: Incident
    metric_anomalies: list[MetricAnomaly] = field(default_factory=list)
    log_anomalies: list[dict] = field(default_factory=list)
    trace_anomalies: list[dict] = field(default_factory=list)
    deploy_events: list[DeployEvent] = field(default_factory=list)
    suspects: list[Suspect] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "incident": self.incident.to_dict(),
            "metric_anomalies": [m.to_dict() for m in self.metric_anomalies],
            "log_anomalies": self.log_anomalies,
            "trace_anomalies": self.trace_anomalies,
            "deploy_events": [d.to_dict() for d in self.deploy_events],
            "suspects": [s.to_dict() for s in self.suspects],
        }
