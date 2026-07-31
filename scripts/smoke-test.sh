#!/usr/bin/env bash
set -euo pipefail

# The collector half of this script is registry-driven. It used to carry three
# near-identical copies of the same contract assertions, one per collector,
# each with its own port-forward and its own separately-named PID variable in
# the trap. With twenty-four collectors queued, the copies are the bug: the
# fourth one gets pasted with the third one's port and passes over the wrong
# service.
#
# Now `scripts/collectors.py` prints one line per collector and the loop below
# does the standard contract surface for each. A collector with routes beyond
# the standard five contributes its own extra assertions as an executable
# `services/<name>/smoke.sh` — a new file in its own directory, not an edit to
# this one.
#
# player-projections is NOT a collector: no gateway route, no /catalog, no
# signal types. Its block stays hand-written, below the loop.

ROOT=$(cd "$(dirname "$0")/.." && pwd)

# --no-project because these scripts need pyyaml and the standard library and
# nothing from the uv workspace; without it, `uv run` from the repo root builds
# collector-core and every service into a throwaway venv to run a file that
# never imports them.
FLEET=(uv run --no-project --with pyyaml==6.0.3 python3 "$ROOT/scripts/collectors.py")

# Every port-forward's PID, so the trap no longer needs one variable per
# service — a list that could (and did) fall out of step with the forwards.
PF_PIDS=()

cleanup() {
  # Expanded as "possibly-empty" (":-") on purpose. The trap is armed before
  # the Envoy Service lookup below, and if that lookup fails under set -e the
  # array is still empty; without the default, set -u would throw its own
  # "unbound variable" error on top of the real failure.
  kill "${PF_PIDS[@]:-}" 2>/dev/null || true
}
trap cleanup EXIT

forward() {  # forward <namespace> <service> <local:remote>
  kubectl port-forward -n "$1" "svc/$2" "$3" &
  PF_PIDS+=("$!")
}

expect_status() {  # expect_status <code> <description> <curl args...>
  local expected="$1" description="$2"
  shift 2
  local status
  status=$(curl -o /dev/null -sw '%{http_code}' "$@")
  if [ "$status" != "$expected" ]; then
    echo "$description: expected $expected, got $status"
    exit 1
  fi
}

# Assigned rather than piped straight into `mapfile`, so a failure to read the
# registry fails here under set -e instead of yielding an empty list that the
# loop then skips silently.
TARGETS_RAW=$("${FLEET[@]}" --smoke-targets)
# `-z`, not `${#TARGETS[@]} -eq 0`: mapfile over an empty string yields one
# empty element, so counting the array cannot see the case this guards.
if [ -z "$TARGETS_RAW" ]; then
  echo "no collectors in the registry — this check would pass having tested nothing"
  exit 1
fi
mapfile -t TARGETS <<< "$TARGETS_RAW"

# The Envoy data-plane Service name is generated and includes a hash, so it is
# found by the label Envoy Gateway stamps on it rather than hardcoded.
ENVOY_SVC=$(kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=foundry \
  -o jsonpath='{.items[0].metadata.name}')
forward envoy-gateway-system "$ENVOY_SVC" 8080:80

for target in "${TARGETS[@]}"; do
  IFS=$'\t' read -r NAME _PREFIX PORT _CAPTURE <<< "$target"
  forward default "$NAME" "$PORT:$PORT"
done

PP_PORT=$("${FLEET[@]}" --port player-projections)
forward default player-projections "$PP_PORT:$PP_PORT"

sleep 3

# Matches scripts/deploy-local.py's LOCAL_DEV_TOKEN. Kind-only.
TOKEN=local-dev-token
AUTH="Authorization: Bearer $TOKEN"
GATEWAY_BASE=http://localhost:8080

# ── the standard collector contract surface, once per registered collector ────

