#!/usr/bin/env bash
#
# roster-transactions's own smoke assertions — the routes beyond the standard five.
# scripts/smoke-test.sh runs this after the standard contract surface passes,
# with SMOKE_COLLECTOR, SMOKE_BASE_URL (direct to the Service),
# SMOKE_GATEWAY_URL, SMOKE_TOKEN and SMOKE_CAPTURE_ENABLED in the environment.
# services/weather/smoke.sh is the model.
#
# Do NOT POST /refresh from here when this collector runs with
# CAPTURE_ENABLED=false: a dispatched refresh reaches the upstream regardless
# of that flag, so it would hit a third party on every PR.
#
set -euo pipefail

AUTH="Authorization: Bearer $SMOKE_TOKEN"

# TODO: assert this collector's own routes here. Assert them at BOTH
# the gateway and the Service — the direct-to-Service call is the one that
# matters, because under gateway-only auth it would return 200 and this
# required check would pass over an unprotected path.
curl -sf -H "$AUTH" "$SMOKE_GATEWAY_URL/catalog" >/dev/null
echo "roster-transactions: extra-route smoke OK"
