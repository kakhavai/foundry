#!/usr/bin/env python3
"""Dispatch `POST /refresh` to one collector, several, or the whole fleet.

The contract in `docs/architecture/phase-8-data-source-collectors.md` promises
this tool. Without it the operator path is a hand-rolled `kubectl port-forward`
plus a `curl` carrying a bearer token on the command line -- three chances to
get it wrong, one of which puts a real token in shell history.

    uv run --no-project --with pyyaml==6.0.3 python3 \\
      scripts/refresh-collector.py weather
    uv run --no-project --with pyyaml==6.0.3 python3 \\
      scripts/refresh-collector.py --all --week 4
    uv run --no-project --with pyyaml==6.0.3 python3 \\
      scripts/refresh-collector.py weather --via-pod

`--no-project` because this needs pyyaml and the standard library and nothing
from the uv workspace.

The target list comes from `scripts/collectors.py`, which derives it from
`contracts/collector-registry.yaml` plus each service's Helm values. There is
no second list here: a collector added tomorrow is refreshable today, and a
name that is not a registered collector fails loudly with the valid list rather
than quietly refreshing nothing.

## 202 is *accepted*, not *done* -- and this tool does not pretend otherwise

`POST /refresh` dispatches the capture as a background task and returns
immediately (`collector_core/routes.py`). This tool therefore reports the
`refresh_id` and exits. It does **not** poll for completion, and that is a
decision rather than an omission:

  * The contract defines no status endpoint keyed by `refresh_id`. Inventing
    one here would mean inventing it in the collector too.
  * `/catalog`'s `last_capture_at` is the only completion-ish signal that
    exists, and it describes *a* capture, not *this* one. The cadence loop and
    a dispatched refresh share `CaptureState.lock` and both call
    `apply_capture`, so a loop tick landing mid-wait would satisfy a poll while
    the dispatched capture is still running -- reporting "done" for work that
    has not finished.
  * It fails in the other direction too. When `spec.capture` raises,
    `_run_capture` logs and swallows it and `apply_capture` is never reached,
    so `last_capture_at` never advances. A poll would time out and report a
    failure for a refresh that was genuinely accepted and genuinely ran.

A wait that is wrong in both directions is worse than no wait, so the tool
prints what it actually knows -- the request was accepted, here is its id --
and tells the operator where to watch.

## Reaching the collector

Two transports, neither of them a port-forward (a stale `kubectl port-forward`
survives `pkill` on Windows and silently holds the port).

`--base-url` (the default, `http://localhost:8080`) posts through the gateway,
where `/refresh` is one of the generic-service chart's default
`gateway.publicPaths`. On Kind that URL is the NodePort 30080 mapping in
`infra/kind/cluster.yaml`, so it needs no forwarding at all. This is the same
door the projections generator comes through, and the same one
`scripts/check-registry.py` uses.

`--via-pod` runs the request from inside the collector's own pod via
`kubectl exec`, reading `COLLECTOR_TOKEN` out of the pod's environment. It
needs no token on the operator's machine and no route through the gateway.

**The Kubernetes API proxy (`kubectl get --raw .../proxy/...`) cannot serve
this**, which is why it is not offered. The proxy path reaches the collector --
`/health` answers 200 through it -- but `kubectl` owns the one `Authorization`
header slot and fills it with the cluster credential, so every authenticated
route answers 401 and there is nowhere to put the collector's bearer token.
Verified against the live Kind cluster; `--via-pod` is the in-cluster answer
instead.

## Exit codes

    0   every targeted refresh was accepted (202)
    1   at least one was not -- rate-limited, rejected, or unreachable
    2   the invocation itself was wrong (unknown collector, no token, bad
        registry). Nothing was contacted.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collectors as fleet  # noqa: E402  (needs the path insert above)

# Imported after `collectors`, which prints an actionable message and exits 2
# when PyYAML is missing rather than letting a traceback send the reader
# looking for a bug in this file.
import yaml  # noqa: E402

# The gateway on Kind, via the NodePort 30080 mapping in infra/kind/cluster.yaml.
DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_NAMESPACE = "default"
TOKEN_ENV = "COLLECTOR_TOKEN"

# One line per outcome, so a fleet run reads as a column.
ACCEPTED = "ACCEPTED"
RATE_LIMITED = "RATE-LIMITED"
FAILED = "FAILED"

# Prefixes the single line the in-pod script prints, so kubectl's own chatter
# ("Defaulted container ...") cannot be mistaken for the payload.
POD_MARKER = "FOUNDRY-REFRESH "


class UsageError(Exception):
    """The invocation was wrong. Nothing was contacted."""


class TransportError(Exception):
    """The collector could not be reached, or the reply could not be read."""


@dataclass(frozen=True)
class Outcome:
    """What happened to one collector's refresh request."""

    name: str
    status: str
    detail: str

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED


