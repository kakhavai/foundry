import statistics

from foundry.triage.models.evidence import MetricAnomaly

_EPSILON = 1e-9


def robust_zscore(current: float, baseline: list[float]) -> float:
    """Robust z-score: deviation from the baseline median, scaled by the median
    absolute deviation (MAD). Resists the outliers that wreck mean/stddev on bursty
    telemetry. A near-zero MAD makes any deviation look large, which is correct: a
    flat baseline means any change is abnormal."""

    if not baseline:
        return 0.0
    median = statistics.median(baseline)
    mad = statistics.median([abs(x - median) for x in baseline])
    return abs(current - median) / (mad + _EPSILON)


def detect_metric_anomalies(
    metrics: dict[str, dict], threshold: float = 3.0
) -> list[MetricAnomaly]:
    """Score each metric and return those whose robust z-score exceeds `threshold`.

    `metrics` maps a metric name to {"current": float, "baseline": list[float],
    "unit": str}."""

    anomalies: list[MetricAnomaly] = []
    for name, data in metrics.items():
        baseline = data["baseline"]
        score = robust_zscore(data["current"], baseline)
        if score >= threshold:
            median = statistics.median(baseline) if baseline else 0.0
            anomalies.append(
                MetricAnomaly(
                    metric=name,
                    current=data["current"],
                    baseline=median,
                    score=round(score, 2),
                    unit=data.get("unit", ""),
                )
            )
    return anomalies
