# Stage 1 — workspace-manifests. Verbatim in every collector; the write-up
# lives in services/weather/Dockerfile, and tests/test_dockerfile_workspace.py
# enforces that they agree.
#
# Short version: `uv sync --package <x>` resolves the whole workspace graph
# before it can sync any member, so every member's pyproject.toml must be in
# the context — even members this service does not depend on. This stage
# reduces the context to exactly that set by glob, so adding a workspace member
# requires no edit here. Do NOT replace it with per-member COPY lines.
FROM python:3.12-slim AS workspace-manifests

WORKDIR /src

COPY . .

RUN set -eu; \
    mkdir -p /manifests; \
    cp pyproject.toml uv.lock /manifests/; \
    cp -a libs /manifests/libs; \
    find services -mindepth 2 -maxdepth 2 -name pyproject.toml \
        -exec cp --parents {} /manifests/ \;

# Stage 2 — builder
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Everything uv needs to resolve the workspace and build this service's
# dependencies — and deliberately none of this service's own source, so the
# sync below stays cached across source edits.
COPY --from=workspace-manifests /manifests/ ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --package roster-transactions

COPY services/roster-transactions/roster_transactions/ ./services/roster-transactions/roster_transactions/
# `--reinstall-package` is not optional, and the reason is nasty: the package
# version never changes (0.1.0), so uv's build cache under the mount above can
# serve a PREVIOUSLY BUILT WHEEL even though the source just changed. The image
# then silently ships stale code — it built, it ran, it passed a health check,
# and it was executing a different revision than the tree it was built from.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package roster-transactions \
        --reinstall-package roster-transactions

# Stage 3 — runtime
FROM python:3.12-slim

# A numeric UID: Kubernetes `runAsNonRoot` can only verify a numeric user.
RUN addgroup --system --gid 65532 app \
    && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8008

CMD ["uvicorn", "roster_transactions.main:app", "--host", "0.0.0.0", "--port", "8008"]
