#!/usr/bin/env bash
#
# depth-chart's own smoke assertions — the route beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
# services/weather/smoke.sh is the model.
#
# NO `POST /refresh` HERE. depth-chart runs with CAPTURE_ENABLED=false, and a
# dispatched refresh reaches the upstream regardless of that flag — it would
# pull a 52.9 MB community-hosted asset on every PR.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"
QUERY="from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z"

# `/signals/diff` THROUGH THE GATEWAY. This is the assertion that catches a
# missing `gateway.publicPaths` entry: the gateway rewrites onto each declared
# path, so `/signals` alone does not carry `/signals/diff` through, and the
# failure mode is a 404 at the edge while the route works perfectly in-cluster.
#
# Nothing has been captured (CAPTURE_ENABLED=false), so the route's own honest
# answer is also a 404 — which is why the STATUS is not enough. The body is: the
# collector answers with its own JSON `detail` naming the missing snapshot,
# where an unrouted path returns the gateway's body instead.
BODY=$(curl -s -H "$AUTH" "$SMOKE_GATEWAY_URL/signals/diff?$QUERY")
DETAIL=$(printf '%s' "$BODY" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("detail",""))' \
  2>/dev/null || true)

case "$DETAIL" in
  *snapshot*) ;;
  *)
    echo "depth-chart: /signals/diff did not answer at the gateway."
    echo "  Expected the collector's own 'no ... snapshot at ...' detail; got:"
    echo "  $BODY"
    echo "  Check gateway.publicPaths in helm/values/depth-chart/values.yaml."
    exit 1
    ;;
esac

# The same route DIRECT TO THE SERVICE, without a token. This is the check that
# matters most: auth is enforced in-process rather than at the gateway, so a
# route added after the fact must still be rejected on a ClusterIP that anything
# in the namespace can reach. Under gateway-only enforcement this would answer
# normally and the required check would pass over an unprotected path.
UNAUTH=$(curl -s -o /dev/null -w '%{http_code}' "$SMOKE_BASE_URL/signals/diff?$QUERY")
if [ "$UNAUTH" != "401" ]; then
  echo "depth-chart: /signals/diff returned $UNAUTH without a token, expected 401"
  exit 1
fi

echo "depth-chart: extra-route smoke OK (/signals/diff routed, and behind auth)"
