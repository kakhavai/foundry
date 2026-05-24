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
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim

RUN addgroup --system app && adduser --system --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "github_stats.main:app", "--host", "0.0.0.0", "--port", "8000"]
