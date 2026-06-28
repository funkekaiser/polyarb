# syntax=docker/dockerfile:1
# polyarb — read-only Polymarket structural-arbitrage scanner
#
# Build:  docker build -f docker/Dockerfile -t polyarb .
# Run:    docker run --env-file .env -v polyarb-data:/data polyarb

# ---------------------------------------------------------------------------
# Stage 1: dependency install
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Bring in uv from the official image (no pip, no pipx, just uv).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first so Docker can cache this layer.
# The src tree is also needed here because pyproject.toml uses uv_build
# with a src layout — uv sync --frozen resolves the editable install.
COPY pyproject.toml uv.lock uv.toml README.md ./
COPY src/ src/

# Install production dependencies into /app/.venv.
# --frozen    → honour the lock file exactly, no version resolution.
# --no-dev    → skip pytest/ruff/mypy; not needed at runtime.
# --no-cache  → keep the layer lean (no uv HTTP cache on disk).
RUN uv sync --frozen --no-dev --no-cache

# ---------------------------------------------------------------------------
# Stage 2: minimal runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Copy the pre-built venv and source tree from the builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src   /app/src

# Create a non-root user and writable data directory for SQLite.
RUN useradd --system --no-create-home --uid 1001 polyarb \
 && mkdir -p /data \
 && chown polyarb:polyarb /data

USER polyarb

# Runtime environment.
# SQLITE_PATH is also the canonical location for the named volume mount.
ENV SQLITE_PATH=/data/polyarb.db \
    PYTHONUNBUFFERED=1

# Run the pre-built binary directly — no uv wrapper needed at runtime.
ENTRYPOINT ["/app/.venv/bin/polyarb"]
CMD ["scan", "--dry-run"]
