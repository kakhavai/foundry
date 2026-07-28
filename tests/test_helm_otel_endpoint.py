"""Guards the OTel collector DNS name against the silent-failure mode
documented in CLAUDE.md: the Helmfile release is named `otel-collector`, but
the chart appends `-opentelemetry-collector`. A wrong name here stops traces
and logs while /metrics keeps working, because Prometheus scrapes pod
annotations directly and is unaffected.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "charts" / "generic-service"
EXPECTED_ENDPOINT = (
    "http://otel-collector-opentelemetry-collector.monitoring.svc.cluster.local:4317"
)

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm binary not installed"
)


def _render() -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(CHART),
            "--set",
            "service.name=test",
            "--set",
            "image.repository=test",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def test_values_declare_the_expected_collector_endpoint():
    values = yaml.safe_load((CHART / "values.yaml").read_text())

    assert values["otel"]["endpoint"] == EXPECTED_ENDPOINT


def test_rendered_configmap_carries_the_collector_endpoint():
    configmaps = [d for d in _render() if d.get("kind") == "ConfigMap"]

    assert configmaps, "chart rendered no ConfigMap"
    endpoints = [
        v
        for cm in configmaps
        for v in (cm.get("data") or {}).values()
        if isinstance(v, str) and "otel-collector" in v
    ]
    assert EXPECTED_ENDPOINT in endpoints


def test_endpoint_includes_the_chart_name_suffix():
    """The specific mistake: using the release name without the chart suffix."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    endpoint = values["otel"]["endpoint"]

    assert "otel-collector-opentelemetry-collector" in endpoint, (
        "endpoint is missing the `-opentelemetry-collector` suffix the Helm "
        "chart appends to the release name — traces and logs will silently stop"
    )
    assert endpoint.endswith(":4317"), "OTLP gRPC port must be 4317"
