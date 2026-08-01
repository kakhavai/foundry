#!/usr/bin/env bash
#
# durability-history's own smoke assertions — the route beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
#
# **No POST /refresh here.** This collector deploys with CAPTURE_ENABLED=false,
# and a dispatched refresh reaches the upstream REGARDLESS of that flag — it
# would put 43.8 MB of third-party nflverse traffic on every PR. So everything
# below is asserted against an EMPTY capture, which is exactly the state a
# CAPTURE_ENABLED=false pod is in, and the assertions are chosen so an empty
# capture still proves something: parameter validation runs before the capture is
# read, so a 422 is reachable with no data at all.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"
ROUTE="/signals/return-profile?player_id=fdy-smoke00000001&body_part=hamstring"
BAD_ROUTE="/signals/return-profile?player_id=fdy-smoke00000001&body_part=spleen"

# Asserted at BOTH the gateway and the Service. The direct-to-Service call is the
# one that matters: under gateway-only auth it would return 200 and this required
# check would pass over an unprotected path.
for BASE in "$SMOKE_GATEWAY_URL" "$SMOKE_BASE_URL"; do
    # 404 before any capture, NOT 200-with-an-empty-body: "no capture has landed"
    # and "this player has no hamstring events" are different facts. 200 is
    # accepted too, so this hook does not start failing the day the collector is
    # deployed with CAPTURE_ENABLED=true.
    STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "$BASE$ROUTE")
    if [ "$STATUS" != "404" ] && [ "$STATUS" != "200" ]; then
        echo "durability-history: $BASE$ROUTE returned $STATUS (want 404 or 200)" >&2
        exit 1
    fi

    # An unknown body part is 422 even with no capture — the validation runs
    # before the capture is read, which is what makes this assertable without
    # touching a third party. An empty result for a typo would be filed by a
    # client as "he has never hurt that".
    STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "$BASE$BAD_ROUTE")
    if [ "$STATUS" != "422" ]; then
        echo "durability-history: an unknown body_part returned $STATUS, want 422" >&2
        exit 1
    fi
done

# The extra route must be behind the same bearer middleware as everything else.
# It is added AFTER build_collector_app, so "protected by default" is a claim
# worth one assertion at the Service itself.
STATUS=$(curl -s -o /dev/null -w '%{http_code}' "$SMOKE_BASE_URL$ROUTE")
if [ "$STATUS" != "401" ]; then
    echo "durability-history: the extra route answered $STATUS with no token" >&2
    exit 1
fi

echo "durability-history: extra-route smoke OK"
