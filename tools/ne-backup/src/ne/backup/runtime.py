# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi @ 4b0d12f3ed0e (MIT, Copyright (C) 2025
# Naas.ai): the create/restore orchestration shape — staged-then-atomic create,
# safety-snapshot-before-restore, validate-before-destructive ordering, and the
# partway-failure recovery/restart pattern. Adapted for DB-native dumps + qdrant
# snapshots + NEB1 envelope encryption, with the ABI hardcoded volume list swapped
# for the pilot allowlist and every side effect injected for unit testing.
"""High-level backup/restore orchestration.

create_backup: stage the ciphertext under ``<dest>/<id>.partial`` while the
PLAINTEXT dumps/snapshots stage on a HOST-LOCAL ``tempfile.mkdtemp`` dir (off the
customer backup dest — P2-8), each encrypted NEB1; write keyinfo + manifest LAST,
atomic rename. A backup is INVISIBLE unless complete: any failure removes the
``.partial`` dir and the host-local plaintext staging dir.

The manifest is bound to the key material (P2-7): ``sha256(manifest.json)`` is
folded into the DEK-wrap AEAD AAD, so any post-hoc edit of ``manifest.json`` —
including silently dropping a database from the artifact SET — makes the DEK unwrap
fail closed at restore/verify instead of restoring a truncated set. Versioning is
the ``manifest_sha256`` field in ``keyinfo.json`` (absent ⇒ legacy AAD ⇒ pre-change
backups still restore; present ⇒ manifest-bound AAD).

restore_backup (safe-by-construction): validate manifest → KEK fingerprint → DEK
unwrap (now also manifest-authenticating) → ciphertext hashes → decrypt ALL
artifacts to a HOST-LOCAL 0700 staging dir with plaintext-hash checks — ALL
validation precedes ANY destructive step — then a safety backup, then the
destructive restore, then integrity verification. A partway failure prints
recovery instructions naming the safety id and best-effort restarts services.

Every create/restore/prune first sweeps age-guarded orphaned staging dirs
(``*.partial`` / ``.restore-*`` on the dest; ``ne-backup-*`` on the host staging
root) so a SIGKILL/OOM/power-loss mid-run cannot strand plaintext or ciphertext
scratch forever.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag

from ne.backup.config import BackupConfig
from ne.backup.crypto import (
    decrypt_file,
    encrypt_file,
    kek_fingerprint,
    new_dek,
    sha256_file,
    unwrap_dek,
    wrap_dek,
)
from ne.backup.errors import ManifestError, NeBackupError, RestoreError
from ne.backup.manifest import (
    PILOT_CUTOVER_DATABASES,
    ArtifactRef,
    BackupKind,
    BackupManifest,
    QdrantCollectionRef,
    VerifiedBackupReceipt,
    detect_drift,
    select_prunable,
)
from ne.backup.runner import Runner
from ne.backup.tarsafe import archive_dir, is_unsafe_component, safe_extract


# --------------------------------------------------------------------------- #
# Service ports (injectable seams — real impls in postgres/qdrant/volumes)
# --------------------------------------------------------------------------- #
class PostgresPort(Protocol):
    def list_databases(self) -> list[str]: ...
    def dump_globals(self, dest: Path) -> None: ...
    def dump_database(self, db: str, dest: Path) -> None: ...
    def restore_globals(self, src: Path) -> None: ...
    def restore_database(self, db: str, src: Path) -> None: ...
    def scalar(self, db: str, sql: str) -> str: ...
    def user_table_count(self, db: str) -> int: ...


class QdrantPort(Protocol):
    def list_collections(self) -> list[str]: ...
    def points_count(self, name: str) -> int: ...
    def create_snapshot(self, name: str) -> str: ...
    def download_snapshot(self, name: str, snapshot: str, dest: Path) -> None: ...
    def delete_snapshot(self, name: str, snapshot: str) -> None: ...
    def upload_snapshot(self, name: str, src: Path) -> None: ...


class VolumesPort(Protocol):
    def archive(self, key: str, dest_tar: Path) -> None: ...
    def reset(self, key: str) -> None: ...
    def extract(self, key: str, src_tar: Path) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _null_echo(_: str) -> None:
    return None


@dataclass
class Services:
    postgres: PostgresPort
    volumes: VolumesPort
    runner: Runner  # container stop/start only
    qdrant: QdrantPort | None = None
    clock: Callable[[], datetime] = _utcnow
    echo: Callable[[str], None] = _null_echo


KEYINFO_NAME = "keyinfo.json"
MANIFEST_NAME = "manifest.json"
ARTIFACTS_DIR = "artifacts"
_BUNDLE_ARCNAME = "bundle"
_PROTECTED_TRANSPORT_DATABASES = frozenset(PILOT_CUTOVER_DATABASES)

# Orphan-sweep policy (P2-8): a staging dir older than this is presumed stranded by
# a crashed run and reclaimed. The age guard means a legitimate concurrent run's
# fresh staging is never clobbered (accepted: a create/restore running > this long
# could be swept by a concurrent run — pilot scale, documented in README).
ORPHAN_MIN_AGE_SECONDS = 6 * 60 * 60
_RESTORE_STAGE_PREFIX = ".restore-"
_HOST_STAGE_PREFIX = "ne-backup-"
# Dest-side staging patterns (ciphertext scratch on the customer dest).
_DEST_STAGE_PATTERNS: tuple[str, ...] = ("*.partial", f"{_RESTORE_STAGE_PREFIX}*")
# Host-local staging-root patterns (plaintext scratch off the dest).
_HOST_STAGE_PATTERNS: tuple[str, ...] = (f"{_HOST_STAGE_PREFIX}*",)


def sweep_orphaned_staging(
    root: Path,
    patterns: tuple[str, ...],
    *,
    echo: Callable[[str], None] = _null_echo,
    min_age_seconds: float = ORPHAN_MIN_AGE_SECONDS,
    now: float | None = None,
) -> list[str]:
    """Reclaim age-guarded orphaned staging dirs under ``root`` (P2-8).

    An entry is reclaimed only when it is a real (non-symlink) directory whose name
    matches one of ``patterns`` AND whose mtime is older than ``min_age_seconds`` —
    so a crashed run's plaintext/ciphertext scratch cannot linger forever while a
    concurrent run's fresh staging is left untouched. Returns the reclaimed names.
    """
    if not root.exists():
        return []
    reference = time.time() if now is None else now
    reclaimed: list[str] = []
    for entry in root.iterdir():
        if not any(fnmatch.fnmatch(entry.name, p) for p in patterns):
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            age = reference - entry.stat().st_mtime
        except OSError:
            continue
        if age <= min_age_seconds:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        echo(f"  - reclaimed orphaned staging dir {entry.name}")
        reclaimed.append(entry.name)
    return reclaimed


def _staging_root(staging_root: Path | None) -> Path:
    """The host-LOCAL plaintext staging root (off the backup dest). Defaults to the
    system temp dir when unset. A configured root is created (0700) on first use so
    a documented override like ``/var/tmp/ne-backup`` works without manual setup."""
    if staging_root is None:
        return Path(tempfile.gettempdir())
    try:
        staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise NeBackupError(
            f"cannot create staging root {staging_root}: {error} "
            "(check the staging_dir config value)"
        ) from error
    return staging_root


# --------------------------------------------------------------------------- #
# Artifact naming (also the AEAD AAD binding — see crypto._chunk_aad)
# --------------------------------------------------------------------------- #
_GLOBALS_NAME = "pg-globals"


def _db_artifact(db: str) -> str:
    return f"pgdb-{db}"


def _qdrant_artifact(name: str) -> str:
    return f"qdrant-{name}"


def _volume_artifact(key: str) -> str:
    return f"volume-{key}"


def _generate_id(now: datetime, kind: BackupKind) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{kind}-{secrets.token_hex(3)}"


def _write_0600(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _config_hashes(bundle: Path | None, compose_file: Path | None) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if bundle is not None:
        manifest = bundle / "manifest.json"
        if manifest.is_file():
            hashes["bundle/manifest.json"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if compose_file is not None and compose_file.is_file():
        hashes["compose.yaml"] = hashlib.sha256(compose_file.read_bytes()).hexdigest()
    return hashes


@dataclass
class _StagedArtifacts:
    artifacts_dir: Path
    tmp_dir: Path
    dek: bytes
    refs: list[ArtifactRef] = field(default_factory=list)

    def add(self, name: str, produce: Callable[[Path], None]) -> None:
        plaintext = self.tmp_dir / name
        produce(plaintext)
        plaintext_sha = sha256_file(plaintext)
        relpath = f"{ARTIFACTS_DIR}/{name}.neb"
        ciphertext = self.artifacts_dir / f"{name}.neb"
        encrypt_file(self.dek, name, plaintext, ciphertext)
        self.refs.append(
            ArtifactRef(
                name=name,
                relpath=relpath,
                ciphertext_sha256=sha256_file(ciphertext),
                plaintext_sha256=plaintext_sha,
                size=ciphertext.stat().st_size,
            )
        )
        plaintext.unlink()


def create_backup(
    *,
    config: BackupConfig,
    services: Services,
    dest: Path,
    kek: bytes,
    kind: BackupKind = "on-demand",
    note: str = "",
    bundle: Path | None = None,
    compose_file: Path | None = None,
    staging_root: Path | None = None,
    _expected_databases: frozenset[str] | None = None,
) -> BackupManifest:
    """Produce a complete, encrypted backup under ``<dest>/<id>``."""
    started = time.monotonic()
    now = services.clock()
    backup_id = _generate_id(now, kind)
    dest.mkdir(parents=True, exist_ok=True)
    sweep_orphaned_staging(dest, _DEST_STAGE_PATTERNS, echo=services.echo)
    sweep_orphaned_staging(_staging_root(staging_root), _HOST_STAGE_PATTERNS, echo=services.echo)
    staging = dest / f"{backup_id}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    artifacts_dir = staging / ARTIFACTS_DIR
    artifacts_dir.mkdir(parents=True)
    # Plaintext DB dumps/snapshots stage HOST-LOCAL (off the customer backup dest —
    # P2-8: a crash must never strand full-DB plaintext on the network dest). Only
    # the encrypted artifacts (ciphertext) ever land under the dest .partial dir.
    # mkdtemp is 0700; it is ALWAYS reclaimed by the finally below.
    tmp_dir = Path(
        tempfile.mkdtemp(
            prefix=f"{_HOST_STAGE_PREFIX}create-{backup_id}-", dir=_staging_root(staging_root)
        )
    )

    try:
        staged = _StagedArtifacts(artifacts_dir=artifacts_dir, tmp_dir=tmp_dir, dek=new_dek())

        databases = services.postgres.list_databases()
        if _expected_databases is not None:
            _require_exact_databases(databases, _expected_databases)
        services.echo("  - dumping postgres globals")
        staged.add(_GLOBALS_NAME, services.postgres.dump_globals)
        for db in databases:
            services.echo(f"  - dumping database {db}")
            staged.add(_db_artifact(db), lambda p, _db=db: services.postgres.dump_database(_db, p))

        collections: list[QdrantCollectionRef] = []
        if services.qdrant is not None:
            for name in services.qdrant.list_collections():
                services.echo(f"  - snapshotting qdrant collection {name}")
                points = services.qdrant.points_count(name)
                collections.append(QdrantCollectionRef(name=name, points_count=points))
                staged.add(
                    _qdrant_artifact(name),
                    lambda p, _n=name: _snapshot_qdrant(services, _n, p),
                )

        if bundle is not None:
            services.echo("  - archiving config bundle (runtime/ excluded)")
            staged.add(
                _BUNDLE_ARCNAME,
                lambda p: archive_dir(bundle, p, arcname=_BUNDLE_ARCNAME),
            )

        for key in config.volumes:
            services.echo(f"  - archiving volume {key}")
            staged.add(_volume_artifact(key), lambda p, _k=key: services.volumes.archive(_k, p))

        fingerprint = kek_fingerprint(kek)
        # Serialize the manifest BEFORE wrapping the DEK so its SHA-256 can be bound
        # into the DEK-wrap AAD (P2-7). The EXACT bytes hashed here are the exact
        # bytes written to manifest.json below — any later edit ⇒ InvalidTag.
        manifest = BackupManifest(
            id=backup_id,
            created_at=now.isoformat(),
            note=note,
            kind=kind,
            kek_fingerprint=fingerprint,
            databases=sorted(databases),
            qdrant_collections=sorted(collections, key=lambda c: c.name),
            artifacts=sorted(staged.refs, key=lambda a: a.name),
            volumes=sorted(config.volumes),
            config_hashes=_config_hashes(bundle, compose_file),
            durations={"create_seconds": round(time.monotonic() - started, 3)},
        )
        manifest_json = manifest.to_json()
        manifest_sha = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
        wrapped_dek, dek_nonce = wrap_dek(kek, staged.dek, manifest_sha256=manifest_sha)
        _write_0600(
            staging / KEYINFO_NAME,
            json.dumps(
                {
                    "format": "NEB1",
                    "wrapped_dek": wrapped_dek,
                    "dek_nonce": dek_nonce,
                    "kek_fingerprint": fingerprint,
                    "manifest_sha256": manifest_sha,
                },
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )
        (staging / MANIFEST_NAME).write_text(manifest_json, encoding="utf-8")
        os.rename(staging, dest / backup_id)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _require_exact_databases(databases: list[str], expected: frozenset[str]) -> frozenset[str]:
    """Reject incomplete, expanded, or ambiguous database identity sets."""
    observed = frozenset(databases)
    if len(observed) != len(databases):
        raise ManifestError("backup database identities contain duplicates")
    missing = expected - observed
    extra = observed - expected
    if missing or extra:
        raise ManifestError(
            "backup database identities do not match the protected-transport set "
            f"(missing={sorted(missing)!r}, extra={sorted(extra)!r})"
        )
    return observed


def create_verified_backup_receipt(
    *,
    config: BackupConfig,
    services: Services,
    dest: Path,
    kek: bytes,
    bundle: Path | None = None,
    compose_file: Path | None = None,
    staging_root: Path | None = None,
) -> VerifiedBackupReceipt:
    """Create and decrypt-verify the exact backup required before cutover.

    Every receipt field is derived from the persisted, manifest-bound backup.
    The function returns only after all ciphertext and plaintext hashes verify.
    """
    manifest = create_backup(
        config=config,
        services=services,
        dest=dest,
        kek=kek,
        kind="on-demand",
        note="agent protected-transport pre-cutover",
        bundle=bundle,
        compose_file=compose_file,
        staging_root=staging_root,
        _expected_databases=_PROTECTED_TRANSPORT_DATABASES,
    )
    verification = _verify_backup_details(
        dest,
        manifest.id,
        kek,
        staging_root=staging_root,
        require_manifest_binding=True,
    )
    created_manifest_sha256 = hashlib.sha256(manifest.to_json().encode("utf-8")).hexdigest()
    if verification.manifest_sha256 != created_manifest_sha256:
        raise RestoreError("persisted backup manifest does not match the created backup")
    databases = _require_exact_databases(
        verification.manifest.databases, _PROTECTED_TRANSPORT_DATABASES
    )
    if not verification.plaintext_verified:
        raise RestoreError("backup did not complete authenticated decrypt verification")
    return VerifiedBackupReceipt(
        backup_id=verification.manifest.id,
        manifest_sha256=verification.manifest_sha256,
        kek_fingerprint=verification.manifest.kek_fingerprint,
        databases=tuple(sorted(databases)),
        verified=True,
    )


def _snapshot_qdrant(services: Services, name: str, dest: Path) -> None:
    assert services.qdrant is not None
    snapshot = services.qdrant.create_snapshot(name)
    try:
        services.qdrant.download_snapshot(name, snapshot, dest)
    finally:
        services.qdrant.delete_snapshot(name, snapshot)


# --------------------------------------------------------------------------- #
# Read side
# --------------------------------------------------------------------------- #
def _load_manifest_raw(snap_dir: Path) -> tuple[BackupManifest, bytes]:
    """Read manifest.json ONCE and return both the parsed manifest and the exact
    raw bytes. Reading once kills any parse-vs-hash TOCTOU: the bytes that were
    parsed are the same bytes whose SHA-256 the DEK-wrap AAD authenticates."""
    path = snap_dir / MANIFEST_NAME
    if not path.is_file():
        raise ManifestError(f"no manifest at {path}")
    raw = path.read_bytes()
    return BackupManifest.from_json(raw), raw


def _load_manifest(snap_dir: Path) -> BackupManifest:
    manifest, _ = _load_manifest_raw(snap_dir)
    return manifest


def list_backups(dest: Path) -> list[BackupManifest]:
    if not dest.exists():
        return []
    out: list[BackupManifest] = []
    for entry in sorted(dest.iterdir()):
        if not entry.is_dir() or entry.name.endswith(".partial"):
            continue
        try:
            out.append(_load_manifest(entry))
        except ManifestError:
            continue
    out.sort(key=lambda m: m.created_at, reverse=True)
    return out


@dataclass(frozen=True)
class _BackupVerification:
    manifest: BackupManifest
    manifest_sha256: str
    plaintext_verified: bool


def _verify_backup_details(
    dest: Path,
    backup_id: str,
    kek: bytes | None = None,
    *,
    staging_root: Path | None = None,
    require_manifest_binding: bool = False,
) -> _BackupVerification:
    if is_unsafe_component(backup_id):
        raise ManifestError(f"unsafe backup id: {backup_id!r}")
    snap_dir = dest / backup_id
    manifest, manifest_raw = _load_manifest_raw(snap_dir)
    if manifest.id != backup_id:
        raise RestoreError(
            f"backup id mismatch: requested {backup_id!r}, manifest contains {manifest.id!r}"
        )
    for art in manifest.artifacts:
        ct = snap_dir / art.relpath
        if not ct.is_file():
            raise RestoreError(f"missing artifact {art.name!r} ({art.relpath})")
        if sha256_file(ct) != art.ciphertext_sha256:
            raise RestoreError(f"ciphertext hash mismatch for artifact {art.name!r}")
    decrypted = False
    if kek is not None:
        dek = _unwrap_backup_dek(
            snap_dir,
            manifest,
            manifest_raw,
            kek,
            require_manifest_binding=require_manifest_binding,
        )
        with tempfile.TemporaryDirectory(
            prefix="ne-backup-verify-", dir=_staging_root(staging_root)
        ) as scratch:
            for art in manifest.artifacts:
                out = Path(scratch) / art.name
                decrypt_file(dek, art.name, snap_dir / art.relpath, out)
                if sha256_file(out) != art.plaintext_sha256:
                    raise RestoreError(f"plaintext hash mismatch for artifact {art.name!r}")
                out.unlink()
        decrypted = True
    return _BackupVerification(
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        plaintext_verified=decrypted,
    )


def verify_backup(
    dest: Path,
    backup_id: str,
    kek: bytes | None = None,
    *,
    staging_root: Path | None = None,
) -> dict[str, Any]:
    """Verify a backup's ciphertext hashes; with ``kek`` also decrypt-verify every
    artifact's plaintext hash (written to HOST-LOCAL throwaway storage off the
    backup dest — P2-8 sibling: verify used to stage plaintext on the dest too)."""
    verification = _verify_backup_details(dest, backup_id, kek, staging_root=staging_root)
    return {
        "id": verification.manifest.id,
        "kind": verification.manifest.kind,
        "artifacts": len(verification.manifest.artifacts),
        "ciphertext_verified": True,
        "plaintext_verified": verification.plaintext_verified,
    }


def prune_backups(dest: Path, keep: int, *, echo: Callable[[str], None] = _null_echo) -> list[str]:
    """Delete all but the ``keep`` newest (never the newest safety). Returns
    deleted ids. Also sweeps age-guarded orphaned staging dirs on the dest (P2-8)."""
    sweep_orphaned_staging(dest, _DEST_STAGE_PATTERNS, echo=echo)
    manifests = list_backups(dest)
    prunable = select_prunable(manifests, keep)
    for backup_id in prunable:
        if is_unsafe_component(backup_id):
            continue
        shutil.rmtree(dest / backup_id, ignore_errors=True)
    return prunable


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
def _unwrap_backup_dek(
    snap_dir: Path,
    manifest: BackupManifest,
    manifest_raw: bytes,
    kek: bytes,
    *,
    require_manifest_binding: bool = False,
) -> bytes:
    keyinfo = json.loads((snap_dir / KEYINFO_NAME).read_text(encoding="utf-8"))
    fingerprint = kek_fingerprint(kek)
    if fingerprint != keyinfo.get("kek_fingerprint") or fingerprint != manifest.kek_fingerprint:
        raise RestoreError(
            "KEK fingerprint mismatch — wrong customer key; aborting before any change"
        )
    # P2-7: bind the DEK unwrap to the manifest. Presence of "manifest_sha256" in
    # keyinfo is the v2 version signal; hash the ACTUAL manifest file bytes (never
    # trust the keyinfo's claimed value) so a post-hoc manifest edit — including a
    # dropped-database set tamper — fails closed here. A legacy keyinfo (no field)
    # unwraps under the original AAD so pre-change backups still restore; stripping
    # the field from a v2 keyinfo yields the legacy AAD against a v2-wrapped DEK ⇒
    # InvalidTag (downgrade-proof).
    bound = "manifest_sha256" in keyinfo
    if require_manifest_binding and not bound:
        raise RestoreError("backup integrity check failed: authenticated manifest binding required")
    sha = hashlib.sha256(manifest_raw).hexdigest() if bound else None
    if bound and keyinfo.get("manifest_sha256") != sha:
        raise RestoreError("backup integrity check failed: manifest hash does not match keyinfo")
    try:
        return unwrap_dek(kek, keyinfo["wrapped_dek"], keyinfo["dek_nonce"], manifest_sha256=sha)
    except InvalidTag as error:
        raise RestoreError(
            "backup integrity check failed: manifest or key material was modified "
            "after the backup was written (set-membership/manifest tamper), or the "
            "wrong KEK was supplied"
        ) from error


def restore_backup(
    *,
    config: BackupConfig,
    services: Services,
    dest: Path,
    backup_id: str,
    kek: bytes,
    bundle: Path | None = None,
    restore_bundle_to: Path | None = None,
    no_safety: bool = False,
    current_config_hashes: dict[str, str] | None = None,
    staging_root: Path | None = None,
) -> dict[str, Any]:
    """Restore ``backup_id`` in place. Returns a restore-report dict."""
    started = time.monotonic()
    if is_unsafe_component(backup_id):
        raise RestoreError(f"unsafe backup id: {backup_id!r}")
    sweep_orphaned_staging(dest, _DEST_STAGE_PATTERNS, echo=services.echo)
    sweep_orphaned_staging(_staging_root(staging_root), _HOST_STAGE_PATTERNS, echo=services.echo)
    snap_dir = dest / backup_id
    manifest, manifest_raw = _load_manifest_raw(snap_dir)

    # (2) KEK fingerprint + manifest-bound DEK unwrap — must match BEFORE anything.
    dek = _unwrap_backup_dek(snap_dir, manifest, manifest_raw, kek)

    for message in detect_drift(manifest, current_config_hashes or {}):
        services.echo(f"Warning: {message}")

    # (3) ciphertext hashes — no destructive step yet.
    for art in manifest.artifacts:
        ct = snap_dir / art.relpath
        if not ct.is_file():
            raise RestoreError(f"missing artifact {art.name!r} ({art.relpath})")
        if sha256_file(ct) != art.ciphertext_sha256:
            raise RestoreError(f"ciphertext hash mismatch for artifact {art.name!r}")

    # (4) decrypt ALL to a HOST-LOCAL 0700 staging dir (off the backup dest — P2-8)
    # and check plaintext hashes.
    stage = Path(
        tempfile.mkdtemp(
            prefix=f"{_HOST_STAGE_PREFIX}restore-{backup_id}-", dir=_staging_root(staging_root)
        )
    )
    try:
        paths: dict[str, Path] = {}
        for art in manifest.artifacts:
            out = stage / art.name
            decrypt_file(dek, art.name, snap_dir / art.relpath, out)
            if sha256_file(out) != art.plaintext_sha256:
                raise RestoreError(f"plaintext hash mismatch for artifact {art.name!r}")
            paths[art.name] = out

        # ===== live data is still UNTOUCHED past this line =====
        safety_id = _maybe_safety_backup(
            config=config,
            services=services,
            dest=dest,
            kek=kek,
            bundle=bundle,
            no_safety=no_safety,
            target_id=backup_id,
            staging_root=staging_root,
        )

        try:
            _apply_restore(config, services, manifest, paths, restore_bundle_to)
        except BaseException:
            services.echo("ERROR: restore failed partway through; data may be inconsistent.")
            if safety_id is not None:
                services.echo(
                    f"Recover the previous state with: ne-backup restore {safety_id} "
                    f"--dest {dest} --bundle <bundle> --no-safety"
                )
            _start_containers(services, config.stop_containers)
            raise

        report = _verify_restore(services, manifest)
        report["id"] = manifest.id
        report["safety_id"] = safety_id
        report["restore_seconds"] = round(time.monotonic() - started, 3)
        return report
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _maybe_safety_backup(
    *,
    config: BackupConfig,
    services: Services,
    dest: Path,
    kek: bytes,
    bundle: Path | None,
    no_safety: bool,
    target_id: str,
    staging_root: Path | None = None,
) -> str | None:
    if no_safety:
        return None
    live_dbs = services.postgres.list_databases()
    live_cols = services.qdrant.list_collections() if services.qdrant is not None else []
    if not live_dbs and not live_cols:
        services.echo("No live databases or collections; skipping safety snapshot.")
        return None
    services.echo("Taking a safety snapshot of the current state first...")
    safety = create_backup(
        config=config,
        services=services,
        dest=dest,
        kek=kek,
        kind="safety",
        note=f"auto-safety before restore of {target_id}",
        bundle=bundle,
        staging_root=staging_root,
    )
    return safety.id


def _apply_restore(
    config: BackupConfig,
    services: Services,
    manifest: BackupManifest,
    paths: dict[str, Path],
    restore_bundle_to: Path | None,
) -> None:
    _stop_containers(services, config.stop_containers)

    if _GLOBALS_NAME in paths:
        services.echo("  - restoring postgres globals")
        services.postgres.restore_globals(paths[_GLOBALS_NAME])
    for db in manifest.databases:
        services.echo(f"  - restoring database {db}")
        services.postgres.restore_database(db, paths[_db_artifact(db)])

    if services.qdrant is not None:
        for col in manifest.qdrant_collections:
            services.echo(f"  - restoring qdrant collection {col.name}")
            services.qdrant.upload_snapshot(col.name, paths[_qdrant_artifact(col.name)])

    if _BUNDLE_ARCNAME in paths and restore_bundle_to is not None:
        services.echo(f"  - restoring config bundle into {restore_bundle_to}")
        restore_bundle_to.mkdir(parents=True, exist_ok=True)
        _extract_tar(paths[_BUNDLE_ARCNAME], restore_bundle_to)

    for key in manifest.volumes:
        services.echo(f"  - restoring volume {key}")
        services.volumes.reset(key)
        services.volumes.extract(key, paths[_volume_artifact(key)])

    _start_containers(services, config.stop_containers)


def _extract_tar(src_tar: Path, dest: Path) -> None:
    import tarfile

    with tarfile.open(src_tar, "r") as tar:
        safe_extract(tar, dest)


def _verify_restore(services: Services, manifest: BackupManifest) -> dict[str, Any]:
    db_report: dict[str, int] = {}
    for db in manifest.databases:
        if services.postgres.scalar(db, "SELECT 1") != "1":
            raise RestoreError(f"post-restore connectivity check failed for database {db!r}")
        db_report[db] = services.postgres.user_table_count(db)
    col_report: dict[str, int] = {}
    if services.qdrant is not None:
        for col in manifest.qdrant_collections:
            actual = services.qdrant.points_count(col.name)
            if actual != col.points_count:
                raise RestoreError(
                    f"collection {col.name!r} restored {actual} points, "
                    f"manifest expected {col.points_count}"
                )
            col_report[col.name] = actual
    return {"databases": db_report, "qdrant_collections": col_report, "verified": True}


def _stop_containers(services: Services, names: list[str]) -> None:
    for name in names:
        services.echo(f"  - stopping container {name}")
        services.runner.run(["stop", name])


def _start_containers(services: Services, names: list[str]) -> None:
    for name in names:
        services.runner.run(["start", name], check=False)
