#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Scheduled backup entrypoint for ne-backup.timer: take a scheduled backup, then
# enforce the retention policy (keep the 7 newest; the newest safety backup is
# always spared). C12: NO secrets here — the customer KEK is read from the bundle
# SecretStore by ne-backup itself and is never passed on the command line or env.
set -euo pipefail

: "${NE_BACKUP_BUNDLE:?set NE_BACKUP_BUNDLE to the per-customer bundle dir}"
: "${NE_BACKUP_DEST:?set NE_BACKUP_DEST to the customer-mounted backup destination}"
KEEP="${NE_BACKUP_KEEP:-7}"

ne-backup create --kind scheduled --bundle "${NE_BACKUP_BUNDLE}" --dest "${NE_BACKUP_DEST}"
ne-backup prune --keep "${KEEP}" --dest "${NE_BACKUP_DEST}"
