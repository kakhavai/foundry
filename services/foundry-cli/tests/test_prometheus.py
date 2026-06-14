import httpx
import respx

from foundry.triage.collectors.prometheus import collect_metrics, query_instant


def _instant(value):
    return {
        "status": "success",
        "data": {"result": [{"value": [1718000000, str(value)]}]},
    }


def _range(values):
    samples = [[1718000000 + i * 60, str(v)] for i, v in enumerate(values)]
    return {"status": "success", "data": {"result": [{"values": samples}]}}


@respx.mock
def test_query_instant_returns_float():
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(200, json=_instant(0.124))
    )
    with httpx.Client() as client:
        value = query_instant("http://prom:9090", "up", client)
    assert value == 0.124


@respx.mock
def test_query_instant_empty_result_returns_none():
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"result": []}}
        )
    )
    with httpx.Client() as client:
        assert query_instant("http://prom:9090", "up", client) is None


@respx.mock
def test_collect_metrics_returns_current_and_baseline():
    respx.get("http://prom:9090/api/v1/query").mock(
        side_effect=[
            httpx.Response(200, json=_instant(0.124)),  # error_rate current
            httpx.Response(200, json=_instant(4200)),  # p95_latency current
            httpx.Response(200, json=_instant(50)),  # request_count current
        ]
    )
    respx.get("http://prom:9090/api/v1/query_range").mock(
        side_effect=[
            httpx.Response(200, json=_range([0.002, 0.001, 0.003])),
            httpx.Response(200, json=_range([300, 310, 295])),
            httpx.Response(200, json=_range([48, 52, 49])),
        ]
    )
    with httpx.Client() as client:
        metrics = collect_metrics("http://prom:9090", "weather", "/activity", client)

    assert metrics["error_rate"]["current"] == 0.124
    assert metrics["error_rate"]["baseline"] == [0.002, 0.001, 0.003]
    assert metrics["p95_latency"]["unit"] == "ms"
