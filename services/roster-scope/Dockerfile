FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# A uv workspace has ONE lockfile, at the workspace root — there is no
# services/roster-scope/uv.lock. The root pyproject.toml and root uv.lock both
# come in, plus EVERY member's manifest, because uv resolves the whole
# workspace graph before it can sync any single member.
#
# "Every member" is not a figure of speech: `uv sync --locked --package
# roster-scope` fails with "the lockfile needs to be updated" if any sibling
# member's pyproject.toml is absent, even though nothing here depends on it.
# Adding roster-scope to the workspace broke `services/weather/Dockerfile`
# for exactly this reason, and that file now carries the mirror-image line.
# Every future workspace member has to be added to every collector's
# Dockerfile until someone factors this into a shared build stage.
COPY pyproject.toml uv.lock ./
COPY libs/collector-core/ ./libs/collector-core/
COPY services/roster-scope/pyproject.toml ./services/roster-scope/
COPY services/weather/pyproject.toml ./services/weather/
COPY services/player-identity/pyproject.toml ./services/player-identity/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --package roster-scope

COPY services/roster-scope/roster_scope/ ./services/roster-scope/roster_scope/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package roster-scope

FROM python:3.12-slim

RUN addgroup --system --gid 65532 app && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8003

CMD ["uvicorn", "roster_scope.main:app", "--host", "0.0.0.0", "--port", "8003"]
