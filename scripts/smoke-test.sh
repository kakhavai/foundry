#!/usr/bin/env bash
set -euo pipefail

cleanup() {
  # Expand each as "possibly-unset" (":-") — the trap is armed before the
  # Envoy Service lookup below, and if that lookup fails under set -e, these
  # three are never assigned; without the default, set -u would throw its own
  # "unbound variable" error on top of the real failure.
  kill "${WEATHER_PF:-}" "${PP_PF:-}" "${GW_PF:-}" "${PI_PF:-}" "${RS_PF:-}" 2>/dev/null || true
}
trap cleanup EXIT

# The Envoy data-plane Service name is generated and includes a hash, so it is
# found by the label Envoy Gateway stamps on it rather than hardcoded.
ENVOY_SVC=$(kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=foundry \
  -o jsonpath='{.items[0].metadata.name}')

kubectl port-forward svc/weather 8000:8000 &
WEATHER_PF=$!
kubectl port-forward svc/player-projections 8001:8001 &
PP_PF=$!
kubectl port-forward svc/roster-scope 8003:8003 &
RS_PF=$!
kubectl port-forward -n envoy-gateway-system "svc/$ENVOY_SVC" 8080:80 &
GW_PF=$!
kubectl port-forward svc/player-identity 8002:8002 &
PI_PF=$!
sleep 3

# Matches scripts/deploy-local.py's LOCAL_DEV_TOKEN. Kind-only.
TOKEN=local-dev-token
GATEWAY=http://localhost:8080/collectors/weather
AUTH="Authorization: Bearer $TOKEN"

# weather — /health and /metrics are exempt from auth so the kubelet's probes
# and Prometheus's scrape keep working.
curl -sf http://localhost:8000/health | grep '"status":"ok"'
curl -sf http://localhost:8000/metrics | grep '# HELP'

curl -sf -H "$AUTH" http://localhost:8000/catalog | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['collector'] == 'weather', data
assert set(data['signal_types']) == {
    'venue_forecast_kickoff', 'venue_conditions_current'}, data
print('catalog OK')
"

# The doubled path segment is gone: weather's routes no longer live under
# /weather/, so the gateway's strip of /collectors/weather lands on /signals.
curl -sf -H "$AUTH" "$GATEWAY/signals" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'envelopes' in data, data
print('gateway routing OK')
"

# An unknown signal_type must 422 rather than return an empty list, so a client
# bug surfaces instead of looking like a quiet week.
STATUS=$(curl -o /dev/null -sw '%{http_code}' -H "$AUTH" \
  "$GATEWAY/signals?signal_type=nonsense")
[ "$STATUS" = "422" ] || (echo "unknown signal_type should be 422, got $STATUS" && exit 1)

# Rejections. The second one is the one that matters: it goes straight at the
# Service, bypassing the gateway entirely. Under gateway-only auth it would
# return 200 and this required check would pass over an unprotected path.
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$GATEWAY/signals")
[ "$STATUS" = "401" ] || (echo "gateway without token should be 401, got $STATUS" && exit 1)
STATUS=$(curl -o /dev/null -sw '%{http_code}' http://localhost:8000/signals)
[ "$STATUS" = "401" ] || (echo "direct Service call without token should be 401, got $STATUS" && exit 1)
STATUS=$(curl -o /dev/null -sw '%{http_code}' -H "Authorization: Bearer wrong-token" "$GATEWAY/signals")
[ "$STATUS" = "401" ] || (echo "gateway with wrong token should be 401, got $STATUS" && exit 1)
echo "collector auth OK"

# The auth exemption for /health and /metrics is necessary in-cluster: the
# kubelet's probes and the annotation scrape cannot carry a token, and a probe
# cannot read a Secret. That is not a reason to publish them at the edge, so the
# gateway routes only the contract paths. In-cluster they must still answer.
for p in health metrics; do
  STATUS=$(curl -o /dev/null -sw '%{http_code}' "$GATEWAY/$p")
  [ "$STATUS" = "404" ] || (echo "$p must not be published at the edge, got $STATUS" && exit 1)
done
echo "edge surface OK (/health and /metrics not routed)"

# Same two paths, in-cluster, unauthenticated, must still work — otherwise the
# probes and the metrics scrape break and the fix has overshot.
curl -sf http://localhost:8000/health | grep '"status":"ok"'
curl -sf http://localhost:8000/metrics | grep '# HELP'
echo "in-cluster exempt paths OK"

# Force a capture, then prove it reached the object store. This is the only
# check that exercises a real Pod reading its credentials from a Secret and
# resolving MinIO through cluster DNS — the local ArgoCD-managed cluster cannot.
curl -sf -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"season":2026,"week":1}' http://localhost:8000/refresh \
  | python3 -c "import sys,json; assert json.load(sys.stdin)['refresh_id']; print('refresh accepted')"

