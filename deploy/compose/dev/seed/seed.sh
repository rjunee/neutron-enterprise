#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Idempotent dev-stack seed script. Waits for the base services to answer,
# then seeds a smoke-test Qdrant collection and an OpenBao KV2 secret, and
# prints a summary of every service endpoint. Safe to re-run; exits non-zero
# on any failure so `ne dev seed` reports errors clearly.

set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_DIR="$(cd "${SCRIPT_PATH}/.." && pwd)"

QDRANT_URL="http://localhost:6333"
OPENBAO_URL="http://localhost:8200"
OPENBAO_TOKEN="ne-dev-root"
KEYCLOAK_URL="http://localhost:8081"

wait_http() {
  # wait_http <name> <url> [timeout_seconds]
  local name="$1"
  local url="$2"
  local timeout="${3:-60}"
  local waited=0
  echo "waiting for ${name} at ${url} ..."
  until curl -sf -o /dev/null "${url}"; do
    if [ "${waited}" -ge "${timeout}" ]; then
      echo "ERROR: ${name} did not become ready at ${url} within ${timeout}s" >&2
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "  -> ${name} is ready."
}

wait_http "qdrant" "${QDRANT_URL}/readyz"
wait_http "openbao" "${OPENBAO_URL}/v1/sys/health"
wait_http "keycloak" "${KEYCLOAK_URL}/realms/ne-dev"

echo "seeding Qdrant collection 'ne_dev_smoke' ..."
if curl -sf -o /dev/null "${QDRANT_URL}/collections/ne_dev_smoke"; then
  echo "  -> ne_dev_smoke already exists, skipping create."
else
  curl -sf -X PUT "${QDRANT_URL}/collections/ne_dev_smoke" \
    -H "Content-Type: application/json" \
    -d '{"vectors":{"size":4,"distance":"Cosine"}}' >/dev/null
  echo "  -> created ne_dev_smoke."
fi

echo "seeding OpenBao KV2 secret 'ne-dev/example' ..."
curl -sf -X POST "${OPENBAO_URL}/v1/secret/data/ne-dev/example" \
  -H "X-Vault-Token: ${OPENBAO_TOKEN}" \
  -d '{"data":{"hello":"world"}}' >/dev/null
echo "  -> wrote ne-dev/example."

cat <<SUMMARY

Dev stack seeded successfully. Endpoints:
  postgres     localhost:5432   (nedev/nedev, db=neutron)
  qdrant       ${QDRANT_URL}    (collection ne_dev_smoke seeded)
  keycloak     ${KEYCLOAK_URL}  (realm ne-dev, user dev/dev)
  opa          http://localhost:8181
  tei-embed    http://localhost:8085
  tei-rerank   http://localhost:8086
  openbao      ${OPENBAO_URL}   (token ${OPENBAO_TOKEN}, secret ne-dev/example seeded)
  otel-collector grpc:4317 http:4318
  langfuse-web http://localhost:3000  (only when started with --langfuse)

Dev dir: ${DEV_DIR}
SUMMARY
