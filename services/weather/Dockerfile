# =============================================================================
# Stage 1 — workspace-manifests. THE REFERENCE COPY OF THIS STAGE.
#
# Every collector's Dockerfile carries this stage verbatim; this one carries
# the write-up. tests/test_dockerfile_workspace.py enforces that they agree.
#
# WHY IT EXISTS
#
# `uv sync --locked --package <x>` resolves the ENTIRE workspace graph before
# it can sync any single member, so every member's pyproject.toml has to be in
# the build context — including members this service does not depend on. Miss
# one and the build fails with "the lockfile needs to be updated".
#
# Listing them one COPY line at a time is quadratic. At 26 collectors that is
# 26 lines in each of 26 Dockerfiles that must all agree, and the line you
# forget breaks an UNRELATED service's image — a break no pytest run can see,
# because pytest never touches a Dockerfile. This is not hypothetical: adding
# `roster-scope` to the workspace broke this file, and the agent that did it
# had built only its own image and concluded Docker was fine.
#
# WHAT IT DOES
#
# Takes the whole context and reduces it to exactly what dependency resolution
# needs, and nothing else:
#
#   pyproject.toml, uv.lock     the workspace root and its single lockfile
#   libs/**                     in FULL — libs are BUILT as dependencies
#                               (`Building collector-core @ file:///app/...`),
#                               not merely resolved, so a manifest is not enough
#   services/*/pyproject.toml    manifest only, no source, for every service
#
# Adding a workspace member therefore requires no edit to any Dockerfile. The
# globs are `libs` and `services` wholesale; no member is named anywhere.
#
# WHY A WHOLE STAGE AND NOT ONE COPY LINE
#
# `COPY services/*/pyproject.toml ./services/` does not work: Docker FLATTENS a
# multi-source COPY into the destination directory, so 26 manifests collapse
# onto one path and the last one wins. Verify this yourself before "simplifying"
# the stage away.
#
# `COPY --parents services/*/pyproject.toml ./` does preserve the tree, and is
# one line — but it is a BuildKit *labs* feature requiring
# `# syntax=docker/dockerfile:1.7-labs`, which pins every image in the repo to
# an unstable frontend fetched over the network at build time. This stage uses
# only FROM/COPY/RUN and cannot rot.
#
# A `.dockerignore` broad enough to make `COPY services/ ./services/` cheap was
# also rejected: .dockerignore is global to a build, so excluding service source
# to protect the dependency layer would also break the `COPY services/<self>/`
# below. One file cannot say both things.
#
# WHY IT DOES NOT COST YOU THE LAYER CACHE
#
# This stage's own `COPY . .` busts on any source edit, and the RUN below
# re-executes — for about half a second. That is deliberate. BuildKit keys
# `COPY --from=<stage>` on the CONTENT of the files it copies, not on the
# source stage's layer digest, so the builder stage below sees an unchanged
# /manifests and the expensive `uv sync` layer is reused. Editing weather's
# source does not re-resolve dependencies; editing any pyproject.toml or
# uv.lock does. Both directions are measured in the PR that introduced this.
# =============================================================================
FROM python:3.12-slim AS workspace-manifests

WORKDIR /src

COPY . .

RUN set -eu; \
    mkdir -p /manifests; \
    cp pyproject.toml uv.lock /manifests/; \
    cp -a libs /manifests/libs; \
    find services -mindepth 2 -maxdepth 2 -name pyproject.toml \
        -exec cp --parents {} /manifests/ \;

# =============================================================================
# Stage 2 — builder
# =============================================================================
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
    uv sync --locked --no-dev --no-install-project --package weather

COPY services/weather/weather/ ./services/weather/weather/
# `--reinstall-package` is not optional, and the reason is nasty: the package
# version never changes (0.1.0), so uv's build cache under the mount above can
# serve a PREVIOUSLY BUILT WHEEL even though the source just changed. The image
# then silently ships stale code — it builds, it starts, it passes a health
# check, and it is executing a different revision than the tree it was built
# from. That happened twice while debugging roster-scope and invalidated two
# whole rounds of container measurements.
#
# Dropping the cache mount would also fix it, at the cost of re-downloading
# every dependency on every build. Reinstalling just this one package keeps the
# dependency cache and removes the hazard.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package weather \
        --reinstall-package weather

# =============================================================================
# Stage 3 — runtime
# =============================================================================
FROM python:3.12-slim

RUN addgroup --system --gid 65532 app && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "weather.main:app", "--host", "0.0.0.0", "--port", "8000"]
