#!/usr/bin/env bash
#
# weather's own smoke assertions — the ones the standard collector contract
# surface in scripts/smoke-test.sh cannot make on every collector's behalf.
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

# Force a capture, then prove it reached the object store. This is the only
# check in the whole suite that exercises a real Pod reading its credentials
# from a Secret and resolving MinIO through cluster DNS — the local
# ArgoCD-managed cluster cannot.
#
# weather is the collector that can afford it: it runs with
# CAPTURE_ENABLED=true because its upstream is cheap per call. The collectors
# deployed with CAPTURE_ENABLED=false deliberately do NOT do this, because a
# dispatched /refresh reaches the upstream regardless of that flag and would
# hit a third party on every PR.
if [ "$SMOKE_CAPTURE_ENABLED" != "true" ]; then
  echo "weather: CAPTURE_ENABLED is not true — the lake-write check assumes it is"
  exit 1
fi

curl -sf -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"season":2026,"week":1}' "$SMOKE_BASE_URL/refresh" \
  | python3 -c "import sys,json; assert json.load(sys.stdin)['refresh_id']; print('refresh accepted')"

# /refresh returns before the capture finishes, so poll rather than assume.
#
# Count on the runner, not inside the container: the MinIO image is minimal and
# has no `grep`, so piping to it in-container fails with exit 127 on every poll
# and the loop can only ever report zero — a broken counter that looks exactly
# like a broken lake write.
OBJECTS=0
for _ in $(seq 1 30); do
  OBJECTS=$(kubectl exec -n monitoring deploy/minio -- \
    ls -R /export/foundry-signals 2>/dev/null | grep -c '\.json' || true)
  OBJECTS=${OBJECTS:-0}
  [ "$OBJECTS" -gt 0 ] && break
  sleep 2
done
if [ "$OBJECTS" -eq 0 ]; then
  echo "no envelope reached the lake after 60s. Bucket contents:"
  kubectl exec -n monitoring deploy/minio -- ls -R /export 2>&1 | head -30 || true
  exit 1
fi
echo "lake write OK ($OBJECTS object(s))"
