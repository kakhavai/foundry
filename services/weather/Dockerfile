FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# A uv workspace has ONE lockfile, at the workspace root — there is no
# services/weather/uv.lock (Task 1 deleted it). The root pyproject.toml and
# root uv.lock both come in, plus EVERY member's manifest, because uv resolves
# the whole workspace graph before it can sync any single member.
#
# That is why player-identity's and roster-scope's manifests are here despite
# weather not depending on either: `--package weather` still resolves the
# whole graph first, so a member missing from the context fails the build with
# "the lockfile needs to be updated" — a break that no pytest run can see,
# because pytest never touches the Dockerfile. Every new workspace member adds
# a line here.
COPY pyproject.toml uv.lock ./
COPY libs/collector-core/ ./libs/collector-core/
COPY services/weather/pyproject.toml ./services/weather/
COPY services/player-identity/pyproject.toml ./services/player-identity/
COPY services/roster-scope/pyproject.toml ./services/roster-scope/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --package weather

COPY services/weather/weather/ ./services/weather/weather/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package weather

FROM python:3.12-slim

RUN addgroup --system --gid 65532 app && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "weather.main:app", "--host", "0.0.0.0", "--port", "8000"]
