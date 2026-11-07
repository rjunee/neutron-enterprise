# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12: KEK and DEK material appear in NO produced backup file and NO captured
output — only the AESGCM-wrapped DEK (ciphertext) is ever persisted."""

from __future__ import annotations

import base64
import logging
import os
import stat
from pathlib import Path

import pytest

from ne.backup import runtime
from ne.backup.config import BackupConfig
from ne.backup.runtime import create_backup

from .conftest import make_harness

_FIXED_DEK = bytes(range(32))  # a known DEK so we can hunt for it everywhere


def _forms(material: bytes) -> list[bytes]:
    return [
        material,
        base64.b64encode(material),
        material.hex().encode(),
        material.hex().upper().encode(),
    ]


def test_key_material_absent_from_files_and_output(
    bundle: Path, dest: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runtime, "new_dek", lambda: _FIXED_DEK)
    kek = os.urandom(32)
    captured: list[str] = []
    harness = make_harness(databases=["neutron"], collections={"drill": 2})
    services = harness.services()
    services.echo = captured.append

    with caplog.at_level(logging.DEBUG):
        manifest = create_backup(
            config=BackupConfig(),
            services=services,
            dest=dest,
            kek=kek,
            kind="on-demand",
            bundle=bundle,
        )

    forbidden = _forms(kek) + _forms(_FIXED_DEK)
    snap_dir = dest / manifest.id
    for path in snap_dir.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        for form in forbidden:
            assert form not in data, f"key material leaked into {path.name}"

    combined = ("\n".join(captured) + "\n" + caplog.text).encode()
    for form in forbidden:
        assert form not in combined, "key material leaked into logs/output"

    # The excluded runtime/ secret never entered the object backup either.
    for path in snap_dir.rglob("*"):
        if path.is_file():
            assert b"super-secret-value" not in path.read_bytes()

    # keyinfo.json DOES carry the wrapped DEK (a ciphertext) and the fingerprint —
    # but neither is the raw key.
    keyinfo = (snap_dir / "keyinfo.json").read_text()
    assert "wrapped_dek" in keyinfo
    assert base64.b64encode(_FIXED_DEK).decode() not in keyinfo


def test_plaintext_staging_dir_is_0700(bundle: Path, dest: Path) -> None:
    """Codex NE-100 P2: plaintext DB dumps stage in tmp_dir BEFORE encryption, so
    that dir must be 0700 — otherwise other local users on the shared dest mount
    could read the unencrypted artifacts mid-backup."""
    harness = make_harness(databases=["neutron"], collections={"drill": 2})
    assert harness.postgres is not None
    seen: list[int] = []
    original = harness.postgres.dump_database

    def _spy(db: str, dst: Path) -> None:
        seen.append(stat.S_IMODE(dst.parent.stat().st_mode))
        original(db, dst)

    harness.postgres.dump_database = _spy  # type: ignore[method-assign]
    create_backup(
        config=BackupConfig(),
        services=harness.services(),
        dest=dest,
        kek=os.urandom(32),
        kind="on-demand",
        bundle=bundle,
    )
    assert seen, "dump_database was never called"
    assert all(mode == 0o700 for mode in seen), [oct(m) for m in seen]


# --- PRE-009A (A3): the Agent Run private-payload KEK is equally excluded ------


def test_no_agent_private_payload_key_material_reaches_a_backup(
    bundle: Path, dest: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C12 for PRE-009A's key, not only the backup KEK.

    A backup of a deployment running Agent Run private payloads must carry the
    WRAPPED envelopes and their key ids -- never the KEK that opens them. The
    private-payload key is a different key with a different lifecycle, so the
    existing backup-KEK sweep does not cover it and would keep passing while this
    material leaked.
    """
    private_kek = bytes(range(32, 64))  # distinct from the backup KEK and DEK
    key_id = "k7"
    captured: list[str] = []
    harness = make_harness(databases=["neutron"], collections={"drill": 2})
    services = harness.services()
    services.echo = captured.append

    os.environ["NE_AGENT_PRIVATE_PAYLOAD_KEY_ID"] = key_id
    os.environ["NE_AGENT_PRIVATE_PAYLOAD_KEK_B64"] = (
        base64.urlsafe_b64encode(private_kek).decode("ascii").rstrip("=")
    )
    try:
        with caplog.at_level(logging.DEBUG):
            manifest = create_backup(
                config=BackupConfig(),
                services=services,
                dest=dest,
                kek=os.urandom(32),
                kind="on-demand",
                bundle=bundle,
            )
    finally:
        os.environ.pop("NE_AGENT_PRIVATE_PAYLOAD_KEY_ID", None)
        os.environ.pop("NE_AGENT_PRIVATE_PAYLOAD_KEK_B64", None)

    forbidden = [
        *_forms(private_kek),
        base64.urlsafe_b64encode(private_kek).rstrip(b"="),
    ]
    snap_dir = dest / manifest.id
    for path in snap_dir.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        for form in forbidden:
            assert form not in data, f"{path} leaked private-payload key material"
    blob = (caplog.text + "\n".join(captured)).encode()
    for form in forbidden:
        assert form not in blob
    # Positive control: the sweep is excluding MATERIAL, not everything -- a key id
    # is deployment truth and is not among the forbidden forms.
    assert all(key_id.encode() != form for form in forbidden)
