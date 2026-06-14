import os
from datetime import datetime, timezone

import httpx

from foundry.triage.collectors.gitops import recent_deploys
from foundry.triage.collectors.prometheus import collect_metrics
from foundry.triage.detectors.metric_anomaly import detect_metric_anomalies
from foundry.triage.models.evidence import EvidenceBundle
from foundry.triage.models.incident import Incident
from foundry.triage.ranker import rank

# A latency-or-error anomaly localized to an external dependency is, in Milestone 1,
# inferred heuristically: latency dominates and error rate is modest. The
# trace-dependency detector (Milestone 2) replaces this with real downstream-span
# evidence.
_EXTERNAL_DEP_LATENCY_THRESHOLD_MS = 1000.0


def _external_dependency_implicated(metric_anomalies) -> bool:
    latency = next((a for a in metric_anomalies if a.metric == "p95_latency"), None)
    return latency is not None and latency.current >= _EXTERNAL_DEP_LATENCY_THRESHOLD_MS


def detect(
    service: str,
    endpoint: str | None,
    description: str,
    prometheus_url: str,
    gitops_dir: str,
    repo_dir: str = ".",
) -> EvidenceBundle:
    """Run the Detection Engine end to end: collect -> detect -> rank -> EvidenceBundle.
    No LLM call. `gitops_dir` is the path passed to `git log`;
    `repo_dir` is the repo root."""

    route = endpoint or "/"
    incident = Incident(service=service, endpoint=endpoint, description=description)

    with httpx.Client(timeout=10.0) as client:
        metrics = collect_metrics(prometheus_url, service, route, client)
    metric_anomalies = detect_metric_anomalies(metrics)

    # gitops_subdir must be repo-relative for `git log -- <path>`.
    gitops_subdir = os.path.relpath(gitops_dir, repo_dir).replace(os.sep, "/")
    deploys = recent_deploys(repo_dir=repo_dir, gitops_subdir=gitops_subdir, limit=5)

    return rank(
        incident=incident,
        metric_anomalies=metric_anomalies,
        deploys=deploys,
        anomaly_onset=datetime.now(timezone.utc),
        external_dependency_implicated=_external_dependency_implicated(
            metric_anomalies
        ),
    )
