#!/usr/bin/env bash
set -euo pipefail

cleanup() {
  kill "$WEATHER_PF" "$PP_PF" 2>/dev/null || true
}
trap cleanup EXIT

kubectl port-forward svc/weather 8000:8000 &
WEATHER_PF=$!
kubectl port-forward svc/player-projections 8001:8001 &
PP_PF=$!
sleep 3

# weather
curl -sf http://localhost:8000/health | grep '"status":"ok"'
curl -sf http://localhost:8000/metrics | grep '# HELP'
curl -sf http://localhost:8000/weather/stadiums | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['count'] == 30, f'expected 30 stadiums, got {data[\"count\"]}'
print('stadiums OK')
"
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
