#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Customer-side OFFLINE verification of a Neutron Enterprise release artifact.
#
# Key-based (NOT keyless): verification uses the committed public key and the
# per-artifact bundle only — no network, no Rekor/Fulcio. This is a hard
# requirement for air-gapped / offline customers (SPEC C1/C2).
#
# Usage:
#   verify-release.sh <artifact> <artifact.bundle> <provenance.att.bundle> [cosign.pub]
set -euo pipefail

ARTIFACT="${1:?usage: verify-release.sh <artifact> <artifact.bundle> <provenance.att.bundle> [cosign.pub]}"
SIG_BUNDLE="${2:?missing signature bundle}"
ATT_BUNDLE="${3:?missing provenance attestation bundle}"
PUBKEY="${4:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cosign.pub}"

if ! command -v cosign >/dev/null 2>&1; then
  echo "ERROR: cosign not found on PATH. Install it (see docs/security/signing.md)." >&2
  exit 1
fi
if [[ ! -f "$PUBKEY" ]]; then
  echo "ERROR: public key not found: ${PUBKEY}" >&2
  echo "       The signing key is owner-provisioned; commit cosign.pub to" >&2
  echo "       tools/supply-chain/cosign.pub (see docs/security/signing.md)." >&2
  exit 1
fi

echo "Verifying signature (offline, key-based)…"
cosign verify-blob \
  --key "$PUBKEY" \
  --bundle "$SIG_BUNDLE" \
  --insecure-ignore-tlog=true \
  "$ARTIFACT"

echo "Verifying SLSA provenance attestation (offline, key-based)…"
cosign verify-blob-attestation \
  --key "$PUBKEY" \
  --bundle "$ATT_BUNDLE" \
  --type slsaprovenance1 \
  --insecure-ignore-tlog=true \
  "$ARTIFACT"

echo "OK: ${ARTIFACT} signature and provenance verified offline."
