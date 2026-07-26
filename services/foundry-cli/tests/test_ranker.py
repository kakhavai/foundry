from datetime import datetime, timezone

from foundry.triage.collectors.gitops import RawDeploy
from foundry.triage.models.evidence import MetricAnomaly
from foundry.triage.models.incident import Incident
from foundry.triage.ranker import rank


def _deploy(sha, minutes, paths):
    onset = datetime(2026, 6, 14, 14, 35, tzinfo=timezone.utc)
    ts = datetime(2026, 6, 14, 14, 35 - int(minutes), tzinfo=timezone.utc)
    return RawDeploy(sha=sha, timestamp=ts, touched_paths=paths, subject="bump"), onset


def test_app_regression_ranks_deploy_first():
    incident = Incident("weather", "/activity", "errors after deploy")
    metric_anomalies = [MetricAnomaly("error_rate", 0.12, 0.002, 8.7)]
    deploy, onset = _deploy("abc1234", 3, ["services/weather/weather/client.py"])
    bundle = rank(
        incident=incident,
        metric_anomalies=metric_anomalies,
        deploys=[deploy],
        anomaly_onset=onset,
        external_dependency_implicated=False,
    )
    assert bundle.suspects[0].name.startswith("weather")
    assert "abc1234" in bundle.suspects[0].name or any(
        "abc1234" in e for e in bundle.suspects[0].evidence
    )
    assert bundle.deploy_events[0].sha == "abc1234"


def test_external_dependency_outranks_deploy_when_contradicted():
    incident = Incident("weather", "/activity", "upstream slow, no deploy")
    metric_anomalies = [
        MetricAnomaly("p95_latency", 4200, 300, 7.9, unit="ms"),
        MetricAnomaly("error_rate", 0.12, 0.002, 8.7),
    ]
    deploy, onset = _deploy("def5678", 3, ["services/weather/weather/client.py"])
    bundle = rank(
        incident=incident,
        metric_anomalies=metric_anomalies,
        deploys=[deploy],
        anomaly_onset=onset,
        external_dependency_implicated=True,
    )
    # The external dependency suspect should rank above the contradicted deploy.
    assert "dependency" in bundle.suspects[0].name.lower()
    deploy_suspect = next(s for s in bundle.suspects if "def5678" in s.name)
    assert bundle.suspects[0].score > deploy_suspect.score


def test_suspects_sorted_descending():
    incident = Incident("weather", "/activity", "x")
    deploy, onset = _deploy("a", 3, ["services/weather/x.py"])
    bundle = rank(
        incident=incident,
        metric_anomalies=[MetricAnomaly("error_rate", 0.12, 0.002, 8.7)],
        deploys=[deploy],
        anomaly_onset=onset,
        external_dependency_implicated=False,
    )
    scores = [s.score for s in bundle.suspects]
    assert scores == sorted(scores, reverse=True)
