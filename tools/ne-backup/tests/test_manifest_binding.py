# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P2-7: the manifest is bound to the key material via the DEK-wrap AAD.

Any post-hoc edit of manifest.json — including silently dropping a database (and
its ArtifactRef) from the SET — fails closed at DEK unwrap in both restore and
verify. A pre-change (legacy) keyinfo still restores (NEB1 envelope back-compat).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest

from ne.backup import runtime as backup_runtime
from ne.backup.config import BackupConfig
from ne.backup.crypto import kek_fingerprint, unwrap_dek, wrap_dek
from ne.backup.errors import ManifestError, RestoreError
from ne.backup.runtime import (
    create_backup,
    create_verified_backup_receipt,
    restore_backup,
    verify_backup,
)

from .conftest import Harness, make_harness

_CONFIG = BackupConfig(stop_containers=["ne-pilot-keycloak-1"], volumes=["caddy-data"])


def _kek() -> bytes:
    return os.urandom(32)


def _make_backup(dest: Path, kek: bytes, harness: Harness, staging: Path) -> str:
    staging.mkdir(parents=True, exist_ok=True)
    manifest = create_backup(
        config=_CONFIG,
        services=harness.services(),
        dest=dest,
        kek=kek,
        kind="on-demand",
        staging_root=staging,
    )
    return manifest.id


def _restore_harness() -> Harness:
    return make_harness(databases=["auditdb", "neutron"], collections={"drill": 5})


def _two_db_harness() -> Harness:
    return make_harness(databases=["auditdb", "neutron"], collections={"drill": 5})


def _cutover_harness() -> Harness:
    return make_harness(databases=["ne_dbos", "neutron"], collections={"drill": 5})


def test_verified_receipt_is_derived_from_authenticated_backup(dest: Path, tmp_path: Path) -> None:
    kek = _kek()
    receipt = create_verified_backup_receipt(
        config=_CONFIG,
        services=_cutover_harness().services(),
        dest=dest,
        kek=kek,
        staging_root=tmp_path / "stage",
    )
    raw = (dest / receipt.backup_id / "manifest.json").read_bytes()
    assert receipt.manifest_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.kek_fingerprint == kek_fingerprint(kek)
    assert receipt.databases == ("ne_dbos", "neutron")
    assert receipt.verified is True
    parameters = inspect.signature(create_verified_backup_receipt).parameters
    assert "backup_id" not in parameters
    assert "receipt" not in parameters


def test_verified_receipt_requires_plaintext_decrypt_verification(
    dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_decrypt(*args: object, **kwargs: object) -> None:
        raise RestoreError("injected decrypt verification failure")

    monkeypatch.setattr(backup_runtime, "decrypt_file", reject_decrypt)
    with pytest.raises(RestoreError, match="decrypt verification failure"):
        create_verified_backup_receipt(
            config=_CONFIG,
            services=_cutover_harness().services(),
            dest=dest,
            kek=_kek(),
            staging_root=tmp_path / "stage",
        )


@pytest.mark.parametrize(
    "databases",
    [
        ["neutron"],
        ["ne_dbos"],
        ["neutron", "ne_dbos", "extra"],
        ["neutron", "ne_dbos", "ne_dbos"],
    ],
)
def test_verified_receipt_rejects_inexact_database_set(
    dest: Path, tmp_path: Path, databases: list[str]
) -> None:
    with pytest.raises(ManifestError, match="database identities"):
        create_verified_backup_receipt(
            config=_CONFIG,
            services=make_harness(databases=databases).services(),
            dest=dest,
            kek=_kek(),
            staging_root=tmp_path / "stage",
        )


def _rewrite_manifest(dest: Path, backup_id: str, mutate) -> None:
    path = dest / backup_id / "manifest.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def test_dropping_database_from_manifest_fails_restore_closed(dest: Path, tmp_path: Path) -> None:
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)

    def drop_auditdb(data: dict) -> None:
        data["databases"] = [d for d in data["databases"] if d != "auditdb"]
        data["artifacts"] = [a for a in data["artifacts"] if a["name"] != "pgdb-auditdb"]

    _rewrite_manifest(dest, backup_id, drop_auditdb)

    restore_h = _restore_harness()
    with pytest.raises(RestoreError, match="integrity"):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=kek,
            staging_root=stage,
        )
    assert restore_h.destructive_events() == []


def test_dropping_database_from_manifest_fails_verify(dest: Path, tmp_path: Path) -> None:
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)

    def drop_auditdb(data: dict) -> None:
        data["databases"] = [d for d in data["databases"] if d != "auditdb"]
        data["artifacts"] = [a for a in data["artifacts"] if a["name"] != "pgdb-auditdb"]

    _rewrite_manifest(dest, backup_id, drop_auditdb)
    with pytest.raises(RestoreError, match="integrity"):
        verify_backup(dest, backup_id, kek, staging_root=stage)


