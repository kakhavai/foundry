#!/usr/bin/env python3
"""
Run a chaos scenario and check its hypothesis against Prometheus.

A scenario file is multi-document YAML: one `foundry.chaos/v1` Scenario head
carrying the steady state, hypothesis, and criteria, followed by the Chaos Mesh
resources that inject the fault. The criterion lives beside the fault it judges.

Usage:
  uv run --with pyyaml==6.0.3 python scripts/run-chaos.py <scenario>
  uv run --with pyyaml==6.0.3 python scripts/run-chaos.py --all
  uv run --with pyyaml==6.0.3 python scripts/run-chaos.py --list

Exits non-zero if any criterion failed.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "chaos" / "scenarios"
TRAFFIC_DIR = ROOT / "chaos" / "traffic"

SCENARIO_API = "foundry.chaos/v1"
PROM_PROXY = (
    "/api/v1/namespaces/monitoring/services/prometheus-server:80"
    "/proxy/api/v1/query"
)

# Two-character operators first: ">= 2" must not parse as ">" of "= 2".
_OPERATORS = ("==", "!=", ">=", "<=", ">", "<")

OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def parse_expect(expect: str) -> tuple[str, float]:
    """Split an `expect` string such as "== 1" into (operator, value)."""
    text = (expect or "").strip()
    for op in _OPERATORS:
        if text.startswith(op):
            rest = text[len(op):].strip()
            try:
                return op, float(rest)
            except ValueError:
                raise ValueError(f"expect value is not a number: {expect!r}")
    raise ValueError(f"expect must start with a comparison operator: {expect!r}")


def is_trivially_true(op: str, value: float) -> bool:
    """True when no observation could falsify the comparison.

    Every metric these scenarios query is non-negative — counters, byte gauges,
    ratios, and `up` — so a comparison satisfied by every non-negative number
    can never go red. Such a criterion reports coverage it does not have, which
    is worse than having no scenario at all.
    """
    if op == ">=" and value <= 0:
        return True
    if op == ">" and value < 0:
        return True
    if op == "!=" and value < 0:
        return True
    return False


def parse_duration(text) -> float:
    """Parse "90s", "2m", "500ms", "1h", or a bare number, into seconds."""
    text = str(text).strip()
    # "ms" before "s": "500ms" also ends in "s".
    for suffix, factor in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


def extract_scalar(payload: dict, *, allow_empty: bool) -> float | None:
    """Pull a single number out of a Prometheus query response.

    Returns None when the result is empty and the check did not opt in with
    `allowEmpty`. Prometheus answers `{"status":"success","result":[]}` both for
    a series that has never existed and for a typo'd metric name, so treating
    empty as 0 by default would let a typo satisfy every criterion silently.
    """
    if payload.get("status") != "success":
        raise ValueError(f"Prometheus query failed: {payload.get('error', payload)}")
    result = payload.get("data", {}).get("result", [])
    if not result:
        return 0.0 if allow_empty else None
    if len(result) > 1:
        raise ValueError(
            f"query returned {len(result)} series; a check must reduce to one "
            "(wrap it in an aggregation such as max() or count())"
        )
    return float(result[0]["value"][1])
