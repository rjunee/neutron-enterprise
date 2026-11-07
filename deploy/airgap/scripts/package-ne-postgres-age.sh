#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

image_tag="ne-postgres-age:18.6-1.7.0"
expected_digest="sha256:6ba66e01b43cbfde0f0a03699f609fed1e218612aab7b2119052e0fd5aeb4f26"
expected_revision="7b7cede30d7af58ac3259b51cf59c76f1e95754c"

if [[ $# -ne 2 || "$1" != "--out" || -z "$2" ]]; then
  echo "usage: $0 --out DIRECTORY" >&2
  exit 2
fi
out_dir="$2"
mkdir -p "${out_dir}"

actual="$(docker image inspect "${image_tag}" --format '{{.Id}} {{index .Config.Labels "org.neutron.ne-postgres-age.source-rev"}}')"
if [[ "${actual}" != "${expected_digest} ${expected_revision}" ]]; then
  echo "local PostgreSQL AGE image is not the admitted artifact" >&2
  exit 3
fi

archive="${out_dir}/ne-postgres-age-18.6-1.7.0-amd64.tar"
manifest="${out_dir}/ne-postgres-age-18.6-1.7.0-amd64.json"
docker image save --output "${archive}.tmp" "${image_tag}"
archive_sha256="$(sha256sum "${archive}.tmp" | awk '{print $1}')"
printf \
  '{"archive_sha256":"%s","image_digest":"%s","source_revision":"%s"}\n' \
  "${archive_sha256}" "${expected_digest}" "${expected_revision}" \
  >"${manifest}.tmp"
mv "${archive}.tmp" "${archive}"
mv "${manifest}.tmp" "${manifest}"
printf '%s\n' "packaged ${archive}"
