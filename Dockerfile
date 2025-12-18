# syntax=docker/dockerfile:1

# --- base: shared environment and base packages ---
FROM python:3.12-slim AS base
LABEL org.opencontainers.image.source="https://github.com/gaardhus/ainterviewer-backend"

ENV UV_LINK_MODE=copy \
  UV_NO_MANAGED_PYTHON=1 \
  PATH="/app/.venv/bin:/root/.local/bin:$PATH"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt,sharing=locked \
  apt-get update && \
  apt-get install -y --no-install-recommends ca-certificates curl make && \
  rm -rf /var/lib/apt/lists/*

# bring uv into all stages
COPY --from=ghcr.io/astral-sh/uv:0.9.13 /uv /uvx /bin/

# --- deps: install external dependencies ---
FROM base AS deps
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt,sharing=locked \
  apt-get update && \
  apt-get install -y --no-install-recommends git && \
  rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
  --mount=type=secret,id=github_token \
  TOKEN=$(cat /run/secrets/github_token) && \
  git config --global url."https://${TOKEN}@github.com/".insteadOf "https://github.com/" && \
  uv sync --locked --no-install-project && \
  git config --global --unset-all url."https://${TOKEN}@github.com/".insteadOf

# --- build: install the project itself ---
FROM deps AS build
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
  --mount=type=secret,id=github_token \
  TOKEN=$(cat /run/secrets/github_token) && \
  git config --global url."https://${TOKEN}@github.com/".insteadOf "https://github.com/" && \
  uv sync --locked && \
  git config --global --unset-all url."https://${TOKEN}@github.com/".insteadOf

# --- runtime: final compact image ---
FROM base AS runtime
WORKDIR /app
COPY --from=build /app /app

VOLUME ["/app/storage"]
EXPOSE 8666
HEALTHCHECK CMD curl -fsS http://127.0.0.1:8666/api/health || exit 1
CMD ["fastapi", "run", "app/main.py", "--port=8666"]
