"""PromQL templates, centralized so the eval step can adjust them in one place if the
live metric names differ from the OTel/Prometheus exporter defaults. `{service}`,
`{route}`, and `{window}` are filled by the collector.

These target the OpenTelemetry FastAPI instrumentation exported via PrometheusMetricReader.
The eval harness (a later task) confirms the exact series names against live Prometheus and
edits these constants if needed."""

ERROR_RATE = (
    'sum(rate(http_server_duration_milliseconds_count'
    '{{job="{service}",http_route="{route}",http_status_code=~"5.."}}[{window}]))'
    ' / '
    'clamp_min(sum(rate(http_server_duration_milliseconds_count'
    '{{job="{service}",http_route="{route}"}}[{window}])), 1e-9)'
)

P95_LATENCY = (
    'histogram_quantile(0.95, sum(rate(http_server_duration_milliseconds_bucket'
    '{{job="{service}",http_route="{route}"}}[{window}])) by (le))'
)

REQUEST_COUNT = (
    'sum(rate(http_server_duration_milliseconds_count'
    '{{job="{service}",http_route="{route}"}}[{window}]))'
)

# metric name -> (promql template, unit)
METRICS = {
    "error_rate": (ERROR_RATE, ""),
    "p95_latency": (P95_LATENCY, "ms"),
    "request_count": (REQUEST_COUNT, ""),
}