for target in "${TARGETS[@]}"; do
  IFS=$'\t' read -r NAME PREFIX PORT CAPTURE <<< "$target"
  BASE="http://localhost:$PORT"
  GATEWAY="$GATEWAY_BASE$PREFIX"
  echo "--- $NAME  (direct $BASE, gateway $GATEWAY)"

  # /health and /metrics are exempt from bearer auth in-process, because the
  # kubelet's probes and Prometheus's annotation scrape cannot carry a token
  # and a probe cannot read a Secret. In-cluster they must answer.
  curl -sf "$BASE/health" | grep '"status":"ok"'
  curl -sf "$BASE/metrics" | grep '# HELP'

  # That exemption is not a reason to publish them at the edge, so the gateway
  # routes only the declared contract paths and these two 404 there.
  for p in health metrics; do
    expect_status 404 "$NAME: /$p must not be published at the edge" "$GATEWAY/$p"
  done

  # The shared /signals surface, through the gateway — which also proves the
  # prefix strip and rewrite land on the right route.
  curl -sf -H "$AUTH" "$GATEWAY/signals" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'envelopes' in data, data
"

  # An unknown signal_type must 422 rather than return an empty list, so a
  # client bug surfaces instead of looking like a quiet week.
  expect_status 422 "$NAME: unknown signal_type" \
    -H "$AUTH" "$GATEWAY/signals?signal_type=nonsense"

  # Rejections. The direct-to-Service one is the one that matters: it bypasses
  # the gateway entirely, so under gateway-only auth it would return 200 and
  # this required check would pass over an unprotected path. That is the whole
  # reason auth is enforced in-process.
  expect_status 401 "$NAME: gateway without token" "$GATEWAY/signals"
  expect_status 401 "$NAME: direct Service call without token" "$BASE/signals"
  expect_status 401 "$NAME: gateway with wrong token" \
    -H "Authorization: Bearer wrong-token" "$GATEWAY/signals"

  # /catalog's agreement with the registry is not asserted here — it is
  # asserted for every entry, live and through the gateway, by
  # scripts/check-registry.py at the bottom of this file. Two copies of that
  # comparison would be one copy too many.

  # Anything beyond the standard five is the collector's own business. An
  # executable services/<name>/smoke.sh runs here if it exists; a collector
  # with no extra routes needs no file and no edit to this script.
  HOOK="$ROOT/services/$NAME/smoke.sh"
  if [ -f "$HOOK" ]; then
    echo "    running services/$NAME/smoke.sh"
    SMOKE_COLLECTOR="$NAME" \
    SMOKE_BASE_URL="$BASE" \
    SMOKE_GATEWAY_URL="$GATEWAY" \
    SMOKE_TOKEN="$TOKEN" \
    SMOKE_CAPTURE_ENABLED="$CAPTURE" \
      bash "$HOOK"
  fi

  echo "$NAME: OK"
done

# ── player-projections — not a collector, so none of the above applies ────────

PP="http://localhost:$PP_PORT"

curl -sf "$PP/health" | grep '"status":"ok"'
curl -sf "$PP/metrics" | grep '# HELP'
curl -sf "$PP/projections" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'projections' in data
assert 'count' in data
assert data['format'] == 'ppr', f\"default format should be ppr, got {data['format']}\"
print('projections OK')
"
# All three scoring modes must be served by the same deployment.
for FMT in standard half-ppr ppr; do
  curl -sf "$PP/projections?format=$FMT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['format'] == '$FMT', f\"asked for $FMT, got {data['format']}\"
"
done
echo "scoring formats OK"

# The position filter and its 422s are part of the public surface.
curl -sf "$PP/projections?pos=RB,WR,TE" > /dev/null
expect_status 422 "pos=FLEX" "$PP/projections?pos=FLEX"
expect_status 422 "unknown format" "$PP/projections?format=quarter-ppr"
echo "filters OK"

expect_status 404 "unknown player" "$PP/projections/unknown-player"
echo "player-projections: OK"

# ── the registry drift gate's live half ───────────────────────────────────────
#
# tests/test_collector_registry.py checks everything decidable from files; this
# proves each registered collector is actually deployed, routed at its registry
# path, and serving a /catalog that agrees with the file on collector name,
# cadence class, envelope version and signal types. It goes through the gateway
# on :8080 (already forwarded above) rather than per-service port-forwards,
# because /catalog is in the chart's default gateway.publicPaths for every
# collector — so this needs no new plumbing as the fleet grows.
uv run --no-project --with pyyaml==6.0.3 python3 "$ROOT/scripts/check-registry.py" \
  --base-url "$GATEWAY_BASE" --token "$TOKEN"
