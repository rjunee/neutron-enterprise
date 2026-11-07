# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-backup — pilot-tier backup/restore + encryption at rest (customer keys).

DB-native backups (Postgres ``pg_dump``/``pg_dumpall`` via ``docker exec``;
Qdrant per-collection snapshot API over loopback HTTP), object backups (the
customer config bundle minus its ephemeral ``runtime/`` secrets dir, plus
allowlisted Docker volumes), and envelope encryption with a customer-managed
256-bit KEK held ONLY in the bundle SecretStore. The hardened
tar-traversal/symlink/hardlink guards, drift manifests, allowlists, and
safety-snapshot-before-restore core is derived from jupyter-naas/abi
``snapshot_runtime.py`` @ 4b0d12f3ed0e (MIT, Copyright (C) 2025 Naas.ai).

C2: the tool makes zero off-VM connections (loopback / ``docker exec`` only).
C12: the customer KEK lives only in the SecretStore; it is never persisted to a
manifest, keyinfo, artifact, or log.
"""

from __future__ import annotations

__version__ = "0.1.0"
