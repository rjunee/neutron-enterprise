#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Box-side runner for the demo-scenario E2E suite. This is the PRIMARY
# invocation (the GitHub lane exists for scheduling/dispatch, but the suite is
# most useful run from the deployment host right before a demo or a release).
#
#   e2e/run.sh --env /root/ne-e2e.env                 # read-only lane
#   e2e/run.sh --env /root/ne-e2e.env --mutating      # + S5 send, S8 edit
#   e2e/run.sh --selftest                             # offline helper check only
#
# The env file is `KEY=value` lines for the NE_E2E_* contract documented in
# e2e/scenarios/conftest.py. Keep it root-readable on the box; NEVER commit it.
#
# ALWAYS SERIAL. Never add -n/xdist here: the suite is load-sensitive and the
# conftest refuses parallel runs outright.

set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=""
MUTATING=0
SELFTEST=0
SKIP_PREFLIGHT=0
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --mutating) MUTATING=1; shift ;;
    --selftest) SELFTEST=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

if [[ "$SELFTEST" == 1 ]]; then
  exec uv run pytest e2e/scenarios/test_00_selftest.py -q "${EXTRA_ARGS[@]}"
fi

if [[ -n "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${NE_E2E_API_BASE:-}" ]]; then
  echo "ERROR: NE_E2E_API_BASE is not set (pass --env <file> or export the NE_E2E_*" >&2
  echo "contract; see e2e/scenarios/conftest.py)." >&2
  exit 2
fi

if [[ "$MUTATING" == 1 ]]; then
  export NE_E2E_MUTATING=1
fi

# Demo-up + external-GPU gate (e2e/preflight.py): the owner does not keep the
# GPU rig up, and cpu-vllm is far too slow — run against it and every timeout
# looks like a regression. Exit 3 = skip with the reason (not a failure);
# --skip-preflight exists for debugging the suite itself.
if [[ "$SKIP_PREFLIGHT" != 1 ]]; then
  set +e
  python3 e2e/preflight.py
  PREFLIGHT_CODE=$?
  set -e
  if [[ "$PREFLIGHT_CODE" -eq 3 ]]; then
    echo "== SKIPPED: preflight says a run now would only burn money/produce noise" >&2
    echo "== (reason above; --skip-preflight overrides for suite debugging only) ==" >&2
    exit 3
  elif [[ "$PREFLIGHT_CODE" -ne 0 ]]; then
    echo "== preflight itself is broken (exit $PREFLIGHT_CODE) — fix it before running ==" >&2
    exit "$PREFLIGHT_CODE"
  fi
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"
export NE_E2E_RESULTS_DIR="${NE_E2E_RESULTS_DIR:-e2e/results/$STAMP}"
mkdir -p "$NE_E2E_RESULTS_DIR"

echo "== e2e scenario suite -> ${NE_E2E_API_BASE} (results: ${NE_E2E_RESULTS_DIR}) =="
echo "== mutating lane: ${NE_E2E_MUTATING:-0} (S5 sends real email; S8 edits the live SOP) =="

# -p no:cacheprovider: keep the repo clean of .pytest_cache churn from live runs.
# Never pipe this through tail: a piped exit code is tail's, not pytest's
# (POST_DEMO "two testing traps").
uv run pytest e2e/scenarios -p no:cacheprovider -ra "${EXTRA_ARGS[@]}" \
  --junit-xml "$NE_E2E_RESULTS_DIR/junit.xml"
