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
    uv sync --locked --no-dev --no-install-project --package roster-scope

COPY services/roster-scope/roster_scope/ ./services/roster-scope/roster_scope/
# `--reinstall-package` is not optional, and the reason is nasty: the package
# version never changes (0.1.0), so uv's build cache under the mount above can
# serve a PREVIOUSLY BUILT WHEEL even though the source just changed. The
# image then silently ships stale code — it built, it ran, it passed a health
# check, and it was executing a different revision than the tree it was built
# from. That happened twice while debugging this service, and it invalidated a
# whole round of container measurements before it was spotted.
#
# Dropping the cache mount would also fix it, at the cost of re-downloading
# every dependency on every build. Reinstalling just this one package keeps
# the dependency cache and removes the hazard.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package roster-scope \
        --reinstall-package roster-scope

# Stage 3 — runtime
FROM python:3.12-slim

RUN addgroup --system --gid 65532 app && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8003

CMD ["uvicorn", "roster_scope.main:app", "--host", "0.0.0.0", "--port", "8003"]
