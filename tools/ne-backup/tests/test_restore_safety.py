# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Restore is safe-by-construction: all validation precedes any destructive step;
the safety snapshot runs first; failures abort or recover cleanly."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ne.backup.config import BackupConfig
from ne.backup.errors import NeBackupError, RestoreError
from ne.backup.runtime import create_backup, restore_backup

from .conftest import Harness, make_harness

_CONFIG = BackupConfig(stop_containers=["ne-pilot-keycloak-1"], volumes=["caddy-data"])


def _kek() -> bytes:
    return os.urandom(32)


def _make_backup(dest: Path, kek: bytes, harness: Harness) -> str:
    manifest = create_backup(
        config=_CONFIG, services=harness.services(), dest=dest, kek=kek, kind="on-demand"
    )
    return manifest.id


def _fresh_restore_harness() -> Harness:
    return make_harness(databases=["neutron"], collections={"drill": 5})


def test_fingerprint_mismatch_aborts_before_destructive(dest: Path) -> None:
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness())
    restore_h = _fresh_restore_harness()
    before = len(restore_h.events)
    with pytest.raises(RestoreError, match="fingerprint"):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=os.urandom(32),  # wrong key
        )
    assert restore_h.destructive_events() == []
    # No service calls at all past the fingerprint check.
    assert len(restore_h.events) == before


def test_missing_artifact_aborts_before_destructive(dest: Path) -> None:
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness())
    (dest / backup_id / "artifacts" / "pg-globals.neb").unlink()
    restore_h = _fresh_restore_harness()
    with pytest.raises(RestoreError, match="missing artifact"):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=kek,
        )
    assert restore_h.destructive_events() == []


def test_ciphertext_tamper_aborts_before_destructive(dest: Path) -> None:
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness())
    art = dest / backup_id / "artifacts" / "pg-globals.neb"
    raw = bytearray(art.read_bytes())
    raw[-1] ^= 0x01
    art.write_bytes(bytes(raw))
    restore_h = _fresh_restore_harness()
    with pytest.raises(RestoreError, match="ciphertext hash mismatch"):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=kek,
        )
    assert restore_h.destructive_events() == []


def test_manifest_hash_field_tamper_aborts_before_destructive(dest: Path) -> None:
    # NOTE (behavior-change rename of test_plaintext_mismatch_aborts_before_destructive):
    # editing manifest.json now fails EARLIER — at the manifest-bound DEK unwrap
    # (P2-7) rather than at the later plaintext-hash check. Coverage does not shrink:
    # the tamper is still rejected with zero destructive events, just sooner.
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness())
    # Corrupt the manifest's recorded plaintext hash (ciphertext hash still valid).
    manifest_path = dest / backup_id / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["artifacts"][0]["plaintext_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(data))
    restore_h = _fresh_restore_harness()
    with pytest.raises(RestoreError, match="integrity"):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=kek,
        )
    assert restore_h.destructive_events() == []


def test_safety_snapshot_runs_before_first_destructive(dest: Path) -> None:
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness())
    restore_h = _fresh_restore_harness()
    report = restore_backup(
        config=_CONFIG,
        services=restore_h.services(),
        dest=dest,
        backup_id=backup_id,
        kek=kek,
    )
    assert report["safety_id"] is not None
    # A safety backup dir exists on disk.
    assert (dest / report["safety_id"]).is_dir()
    # The first destructive event is preceded by safety-backup dump activity.
    names = [e.name for e in restore_h.events]
    first_destructive = next(i for i, e in enumerate(restore_h.events) if e.destructive)
    assert "pg.dump_globals" in names[:first_destructive]  # the safety backup ran first


def test_safety_backup_failure_aborts(dest: Path) -> None:
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness())
    # Safety backup's postgres dump fails → restore must abort before destructive.
    restore_h = make_harness(
        databases=["neutron"], collections={"drill": 5}, pg_fail=frozenset({"dump_globals"})
    )
    with pytest.raises(NeBackupError):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=kek,
        )
    assert restore_h.destructive_events() == []


def test_partway_failure_recovers_and_restarts(dest: Path) -> None:
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness())
    # Destructive restore_database fails partway; recovery must name safety + restart.
    restore_h = make_harness(
        databases=["neutron"], collections={"drill": 5}, pg_fail=frozenset({"restore_database"})
    )
    with pytest.raises(NeBackupError):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=kek,
        )
    echoes = "\n".join(restore_h.echoes())
    assert "failed partway" in echoes
    assert "Recover the previous state" in echoes
    # A best-effort restart (docker start) was attempted.
    assert any(e.name == "docker.start" for e in restore_h.events)


def test_create_failure_leaves_no_partial(dest: Path) -> None:
    kek = _kek()
    harness = make_harness(databases=["neutron"], pg_fail=frozenset({"dump_database"}))
    with pytest.raises(NeBackupError):
        create_backup(
            config=_CONFIG, services=harness.services(), dest=dest, kek=kek, kind="on-demand"
        )
    assert list(dest.iterdir()) == []  # no <id> dir, no <id>.partial


def test_no_safety_skips_safety(dest: Path) -> None:
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness())
    restore_h = _fresh_restore_harness()
    report = restore_backup(
        config=_CONFIG,
        services=restore_h.services(),
        dest=dest,
        backup_id=backup_id,
        kek=kek,
        no_safety=True,
    )
    assert report["safety_id"] is None
