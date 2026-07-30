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
    uv sync --locked --no-dev --no-install-project --package player-identity

COPY services/player-identity/player_identity/ ./services/player-identity/player_identity/
# --reinstall-package: the package version never changes, so uv's build cache
# under the mount can serve a previously built wheel even though the source
# just changed — the image then silently ships stale code. Observed twice
# while debugging roster-scope; see that service's Dockerfile for the full
# write-up.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package player-identity \
        --reinstall-package player-identity

# Stage 3 — runtime
FROM python:3.12-slim

RUN addgroup --system --gid 65532 app && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8002

CMD ["uvicorn", "player_identity.main:app", "--host", "0.0.0.0", "--port", "8002"]
