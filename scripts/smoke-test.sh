#!/usr/bin/env bash
set -euo pipefail

cleanup() {
  # Expand each as "possibly-unset" (":-") — the trap is armed before the
  # Envoy Service lookup below, and if that lookup fails under set -e, these
  # three are never assigned; without the default, set -u would throw its own
  # "unbound variable" error on top of the real failure.
  kill "${WEATHER_PF:-}" "${PP_PF:-}" "${GW_PF:-}" 2>/dev/null || true
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
kubectl port-forward -n envoy-gateway-system "svc/$ENVOY_SVC" 8080:80 &
GW_PF=$!
sleep 3

# Matches scripts/deploy-local.py's LOCAL_DEV_TOKEN. Kind-only.
TOKEN=local-dev-token
GATEWAY=http://localhost:8080/collectors/weather
AUTH="Authorization: Bearer $TOKEN"

# weather — /health and /metrics are exempt from auth so the kubelet's probes
# and Prometheus's scrape keep working.
curl -sf http://localhost:8000/health | grep '"status":"ok"'
curl -sf http://localhost:8000/metrics | grep '# HELP'
curl -sf -H "$AUTH" http://localhost:8000/weather/stadiums | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['count'] == 30, f'expected 30 stadiums, got {data[\"count\"]}'
print('stadiums OK')
"

# Through the gateway, authenticated. The doubled path segment is expected:
# the gateway strips /collectors/weather and weather's own routes live under
# /weather/. Phase 8's 8A removes the doubling.
curl -sf -H "$AUTH" "$GATEWAY/weather/stadiums" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['count'] == 30, f'gateway: expected 30 stadiums, got {data[\"count\"]}'
print('gateway routing OK')
"

# Rejections. The second one is the one that matters: it goes straight at the
# Service, bypassing the gateway entirely. Under gateway-only auth it would
# return 200 and this required check would pass over an unprotected path.
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$GATEWAY/weather/stadiums")
[ "$STATUS" = "401" ] || (echo "gateway without token should be 401, got $STATUS" && exit 1)
STATUS=$(curl -o /dev/null -sw '%{http_code}' http://localhost:8000/weather/stadiums)
[ "$STATUS" = "401" ] || (echo "direct Service call without token should be 401, got $STATUS" && exit 1)
# A wrong (non-empty) token must be rejected too, not just a missing one — this
# is the deployed-path check for what test_wrong_token_is_rejected covers in-process.
STATUS=$(curl -o /dev/null -sw '%{http_code}' -H "Authorization: Bearer wrong-token" "$GATEWAY/weather/stadiums")
[ "$STATUS" = "401" ] || (echo "gateway with wrong token should be 401, got $STATUS" && exit 1)
echo "collector auth OK"
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
