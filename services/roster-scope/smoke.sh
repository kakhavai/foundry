#!/usr/bin/env bash
#
# roster-scope's own smoke assertions — the /scope surface beyond the standard
# five. See services/weather/smoke.sh for the SMOKE_* contract this is invoked
# with; scripts/smoke-test.sh runs it after the standard surface passes.
#
# Deliberately no POST /refresh: this collector runs with CAPTURE_ENABLED=false
# because its upstream publishes a ~37 MB document, and a dispatched /refresh
# reaches the upstream regardless of that flag.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

# /scope is published at the edge as well as /signals — the resolved list is
# the route every other collector calls, so it has to survive the gateway's
# prefix strip and rewrite.
curl -sf -H "$AUTH" "$SMOKE_GATEWAY_URL/scope/rules" | python3 -c "
import sys, json
data = json.load(sys.stdin)
ids = {r['rule_id'] for r in data['rules']}
assert 'wr_depth_le_4' in ids, data
assert data['teams'] == 32, data
print('scope/rules OK')
"

# The direct-Service call is the one that matters: under gateway-only auth it
# would return 200 and this required check would pass over an unprotected path.
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$SMOKE_BASE_URL/scope/players")
[ "$STATUS" = "401" ] || { echo "direct /scope/players without token should be 401, got $STATUS"; exit 1; }
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$SMOKE_GATEWAY_URL/scope/players")
[ "$STATUS" = "401" ] || { echo "gateway /scope/players without token should be 401, got $STATUS"; exit 1; }
