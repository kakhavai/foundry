import httpx

from foundry.triage.collectors.promql import METRICS

_CURRENT_WINDOW = "5m"
_BASELINE_WINDOW = "5m"
_BASELINE_RANGE = "24h"
_BASELINE_STEP = "1h"


def query_instant(base_url: str, promql: str, client: httpx.Client) -> float | None:
    """Run an instant query and return the single scalar value, or None if empty."""
    resp = client.get(f"{base_url}/api/v1/query", params={"query": promql})
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return None
    return float(result[0]["value"][1])


def query_range_series(
    base_url: str, promql: str, client: httpx.Client
) -> list[float]:
    """Run a range query over the trailing baseline window and return the sample values."""
    resp = client.get(
        f"{base_url}/api/v1/query_range",
        params={
            "query": promql,
            "start": f"now()-{_BASELINE_RANGE}",
            "end": "now()",
            "step": _BASELINE_STEP,
        },
    )
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return []
    return [float(v[1]) for v in result[0]["values"]]


def collect_metrics(
    base_url: str, service: str, route: str, client: httpx.Client
) -> dict[str, dict]:
    """For each metric, query the current window value and the trailing baseline series.
    Baselines are queried live — there is no separate store. Returns
    {metric: {"current": float, "baseline": list[float], "unit": str}}; metrics with no
    current sample are skipped."""

    out: dict[str, dict] = {}
    for name, (template, unit) in METRICS.items():
        current_q = template.format(service=service, route=route, window=_CURRENT_WINDOW)
        baseline_q = template.format(service=service, route=route, window=_BASELINE_WINDOW)
        current = query_instant(base_url, current_q, client)
        if current is None:
            continue
        baseline = query_range_series(base_url, baseline_q, client)
        out[name] = {"current": current, "baseline": baseline, "unit": unit}
    return out
