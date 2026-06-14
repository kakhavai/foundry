from foundry.triage.detectors.metric_anomaly import (
    detect_metric_anomalies,
    robust_zscore,
)


def test_robust_zscore_flags_large_deviation():
    baseline = [0.002, 0.001, 0.003, 0.002, 0.002]
    score = robust_zscore(0.124, baseline)
    assert score > 5.0


def test_robust_zscore_normal_value_is_low():
    baseline = [0.30, 0.31, 0.29, 0.30, 0.32]
    score = robust_zscore(0.305, baseline)
    assert score < 1.0


def test_robust_zscore_zero_variance_baseline_does_not_crash():
    score = robust_zscore(1.0, [0.0, 0.0, 0.0])
    assert (
        score > 0
    )  # any deviation from a flat baseline is anomalous, not a ZeroDivisionError


def test_detect_builds_metric_anomalies_above_threshold():
    metrics = {
        "error_rate": {"current": 0.124, "baseline": [0.002, 0.001, 0.003], "unit": ""},
        "p95_latency": {"current": 305, "baseline": [300, 310, 295], "unit": "ms"},
    }
    anomalies = detect_metric_anomalies(metrics, threshold=3.0)
    names = [a.metric for a in anomalies]
    assert "error_rate" in names  # large deviation
    assert "p95_latency" not in names  # within normal range
    flagged = next(a for a in anomalies if a.metric == "error_rate")
    assert flagged.current == 0.124
    assert flagged.unit == ""
