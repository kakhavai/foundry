import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import REGISTRY, generate_latest


@pytest.fixture(scope="session", autouse=True)
def _meter_provider():
    """Install one real MeterProvider for the whole test session.

    Instruments are created at import time and record nothing until a provider
    exists; anything recorded before it is installed is silently lost. Because
    `set_meter_provider` is one-shot per process, this must happen exactly once,
    before any test records. test_telemetry's `patched_sdk` fixture no-ops
    `set_meter_provider`, so `setup_telemetry` cannot clobber this.
    """
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()])
    )


@pytest.fixture
def metric_value():
    """Read one series out of real /metrics output.

    Asserting on `generate_latest` rather than the SDK's internal view means
    these tests check exactly what Prometheus scrapes, including OTel's name
    mangling — a rename that broke a chaos query fails here.

    Returns None when the series is absent, which is distinct from a series
    present with value 0.0. Counters accumulate for the whole session, so
    callers assert on the delta across an action, not an absolute value.
    """

    def read(name: str, **labels: str) -> float | None:
        for line in generate_latest(REGISTRY).decode().splitlines():
            if line.startswith("#"):
                continue
            head, _, raw_value = line.rpartition(" ")
            if "{" in head:
                series, _, raw_labels = head.partition("{")
                if series != name:
                    continue
                found = {
                    k: v.strip('"')
                    for k, v in (
                        pair.split("=", 1)
                        for pair in raw_labels.rstrip("}").split(",")
                        if pair
                    )
                }
            else:
                if head != name:
                    continue
                found = {}
            if all(found.get(k) == v for k, v in labels.items()):
                return float(raw_value)
        return None

    return read
