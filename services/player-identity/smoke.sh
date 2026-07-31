#!/usr/bin/env bash
#
# player-identity's own smoke assertions — the three routes beyond the standard
# five. See services/weather/smoke.sh for the SMOKE_* contract this is invoked
# with; scripts/smoke-test.sh runs it after the standard surface passes.
#
# Deliberately no POST /refresh, unlike weather. This collector runs with
# CAPTURE_ENABLED=false because the upstream players document is ~5 MB and
# Sleeper asks for at-most-daily polling — and a dispatched /refresh reaches
# the upstream regardless of that flag, so calling it would hit a third party
# on every PR.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

# With no capture run the index is empty, so a resolve refuses rather than
# guessing — which is the contract.
curl -sf -H "$AUTH" "$SMOKE_GATEWAY_URL/resolve?name=Patrick%20Mahomes&team=KC" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['resolved'] is False, data
assert data['candidates'] == [], data
print('resolve OK')
"
curl -sf -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"queries":[{"name":"Patrick Mahomes","team":"KC"}]}' \
  "$SMOKE_GATEWAY_URL/resolve/batch" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['count'] == 1, data
print('resolve/batch OK')
"
curl -sf -H "$AUTH" "$SMOKE_GATEWAY_URL/unresolved" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'misses' in data, data
print('unresolved OK')
"

# A resolve query with nothing to match on must be loud, not an empty list.
STATUS=$(curl -o /dev/null -sw '%{http_code}' -H "$AUTH" "$SMOKE_GATEWAY_URL/resolve")
[ "$STATUS" = "422" ] || { echo "empty resolve query should be 422, got $STATUS"; exit 1; }

# Same auth story as /signals, on this collector's own routes — including the
# direct-to-Service path that bypasses the gateway entirely.
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$SMOKE_GATEWAY_URL/resolve?name=x")
[ "$STATUS" = "401" ] || { echo "gateway /resolve without token should be 401, got $STATUS"; exit 1; }
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$SMOKE_BASE_URL/resolve?name=x")
[ "$STATUS" = "401" ] || { echo "direct /resolve without token should be 401, got $STATUS"; exit 1; }
