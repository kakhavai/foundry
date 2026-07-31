#!/usr/bin/env bash
#
# roster-transactions's own smoke assertions — the one route beyond the standard
# five, which scripts/smoke-test.sh cannot assert on every collector's behalf.
#
# Run by scripts/smoke-test.sh after the standard surface passes, with:
#
#   SMOKE_COLLECTOR         the collector name
#   SMOKE_BASE_URL          direct to the Service, bypassing the gateway
#   SMOKE_GATEWAY_URL       through the gateway, prefix already applied
#   SMOKE_TOKEN             the bearer token
#   SMOKE_CAPTURE_ENABLED   "true"/"false", from the Helm values' CAPTURE_ENABLED
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

# NO `POST /refresh` here. This collector deploys with CAPTURE_ENABLED=false,
# and a dispatched refresh reaches the upstream regardless of that flag — it
# would hit a third-party transaction wire on every PR. weather is the collector
# that carries the lake-write check, because its upstream is cheap per call.
if [ "$SMOKE_CAPTURE_ENABLED" = "true" ]; then
  echo "roster-transactions: CAPTURE_ENABLED is true — this hook assumes it is not"
  exit 1
fi

# Asserted at BOTH the gateway and the Service. The direct-to-Service call is
# the one that matters: under gateway-only auth it would return 200 over an
# unprotected path and this required check would pass anyway.
for BASE in "$SMOKE_GATEWAY_URL" "$SMOKE_BASE_URL"; do
  curl -sf -H "$AUTH" "$BASE/events?limit=1" \
    | python3 -c "
import sys, json
body = json.load(sys.stdin)
for field in ('events', 'count', 'next_cursor'):
    assert field in body, f'/events response is missing {field!r}: {body}'
assert isinstance(body['events'], list), body
assert body['count'] == len(body['events']), body
"
done

# Unauthenticated must be refused at the Service, not merely at the edge.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' "$SMOKE_BASE_URL/events")
if [ "$STATUS" != "401" ]; then
  echo "roster-transactions: /events without a token returned $STATUS, expected 401"
  exit 1
fi

# A cursor this collector never issued is a client bug and must be loud —
# silently restarting from the beginning would re-deliver a whole week.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" \
  "$SMOKE_BASE_URL/events?since=garbage")
if [ "$STATUS" != "422" ]; then
  echo "roster-transactions: a malformed cursor returned $STATUS, expected 422"
  exit 1
fi

echo "roster-transactions: extra-route smoke OK"
