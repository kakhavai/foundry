from foundry.triage.models.evidence import (
    DeployEvent,
    EvidenceBundle,
    MetricAnomaly,
    Suspect,
)
from foundry.triage.models.incident import Incident


def test_bundle_to_dict_matches_contract():
    bundle = EvidenceBundle(
        incident=Incident("weather", "/activity", "elevated error rate"),
        metric_anomalies=[
            MetricAnomaly(metric="error_rate", current=0.124, baseline=0.002, score=8.7),
            MetricAnomaly(
                metric="p95_latency", current=4200, baseline=300, score=7.9, unit="ms"
            ),
        ],
        deploy_events=[
            DeployEvent(
                sha="abc1234",
                minutes_before_anomaly=3.0,
                touched_paths=["services/weather/weather/client.py"],
                score=0.73,
            )
        ],
        suspects=[
            Suspect(name="api.github.com upstream", score=0.86, evidence=["upstream p95 up"])
        ],
    )
    d = bundle.to_dict()

    assert d["incident"]["service"] == "weather"
    assert d["metric_anomalies"][0] == {
        "metric": "error_rate",
        "current": 0.124,
        "baseline": 0.002,
        "score": 8.7,
        "unit": "",
    }
    assert d["metric_anomalies"][1]["unit"] == "ms"
    # M2 slots present but empty in M1.
    assert d["log_anomalies"] == []
    assert d["trace_anomalies"] == []
    assert d["deploy_events"][0]["sha"] == "abc1234"
    assert d["suspects"][0] == {
        "name": "api.github.com upstream",
        "score": 0.86,
        "evidence": ["upstream p95 up"],
    }


def test_bundle_round_trips_through_json():
    import json

    bundle = EvidenceBundle(incident=Incident("weather", None, "x"))
    text = json.dumps(bundle.to_dict())
    assert json.loads(text)["incident"]["endpoint"] is None
