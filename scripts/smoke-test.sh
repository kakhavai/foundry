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
