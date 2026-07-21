# syntax=docker/dockerfile:1

# ---- builder: resolve + install into a self-contained venv ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer 1: external dependencies only. Copy just the manifests so this layer
# is cached until the lockfile or a package's pyproject changes. All three
# workspace members are needed for uv to validate the frozen lockfile, even
# though we only install the server and its protocol dependency.
COPY pyproject.toml uv.lock ./
COPY packages/protocol/pyproject.toml packages/protocol/pyproject.toml
COPY packages/server/pyproject.toml packages/server/pyproject.toml
COPY packages/client/pyproject.toml packages/client/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package notehook-server --no-install-workspace

# Layer 2: install the server + protocol source themselves. --no-editable
# copies them into site-packages so the runtime image needs only the venv.
COPY packages/protocol/src packages/protocol/src
COPY packages/server/src packages/server/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package notehook-server

# ---- runtime: slim image carrying only the built venv ----
FROM python:3.12-slim-bookworm AS runtime

# Run unprivileged; the data volume is chowned to this uid below.
RUN useradd --create-home --uid 10001 notehook
WORKDIR /app

COPY --from=builder --chown=notehook:notehook /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOTEHOOK_DATA_DIR=/data

# Runtime state (sqlite db, blobs, secret_key, captures) lives here — mount it.
RUN install -d -o notehook -g notehook /data
VOLUME ["/data"]

USER notehook
EXPOSE 8080

# The server must run single-worker: auth nonce cache and rate limiter are
# in-process. The console script runs one uvicorn on 0.0.0.0:8080 — do not
# wrap it in a multi-worker gunicorn.
CMD ["notehook-server"]
