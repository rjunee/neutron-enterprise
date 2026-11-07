# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P2-8: plaintext staging lives HOST-LOCAL (off the customer dest) and orphaned
staging dirs are age-guarded-swept by every create/restore/prune, so a crash can
never strand full-DB plaintext (or ciphertext scratch) on the backup dest forever.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ne.backup.config import BackupConfig
from ne.backup.crypto import decrypt_file as _real_decrypt_file
from ne.backup.errors import NeBackupError
from ne.backup.runtime import create_backup, prune_backups, restore_backup, verify_backup

from .conftest import Harness, make_harness

_CONFIG = BackupConfig(stop_containers=["ne-pilot-keycloak-1"], volumes=["caddy-data"])
_STALE = 7 * 3600  # older than ORPHAN_MIN_AGE_SECONDS (6h)


def _kek() -> bytes:
    return os.urandom(32)


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    return stage


def _aged_dir(path: Path, seconds_old: float) -> Path:
    path.mkdir(parents=True)
    (path / "dummy").write_text("plaintext-dump-fragment")
    stamp = time.time() - seconds_old
    os.utime(path, (stamp, stamp))
    return path


def _make_backup(dest: Path, kek: bytes, harness: Harness, stage: Path) -> str:
    manifest = create_backup(
        config=_CONFIG,
        services=harness.services(),
        dest=dest,
        kek=kek,
        kind="on-demand",
        staging_root=stage,
    )
    return manifest.id


def test_stale_partial_dir_reclaimed_by_create(dest: Path, tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    orphan = _aged_dir(dest / "20260101T000000Z-on-demand-aaaaaa.partial", _STALE)
    backup_id = _make_backup(dest, _kek(), make_harness(), stage)
    assert not orphan.exists()
    assert (dest / backup_id).is_dir()


def test_stale_restore_dir_reclaimed_by_prune(dest: Path, tmp_path: Path) -> None:
    orphan = _aged_dir(dest / ".restore-20260101T000000Z-on-demand-aaaaaa-x1y2", _STALE)
    prune_backups(dest, keep=7)
    assert not orphan.exists()


def test_fresh_partial_dir_survives_sweep(dest: Path, tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    fresh_partial = _aged_dir(dest / "20260101T000000Z-on-demand-bbbbbb.partial", 0)
    fresh_restore = _aged_dir(dest / ".restore-20260101T000000Z-on-demand-bbbbbb-z9", 0)
    _make_backup(dest, _kek(), make_harness(), stage)
    assert fresh_partial.exists()
    assert fresh_restore.exists()
    prune_backups(dest, keep=7)
    assert fresh_partial.exists()
    assert fresh_restore.exists()


def test_stale_host_staging_reclaimed_by_create(dest: Path, tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    orphan = _aged_dir(stage / "ne-backup-create-old-xyz", _STALE)
    _make_backup(dest, _kek(), make_harness(), stage)
    assert not orphan.exists()


def test_create_stages_plaintext_off_dest(dest: Path, tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    harness = make_harness()
    _make_backup(dest, _kek(), harness, stage)
    assert harness.postgres is not None
    dumps = harness.postgres.dump_dests
    assert dumps  # dumps actually happened
    for p in dumps:
        assert not str(p).startswith(str(dest))
        assert str(p).startswith(str(stage))


def test_restore_stages_plaintext_off_dest(dest: Path, tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness(), stage)
    restore_h = make_harness()
    restore_backup(
        config=_CONFIG,
        services=restore_h.services(),
        dest=dest,
        backup_id=backup_id,
        kek=kek,
        staging_root=stage,
    )
    assert restore_h.postgres is not None
    srcs = restore_h.postgres.restore_srcs
    assert srcs  # restores actually happened
    for p in srcs:
        assert not str(p).startswith(str(dest))


def test_safety_backup_stages_under_configured_staging_root(dest: Path, tmp_path: Path) -> None:
    """The auto-safety backup taken by restore must inherit staging_root — otherwise a
    configured disk-backed staging_dir is silently ignored for the safety dumps and
    they land in the system temp dir (tmpfs hazard the option exists to avoid)."""
    stage = _stage(tmp_path)
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness(), stage)
    restore_h = make_harness()
    restore_backup(
        config=_CONFIG,
        services=restore_h.services(),
        dest=dest,
        backup_id=backup_id,
        kek=kek,
        staging_root=stage,
    )
    assert restore_h.postgres is not None
    dumps = restore_h.postgres.dump_dests  # written by the auto-safety create_backup
    assert dumps  # a safety backup actually ran
    for p in dumps:
        assert str(p).startswith(str(stage))


def test_missing_staging_root_created_on_demand(dest: Path, tmp_path: Path) -> None:
    """A configured staging root that does not exist yet (e.g. /var/tmp/ne-backup on
    first run) is created 0700 instead of failing with FileNotFoundError."""
    stage = tmp_path / "not-yet" / "ne-backup"
    assert not stage.exists()
    backup_id = _make_backup(dest, _kek(), make_harness(), stage)
    assert (dest / backup_id).is_dir()
    assert stage.is_dir()


def test_no_host_staging_left_behind_on_success_and_failure(dest: Path, tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    _make_backup(dest, _kek(), make_harness(), stage)
    assert [e for e in stage.iterdir() if e.name.startswith("ne-backup-")] == []

    fail_h = make_harness(pg_fail=frozenset({"dump_database"}))
    with pytest.raises(NeBackupError):
        create_backup(
            config=_CONFIG,
            services=fail_h.services(),
            dest=dest,
            kek=_kek(),
            kind="on-demand",
            staging_root=stage,
        )
    assert [e for e in stage.iterdir() if e.name.startswith("ne-backup-")] == []


def test_verify_scratch_off_dest(dest: Path, tmp_path: Path, monkeypatch) -> None:
    stage = _stage(tmp_path)
    kek = _kek()
    backup_id = _make_backup(dest, kek, make_harness(), stage)

    outs: list[Path] = []

    def recording_decrypt(dek: bytes, name: str, src: Path, dst: Path) -> None:
        outs.append(dst)
        _real_decrypt_file(dek, name, src, dst)

    monkeypatch.setattr("ne.backup.runtime.decrypt_file", recording_decrypt)
    verify_backup(dest, backup_id, kek, staging_root=stage)
    assert outs  # decryption actually happened
    for p in outs:
        assert not str(p).startswith(str(dest))
