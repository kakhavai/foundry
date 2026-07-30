#!/usr/bin/env python3
"""Assert every registry entry's LIVE `GET /catalog` agrees with the file.

This is the half of the phase-8 drift gate that needs a running cluster —
assertion 2 of `docs/architecture/phase-8-data-source-collectors.md`'s "The
Registry": *each collector's live `GET /catalog` agrees with its registry entry
on signal types, cadence class, and envelope version.*

`tests/test_collector_registry.py` covers everything that can be decided from
files alone, and unit-tests `compare_entry_to_catalog` below against synthetic
catalogs. It deliberately does NOT stand in for this script with a committed
catalog fixture: a fixture is a copy of the answer, and a copy of the answer
cannot detect that the answer changed.

Run from `scripts/smoke-test.sh`, so it executes inside the required
`integration-test` check on every PR that touches the deployable surface:

    uv run --no-project --with pyyaml==6.0.3 python3 scripts/check-registry.py \\
      --base-url http://localhost:8080 --token "$TOKEN"

It goes through the gateway rather than per-service port-forwards, because
`/catalog` is in the generic-service chart's default `gateway.publicPaths` for
every collector. That is what gives assertion 1 teeth at runtime: a collector
that is in the registry but was never added to the cluster's deploy step 404s
here, and the required check goes red.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "contracts" / "collector-registry.yaml"


def compare_entry_to_catalog(entry: dict, catalog: dict) -> list[str]:
    """Problems found comparing one registry entry to one live `/catalog` body.

    Empty list means they agree. Pure — no I/O — so the platform suite can
    exercise it without a cluster.

    `envelope_version` is compared with `str()` on both sides on purpose.
    The registry stores an integer (the phase doc's example writes `1`), while
    `collector_core.envelope.ENVELOPE_VERSION` is the string `"1"` because it
    becomes the lake path segment `v1`, and `/catalog` returns that string.
    That inconsistency is real and is flagged rather than papered over; until
    it is settled deliberately, the comparison must not care which side is
    which.
    """
    name = entry["name"]
    problems: list[str] = []

    for key in ("collector", "envelope_version", "cadence_class", "signal_types"):
        if key not in catalog:
            problems.append(f"{name}: /catalog response has no {key!r} field")
    if problems:
        return problems

    if catalog["collector"] != name:
        problems.append(
            f"{name}: /catalog identifies itself as {catalog['collector']!r} — "
            f"the registry path may be routed to the wrong service"
        )

    if str(catalog["envelope_version"]) != str(entry["envelope_version"]):
        problems.append(
            f"{name}: envelope_version drift — registry says "
            f"{entry['envelope_version']!r}, live /catalog says "
            f"{catalog['envelope_version']!r}"
        )

    if str(catalog["cadence_class"]) != str(entry["cadence_class"]):
        problems.append(
            f"{name}: cadence_class drift — registry says "
            f"{entry['cadence_class']!r}, live /catalog says "
            f"{catalog['cadence_class']!r}"
        )

    live = set(catalog["signal_types"])
    declared = set(entry["signal_types"])
    if live != declared:
        missing = sorted(declared - live)
        extra = sorted(live - declared)
        detail = []
        if missing:
            detail.append(f"in the registry but not served: {missing}")
        if extra:
            detail.append(f"served but not in the registry: {extra}")
        problems.append(f"{name}: signal_types drift — {'; '.join(detail)}")

    return problems


def fetch_catalog(base_url: str, path: str, token: str, timeout: float) -> dict:
    """GET <base_url><path>/catalog through the gateway, bearer-authenticated."""
    url = f"{base_url.rstrip('/')}{path}/catalog"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080",
        help="Gateway base URL (the registry supplies each collector's path).",
    )
    parser.add_argument("--token", required=True, help="Collector bearer token.")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    # Bad input exits with a message, not a traceback. This runs from
    # smoke-test.sh under `set -e`, where a stack dump buries the one line a
    # reader needs among CI log noise.
    try:
        raw = args.registry.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"FAIL: cannot read registry {args.registry}: {exc}", file=sys.stderr)
        return 2
    try:
        registry = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(f"FAIL: {args.registry} is not valid YAML: {exc}", file=sys.stderr)
        return 2
    if not isinstance(registry, dict) or "collectors" not in registry:
        print(
            f"FAIL: {args.registry} has no top-level 'collectors' key — see "
            f"contracts/collector-registry.schema.json",
            file=sys.stderr,
        )
        return 2

    entries = registry["collectors"]
    if not entries:
        # A registry with no entries would let this check pass having proved
        # nothing at all — the same vacuous-pass concern the platform suite
        # guards for the file itself.
        print("FAIL: registry lists no collectors — nothing was checked")
        return 1

    problems: list[str] = []
    for entry in entries:
        name = entry["name"]
        try:
            catalog = fetch_catalog(
                args.base_url, entry["path"], args.token, args.timeout
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # Not a registry problem — say so, rather than sending the
                # reader off to look for a missing deployment.
                reason = (
                    "the bearer token was rejected; this check could not run, "
                    "which is a broken invocation rather than registry drift"
                )
            elif exc.code == 503:
                reason = (
                    "the collector has no COLLECTOR_TOKEN — its Secret never "
                    "synced, so it fails closed on every data route"
                )
            else:
                reason = (
                    "a registered collector must be deployed and routed at "
                    "its registry path"
                )
            problems.append(
                f"{name}: GET {args.base_url}{entry['path']}/catalog returned "
                f"{exc.code} — {reason}"
            )
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            problems.append(
                f"{name}: GET {args.base_url}{entry['path']}/catalog is "
                f"unreachable ({exc}) — a registered collector must be "
                f"deployed and routed at its registry path"
            )
            continue
        except json.JSONDecodeError as exc:
            problems.append(f"{name}: /catalog did not return JSON ({exc})")
            continue

        found = compare_entry_to_catalog(entry, catalog)
        problems.extend(found)
        if not found:
            print(f"  {name}: live /catalog agrees with the registry")

    if problems:
        print("\nregistry drift:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"collector registry OK ({len(entries)} collector(s) checked live)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
