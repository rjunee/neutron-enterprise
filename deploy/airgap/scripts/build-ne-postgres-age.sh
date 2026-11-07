#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
image_tag="ne-postgres-age:18.6-1.7.0"
source_revision="${NE_POSTGRES_AGE_SOURCE_REV:-}"

if [[ ! "${source_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "NE_POSTGRES_AGE_SOURCE_REV must be the exact lowercase 40-hex source commit" >&2
  exit 2
fi

if [[ $# -gt 0 ]]; then
  if [[ $# -eq 2 && "$1" == "--iidfile" && -n "$2" ]]; then
    iidfile_path="$2"
  else
    echo "usage: $0 [--iidfile PATH]" >&2
    exit 2
  fi
fi

build_args=(
  --platform linux/amd64
  --file "${repo_root}/deploy/docker/ne-postgres-age/Dockerfile"
  --tag "${image_tag}"
  --label "org.neutron.ne-postgres-age.source-rev=${source_revision}"
)
if [[ -n "${iidfile_path:-}" ]]; then
  build_args+=(--iidfile "${iidfile_path}")
fi
# Optional: a directory holding the Apache AGE source pre-fetched by
# deploy/docker/ne-postgres-age/fetch-age-src.sh. The Dockerfile still
# verifies every file's SHA-512 and the release signature.
age_src_dir="${NE_POSTGRES_AGE_SRC_DIR:-}"
if [[ -n "${age_src_dir}" ]]; then
  if [[ ! -d "${age_src_dir}" ]]; then
    echo "NE_POSTGRES_AGE_SRC_DIR is not a directory: ${age_src_dir}" >&2
    exit 2
  fi
  build_args+=(--build-context "age-src=${age_src_dir}")
fi

DOCKER_BUILDKIT=1 docker build "${build_args[@]}" \
  "${repo_root}"

docker image inspect "${image_tag}" >/dev/null
echo "built ${image_tag}"
