# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi @ 4b0d12f3ed0e (MIT, Copyright (C) 2025
# Naas.ai): select_prunable / detect_drift / managed-volume-allowlist test ideas.
"""Manifest parse-time allowlists, drift, and prune selection."""

from __future__ import annotations

import pytest

from ne.backup.errors import ManifestError
from ne.backup.manifest import (
    PILOT_CUTOVER_DATABASES,
    ArtifactRef,
    BackupManifest,
    VerifiedBackupReceipt,
    detect_drift,
    select_prunable,
)


def _manifest(**over: object) -> BackupManifest:
    base: dict[str, object] = dict(
        id="20260707T000000Z-on-demand-abcdef",
        created_at="2026-07-07T00:00:00+00:00",
        kind="on-demand",
        kek_fingerprint="00112233aabbccdd",
        databases=["neutron"],
        volumes=["caddy-data"],
    )
    base.update(over)
    return BackupManifest(**base)  # type: ignore[arg-type]


def test_roundtrip_determinism() -> None:
    m = _manifest(
        artifacts=[
            ArtifactRef(
                name="pg-globals",
                relpath="artifacts/pg-globals.neb",
                ciphertext_sha256="a",
                plaintext_sha256="b",
                size=1,
            ),
        ],
    )
    once = m.to_json()
    twice = BackupManifest.from_json(once).to_json()
    assert once == twice


def test_unknown_volume_rejected() -> None:
    raw = _manifest(volumes=["caddy-data"]).to_json().replace('"caddy-data"', '"pg-data"')
    with pytest.raises(ManifestError):
        BackupManifest.from_json(raw)


def test_sql_injection_database_name_rejected() -> None:
    raw = _manifest().to_json().replace('"neutron"', '"x\\"; DROP TABLE--"')
    with pytest.raises(ManifestError):
        BackupManifest.from_json(raw)


def test_duplicate_database_identity_is_preserved_for_runtime_rejection() -> None:
    manifest = _manifest(databases=["neutron", "neutron"])
    assert manifest.databases == ["neutron", "neutron"]


def test_verified_receipt_has_exact_content_free_machine_shape() -> None:
    receipt = VerifiedBackupReceipt(
        backup_id="20260707T000000Z-on-demand-abcdef",
        manifest_sha256="a" * 64,
        kek_fingerprint="b" * 16,
        databases=PILOT_CUTOVER_DATABASES,
    )
    assert receipt.model_dump(mode="json") == {
        "backup_id": "20260707T000000Z-on-demand-abcdef",
        "manifest_sha256": "a" * 64,
        "kek_fingerprint": "b" * 16,
        "verified": True,
        "databases": ["ne_dbos", "neutron"],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_sha256", "foreign"),
        ("kek_fingerprint", "not-a-fingerprint"),
        ("verified", False),
        ("databases", ("neutron",)),
        ("databases", ("auditdb", "ne_dbos", "neutron")),
    ],
)
def test_verified_receipt_rejects_unbound_or_inexact_fields(field: str, value: object) -> None:
    fields: dict[str, object] = {
        "backup_id": "20260707T000000Z-on-demand-abcdef",
        "manifest_sha256": "a" * 64,
        "kek_fingerprint": "b" * 16,
        "databases": PILOT_CUTOVER_DATABASES,
    }
    fields[field] = value
    with pytest.raises(ValueError):
        VerifiedBackupReceipt(**fields)  # type: ignore[arg-type]


def test_traversal_artifact_name_rejected() -> None:
    good = _manifest(
        artifacts=[
            ArtifactRef(
                name="ok",
                relpath="artifacts/ok.neb",
                ciphertext_sha256="a",
                plaintext_sha256="b",
                size=1,
            )
        ]
    ).to_json()
    for evil in ('"../evil"', '"a/b"'):
        raw = good.replace('"ok"', evil, 1)
        with pytest.raises(ManifestError):
            BackupManifest.from_json(raw)


def test_traversal_relpath_rejected() -> None:
    raw = (
        _manifest(
            artifacts=[
                ArtifactRef(
                    name="ok",
                    relpath="artifacts/ok.neb",
                    ciphertext_sha256="a",
                    plaintext_sha256="b",
                    size=1,
                )
            ]
        )
        .to_json()
        .replace("artifacts/ok.neb", "artifacts/../ok.neb")
    )
    with pytest.raises(ManifestError):
        BackupManifest.from_json(raw)


def test_detect_drift() -> None:
    m = _manifest(config_hashes={"bundle/manifest.json": "aaa", "compose.yaml": "bbb"})
    assert detect_drift(m, {"bundle/manifest.json": "aaa", "compose.yaml": "bbb"}) == []
    warnings = detect_drift(m, {"bundle/manifest.json": "CHANGED", "compose.yaml": "bbb"})
    assert len(warnings) == 1
    assert "bundle/manifest.json" in warnings[0]


def _dated(created_at: str, kind: str) -> BackupManifest:
    return _manifest(id=f"id-{created_at}-{kind}", created_at=created_at, kind=kind)


def test_select_prunable_keeps_newest_and_newest_safety() -> None:
    manifests = [
        _dated("2026-07-07T05:00:00+00:00", "on-demand"),
        _dated("2026-07-07T04:00:00+00:00", "scheduled"),
        _dated("2026-07-07T03:00:00+00:00", "scheduled"),
        _dated("2026-07-07T02:00:00+00:00", "safety"),
        _dated("2026-07-07T01:00:00+00:00", "scheduled"),
    ]
    prunable = select_prunable(manifests, keep=2)
    # keep the 2 newest (05, 04); would prune 03, safety-02, 01 — but the newest
    # safety (02) is spared.
    assert "id-2026-07-07T03:00:00+00:00-scheduled" in prunable
    assert "id-2026-07-07T01:00:00+00:00-scheduled" in prunable
    assert "id-2026-07-07T02:00:00+00:00-safety" not in prunable
    assert "id-2026-07-07T05:00:00+00:00-on-demand" not in prunable


def test_select_prunable_negative_keep_raises() -> None:
    with pytest.raises(ManifestError):
        select_prunable([], keep=-1)
