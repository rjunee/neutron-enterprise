# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi @ 4b0d12f3ed0e (MIT, Copyright (C) 2025
# Naas.ai): the drift-detection manifest, select_prunable/detect_drift, and the
# managed-volume allowlist idea (ABI's DATA_VOLUMES). Adapted to a pydantic
# frozen model with SQL-identifier / artifact-name / volume allowlists enforced
# at parse time, and DB-native + qdrant + envelope-crypto fields added.
"""The backup manifest — carries names / hashes / counts, never secret values.

Parsing an untrusted manifest (``BackupManifest.from_json``) rejects, BEFORE any
use: an out-of-allowlist volume, an invalid SQL identifier, or an unsafe artifact
name. So an attacker-crafted manifest cannot make restore wipe an out-of-band
volume, inject SQL through a database name, or traverse paths through an artifact
relpath.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ne.backup.errors import ManifestError
from ne.backup.tarsafe import is_unsafe_component

MANIFEST_FORMAT_VERSION = 1

# The pilot's actual persistent Docker volume set — the swap of ABI's hardcoded
# DATA_VOLUMES for pilot reality. pg-data / qdrant-storage are NOT here: those
# engines are captured DB-native (pg_dump / snapshot API), never as cold volume
# tars. caddy-data holds the local-CA material worth surviving a restore.
VOLUME_ALLOWLIST: frozenset[str] = frozenset({"caddy-data"})

# SQL-identifier allowlist (the tar-guard's sibling): a database name must be a
# plain lowercase identifier so it can never carry an injection payload into the
# ``DROP DATABASE "<db>"`` / pg_restore path.
_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# Artifact / collection names: a single, plain filename component.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

BackupKind = Literal["scheduled", "on-demand", "safety"]

# The quiesced Pilot transport cutover is recoverable only from one snapshot of
# both Postgres writer roots.  Keep this fixed rather than letting the cutover
# caller weaken or broaden the database-set requirement.
PILOT_CUTOVER_DATABASES: tuple[str, str] = ("ne_dbos", "neutron")


def _valid_sql_identifier(name: str) -> bool:
    return bool(_SQL_IDENTIFIER.match(name))


def _valid_safe_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name)) and not is_unsafe_component(name)


class QdrantCollectionRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    points_count: int

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _valid_safe_name(value):
            raise ValueError(f"unsafe qdrant collection name: {value!r}")
        return value


class ArtifactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    relpath: str
    ciphertext_sha256: str
    plaintext_sha256: str
    size: int

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _valid_safe_name(value):
            raise ValueError(f"unsafe artifact name: {value!r}")
        return value

    @field_validator("relpath")
    @classmethod
    def _check_relpath(cls, value: str) -> str:
        # A single "artifacts/<safe-name>" relpath — no traversal, no absolute.
        parts = value.split("/")
        if len(parts) != 2 or parts[0] != "artifacts" or is_unsafe_component(parts[1]):
            raise ValueError(f"unsafe artifact relpath: {value!r}")
        return value


class BackupManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    created_at: str
    note: str = ""
    kind: BackupKind
    format_version: int = MANIFEST_FORMAT_VERSION
    kek_fingerprint: str
    databases: list[str] = []
    qdrant_collections: list[QdrantCollectionRef] = []
    artifacts: list[ArtifactRef] = []
    volumes: list[str] = []
    config_hashes: dict[str, str] = {}
    durations: dict[str, float] = {}

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if is_unsafe_component(value):
            raise ValueError(f"unsafe backup id: {value!r}")
        return value

    @field_validator("databases")
    @classmethod
    def _check_databases(cls, value: list[str]) -> list[str]:
        for db in value:
            if not _valid_sql_identifier(db):
                raise ValueError(f"invalid SQL identifier for database: {db!r}")
        return value

    @field_validator("volumes")
    @classmethod
    def _check_volumes(cls, value: list[str]) -> list[str]:
        unknown = [v for v in value if v not in VOLUME_ALLOWLIST]
        if unknown:
            raise ValueError(f"volumes not in the managed allowlist: {unknown}")
        return value

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, raw: str | bytes) -> BackupManifest:
        """Parse and fully validate a manifest. Raises :class:`ManifestError` on
        any malformed / out-of-allowlist / unsafe content."""
        try:
            return cls.model_validate_json(raw)
        except ValueError as error:  # pydantic ValidationError subclasses ValueError
            raise ManifestError(f"invalid backup manifest: {error}") from error


class VerifiedBackupReceipt(BaseModel):
    """Content-free proof of a freshly created Pilot cutover backup.

    This is a machine-facing return value, not a persisted authority object.  Its
    fixed database set prevents a caller from treating a partial or independently
    timed dump as the rollback point for the protected-transport migration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backup_id: str
    manifest_sha256: str
    kek_fingerprint: str
    verified: Literal[True] = True
    databases: tuple[str, ...]

    @field_validator("backup_id")
    @classmethod
    def _check_backup_id(cls, value: str) -> str:
        if is_unsafe_component(value):
            raise ValueError(f"unsafe backup id: {value!r}")
        return value

    @field_validator("manifest_sha256")
    @classmethod
    def _check_manifest_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("kek_fingerprint")
    @classmethod
    def _check_kek_fingerprint(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{16}", value):
            raise ValueError("kek_fingerprint must be a lowercase 16-hex fingerprint")
        return value

    @field_validator("databases")
    @classmethod
    def _check_databases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != PILOT_CUTOVER_DATABASES:
            raise ValueError(
                "verified Pilot cutover receipt requires exact databases "
                f"{list(PILOT_CUTOVER_DATABASES)!r}"
            )
        return value


def detect_drift(manifest: BackupManifest, current_hashes: dict[str, str]) -> list[str]:
    """Human-readable warnings when tracked config differs from backup time."""
    messages: list[str] = []
    for name, snap_hash in sorted(manifest.config_hashes.items()):
        current = current_hashes.get(name)
        if current and current != snap_hash:
            messages.append(f"{name} has changed since the backup was taken")
    return messages


def select_prunable(manifests: list[BackupManifest], keep: int) -> list[str]:
    """Ids to delete, keeping the ``keep`` newest — and NEVER the newest safety
    backup, so a failed restore always has a recovery point to fall back on."""
    if keep < 0:
        raise ManifestError("keep must be >= 0")
    ordered = sorted(manifests, key=lambda m: m.created_at, reverse=True)
    newest_safety = next((m.id for m in ordered if m.kind == "safety"), None)
    return [m.id for m in ordered[keep:] if m.id != newest_safety]
