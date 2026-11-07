#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# canary.sh MODE TARGET
#
#   MODE   = expect-reachable | expect-dead
#   TARGET = a full URL, e.g. https://api.anthropic.com
#
# Runs BOTH a host-level probe (curl executed inside the `ne-airgap` netns
# directly) and a container-level probe (curl run inside a throwaway
# container on the inner dockerd, i.e. the same network path a compromised
# service container would have) against TARGET, and enforces MODE on both:
#
#   expect-reachable  — the ANTI-VACUOUS stage check. curl must complete
#                        (rc==0); ANY HTTP status code (even 4xx/5xx) counts
#                        as "reachable" — we only care that the network path
#                        exists, not that the server likes us. A curl failure
#                        here means the whole no-egress proof would be
#                        vacuous (the "prove-dead" step could pass merely
#                        because the network was ALREADY broken), so it is
#                        itself a hard failure.
#
#   expect-dead        — the C2 proof. curl rc==0 (the request reached
#                        *something* and got a response, any status code) is
#                        an EGRESS VIOLATION and fails the job. Any nonzero
#                        curl exit code (DNS resolution failure, connection
#                        refused/unreachable, timeout) means no byte of the
#                        request ever reached TARGET — that is the proof,
#                        regardless of the EXACT curl error code, so every
#                        nonzero rc passes this check.
#                        CAVEAT: a TLS cert-validation failure (rc 60) happens
#                        AFTER the TCP connection is established, so it does NOT
#                        prove egress was blocked. Raw-IP expect-dead targets
#                        MUST therefore be plain http:// (a bare IP has no valid
#                        cert) — never https://<ip>, which would false-pass when
#                        egress is actually open (Codex NE-073 P2). Hostname
#                        targets are safe: in the sealed netns they fail at
#                        DNS/connect, long before any TLS handshake.
#
# The container-level probe uses the digest-pinned curlimages/curl image
# (CI-canary only, never shipped) — see deploy/airgap/README.md provenance.
# The caller is responsible for ensuring that image is already present in the
# inner dockerd (pulled during staging) before invoking this script in
# expect-dead mode, so an image-pull failure is never confused with an
# egress-blocked result.

set -uo pipefail

CANARY_IMAGE="curlimages/curl:8.21.0@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13"
NS=ne-airgap
INNER_DOCKER_HOST="${NE_AIRGAP_DOCKER_HOST:-unix:///mnt/ne-airgap/run/docker.sock}"

if [ "$#" -ne 2 ]; then
  echo "usage: canary.sh <expect-reachable|expect-dead> <target-url>" >&2
  exit 2
fi

MODE="$1"
TARGET="$2"

case "${MODE}" in
  expect-reachable|expect-dead) ;;
  *)
    echo "usage: canary.sh <expect-reachable|expect-dead> <target-url>" >&2
    exit 2
    ;;
esac

_check() {
  local label="$1"
  local rc="$2"
  if [ "${MODE}" = "expect-reachable" ]; then
    if [ "${rc}" -ne 0 ]; then
      echo "ANTI-VACUOUS CANARY FAILED (${label}): curl rc=${rc} against ${TARGET} — staging network is broken, the later prove-dead step would be meaningless" >&2
      exit 1
    fi
    echo "canary OK (${label}, expect-reachable): curl completed (rc=0) against ${TARGET}"
  else
    if [ "${rc}" -eq 0 ]; then
      echo "EGRESS POSSIBLE — C2 VIOLATION (${label}): curl rc=0 against ${TARGET} — a request reached an external host from inside the sealed namespace" >&2
      exit 1
    fi
    echo "canary OK (${label}, expect-dead): curl failed closed (rc=${rc}) against ${TARGET}"
  fi
}

echo "== canary: ${MODE} host-level probe against ${TARGET} =="
sudo ip netns exec "${NS}" curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "${TARGET}"
host_rc=$?
_check "host" "${host_rc}"

echo "== canary: ${MODE} container-level probe against ${TARGET} (image ${CANARY_IMAGE}) =="
# Anti-vacuous pre-check: post-seal, a missing image would make `docker run`
# fail on the PULL (which looks identical to an egress-dead result), letting
# the expect-dead check pass for the wrong reason. Caller is responsible for
# pulling CANARY_IMAGE during staging; a missing image here is a harness
# error, never a pass — canary.sh runs with `set -uo pipefail` (no -e), so
# this must be an explicit if/exit, not relying on errexit.
if ! docker -H "${INNER_DOCKER_HOST}" image inspect "${CANARY_IMAGE}" >/dev/null 2>&1; then
  echo "HARNESS ERROR: canary image ${CANARY_IMAGE} is not present in the inner dockerd — a missing image must never masquerade as an egress-dead result" >&2
  exit 1
fi
docker -H "${INNER_DOCKER_HOST}" run --rm "${CANARY_IMAGE}" \
  -sS --max-time 15 -o /dev/null -w '%{http_code}' "${TARGET}"
container_rc=$?
_check "container" "${container_rc}"

echo "== canary: ${MODE} against ${TARGET} — all probes consistent =="
