#!/usr/bin/env bash
#
# coaching-scheme's own smoke assertions — the routes beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
# services/weather/smoke.sh is the model.
#
# There is deliberately NO POST /refresh here. This collector ships
# CAPTURE_ENABLED=false, and a dispatched refresh reaches the upstreams
# regardless of that flag — it would pull ~73 MiB from GitHub on every PR, the
# largest per-pass footprint in the fleet. See
# helm/values/coaching-scheme/values.yaml for the arithmetic.
#
# Everything below therefore runs against an EMPTY capture state, which is the
# state this collector is actually deployed in. That is a feature rather than a
# limitation: it is exactly the state in which GET /teams/{team_id}/revisions
# must still answer correctly rather than 500 on a missing envelope.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"
TEAM="KC"

status_of() {
    curl -s -o /dev/null -w '%{http_code}' "$@"
}

# Asserted at BOTH the gateway and the Service. The direct-to-Service call is
# the one that matters: under gateway-only auth it would return 200 and this
# required check would pass over an unprotected path. The gateway call is what
# proves `gateway.publicPaths` actually carries /teams — without that line the
# route works in-cluster and 404s at the edge for a completely different
# reason than the one below.
for BASE in "$SMOKE_GATEWAY_URL" "$SMOKE_BASE_URL"; do
    # A well-formed team with no captured timeline is 404, never an empty 200.
    # A consumer that gets an empty timeline for a typo'd code files it as
    # "that team never changed staff" rather than as its own bug.
    STATUS=$(status_of -H "$AUTH" "$BASE/teams/$TEAM/revisions")
    if [ "$STATUS" != "404" ]; then
        echo "coaching-scheme: expected 404 for an uncaptured team at $BASE, got $STATUS" >&2
        exit 1
    fi

    # A malformed id is 422, not 404 — "you asked wrongly" and "there is no
    # such team" are different answers, and collapsing them sends a client
    # looking for a data problem that does not exist. This also distinguishes
    # a live route from a missing one: a route the gateway does not publish
    # returns 404 for BOTH ids, and the pair of checks is what tells them
    # apart.
    STATUS=$(status_of -H "$AUTH" "$BASE/teams/not-a-team/revisions")
    if [ "$STATUS" != "422" ]; then
        echo "coaching-scheme: expected 422 for a malformed team id at $BASE, got $STATUS" >&2
        exit 1
    fi

    # The season parameter is validated too, for the same reason: a
    # non-numeric value must not silently match nothing.
    STATUS=$(status_of -H "$AUTH" "$BASE/teams/$TEAM/revisions?season=nope")
    if [ "$STATUS" != "422" ]; then
        echo "coaching-scheme: expected 422 for a malformed season at $BASE, got $STATUS" >&2
        exit 1
    fi
done

# Auth is enforced in-process, not at the gateway. Checked against the Service
# directly, because that is the path scripts/smoke-test.sh port-forwards and
# the one a gateway-only policy would leave open.
STATUS=$(status_of "$SMOKE_BASE_URL/teams/$TEAM/revisions")
if [ "$STATUS" != "401" ]; then
    echo "coaching-scheme: /teams must require a bearer token, got $STATUS" >&2
    exit 1
fi

echo "coaching-scheme: extra-route smoke OK"