# /refresh returns before the capture finishes, so poll rather than assume.
#
# Count on the runner, not inside the container: the MinIO image is minimal and
# has no `grep`, so piping to it in-container fails with exit 127 on every poll
# and the loop can only ever report zero — a broken counter that looks exactly
# like a broken lake write.
OBJECTS=0
for _ in $(seq 1 30); do
  OBJECTS=$(kubectl exec -n monitoring deploy/minio -- \
    ls -R /export/foundry-signals 2>/dev/null | grep -c '\.json' || true)
  OBJECTS=${OBJECTS:-0}
  [ "$OBJECTS" -gt 0 ] && break
  sleep 2
done
if [ "$OBJECTS" -eq 0 ]; then
  echo "no envelope reached the lake after 60s. Bucket contents:"
  kubectl exec -n monitoring deploy/minio -- ls -R /export 2>&1 | head -30 || true
  exit 1
fi
echo "lake write OK ($OBJECTS object(s))"

echo "weather: OK"

# player-projections
curl -sf http://localhost:8001/health | grep '"status":"ok"'
curl -sf http://localhost:8001/metrics | grep '# HELP'
curl -sf http://localhost:8001/projections | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'projections' in data
assert 'count' in data
assert data['format'] == 'ppr', f\"default format should be ppr, got {data['format']}\"
print('projections OK')
"
# All three scoring modes must be served by the same deployment.
for FMT in standard half-ppr ppr; do
  curl -sf "http://localhost:8001/projections?format=$FMT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['format'] == '$FMT', f\"asked for $FMT, got {data['format']}\"
"
done
echo "scoring formats OK"

# The position filter and its 422s are part of the public surface.
curl -sf 'http://localhost:8001/projections?pos=RB,WR,TE' > /dev/null
STATUS=$(curl -o /dev/null -sw '%{http_code}' 'http://localhost:8001/projections?pos=FLEX')
[ "$STATUS" = "422" ] || (echo "pos=FLEX should be 422, got $STATUS" && exit 1)
STATUS=$(curl -o /dev/null -sw '%{http_code}' 'http://localhost:8001/projections?format=quarter-ppr')
[ "$STATUS" = "422" ] || (echo "unknown format should be 422, got $STATUS" && exit 1)
echo "filters OK"

STATUS=$(curl -o /dev/null -sw '%{http_code}' http://localhost:8001/projections/unknown-player)
[ "$STATUS" = "404" ] && echo "404 OK" || (echo "expected 404, got $STATUS" && exit 1)
echo "player-projections: OK"

# player-identity
#
# Deliberately no POST /refresh here, unlike weather. This collector runs with
# CAPTURE_ENABLED=false because the upstream players document is ~5 MB and
# Sleeper asks for at-most-daily polling — and a dispatched /refresh reaches
# the upstream regardless of that flag, so calling it would hit a third party
# on every PR. The contract surface is what this asserts.
PI_GATEWAY=http://localhost:8080/collectors/player-identity

curl -sf http://localhost:8002/health | grep '"status":"ok"'
curl -sf http://localhost:8002/metrics | grep '# HELP'

curl -sf -H "$AUTH" "$PI_GATEWAY/catalog" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['collector'] == 'player-identity', data
assert data['cadence_class'] == 'seasonal', data
assert set(data['signal_types']) == {
    'player_identity_crosswalk', 'name_resolution_miss'}, data
print('player-identity catalog OK')
"

curl -sf -H "$AUTH" "$PI_GATEWAY/signals" | python3 -c "
import sys, json
assert 'envelopes' in json.load(sys.stdin)
print('player-identity signals OK')
"

# The three routes beyond the standard five. With no capture run the index is
# empty, so a resolve refuses rather than guessing — which is the contract.
curl -sf -H "$AUTH" "$PI_GATEWAY/resolve?name=Patrick%20Mahomes&team=KC" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['resolved'] is False, data
assert data['candidates'] == [], data
print('resolve OK')
"
curl -sf -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"queries":[{"name":"Patrick Mahomes","team":"KC"}]}' \
  "$PI_GATEWAY/resolve/batch" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['count'] == 1, data
