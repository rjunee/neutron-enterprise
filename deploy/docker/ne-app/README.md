# deploy/docker/ne-app — the first-party application image (NE-103a)

`ne-app` is the ONE first-party container image for Neutron Enterprise. It is
**multi-role by command**: the same image runs the `ne-api` HTTP backend
(default exec-form `CMD ["python", "-m", "ne.api"]`) and the durable `ne-worker`
service (compose `command: ["python", "-m", "ne.worker"]`) — the only difference
is the container `command:`. The image carries no `ENTRYPOINT`, so the worker
role is a one-line compose override. To make BOTH roles importable, stage B
installs the ne-worker closure (which transitively includes ne-api, since
ne-worker depends on ne-api) via `uv sync ... --package ne-worker` (NE-103c
FIX-1): without it, `python -m ne.worker` fails `No module named ne.worker`. The two closures share the ne-runtime
dependency set (dbos, psycopg, …), so the ne-worker delta is small. This is the
same "one image, different command" pattern the pilot uses for Keycloak.

## Architecture (three stages, every base `tag@sha256`)

The build context is the **repo root** (`docker build -f
deploy/docker/ne-app/Dockerfile .`); `Dockerfile.dockerignore` trims it.

| Stage | Base | Role |
|-------|------|------|
| A `console-builder` | `node:24.15.0-bookworm-slim` | `npm ci` + `npm run build` (`tsc -b && vite build`) → the ne-console SPA `dist/`. Build-only — **not** shipped in the runtime image. |
| B `py-builder` | `python:3.13.12-slim-bookworm` + `uv` | `uv sync --frozen --no-dev --no-editable --package ne-worker` into `/app/.venv` (the ne-api + ne-worker closures — uvicorn for the API role, the durable-worker composition root for the worker role). |
| C `runtime` | `python:3.13.12-slim-bookworm` (**same digest as B**) | slim, **non-root** (`uid 10001`), `libpq5` for psycopg, carries the venv + the console `dist/` (served in 103b via `NE_API_SERVE_CONSOLE_DIST`). `CMD ["python","-m","ne.api"]` → uvicorn `ne.api.app:app_factory` on `:8000`, `/healthz`. |

Stage B and C share the **same** `python` digest so the copied venv's interpreter
path is byte-identical (no interpreter mismatch when the venv moves between
stages).

## Provenance (C11 — verified 2026-07-09)

Every base image is `tag@sha256` digest-pinned. `tools/ne-cli/tests/
test_ne_app_image.py` cross-checks each digest embedded in the `Dockerfile`
against this table on every run, so the two can never silently drift.

| Image | Digest | License | Stage | Source registry |
|-------|--------|---------|-------|-----------------|
| `node:24.15.0-bookworm-slim` | `sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d` | MIT-style (Node.js) | A (build-only) | hub.docker.com/_/node |
| `python:3.13.12-slim-bookworm` | `sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6` | PSF-2.0 | B + C | hub.docker.com/_/python |
| `ghcr.io/astral-sh/uv:0.8.0` | `sha256:5778d479c0fd7995fedd44614570f38a9d849256851f2786c451c220d7bd8ccd` | Apache-2.0 OR MIT | B (binary COPY) | ghcr.io/astral-sh/uv |

Notes:

- **Node 24** is the active LTS; matches `ci.yml` `node-version: 24` and
  ne-console's `engines: {node: ">=24"}`.
- **Python 3.13** matches the repo `.python-version`.
- **uv 0.8.0 is pinned deliberately** — it is the exact toolchain that generated
  `uv.lock` (local `uv --version` = 0.8.0; `uv_build` is bound `<0.9` in every
  package). Upstream uv is well past 0.8.0; bumping it is the job of the
  dependency-cadence machinery, **not** this packaging unit. A silent modern
  bump here would risk a lock/resolver mismatch.
- **`libpq5`** (Debian bookworm apt, PostgreSQL License) is installed at runtime
  because ne-api's psycopg arrives via `dbos` **without** the `[binary]` extra
  (verified in `uv.lock`), so it links the system PostgreSQL client library.
- No new Python or npm dependency is introduced by this image, so there is no
  `uv.lock` / `NOTICE` / license-gate change.

## Build-in-seal + local-digest pin (owner decision ii)

A locally-built first-party image has **no published registry digest**, so the
C11 `tag@sha256` rule cannot apply to it. Instead:

1. The image is built during the air-gap **staging window** (egress permitted,
   before the netns seal) by `deploy/airgap/scripts/build-ne-app.sh`, which
   captures the local image id via `--iidfile`.
2. Air-gap **Phase A** (`deploy/airgap/tests/test_airgap_phase_a.py`
   `test_ne_api_runs_the_staged_local_image`) asserts the running container's
   image id equals that staged id. **That match is the pin.**
3. Compose sets `pull_policy: never` on `ne-api` — the sealed stack never tries
   to pull it, and `docker.io/library/ne-app` squatting is blocked.

The `test_pilot_stack.py` guardian encodes this as a **narrow, negatively-tested
exception**: exactly `{ne-api}` may carry a `ne-app:X.Y.Z` local tag +
`pull_policy: never` + no registry prefix + no `@digest`; **every** third-party
image keeps the strict `tag@sha256` rule.

## Local build + boot smoke

```sh
# Build (needs a docker daemon + egress for npm ci / uv sync):
DOCKER_BUILDKIT=1 deploy/airgap/scripts/build-ne-app.sh

# Standalone non-root, read-only boot smoke on throwaway sqlite URLs:
docker run --rm -d --name ne-app-smoke --read-only --tmpfs /tmp \
  -e NE_API_QDRANT_URL=http://localhost:6333 \
  -e NE_API_QDRANT_COLLECTION=smoke \
  -e NE_API_TEI_URL=http://localhost:8085 \
  -e NE_IDENTITY_OIDC__ISSUER=https://smoke.invalid/realms/x \
  -e NE_IDENTITY_OIDC__AUDIENCE=ne-console \
  -e NE_RUNTIME_SESSION_DATABASE_URL=sqlite:////tmp/rt.db \
  -e NE_AUDIT_DATABASE_URL=sqlite:////tmp/audit.db \
  -e NE_PERMISSIONS_DATABASE_URL=sqlite:////tmp/perm.db \
  -e NE_FLYWHEEL_SIGNAL_DATABASE_URL=sqlite:////tmp/fly.db \
  -e NE_API_EVAL_DATABASE_URL=sqlite:////tmp/eval.db \
  ne-app:0.1.0
sleep 25
docker exec ne-app-smoke /app/.venv/bin/python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read())"
# → b'{"status":"ok"}'
docker exec ne-app-smoke id -u          # → 10001
docker rm -f ne-app-smoke
```

sqlite works for the smoke because `create_app` calls
`ensure_transactional_sqlite` on every engine (ne-api's own suite boots on
sqlite); the pilot uses Postgres via `${NE_PILOT_*}` interpolation.

## What parks

- **NE-103b:** console static serving (`NE_API_SERVE_CONSOLE_DIST` over the
  carried `/app/console-dist`), the Caddy 443 → `ne-api:8000` vhost, the pilot
  Keycloak realm/client/approver, and therefore OIDC-live authenticated
  requests + the brain-profile retrieval e2e.
- **NE-103c (now DELIVERED, not parked):** the durable `ne-worker` service runs
  on this same image via a compose `command: ["python", "-m", "ne.worker"]`
  override (the no-ENTRYPOINT / exec-form-CMD seam) plus the dual-closure install
  above (`--package ne-worker`).