# ── target resolution ─────────────────────────────────────────────────────────


def resolve_targets(names: list[str], *, all_collectors: bool, services: list) -> list:
    """The collectors to refresh, in registry order.

    Non-collector services (`player-projections`) are not candidates: they have
    no `/refresh`. They therefore land in the same "unknown" bucket as a typo,
    which is the right answer -- the message lists what *is* refreshable.
    """
    known = {service.name: service for service in services if service.is_collector}
    if not known:
        raise UsageError(
            "the registry lists no collectors — this run would refresh nothing"
        )
    valid = f"known collectors: {', '.join(known)}"

    if all_collectors:
        if names:
            raise UsageError(
                f"--all takes no collector names (got: {', '.join(names)})"
            )
        return list(known.values())

    if not names:
        raise UsageError(f"name at least one collector, or pass --all; {valid}")

    unknown = [name for name in names if name not in known]
    if unknown:
        raise UsageError(f"unknown collector(s): {', '.join(unknown)}; {valid}")

    # Deduplicated, first mention wins: `refresh-collector weather weather`
    # should hit the interval floor once, not report a spurious 429.
    ordered: list = []
    for name in names:
        if known[name] not in ordered:
            ordered.append(known[name])
    return ordered


def build_scope(season: int | None, week: int | None) -> dict:
    """The `/refresh` request body.

    An omitted field is deliberately *not* filled in here. `POST /refresh`
    falls back to the collector's own `default_scope`, which is built from the
    same `CAPTURE_SEASON`/`CAPTURE_WEEK` the cadence loop uses. A default
    guessed from the wall clock in this file would silently diverge from the
    deployment's configured week the moment an operator advanced one -- exactly
    the divergence `CollectorSpec.default_scope` exists to prevent. So the
    default scope is "whatever this collector is configured to capture", and
    `--season`/`--week` override one or both independently.
    """
    scope: dict = {}
    if season is not None:
        scope["season"] = season
    if week is not None:
        scope["week"] = week
    return scope


# ── reading the reply ─────────────────────────────────────────────────────────


def interpret_response(
    name: str, status: int, body: str, retry_after: str | None = None
) -> Outcome:
    """Turn one HTTP reply into an Outcome. Pure -- no I/O, no token.

    Response bodies are never echoed. Nothing a collector returns contains a
    token today, but a rule that holds by construction beats one that holds by
    inspection of every current error path.
    """
    if status == 202:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        refresh_id = payload.get("refresh_id") or "(none returned)"
        scope = payload.get("scope")
        captured = ""
        if isinstance(scope, dict) and scope:
            captured = f", scope {json.dumps(scope, sort_keys=True)}"
        return Outcome(name, ACCEPTED, f"refresh_id {refresh_id}{captured}")

    if status == 429:
        wait = f"; retry in {retry_after}s" if retry_after else ""
        return Outcome(
            name,
            RATE_LIMITED,
            "refused by the interval floor — REFRESH_MIN_INTERVAL_SECONDS has "
            f"not elapsed since the last accepted refresh{wait}",
        )

    if status in (401, 403):
        return Outcome(
            name,
            FAILED,
            f"HTTP {status} — the bearer token was rejected; the collector's "
            "Secret may have rotated (rotation needs a rollout restart)",
        )

    if status == 503:
        return Outcome(
            name,
            FAILED,
            "HTTP 503 — the collector has no COLLECTOR_TOKEN. Its Secret never "
            "synced, so it fails closed on every data route",
        )

    if status == 404:
        return Outcome(
            name,
            FAILED,
            "HTTP 404 — nothing serves /refresh here. Through the gateway, "
            "check gateway.publicPaths lists /refresh for this collector",
        )

    return Outcome(name, FAILED, f"HTTP {status}")


def summarize(outcomes: list[Outcome]) -> tuple[int, list[str]]:
    """The report and the exit code.

    Counted explicitly rather than with `all(...)`: `all([])` is True, so an
    empty run -- every target skipped, a loop that never executed -- would
    exit 0 having refreshed nothing. That is the one failure this summary is
    least able to notice for itself.
    """
    lines = [
        f"  {outcome.status:<13} {outcome.name}: {outcome.detail}"
        for outcome in outcomes
    ]

    if not outcomes:
        lines.append("no collector was contacted — nothing was refreshed")
        return 1, lines

    accepted = [outcome for outcome in outcomes if outcome.accepted]
    lines.append(f"{len(accepted)}/{len(outcomes)} accepted")
    return (0 if len(accepted) == len(outcomes) else 1), lines


