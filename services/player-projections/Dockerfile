# player-projections is NOT a uv workspace member (see the root pyproject.toml's
# [tool.uv.workspace].members) and does not consume libs/collector-core. It owns
# its own uv.lock and builds from its own directory as the context:
#
#     docker build -f services/player-projections/Dockerfile \
#         -t player-projections:local services/player-projections
#
# That is why it has no `workspace-manifests` stage: there is no workspace graph
# to resolve, so the quadratic per-member COPY problem that stage solves does
# not exist here. DO NOT COPY THIS FILE AS A COLLECTOR TEMPLATE —
# services/weather/Dockerfile is the collector reference.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock ./
COPY player_projections/ ./player_projections/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim

RUN addgroup --system --gid 65532 app && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8001

CMD ["uvicorn", "player_projections.main:app", "--host", "0.0.0.0", "--port", "8001"]
