#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Install pinned, checksum-verified supply-chain scanner binaries:
#   trivy v0.72.0, grype v0.115.0, cosign v3.0.2 (linux-amd64).
#
# WHY WE INSTALL FROM PINNED TARBALLS INSTEAD OF GitHub Actions:
#   On 2026-03-19 the `aquasecurity/trivy-action` (tags 0.0.1–0.34.2) and ALL
#   `aquasecurity/setup-trivy` releases were compromised: imposter commits
#   shipped a credential stealer, and a malicious trivy binary (v0.69.4) phoned
#   home to a C2. This repo therefore NEVER uses those actions (nor anchore's
#   setup actions). We download the official release binary and verify its
#   sha256 against tools/supply-chain/tool-checksums.txt — fail closed on any
#   mismatch or missing pin.
set -euo pipefail

TRIVY_VERSION="0.72.0"
GRYPE_VERSION="0.115.0"
COSIGN_VERSION="3.0.2"

BIN_DIR="${1:-${BIN_DIR:-/usr/local/bin}}"
CHECKSUMS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tool-checksums.txt"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

os="$(uname -s)"
arch="$(uname -m)"
if [[ "$os" != "Linux" || "$arch" != "x86_64" ]]; then
  echo "ERROR: this script pins linux-amd64 binaries only (got ${os}/${arch})." >&2
  echo "       For local dev install via your package manager, e.g.:" >&2
  echo "         brew install trivy grype cosign" >&2
  exit 2
fi

# Verify a downloaded file's sha256 against the committed pin. Fail closed.
verify() {
  local file="$1" name="$2"
  local expected actual
  expected="$(awk -v f="$name" '$2 == f { print $1 }' "$CHECKSUMS")"
  if [[ -z "$expected" ]]; then
    echo "ERROR: no pinned checksum for ${name} in ${CHECKSUMS} — refusing to install." >&2
    exit 1
  fi
  actual="$(sha256sum "$file" | awk '{ print $1 }')"
  if [[ "$actual" != "$expected" ]]; then
    echo "ERROR: checksum mismatch for ${name}" >&2
    echo "       expected ${expected}" >&2
    echo "       actual   ${actual}" >&2
    exit 1
  fi
  echo "  verified ${name} (sha256 ${actual:0:16}…)"
}

mkdir -p "$BIN_DIR"

echo "Installing trivy v${TRIVY_VERSION}…"
trivy_tar="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
curl -fsSL -o "${WORK}/${trivy_tar}" \
  "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${trivy_tar}"
verify "${WORK}/${trivy_tar}" "${trivy_tar}"
tar -xzf "${WORK}/${trivy_tar}" -C "$WORK" trivy
install -m 0755 "${WORK}/trivy" "${BIN_DIR}/trivy"

echo "Installing grype v${GRYPE_VERSION}…"
grype_tar="grype_${GRYPE_VERSION}_linux_amd64.tar.gz"
curl -fsSL -o "${WORK}/${grype_tar}" \
  "https://github.com/anchore/grype/releases/download/v${GRYPE_VERSION}/${grype_tar}"
verify "${WORK}/${grype_tar}" "${grype_tar}"
tar -xzf "${WORK}/${grype_tar}" -C "$WORK" grype
install -m 0755 "${WORK}/grype" "${BIN_DIR}/grype"

echo "Installing cosign v${COSIGN_VERSION}…"
cosign_bin="cosign-linux-amd64"
curl -fsSL -o "${WORK}/${cosign_bin}" \
  "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/${cosign_bin}"
verify "${WORK}/${cosign_bin}" "${cosign_bin}"
install -m 0755 "${WORK}/${cosign_bin}" "${BIN_DIR}/cosign"

echo "Done. Installed into ${BIN_DIR}:"
"${BIN_DIR}/trivy" --version | head -1 || true
"${BIN_DIR}/grype" version | head -1 || true
"${BIN_DIR}/cosign" version 2>&1 | head -1 || true
