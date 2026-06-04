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
print('projections OK')
"
STATUS=$(curl -o /dev/null -sw '%{http_code}' http://localhost:8001/projections/unknown-player)
[ "$STATUS" = "404" ] && echo "404 OK" || (echo "expected 404, got $STATUS" && exit 1)
echo "player-projections: OK"
