#!/bin/sh
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Fetch the Apache AGE 1.7.0 source release (tarball, detached signature and
# the AGE KEYS file) into DEST, each verified against its pinned SHA-512.
#
#   fetch-age-src.sh DEST [SEED_DIR]
#
# A file already present in DEST or SEED_DIR is reused only when its SHA-512
# matches the pin (CI pre-fetches into an actions/cache directory and hands it
# to the image build as the `age-src` build context). Otherwise each mirror is
# tried in order, in rounds with exponential backoff. A download whose digest
# does not match is deleted and never used. The Dockerfile verifies the
# digests and the GPG signature again after this script returns.
set -eu

dest="${1:?usage: fetch-age-src.sh DEST [SEED_DIR]}"
seed="${2:-}"

age_path="age/PG18/1.7.0"
mirrors="https://dlcdn.apache.org https://downloads.apache.org https://archive.apache.org/dist"
rounds=5

mkdir -p "${dest}"

sha512_of() {
  sha512sum "$1" | awk '{print $1}'
}

matches() {
  [ -f "$1" ] && [ "$(sha512_of "$1")" = "$2" ]
}

fetch() {
  name="$1"
  rel="$2"
  want="$3"
  out="${dest}/${name}"

  if matches "${out}" "${want}"; then
    echo "fetch-age-src: ${name}: verified (already present)"
    return 0
  fi
  if [ -n "${seed}" ] && matches "${seed}/${name}" "${want}"; then
    cp "${seed}/${name}" "${out}"
    echo "fetch-age-src: ${name}: verified (pre-fetched)"
    return 0
  fi

  round=1
  delay=5
  while [ "${round}" -le "${rounds}" ]; do
    for mirror in ${mirrors}; do
      rm -f "${out}"
      if wget -q --tries=2 --timeout=30 --retry-connrefused \
        --retry-on-http-error=500,502,503,504 --waitretry=2 \
        -O "${out}" "${mirror}/${rel}"; then
        if matches "${out}" "${want}"; then
          echo "fetch-age-src: ${name}: verified from ${mirror} (round ${round})"
          return 0
        fi
        echo "fetch-age-src: ${name}: SHA-512 mismatch from ${mirror}, discarded" >&2
      fi
      rm -f "${out}"
    done
    if [ "${round}" -lt "${rounds}" ]; then
      echo "fetch-age-src: ${name}: round ${round} failed, retrying in ${delay}s" >&2
      sleep "${delay}"
      delay=$((delay * 2))
    fi
    round=$((round + 1))
  done
  echo "fetch-age-src: ${name}: no mirror served a verified copy" >&2
  return 1
}

fetch age-KEYS "age/KEYS" \
  0a79d404d3d00d55d4dcf7b2e010310884acf194e57e37b67c3cb0d79678c5d2dc68ec482c522ed9946f4a3cd4335c936cd7176ead799b30e602741fb115e1f3
fetch apache-age-1.7.0-src.tar.gz "${age_path}/apache-age-1.7.0-src.tar.gz" \
  129e64d70ec82e0fb47202145af45915b335048472c9a869129c31e4cb59f15a271a929a36a7abb2822272591e6476786c36908cb35a10cd73f2ecd0a617d3bb
fetch apache-age-1.7.0-src.tar.gz.asc "${age_path}/apache-age-1.7.0-src.tar.gz.asc" \
  5bc1b1eb000ab85d2f9d567591f8a65a57ee503caff77b6b5a06a603c2e6c60e70420fab4c803ed23848f7fb0084016f03e0075fb4a9a43fb90be7d45c721e2a
