FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim

RUN addgroup --system app && adduser --system --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/

USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src"

EXPOSE 8000

CMD ["uvicorn", "github_stats.main:app", "--host", "0.0.0.0", "--port", "8000"]
