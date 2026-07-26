import httpx
import respx

from foundry.triage.pipeline import detect


def _instant(value):
    return {
        "status": "success",
        "data": {"result": [{"value": [1718000000, str(value)]}]},
    }


def _range(values):
    samples = [[1718000000 + i * 60, str(v)] for i, v in enumerate(values)]
    return {"status": "success", "data": {"result": [{"values": samples}]}}


@respx.mock
def test_detect_produces_bundle_with_suspects(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path
    for args in (
        ["init"],
        ["config", "user.email", "t@t.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "infra").mkdir()
    (repo / "infra" / "gitops").mkdir()
    (repo / "infra" / "gitops" / "weather.yaml").write_text("tag: 0.4.1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "bump weather"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    respx.get("http://prom:9090/api/v1/query").mock(
        side_effect=[
            httpx.Response(200, json=_instant(0.124)),
            httpx.Response(200, json=_instant(4200)),
            httpx.Response(200, json=_instant(50)),
        ]
    )
    respx.get("http://prom:9090/api/v1/query_range").mock(
        side_effect=[
            httpx.Response(200, json=_range([0.002, 0.001, 0.003])),
            httpx.Response(200, json=_range([300, 310, 295])),
            httpx.Response(200, json=_range([48, 52, 49])),
        ]
    )

    bundle = detect(
        service="weather",
        endpoint="/activity",
        description="elevated error rate",
        prometheus_url="http://prom:9090",
        gitops_dir=str(repo / "infra" / "gitops"),
        repo_dir=str(repo),
    )

    assert bundle.incident.service == "weather"
    assert any(a.metric == "error_rate" for a in bundle.metric_anomalies)
    assert bundle.suspects  # at least the deploy suspect