def test_ciphertext_tamper_fails_verify(dest: Path, tmp_path: Path) -> None:
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)
    artifact = next((dest / backup_id / "artifacts").iterdir())
    raw = bytearray(artifact.read_bytes())
    raw[-1] ^= 0x01
    artifact.write_bytes(raw)

    with pytest.raises(RestoreError, match="ciphertext hash mismatch"):
        verify_backup(dest, backup_id, kek, staging_root=stage)


def test_any_manifest_byte_edit_fails_restore(dest: Path, tmp_path: Path) -> None:
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)

    _rewrite_manifest(dest, backup_id, lambda d: d.__setitem__("note", "tampered"))

    restore_h = _restore_harness()
    with pytest.raises(RestoreError, match="integrity"):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=kek,
            staging_root=stage,
        )
    assert restore_h.destructive_events() == []


def test_stripping_binding_field_from_keyinfo_fails_closed(dest: Path, tmp_path: Path) -> None:
    # Downgrade attack: delete manifest_sha256 from keyinfo so the DEK is unwrapped
    # under the LEGACY AAD, but the DEK was v2-wrapped ⇒ InvalidTag ⇒ fail closed.
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)

    keyinfo_path = dest / backup_id / "keyinfo.json"
    keyinfo = json.loads(keyinfo_path.read_text())
    del keyinfo["manifest_sha256"]
    keyinfo_path.write_text(json.dumps(keyinfo))

    restore_h = _restore_harness()
    with pytest.raises(RestoreError, match="integrity"):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=kek,
            staging_root=stage,
        )
    assert restore_h.destructive_events() == []


def test_wrong_manifest_hash_in_keyinfo_fails_verify(dest: Path, tmp_path: Path) -> None:
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)
    path = dest / backup_id / "keyinfo.json"
    keyinfo = json.loads(path.read_text())
    keyinfo["manifest_sha256"] = "0" * 64
    path.write_text(json.dumps(keyinfo))

    with pytest.raises(RestoreError, match="manifest hash"):
        verify_backup(dest, backup_id, kek, staging_root=stage)


def test_foreign_backup_directory_id_fails_verify(dest: Path, tmp_path: Path) -> None:
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)
    foreign_id = "foreign-backup-id"
    (dest / backup_id).rename(dest / foreign_id)

    with pytest.raises(RestoreError, match="backup id mismatch"):
        verify_backup(dest, foreign_id, kek, staging_root=stage)


def test_legacy_pre_binding_backup_still_restores(dest: Path, tmp_path: Path) -> None:
    # Downgrade a v2 backup to the PRE-CHANGE format: legacy DEK AAD + keyinfo with
    # no manifest_sha256. It must still restore — proves NEB1 envelope back-compat.
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)

    keyinfo_path = dest / backup_id / "keyinfo.json"
    keyinfo = json.loads(keyinfo_path.read_text())
    manifest_raw = (dest / backup_id / "manifest.json").read_bytes()
    sha = hashlib.sha256(manifest_raw).hexdigest()
    dek = unwrap_dek(kek, keyinfo["wrapped_dek"], keyinfo["dek_nonce"], manifest_sha256=sha)
    legacy_wrapped, legacy_nonce = wrap_dek(kek, dek)  # no kwarg ⇒ legacy AAD
    keyinfo["wrapped_dek"] = legacy_wrapped
    keyinfo["dek_nonce"] = legacy_nonce
    del keyinfo["manifest_sha256"]
    keyinfo_path.write_text(json.dumps(keyinfo))

    restore_h = _restore_harness()
    report = restore_backup(
        config=_CONFIG,
        services=restore_h.services(),
        dest=dest,
        backup_id=backup_id,
        kek=kek,
        staging_root=stage,
    )
    assert report["verified"] is True


def test_wrong_kek_after_binding_still_reports_fingerprint(dest: Path, tmp_path: Path) -> None:
    kek = _kek()
    stage = tmp_path / "stage"
    backup_id = _make_backup(dest, kek, _two_db_harness(), stage)

    restore_h = _restore_harness()
    with pytest.raises(RestoreError, match="fingerprint"):
        restore_backup(
            config=_CONFIG,
            services=restore_h.services(),
            dest=dest,
            backup_id=backup_id,
            kek=os.urandom(32),  # wrong key: fingerprint check fires before unwrap
            staging_root=stage,
        )
    assert restore_h.destructive_events() == []
