#!/usr/bin/env bash
#
# offensive-line's own smoke assertions — the route beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
# services/weather/smoke.sh is the model.
#
# **No POST /refresh here.** This collector runs with CAPTURE_ENABLED=false,
# and a dispatched refresh reaches the upstream regardless of that flag — it
# would pull ~78 MiB from nflverse on every PR, for a season whose play-by-play
# artifact does not exist yet.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

if [ "$SMOKE_CAPTURE_ENABLED" = "true" ]; then
  echo "offensive-line: CAPTURE_ENABLED is true — this hook assumes it is false"
  echo "  (see helm/values/offensive-line/values.yaml for why it is)"
  exit 1
fi

# /lineups, at BOTH the gateway and the Service.
#
# The direct-to-Service call is the one that matters: auth is enforced
# in-process rather than at the gateway, so under gateway-only enforcement the
# Service call would return 200 over an unprotected path and this required
# check would pass on it.
#
# An uncaptured week answers `{"lineups": [], "count": 0}` rather than an
# error, which is the correct shape for an empty lake partition and is exactly
# what a CAPTURE_ENABLED=false collector has. Asserting the *shape* is the
# point; asserting rows would need a capture this hook must not dispatch.
for BASE in "$SMOKE_GATEWAY_URL" "$SMOKE_BASE_URL"; do
  curl -sf -H "$AUTH" "$BASE/lineups?season=2026&week=1" \
    | python3 -c "
import sys, json
body = json.load(sys.stdin)
assert body['count'] == len(body['lineups']), body
assert body['season'] == 2026 and body['week'] == 1, body
print('lineups OK')
"
done

# And it must be behind the bearer token like every other data route. A 401
# here proves the middleware covers a route added AFTER build_collector_app,
# which is the whole reason auth is middleware rather than a per-route
# dependency.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
  "$SMOKE_BASE_URL/lineups?season=2026&week=1")
if [ "$STATUS" != "401" ]; then
  echo "offensive-line: /lineups answered $STATUS without a token, expected 401"
  exit 1
fi

echo "offensive-line: extra-route smoke OK"
