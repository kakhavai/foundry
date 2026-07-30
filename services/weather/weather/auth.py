"""weather's binding to the shared bearer-token middleware."""

from collector_core.auth import DEFAULT_EXEMPT_PATHS, build_bearer_middleware

from .metrics import metrics

EXEMPT_PATHS = DEFAULT_EXEMPT_PATHS
require_bearer_token = build_bearer_middleware(metrics, EXEMPT_PATHS)
