"""Bearer-token authentication for the collector API.

The projections generator runs outside the cluster and calls in through the
collector gateway. Enforcement lives here rather than at the gateway for two
reasons: a ClusterIP is reachable by anything in the namespace, so edge-only
auth protects nothing in-cluster; and `scripts/smoke-test.sh` port-forwards the
Service directly, so gateway-only auth would leave a required merge check green
over an unprotected path.

Middleware rather than per-route dependencies, because middleware fails safe: an
HTTP route added later is protected by default. This pattern gets copied across
the Phase 8 collector fleet, so the default matters more than the convenience.
The guarantee is scoped to HTTP, though: `app.middleware("http")` only wraps
`http`-scope ASGI requests, so a future `@app.websocket(...)` route would
bypass this check entirely and need its own. No WebSocket routes exist today.
"""

import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from . import metrics

# The kubelet's liveness probe and Prometheus's annotation-based scrape cannot
# carry a token. Exempting them is what keeps a missing Secret a loud 503 rather
# than a crash loop with no metrics.
EXEMPT_PATHS = frozenset({"/health", "/metrics"})


def _rejection(request: Request) -> tuple[int, str] | None:
    """Return `(status, reason)` when the request must be rejected, else None."""
    if request.url.path in EXEMPT_PATHS:
        return None

    expected = os.getenv("COLLECTOR_TOKEN", "")
    if not expected:
        return 503, "unconfigured"

    header = request.headers.get("Authorization")
    if header is None:
        return 401, "missing"

    # split(None, 1) rather than partition(" "): RFC 7235 allows one or more
    # spaces between the scheme and the credentials, and partition would fold
    # the extra spaces into the token and reject a well-formed header.
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return 401, "malformed"
    token = parts[1]

    # Encoded because compare_digest rejects str containing non-ASCII, and the
    # token arrives from an untrusted header.
    if not secrets.compare_digest(token.encode(), expected.encode()):
        return 401, "invalid"

    return None


async def require_bearer_token(request: Request, call_next):
    rejection = _rejection(request)
    if rejection is None:
        return await call_next(request)

    status, reason = rejection
    metrics.record_auth_failure(reason)

    if status == 503:
        return JSONResponse(
            {"detail": "Collector token is not configured"}, status_code=503
        )
    return JSONResponse(
        {"detail": "Invalid or missing bearer token"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )
