#!/usr/bin/env bash
#
# player-stats' own smoke assertions — the route beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
# services/weather/smoke.sh is the model.
#
# NOTHING here posts /refresh: this collector runs with CAPTURE_ENABLED=false
# and a dispatched refresh reaches the upstream regardless of that flag, so it
# would pull an 8.3 MB nflverse asset on every PR. `weather` is the collector
# that owns the real lake-write assertion, for exactly that reason.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

# /revisions at BOTH the gateway and the Service. The direct-to-Service call is
# the one that matters: under gateway-only auth it would return 200 and this
# required check would pass over an unprotected path.
for BASE in "$SMOKE_GATEWAY_URL" "$SMOKE_BASE_URL"; do
  curl -sf -H "$AUTH" "$BASE/revisions" \
    | python3 -c "
import json, sys
body = json.load(sys.stdin)
assert isinstance(body['revisions'], list), body
assert body['count'] == len(body['revisions']), body
"
done

# An unparseable ?since= must be 422, not a silent full history — a caller who
# mistypes a timestamp and gets everything back reads it as 'nothing has been
# restated', which is the opposite of the truth.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" \
  "$SMOKE_BASE_URL/revisions?since=not-a-timestamp")
if [ "$STATUS" != "422" ]; then
  echo "player-stats: /revisions?since=not-a-timestamp returned $STATUS, expected 422"
  exit 1
fi

# The route is bearer-protected in-process, not only at the gateway.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' "$SMOKE_BASE_URL/revisions")
if [ "$STATUS" != "401" ]; then
  echo "player-stats: unauthenticated /revisions returned $STATUS, expected 401"
  exit 1
fi

echo "player-stats: extra-route smoke OK"
