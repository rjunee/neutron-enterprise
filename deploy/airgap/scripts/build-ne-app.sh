#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Build the first-party `ne-app` image (NE-103a, owner dec ii build-in-seal).
#
# A locally-built first-party image has NO published registry digest, so the
# C11 tag@sha256 rule cannot apply to it. Instead the image is built DURING the
# egress-permitted air-gap staging window (BEFORE netns-seal), and its LOCAL
# image id is captured via `--iidfile` and later asserted against the running
# container in air-gap Phase A (test_airgap_phase_a.py) — that image-id match IS
# the pin. compose `pull_policy: never` guarantees the sealed stack never tries
# to pull it.
#
# Honors DOCKER_HOST already in the environment (the no-egress job passes the
# inner-dockerd control socket; local runs hit the default daemon). Echoes only
# the built image id — never any secret.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

# Single source of truth for the tag; the pilot-stack guardian cross-checks this
# assignment against compose.yaml's ne-api image and up.py's NE_APP_IMAGE.
IMAGE_TAG="ne-app:0.1.0"

IIDFILE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --iidfile)
      if [ "$#" -lt 2 ]; then
        echo "build-ne-app.sh: --iidfile requires a PATH argument" >&2
        exit 2
      fi
      IIDFILE="$2"
      shift 2
      ;;
    -h|--help)
      echo "usage: build-ne-app.sh [--iidfile PATH]" >&2
      exit 0
      ;;
    *)
      echo "build-ne-app.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

echo "== building ${IMAGE_TAG} from ${REPO_ROOT} (DOCKER_HOST=${DOCKER_HOST:-default}) =="
# Build the optional --iidfile flag as distinct array elements so the PATH is
# always passed as its own argv item (never fused into the flag token, and safe
# for a path containing spaces) regardless of IFS/word-splitting behavior.
iidfile_args=()
if [ -n "${IIDFILE}" ]; then
  iidfile_args=(--iidfile "${IIDFILE}")
fi
# Stamp the build inputs' revision (git HEAD + dirty-hash, passed by
# `ne demo up`) onto the image so a later up can detect source drift and rebuild
# instead of silently running a stale image (NE-DEMO-STALE). Absent when the
# script is invoked directly — the label is simply omitted then.
label_args=()
if [ -n "${NE_APP_SOURCE_REV:-}" ]; then
  label_args=(--label "org.neutron.ne-app.source-rev=${NE_APP_SOURCE_REV}")
fi
DOCKER_BUILDKIT=1 docker build \
  -f "${REPO_ROOT}/deploy/docker/ne-app/Dockerfile" \
  -t "${IMAGE_TAG}" \
  ${iidfile_args[@]+"${iidfile_args[@]}"} \
  ${label_args[@]+"${label_args[@]}"} \
  "${REPO_ROOT}"

image_id="$(docker image inspect "${IMAGE_TAG}" --format '{{.Id}}')"
echo "== built ${IMAGE_TAG} (${image_id}) =="
if [ -n "${IIDFILE}" ]; then
  echo "== wrote image id to ${IIDFILE} =="
fi
