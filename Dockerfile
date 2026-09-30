# syntax=docker/dockerfile:1

# Build stage: resolve the locked dependencies into a virtual environment.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Runtime stage: only the virtual environment, run as an unprivileged user.
FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app

# Configure with environment variables, e.g.
#   docker run -i --rm -e FLECS_REST_URL=http://host.docker.internal:27750 flecs-mcp
ENTRYPOINT ["flecs-mcp"]
