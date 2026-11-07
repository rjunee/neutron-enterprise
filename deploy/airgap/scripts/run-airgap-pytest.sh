#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# run-airgap-pytest.sh FILE...
#
# The single executing-gate implementation for every AIR_GAP phase test
# module — mirrors ci.yml's Checkpoint-C/Rego/tier-parity gate pattern
# (NE-CKC6 fail-closed): a phase module that is skipped or collects zero
# tests inside the sealed namespace is a HARNESS FAILURE, never silence.
#
# Runs pytest INSIDE the sealed `ne-airgap` namespace (so the phase modules'
# socket/httpx/gateway probes genuinely execute with no egress path), as the
# invoking user (not root) with DOCKER_HOST pointed at the inner dockerd and
# NE_AIRGAP=1 so the module-level gate in deploy/airgap/tests/conftest.py
# lets the phase tests run instead of skipping.
#
# `.venv/bin/python` is invoked directly (not `uv run`) deliberately: `uv run`
# re-resolves the project inside `ip netns exec`, which the harness has not
# validated is safe/available under the sealed namespace's constrained
# environment — using the venv interpreter directly is simpler and exactly
# reproduces what a real deployed installation would do. cwd stays the repo
# root so the root pyproject.toml pytest config (testpaths, addopts) applies.

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: run-airgap-pytest.sh FILE..." >&2
  exit 2
fi

NS=ne-airgap
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
INNER_DOCKER_HOST="${NE_AIRGAP_DOCKER_HOST:-unix:///mnt/ne-airgap/run/docker.sock}"
GATE_LOG="$(mktemp -t airgap-pytest-XXXXXX.txt)"

cd "${REPO_ROOT}"

sudo ip netns exec "${NS}" sudo -u "$(id -un)" env \
  HOME="${HOME}" \
  PATH="${PATH}" \
  DOCKER_HOST="${INNER_DOCKER_HOST}" \
  NE_AIRGAP=1 \
  HF_HUB_OFFLINE=1 \
  "${REPO_ROOT}/.venv/bin/python" -m pytest "$@" -q -rs | tee "${GATE_LOG}"

if ! grep -Eq '[0-9]+ passed' "${GATE_LOG}"; then
  echo "::error::AIR_GAP executing gate did not report any passed tests: $*" >&2
  exit 1
fi

if grep -Eiq 'skipped|no tests ran' "${GATE_LOG}"; then
  echo "::error::AIR_GAP executing gate reported a skip inside the sealed namespace — a skip here is a FAILURE (NE-CKC6 fail-closed pattern): $*" >&2
  exit 1
fi

echo "== run-airgap-pytest: executing gate passed for $* =="
