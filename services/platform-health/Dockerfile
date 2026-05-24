FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock* ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

RUN addgroup --system app && adduser --system --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/

USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8001

CMD ["uvicorn", "platform_health.main:app", "--host", "0.0.0.0", "--port", "8001"]