print('resolve/batch OK')
"
curl -sf -H "$AUTH" "$PI_GATEWAY/unresolved" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'misses' in data, data
print('unresolved OK')
"

# A resolve query with nothing to match on must be loud, not an empty list.
STATUS=$(curl -o /dev/null -sw '%{http_code}' -H "$AUTH" "$PI_GATEWAY/resolve")
[ "$STATUS" = "422" ] || (echo "empty resolve query should be 422, got $STATUS" && exit 1)

# Same auth story as weather, including the direct-to-Service path that
# bypasses the gateway entirely.
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$PI_GATEWAY/resolve?name=x")
[ "$STATUS" = "401" ] || (echo "gateway without token should be 401, got $STATUS" && exit 1)
STATUS=$(curl -o /dev/null -sw '%{http_code}' 'http://localhost:8002/resolve?name=x')
[ "$STATUS" = "401" ] || (echo "direct Service call without token should be 401, got $STATUS" && exit 1)

for p in health metrics; do
  STATUS=$(curl -o /dev/null -sw '%{http_code}' "$PI_GATEWAY/$p")
  [ "$STATUS" = "404" ] || (echo "$p must not be published at the edge, got $STATUS" && exit 1)
done
echo "player-identity: OK"

# roster-scope
RS_GATEWAY=http://localhost:8080/collectors/roster-scope

curl -sf http://localhost:8003/health | grep '"status":"ok"'
curl -sf http://localhost:8003/metrics | grep '# HELP'

curl -sf -H "$AUTH" http://localhost:8003/catalog | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['collector'] == 'roster-scope', data
assert set(data['signal_types']) == {
    'scope_membership_weekly', 'scope_change_event'}, data
assert data['cadence_class'] == 'weekly', data
print('roster-scope catalog OK')
"

# /scope is published at the edge as well as /signals — the resolved list is
# the route every other collector calls, so it has to survive the gateway's
# prefix strip and rewrite.
curl -sf -H "$AUTH" "$RS_GATEWAY/scope/rules" | python3 -c "
import sys, json
data = json.load(sys.stdin)
ids = {r['rule_id'] for r in data['rules']}
assert 'wr_depth_le_4' in ids, data
assert data['teams'] == 32, data
print('roster-scope scope/rules OK')
"

curl -sf -H "$AUTH" "$RS_GATEWAY/signals" | python3 -c "
import sys, json
assert 'envelopes' in json.load(sys.stdin)
print('roster-scope gateway routing OK')
"

# The direct-Service call is the one that matters: under gateway-only auth it
# would return 200 and this required check would pass over an unprotected path.
STATUS=$(curl -o /dev/null -sw '%{http_code}' http://localhost:8003/scope/players)
[ "$STATUS" = "401" ] || (echo "direct roster-scope call without token should be 401, got $STATUS" && exit 1)
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$RS_GATEWAY/scope/players")
[ "$STATUS" = "401" ] || (echo "roster-scope gateway without token should be 401, got $STATUS" && exit 1)

for p in health metrics; do
  STATUS=$(curl -o /dev/null -sw '%{http_code}' "$RS_GATEWAY/$p")
  [ "$STATUS" = "404" ] || (echo "roster-scope $p must not be published at the edge, got $STATUS" && exit 1)
done
echo "roster-scope: OK"

# collector registry — the half of the drift gate that needs a live cluster.
# tests/test_collector_registry.py checks everything decidable from files; this
# proves each registered collector is actually deployed, routed at its registry
# path, and serving a /catalog that agrees with the file. It goes through the
# gateway on :8080 (already forwarded above) rather than per-service
# port-forwards, because /catalog is in the chart's default gateway.publicPaths
# for every collector — so this needs no new plumbing as the fleet grows.
#
# --no-project because the script needs pyyaml and the standard library and
# nothing from the uv workspace; without it, `uv run` from the repo root builds
# collector-core and weather into a throwaway venv to run a file that never
# imports them.
uv run --no-project --with pyyaml==6.0.3 python3 scripts/check-registry.py \
  --base-url http://localhost:8080 --token "$TOKEN"
