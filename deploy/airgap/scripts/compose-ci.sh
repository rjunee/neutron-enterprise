#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Single source of truth for driving the pilot compose tier from inside the
# NE-073 no-egress CI harness.
#
# deploy/compose/pilot/README.md's "never run compose directly" rule targets
# OPERATORS (who must go through `ne up`/`ne down` for preflights + secret
# injection). This wrapper is the documented CI-shaped exception for the
# no-egress harness: it drives ONLY the resource-shape overlay
# (deploy/airgap/compose.ci.yaml) for staging/pull/pre-warm steps that happen
# BEFORE and BETWEEN the real `ne up`/`ne down` install, which remains the
# real, unmocked installer for the actual air-gapped bring-up (see
# .github/workflows/no-egress.yml Phase A).
#
# The interpolation env vars below are SENTINELS, not secrets: this driver
# never brings up the `core` profile (where postgres/keycloak actually read
# them) on its own — that always goes through `ne up` with real SecretStore
# injection. They exist only so `docker compose config`/`pull`/`up` on other
# profiles (brain/cpu) don't fail on an unset required interpolation.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
AGENT_REGISTRY="$REPO_ROOT/deploy/agents/registry.toml"
SKILL_BUNDLE="$REPO_ROOT/deploy/skills/meridian-hardship-draft/SKILL.md"

export DOCKER_HOST="${NE_AIRGAP_DOCKER_HOST:-unix:///mnt/ne-airgap/run/docker.sock}"

export NE_PILOT_HOSTNAME="${NE_PILOT_HOSTNAME:-airgap.ci.local}"
export NE_PILOT_POSTGRES_PASSWORD="${NE_PILOT_POSTGRES_PASSWORD:-ci-interpolation-only-not-a-secret}"
export NE_PILOT_KEYCLOAK_ADMIN_PASSWORD="${NE_PILOT_KEYCLOAK_ADMIN_PASSWORD:-ci-interpolation-only-not-a-secret}"

[[ -f "$AGENT_REGISTRY" && ! -L "$AGENT_REGISTRY" ]] \
  || { printf 'ERROR: exact Agent registry is absent or symlinked\n' >&2; exit 1; }
NE_AGENT_REGISTRY_RAW_SHA256="$(python3 - "$AGENT_REGISTRY" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
[[ "$NE_AGENT_REGISTRY_RAW_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || { printf 'ERROR: exact Agent registry hash is malformed\n' >&2; exit 1; }
export NE_AGENT_REGISTRY_RAW_SHA256

[[ -f "$SKILL_BUNDLE" && ! -L "$SKILL_BUNDLE" ]] \
  || { printf 'ERROR: exact Skill bundle is absent or symlinked\n' >&2; exit 1; }
NE_SKILL_BUNDLE_RAW_SHA256="$(python3 - "$SKILL_BUNDLE" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
[[ "$NE_SKILL_BUNDLE_RAW_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || { printf 'ERROR: exact Skill bundle hash is malformed\n' >&2; exit 1; }
export NE_SKILL_BUNDLE_RAW_SHA256

exec docker compose \
  --project-name ne-pilot \
  -f "${REPO_ROOT}/deploy/compose/pilot/compose.yaml" \
  -f "${REPO_ROOT}/deploy/airgap/compose.ci.yaml" \
  "$@"
