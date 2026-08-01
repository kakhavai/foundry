#!/usr/bin/env bash
#
# officiating's own smoke assertions — the routes beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
# services/weather/smoke.sh is the model.
#
# There is deliberately NO POST /refresh here. This collector ships
# CAPTURE_ENABLED=false, and a dispatched refresh reaches the upstreams
# regardless of that flag — it would pull ~21.5 MiB from GitHub on every PR.
# See helm/values/officiating/values.yaml for the arithmetic.
#
# Everything below therefore runs against an EMPTY capture state, which is the
# state this collector is actually deployed in. That is a feature rather than a
# limitation: it is exactly the state in which GET /crews/{crew_id} must still
# answer correctly rather than 500 on a missing envelope.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"
CREW="2025-ref693"

status_of() {
    curl -s -o /dev/null -w '%{http_code}' "$@"
}

# Asserted at BOTH the gateway and the Service. The direct-to-Service call is
# the one that matters: under gateway-only auth it would return 200 and this
# required check would pass over an unprotected path. The gateway call is what
# proves `gateway.publicPaths` actually carries /crews — without that line the
# route works in-cluster and 404s at the edge for a completely different
# reason than the one below.
for BASE in "$SMOKE_GATEWAY_URL" "$SMOKE_BASE_URL"; do
    # An unknown crew is 404, never an empty 200. A consumer that gets an empty
    # profile for a typo'd id files it as "that crew has no history" rather
    # than as its own bug.
    STATUS=$(status_of -H "$AUTH" "$BASE/crews/$CREW")
    if [ "$STATUS" != "404" ]; then
        echo "officiating: expected 404 for an unknown crew at $BASE, got $STATUS" >&2
        exit 1
    fi

    # A malformed id is 422, not 404 — "you asked wrongly" and "there is no
    # such crew" are different answers, and collapsing them sends a client
    # looking for a data problem that does not exist. This also distinguishes
    # a live route from a missing one: a route the gateway does not publish
    # returns 404 for BOTH ids, and the pair of checks is what tells them
    # apart.
    STATUS=$(status_of -H "$AUTH" "$BASE/crews/not-a-crew")
    if [ "$STATUS" != "422" ]; then
        echo "officiating: expected 422 for a malformed crew id at $BASE, got $STATUS" >&2
        exit 1
    fi
done

# Auth is enforced in-process, not at the gateway. Checked against the Service
# directly, because that is the path scripts/smoke-test.sh port-forwards and
# the one a gateway-only policy would leave open.
STATUS=$(status_of "$SMOKE_BASE_URL/crews/$CREW")
if [ "$STATUS" != "401" ]; then
    echo "officiating: /crews must require a bearer token, got $STATUS" >&2
    exit 1
fi

echo "officiating: extra-route smoke OK"
