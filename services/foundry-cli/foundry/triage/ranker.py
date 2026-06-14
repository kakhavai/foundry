from datetime import datetime

from foundry.triage.collectors.gitops import RawDeploy
from foundry.triage.detectors.deploy_correlation import score_deploy
from foundry.triage.models.evidence import (
    DeployEvent,
    EvidenceBundle,
    MetricAnomaly,
    Suspect,
)
from foundry.triage.models.incident import Incident
from foundry.triage.models.suspect import SuspectScore

# Normalizes a robust z-score into a 0..1-ish support term. A z of ~8 -> ~0.8.
_METRIC_SCALE = 10.0


def _metric_support(metric_anomalies: list[MetricAnomaly]) -> float:
    if not metric_anomalies:
        return 0.0
    top = max(a.score for a in metric_anomalies)
    return min(1.0, top / _METRIC_SCALE)


def rank(
    incident: Incident,
    metric_anomalies: list[MetricAnomaly],
    deploys: list[RawDeploy],
    anomaly_onset: datetime,
    external_dependency_implicated: bool,
) -> EvidenceBundle:
    """Combine detector outputs into ranked suspects and assemble the EvidenceBundle.

    Milestone-1 candidate suspects: each recent deploy, plus (when an external/downstream
    dependency is implicated) an external-dependency suspect. log_support / trace_support
    stay 0 until Milestone 2's detectors populate them."""

    metric_support = _metric_support(metric_anomalies)
    metric_evidence = [
        f"{a.metric} = {a.current}{a.unit} vs baseline {a.baseline}{a.unit} (z={a.score})"
        for a in metric_anomalies
    ]

    # When an external/downstream dependency is independently implicated, the metric
    # anomaly is better explained by that dependency than by a code deploy — so the
    # deploy hypothesis does NOT get to claim the metric evidence as support.
    deploy_metric_support = 0.0 if external_dependency_implicated else metric_support

    scores: list[SuspectScore] = []
    deploy_events: list[DeployEvent] = []

    for deploy in deploys:
        minutes = deploy.minutes_before(anomaly_onset)
        d_score = score_deploy(
            minutes_before_anomaly=minutes,
            touched_paths=deploy.touched_paths,
            service=incident.service,
            external_dependency_implicated=external_dependency_implicated,
        )
        deploy_events.append(
            DeployEvent(
                sha=deploy.sha,
                minutes_before_anomaly=round(minutes, 1),
                touched_paths=deploy.touched_paths,
                score=round(d_score, 2),
            )
        )
        evidence = [
            f"Deploy {deploy.sha} ({deploy.subject}) {minutes:.0f} min before onset",
            *metric_evidence,
        ]
        if external_dependency_implicated:
            evidence.append(
                "An external/downstream dependency shows independent failure — "
                "weakens the deploy hypothesis"
            )
        scores.append(
            SuspectScore(
                suspect=f"{incident.service} deploy {deploy.sha}",
                metric_support=deploy_metric_support,
                deploy_support=d_score,
                contradiction_penalty=0.0,
                evidence=evidence,
            )
        )

    if external_dependency_implicated:
        scores.append(
            SuspectScore(
                suspect=f"{incident.service} external/upstream dependency",
                metric_support=metric_support,
                deploy_support=0.0,
                evidence=[
                    "Latency/error anomaly localized to an external/downstream call",
                    *metric_evidence,
                ],
            )
        )

    scores.sort(key=lambda s: s.total, reverse=True)
    suspects = [
        Suspect(name=s.suspect, score=round(s.total, 2), evidence=s.evidence)
        for s in scores
    ]

    return EvidenceBundle(
        incident=incident,
        metric_anomalies=metric_anomalies,
        deploy_events=deploy_events,
        suspects=suspects,
    )
