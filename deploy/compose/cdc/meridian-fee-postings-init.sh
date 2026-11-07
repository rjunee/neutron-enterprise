#!/bin/sh
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

# Runs only on first initialization of the synthetic demo source volume. The
# owner password and the distinct replication password arrive through container
# environment variables; neither is embedded in SQL, argv, or committed files.
set -eu

: "${NE_CDC_SOURCE_DB_PASSWORD:?missing generated CDC source password}"
if [ "${#NE_CDC_SOURCE_DB_PASSWORD}" -ne 48 ]; then
  printf 'invalid generated CDC source password shape\n' >&2
  exit 1
fi
case "$NE_CDC_SOURCE_DB_PASSWORD" in
  *[!0-9a-f]*)
    printf 'invalid generated CDC source password shape\n' >&2
    exit 1
    ;;
esac

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<SQL
SELECT format(
  'CREATE ROLE meridian_cdc WITH LOGIN REPLICATION PASSWORD %L',
  '$NE_CDC_SOURCE_DB_PASSWORD'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meridian_cdc')
\gexec

-- Re-running the initializer against an explicitly reset volume converges the
-- credential rather than creating a sibling role.
SELECT format(
  'ALTER ROLE meridian_cdc WITH LOGIN REPLICATION PASSWORD %L',
  '$NE_CDC_SOURCE_DB_PASSWORD'
)
\gexec
SQL

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --file=/opt/neutron/meridian-fee-postings.sql
