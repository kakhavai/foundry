#!/usr/bin/env bash
#
# player-profile's own smoke assertions — the route beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
# services/weather/smoke.sh is the model.
#
# **No POST /refresh here, deliberately.** This collector deploys with
# CAPTURE_ENABLED=false, and a dispatched refresh reaches the upstream
# regardless of that flag — it would pull ~44 MB off nflverse on every PR. So
# every assertion below is about the ROUTE rather than about captured data, and
# each one is written to hold whether or not a capture has ever landed.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

if [ "$SMOKE_CAPTURE_ENABLED" = "true" ]; then
  echo "player-profile: CAPTURE_ENABLED is true — see helm/values/player-profile"
  echo "  for why it should not be; this hook assumes no capture has run."
  exit 1
fi

# --- the extra route, through the gateway -----------------------------------
#
# 404 or 200, but never 502/503 and never a gateway 404 for an unrouted path.
# Before any capture the handler answers 404 by design ("no capture yet"), which
# is indistinguishable at the status line from "the gateway does not know this
# path" — so the body is checked too. A path missing from `gateway.publicPaths`
# returns Envoy's own 404 with an empty body, while ours carries `detail`.
STATUS_AND_BODY=$(curl -s -o /tmp/pp-age-curve.json -w '%{http_code}' \
  -H "$AUTH" "$SMOKE_GATEWAY_URL/signals/age-curve?position=RB")

case "$STATUS_AND_BODY" in
  200)
    python3 -c "
import json
body = json.load(open('/tmp/pp-age-curve.json'))
assert body['collector'] == 'player-profile', body
assert body['position'] == 'RB', body
# Both halves, because either alone is unreproducible: the sample the
# percentile was taken against, and the boundaries the stage was bucketed with.
assert 'distribution' in body and 'curve_stages' in body, body
print('age-curve served a distribution')
"
    ;;
  404)
    python3 -c "
import json
body = json.load(open('/tmp/pp-age-curve.json'))
# OUR 404, not the gateway's. An unrouted path has no 'detail' key, so this is
# what distinguishes 'no capture yet' from 'the route was never published'.
assert 'detail' in body, f'gateway 404 — is /signals/age-curve in publicPaths? {body}'
print('age-curve routed; no capture yet (CAPTURE_ENABLED=false)')
"
    ;;
  *)
    echo "player-profile: /signals/age-curve returned $STATUS_AND_BODY"
    cat /tmp/pp-age-curve.json || true
    exit 1
    ;;
esac

# --- an unknown position is 422, not an empty distribution ------------------
#
# The route's own refusal. Asserted through the gateway because a 422 generated
# in-process and a 422 generated at the edge are different things, and only the
# in-process one proves the handler ran.
CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "$AUTH" "$SMOKE_GATEWAY_URL/signals/age-curve?position=NOTAPOSITION")
if [ "$CODE" != "422" ]; then
  echo "player-profile: an unknown position returned $CODE, expected 422"
  exit 1
fi

# --- auth, DIRECT to the Service --------------------------------------------
#
# The one that matters. Under gateway-only enforcement this route would answer
# 200 on a ClusterIP reachable by anything in the namespace, and the required
# integration-test check would pass over an unprotected path. The standard
# surface asserts this for /catalog and /signals; a route added after
# `build_collector_app` needs it asserted too.
CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  "$SMOKE_BASE_URL/signals/age-curve?position=RB")
if [ "$CODE" != "401" ]; then
  echo "player-profile: /signals/age-curve without a token returned $CODE, expected 401"
  exit 1
fi

echo "player-profile: extra-route smoke OK"
