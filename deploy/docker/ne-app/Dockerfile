# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# The first-party `ne-app` image (NE-103a). ONE image, multi-role by command:
#   * ne-api  (NOW)   — `python -m ne.api`  → uvicorn ne.api.app:app_factory
#   * ne-worker (103c) — a compose `command:` override; the CMD/no-ENTRYPOINT
#                        design below makes that a one-line switch.
#
# Three stages, EVERY base pinned tag@sha256 (C11 — see deploy/docker/ne-app/
# README.md provenance table, cross-checked by tools/ne-cli/tests/
# test_ne_app_image.py):
#   A console-builder — Node LTS builds the ne-console SPA `dist/`.
#   B py-builder      — uv syncs the ne-api closure (incl. uvicorn) into a venv.
#   C runtime         — slim NON-ROOT, carries the venv + console dist/ (served
#                       in 103b) and runs uvicorn on the loopback-free high port.
#
# Build context is the REPO ROOT (`docker build -f deploy/docker/ne-app/Dockerfile
# .`); Dockerfile.dockerignore trims it. Build ONLY during the egress-permitted
# air-gap staging window (build-in-seal, owner dec ii) — the local image is then
# digest-pinned by --iidfile, never pulled from a registry (compose pull_policy:
# never). NO ADD, NO secrets, NO :latest anywhere.

# --- stage A: console-builder (build-only; NOT shipped in the runtime image) --
FROM node:24.15.0-bookworm-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d AS console-builder
WORKDIR /build
# Copy the lockfile+manifest first so `npm ci` layer-caches on dependency change.
COPY packages/ne-console/package.json packages/ne-console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY packages/ne-console/ ./
# `tsc -b && vite build` → /build/dist (the SPA the runtime carries; served 103b).
RUN npm run build

# --- stage B: py-builder — uv sync the ne-api closure into /app/.venv ----------
FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6 AS py-builder
# The pinned uv binary (matches the uv.lock toolchain, 0.8.0 — see README).
COPY --from=ghcr.io/astral-sh/uv:0.8.0@sha256:5778d479c0fd7995fedd44614570f38a9d849256851f2786c451c220d7bd8ccd /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Workspace root pins + the full member set (packages/* incl. ne-console's
# pyproject, tools/*, apps/* if present); uv resolves the frozen graph then
# installs the ne-api + ne-worker closures (one image, two roles — NE-103c):
# `python -m ne.api` (default CMD) and `python -m ne.worker` (compose command
# override). Both closures share the ne-runtime dependency set (dbos, psycopg,
# etc.) so the ne-worker delta over ne-api alone is small.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY tools/ tools/
RUN uv sync --frozen --no-dev --no-editable --package ne-worker

# --- stage C: slim NON-ROOT runtime -------------------------------------------
FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6 AS runtime
# libpq5: ne-api's psycopg arrives via dbos WITHOUT the [binary] extra, so the
# runtime needs the system PostgreSQL client library. Slim + no apt lists left.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*
# Dedicated non-root system account (numeric uid asserted by the guardian).
RUN useradd --system --uid 10001 ne
# The interpreter path is identical to the py-builder stage (same base digest),
# so the copied venv's shebangs/edges resolve unchanged.
COPY --from=py-builder /app/.venv /app/.venv
# The isolated Docling closure stays OUT of the application venv/image (C4), but
# an installed runtime must still carry its hash-pinned payload and the pinned uv
# builder so a deployment-provided writable sidecar path can be pre-staged.  The
# demo binds that path at /connect/.parser-venv; sealed deployments pre-build the
# same payload into their own sidecar volume before engaging AIR_GAP.
COPY --from=py-builder /bin/uv /bin/uv
COPY --from=py-builder /src/packages/ne-brain/parser_env /opt/ne/parser_env
# The console SPA — carried NOW, served in 103b via NE_API_SERVE_CONSOLE_DIST.
COPY --from=console-builder /build/dist /app/console-dist
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/tmp \
    NE_PARSER_ENV_DIR=/opt/ne/parser_env \
    NE_API_BIND_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001
EXPOSE 8000
# Exec-form CMD, NO ENTRYPOINT: the 103c ne-worker role is a compose `command:`
# override — the multi-role seam. `python -m ne.api` runs uvicorn (factory
# ne.api.app:app_factory) on NE_API_BIND_HOST:8000 with /healthz.
CMD ["python", "-m", "ne.api"]
