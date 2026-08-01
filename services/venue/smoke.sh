#!/usr/bin/env bash
#
# venue's own smoke assertions — the route beyond the standard five.
#
# Run by scripts/smoke-test.sh after the standard contract surface passes, with:
#
#   SMOKE_COLLECTOR         the collector name
#   SMOKE_BASE_URL          direct to the Service, bypassing the gateway
#   SMOKE_GATEWAY_URL       through the gateway, prefix already applied
#   SMOKE_TOKEN             the bearer token
#   SMOKE_CAPTURE_ENABLED   "true"/"false", from the Helm values' CAPTURE_ENABLED
#
# NO POST /refresh here, and that is not an omission. venue ships with
# CAPTURE_ENABLED=false, and a dispatched refresh reaches the upstream
# regardless of that flag — it would stream the nflverse game table on every
# PR. It does not need one either: GET /venues/{id}/revisions is served from the
# committed reference table, so it answers correctly before any capture has ever
# run. That is the single most useful property of building this collector on a
# committed table, and this hook is where it pays off.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

# A venue every deployment has, because it is in the committed table rather
# than in anything a capture produced.
VENUE="lambeau"

# Asserted at BOTH the gateway and the Service. The direct-to-Service call is
# the one that matters: auth is enforced in-process, and under gateway-only
# enforcement this required check would pass over an unprotected path.
for BASE in "$SMOKE_GATEWAY_URL" "$SMOKE_BASE_URL"; do
  curl -sf -H "$AUTH" "$BASE/venues/$VENUE/revisions" \
    | python3 -c "
import json, sys
body = json.load(sys.stdin)
assert body['venue_id'] == '$VENUE', body
assert body['count'] >= 1, body
revisions = body['revisions']
assert len(revisions) == body['count'], body
# Append-only: the history is ordered and only the LAST revision is open.
assert revisions[-1]['effective_to'] is None, revisions
assert all(r['effective_to'] is not None for r in revisions[:-1]), revisions
assert all(r['venue_id'] == '$VENUE' for r in revisions), revisions
print('venue: %d revision(s) for $VENUE' % body['count'])
"
done

# The refusal is as much of the contract as the answer. A date before the table
# makes any claim must resolve to NOTHING rather than to the closest revision —
# that fallback is exactly the retroactive attribution this collector exists to
# prevent, and it would look like a working route.
curl -sf -H "$AUTH" "$SMOKE_GATEWAY_URL/venues/$VENUE/revisions?on=1999-01-01" \
  | python3 -c "
import json, sys
body = json.load(sys.stdin)
assert body['count'] == 0, body
assert body['revisions'] == [], body
print('venue: a pre-table date resolves to nothing, as it must')
"

# An unknown venue is 404, not an empty history: a typo'd id and a venue with no
# revisions are different facts. `-o /dev/null -w` keeps curl from failing the
# script on the non-2xx this is asserting.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" \
  "$SMOKE_GATEWAY_URL/venues/not-a-real-venue/revisions")
if [ "$STATUS" != "404" ]; then
  echo "venue: expected 404 for an unknown venue id, got $STATUS"
  exit 1
fi

# The route is behind the bearer middleware like every route but /health and
# /metrics — proven against the SERVICE, since the gateway does not enforce it.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
  "$SMOKE_BASE_URL/venues/$VENUE/revisions")
if [ "$STATUS" != "401" ]; then
  echo "venue: /venues/{id}/revisions answered $STATUS without a token, expected 401"
  exit 1
fi

echo "venue: extra-route smoke OK"