# ── transports ────────────────────────────────────────────────────────────────


def refresh_url(base_url: str, service) -> str:
    """The collector's `/refresh` URL behind the gateway. Never carries a token."""
    return f"{base_url.rstrip('/')}{service.path}/refresh"


def post_refresh_http(
    url: str, token: str, scope: dict, timeout: float
) -> tuple[int, str, str | None]:
    """POST the scope to `url`, bearer-authenticated. Returns (status, body, Retry-After).

    A 4xx/5xx is a *reply*, not an exception -- 429 in particular is an
    expected outcome with a documented meaning, and letting urllib raise it
    would turn the interval floor into a stack trace.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(scope).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body, response.headers.get("Retry-After")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body, exc.headers.get("Retry-After")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"{url} is unreachable ({exc})") from None


# Runs inside the collector's own pod. Reads COLLECTOR_TOKEN from that pod's
# environment, so the token never crosses the operator's machine, never enters
# an argument vector, and never reaches a log. Prints exactly one marked line.
POD_SCRIPT = r"""
import json, os, sys, urllib.error, urllib.request

port, scope, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
request = urllib.request.Request(
    "http://127.0.0.1:" + port + "/refresh",
    data=scope.encode(),
    headers={
        "Authorization": "Bearer " + os.environ.get("COLLECTOR_TOKEN", ""),
        "Content-Type": "application/json",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        out = {
            "status": response.status,
            "body": response.read().decode("utf-8", "replace"),
            "retry_after": response.headers.get("Retry-After"),
        }
except urllib.error.HTTPError as exc:
    out = {
        "status": exc.code,
        "body": exc.read().decode("utf-8", "replace"),
        "retry_after": exc.headers.get("Retry-After"),
    }
except Exception as exc:
    out = {"error": type(exc).__name__ + ": " + str(exc)}
print("FOUNDRY-REFRESH " + json.dumps(out))
"""


def pod_command(service, scope: dict, timeout: float, namespace: str) -> list[str]:
    """The `kubectl exec` argv. Pure, and deliberately token-free.

    Scope values are argparse-typed ints and the port comes from the Helm
    values, so nothing attacker-shaped reaches the inner interpreter -- and
    there is no shell in the chain to reach it through, since this is an argv
    rather than a command string.
    """
    return [
        "kubectl",
        "exec",
        "-n",
        namespace,
        f"deploy/{service.name}",
        "--",
        "python3",
        "-c",
        POD_SCRIPT,
        str(service.port),
        json.dumps(scope),
        str(timeout),
    ]


def parse_pod_output(stdout: str) -> tuple[int, str, str | None]:
    """Pull the marked JSON line out of an exec's stdout."""
    for line in stdout.splitlines():
        if not line.startswith(POD_MARKER):
            continue
        try:
            payload = json.loads(line[len(POD_MARKER) :])
        except json.JSONDecodeError as exc:
            raise TransportError(f"the in-pod reply was not JSON ({exc})") from None
        if "error" in payload:
            raise TransportError(f"the in-pod request failed: {payload['error']}")
        return payload["status"], payload.get("body", ""), payload.get("retry_after")
    raise TransportError(
        "the in-pod script printed no reply — is python3 on PATH in this image?"
    )


def post_refresh_pod(
    service, scope: dict, timeout: float, namespace: str
) -> tuple[int, str, str | None]:
    """POST from inside the collector's pod, using that pod's own token."""
    command = pod_command(service, scope, timeout, namespace)
    # A generous margin over the in-pod timeout: kubectl has to schedule the
    # exec before the inner request even starts.
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout + 30
    )
    if result.returncode != 0 and POD_MARKER not in result.stdout:
        message = result.stderr.strip() or result.stdout.strip() or "no output"
        raise TransportError(f"kubectl exec failed: {message}")
    return parse_pod_output(result.stdout)


# ── the run ───────────────────────────────────────────────────────────────────


def refresh_one(
    service,
    *,
    token: str,
    scope: dict,
    timeout: float,
    base_url: str,
    via_pod: bool,
    namespace: str,
) -> Outcome:
    """Dispatch one collector's refresh. Never raises.

    Same reasoning as `run-chaos.py`'s `--all` loop: one collector's unreachable
    Service must not abort the fleet run, leaving the remaining collectors
    untouched and no summary printed.
    """
    try:
        if via_pod:
            status, body, retry_after = post_refresh_pod(
                service, scope, timeout, namespace
            )
        else:
            status, body, retry_after = post_refresh_http(
                refresh_url(base_url, service), token, scope, timeout
            )
    except TransportError as exc:
        return Outcome(service.name, FAILED, str(exc))
    except Exception as exc:  # noqa: BLE001 - one target must not kill the run
        return Outcome(service.name, FAILED, f"{type(exc).__name__}: {exc}")
    return interpret_response(service.name, status, body, retry_after)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="refresh-collector",
        description=(
            "Dispatch POST /refresh to one collector, several, or all of them. "
            "The capture runs asynchronously: 202 means accepted, not done."
        ),
    )
    parser.add_argument(
        "collector",
        nargs="*",
        help="Collector name(s), from contracts/collector-registry.yaml.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Refresh every registered collector. Each one reaches its own "
            "upstream, regardless of CAPTURE_ENABLED."
        ),
    )
    parser.add_argument(
        "--season",
        type=int,
        help="Season to capture. Omitted: the collector's own CAPTURE_SEASON.",
    )
    parser.add_argument(
        "--week",
        type=int,
        help="Week to capture. Omitted: the collector's own CAPTURE_WEEK.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=(
            "Gateway base URL, or an already-forwarded port. The registry "
            f"supplies each collector's path. Default: {DEFAULT_BASE_URL}"
        ),
    )
    parser.add_argument(
        "--via-pod",
        action="store_true",
        help=(
            "Run the request inside the collector's pod (kubectl exec), using "
            "that pod's own COLLECTOR_TOKEN. Needs no token here and no "
            "gateway route."
        ),
    )
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Namespace for --via-pod. Default: {DEFAULT_NAMESPACE}",
    )
    parser.add_argument(
        "--token",
        help=(
            f"Bearer token. Prefer ${TOKEN_ENV} — a flag lands in shell "
            "history and in `ps`. Kind-local value: local-dev-token."
        ),
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Bad input exits with a message, not a traceback: an operator reaching for
    # this during an incident should get the one line they need.
    try:
        services = fleet.services()
    except (fleet.FleetError, OSError, yaml.YAMLError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    try:
        targets = resolve_targets(
            args.collector, all_collectors=args.all, services=services
        )
    except UsageError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    token = ""
    if args.via_pod:
        if args.token:
            # Silently ignoring it would leave an operator believing they had
            # authenticated with a token that was never sent.
            print(
                "FAIL: --via-pod uses the pod's own COLLECTOR_TOKEN; --token "
                "would not be sent. Drop one of the two.",
                file=sys.stderr,
            )
            return 2
    else:
        token = args.token or os.getenv(TOKEN_ENV, "")
        if not token:
            print(
                f"FAIL: no bearer token. Pass --token or set ${TOKEN_ENV} "
                "(Kind-local: local-dev-token), or use --via-pod, which reads "
                "the token from inside the pod.",
                file=sys.stderr,
            )
            return 2

    scope = build_scope(args.season, args.week)
    where = "in-pod" if args.via_pod else args.base_url
    print(
        f"POST /refresh to {len(targets)} collector(s) via {where}; "
        f"scope: {json.dumps(scope, sort_keys=True) if scope else 'each collector default'}"
    )

    outcomes: list[Outcome] = []
    for service in targets:
        print(f"\n{service.name}")
        if not service.capture_enabled:
            # Worth saying out loud: the flag turns off the *cadence loop*, not
            # the upstream call. A dispatched refresh reaches the third party
            # either way, which is why CLAUDE.md forbids it from smoke hooks.
            print(
                "  note: CAPTURE_ENABLED is false — its cadence loop is off, "
                "but this refresh still reaches its upstream"
            )
        if not args.via_pod:
            print(f"  $ POST {refresh_url(args.base_url, service)}")
        outcome = refresh_one(
            service,
            token=token,
            scope=scope,
            timeout=args.timeout,
            base_url=args.base_url,
            via_pod=args.via_pod,
            namespace=args.namespace,
        )
        print(f"  {outcome.status}: {outcome.detail}")
        outcomes.append(outcome)

    code, lines = summarize(outcomes)
    print(f"\n{'=' * 66}\nsummary\n{'=' * 66}")
    print("\n".join(lines))

    if any(outcome.accepted for outcome in outcomes):
        print(
            "\nAccepted is not done. The capture runs in the background and "
            "there is no per-refresh status route.\nWatch last_capture_at "
            "advance in GET <collector>/catalog to see one land."
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
