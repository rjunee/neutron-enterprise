#!/bin/sh
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
set -eu

receipt_dir="/var/lib/neutron-age-admission"
receipt_path="${receipt_dir}/admission.json"
expected_digest="${NE_POSTGRES_AGE_IMAGE_DIGEST:?missing admitted image digest}"
expected_revision="${NE_POSTGRES_AGE_SOURCE_REVISION:?missing admitted source revision}"

case "${expected_digest}" in
  sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "invalid admitted AGE image digest" >&2; exit 2 ;;
esac
case "${expected_digest#sha256:}" in
  *[!0-9a-f]*|'') echo "invalid admitted AGE image digest" >&2; exit 2 ;;
esac
case "${expected_revision}" in
  ????????????????????????????????????????) ;;
  *) echo "invalid admitted AGE source revision" >&2; exit 2 ;;
esac
case "${expected_revision}" in
  *[!0-9a-f]*|'') echo "invalid admitted AGE source revision" >&2; exit 2 ;;
esac

export PGPASSWORD="${NE_PILOT_POSTGRES_PASSWORD:?missing Pilot Postgres password}"
psql_base="postgresql://ne@postgres:5432/neutron"

# This is substrate construction, not corpus population: install AGE and create
# the one fixed graph namespace before any API/worker consumer is admitted.
psql "${psql_base}" --set=ON_ERROR_STOP=1 --quiet <<'SQL'
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('ne_graph')
WHERE NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'ne_graph');
SQL

extension_version="$(
  psql "${psql_base}" --set=ON_ERROR_STOP=1 --tuples-only --no-align --quiet \
    --command="SELECT extversion FROM pg_extension WHERE extname = 'age'"
)"
if [ "${extension_version}" != "1.7.0" ]; then
  echo "unexpected Apache AGE extension version" >&2
  exit 3
fi

system_identifier="$(
  psql "${psql_base}" --set=ON_ERROR_STOP=1 --tuples-only --no-align --quiet \
    --command="SELECT system_identifier FROM pg_control_system()"
)"
case "${system_identifier}" in
  *[!0-9]*|'') echo "invalid PostgreSQL system identifier" >&2; exit 3 ;;
esac

mkdir -p "${receipt_dir}"
chmod 0700 "${receipt_dir}"
expected_receipt="$(printf \
  '{"image_digest":"%s","source_revision":"%s","system_identifier":"%s"}' \
  "${expected_digest}" "${expected_revision}" "${system_identifier}")"

if [ -e "${receipt_path}" ]; then
  actual_receipt="$(cat "${receipt_path}")"
  if [ "${actual_receipt}" != "${expected_receipt}" ]; then
    echo "PostgreSQL AGE admission receipt mismatch" >&2
    exit 4
  fi
else
  temp_path="${receipt_path}.tmp.$$"
  trap 'rm -f "${temp_path}"' EXIT HUP INT TERM
  printf '%s\n' "${expected_receipt}" >"${temp_path}"
  chmod 0600 "${temp_path}"
  mv "${temp_path}" "${receipt_path}"
  trap - EXIT HUP INT TERM
fi

printf '%s\n' "PostgreSQL AGE admission verified"
