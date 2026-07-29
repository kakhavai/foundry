"""weather's binding to the shared collector metrics."""

from collector_core.metrics import CollectorMetrics

COLLECTOR = "weather"
metrics = CollectorMetrics(COLLECTOR)
