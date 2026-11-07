# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exact safety controls for the isolated encrypted-backup restore receipt."""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ne.cli.demo import isolated_restore as subject
from ne.runtime.legacy_transport_migration import Phase

_REAL_AUTHENTICATED_ARCHIVE_IDENTITIES = subject._authenticated_archive_identities

POSTGRES_IMAGE = "sha256:" + "a" * 64
QDRANT_IMAGE = "qdrant/qdrant:v1@sha256:" + "d" * 64
HELPER_IMAGE = subject._HELPER_IMAGE
SOURCE_REVISION = "e" * 40
HISTORICAL_CUTOVER_IMAGE = "sha256:" + "9" * 64
FINAL_RELEASE_IMAGE = "sha256:" + "6" * 64


def _fixture_repo_digest(image: str) -> str | None:
    if "@" not in image:
        return None
    repository, digest = image.rsplit("@", 1)
    if repository.rfind(":") > repository.rfind("/"):
        repository = repository[: repository.rfind(":")]
    return f"{repository}@{digest}"


class FakePort:
    def __init__(
        self,
        *,
        fail: str | None = None,
        drift: bool = False,
        foreign_image: str | None = None,
        cleanup_failure_once: bool = False,
        restore_override: dict[str, Any] | None = None,
        age_override: dict[str, Any] | None = None,
        empty_bundle: bool = False,
        wrong_bundle: bool = False,
        wrong_caddy: bool = False,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.owned: dict[str, list[str]] = {"container": [], "volume": [], "network": []}
        self.fail = fail
        self.drift = drift
        self.root_reads = 0
        self.config_text = ""
        self.foreign_image = foreign_image
        self.cleanup_failure_once = cleanup_failure_once
        self.restore_override = restore_override or {}
        self.age_override = age_override or {}
        self.empty_bundle = empty_bundle
        self.wrong_bundle = wrong_bundle
        self.wrong_caddy = wrong_caddy

    def run(self, argv: Sequence[str], *, capture: bool = False) -> str:
        call = tuple(argv)
        self.calls.append(call)
        if self.fail and self.fail in " ".join(call):
            raise RuntimeError("controlled failure")
        if call[:3] == ("docker", "network", "create"):
            self.owned["network"].append(call[-1])
        elif call[:3] == ("docker", "volume", "create"):
            self.owned["volume"].append(call[-1])
        elif call[:2] == ("docker", "run") and "--detach" in call:
            self.owned["container"].append(call[call.index("--name") + 1])
        elif call[:3] == ("docker", "container", "ls"):
            return "\n".join(self.owned["container"])
        elif call[:3] == ("docker", "volume", "ls"):
            return "\n".join(self.owned["volume"])
        elif call[:3] == ("docker", "network", "ls"):
            return "\n".join(self.owned["network"])
        elif call[:3] == ("docker", "inspect", "--format"):
            resource = call[-1]
            if resource.startswith("ne-pilot-"):
                self.root_reads += 1
                suffix = "drift" if self.drift and self.root_reads > 2 else "stable"
                return f"id|image|{suffix}\n"
            return call[4].split("=")[-1].strip("}") + "\n" if False else self._owner()
        elif call[:3] == ("docker", "image", "inspect"):
            image = call[-1]
            digest = image.rsplit("@", 1)[-1]
            repo_digest = _fixture_repo_digest(image)
            repo_digests = [] if repo_digest is None else [repo_digest]
            if image == self.foreign_image:
                if repo_digest is None:
                    digest = "sha256:" + "0" * 64
                else:
                    repo_digests = ["foreign.invalid/image@sha256:" + "0" * 64]
            label = SOURCE_REVISION if image == POSTGRES_IMAGE else ""
            return f"{json.dumps(repo_digests)}|{digest}|{label}\n"
        elif call[:3] == ("docker", "port", self._qdrant_name()):
            return "127.0.0.1:49152\n"
        elif call[:3] == ("docker", "exec", self._postgres_name()):
            if subject._AGE_SQL in call:
                return json.dumps(
                    {
                        "age_version": "1.7.0",
                        "graph_count": 1,
                        "vertex_count": 45,
                        "edge_count": 40,
                        "catalog_rows": [{"kind": "v", "graph": "1", "id": "3"}],
                        **self.age_override,
                    }
                )
            if "pg_isready" in call:
                return "accepting connections\n"
            if "psql" in call and any("pg_is_in_recovery" in item for item in call):
                return "1\n"
            return ""
        elif call[:3] == ("docker", "rm", "--force"):
            if self.cleanup_failure_once:
                self.cleanup_failure_once = False
                raise subprocess.CalledProcessError(1, call)
            self.owned["container"].remove(call[-1])
        elif call[:4] == ("docker", "volume", "rm", "--force"):
            self.owned["volume"].remove(call[-1])
        elif call[:3] == ("docker", "network", "rm"):
            self.owned["network"].remove(call[-1])
        elif "ne-backup" in call and "verify" in call:
            backup_id = call[call.index("verify") + 1]
            return "verified encrypted clone\n" + json.dumps(
                {
                    "id": backup_id,
                    "kind": "on-demand",
                    "artifacts": 6,
                    "ciphertext_verified": True,
                    "plaintext_verified": True,
                },
                indent=2,
            )
        elif "ne-backup" in call and "restore" in call:
            config = Path(call[call.index("--config") + 1])
            self.config_text = config.read_text(encoding="utf-8")
            target = Path(call[call.index("--restore-bundle-to") + 1])
            (target / "config").mkdir(parents=True)
            if not self.empty_bundle:
                payload = "wrong metadata\n" if self.wrong_bundle else "closed metadata\n"
                (target / "config" / "settings.toml").write_text(payload, encoding="utf-8")
            backup_id = call[call.index("restore") + 1]
            return json.dumps(
                {
                    "id": backup_id,
                    "verified": True,
                    "safety_id": None,
                    "databases": {"ne_dbos": 7, "neutron": 11},
                    "qdrant_collections": {"knowledge": 13},
                    "restore_seconds": 2.5,
                    **self.restore_override,
                }
            )
        elif call[:2] == ("docker", "run") and "--rm" in call:
            bind = next(item for item in call if "destination=/to" in item)
            workspace = Path(bind.split("source=", 1)[1].split(",destination=", 1)[0])
            source = workspace / "fake-caddy-source"
            source.mkdir()
            payload = "wrong certificate\n" if self.wrong_caddy else "certificate\n"
            (source / "root.pem").write_text(payload, encoding="utf-8")
            with tarfile.open(workspace / "observed-caddy.tar", "w") as archive:
                archive.add(source, arcname=".")
            return ""
        return ""

    def _owner(self) -> str:
        names = [*self.owned["container"], *self.owned["volume"], *self.owned["network"]]
        if not names:
            return ""
        name = names[0]
        return (
            name.split("ne-restore-", 1)[1]
            .split("-postgres", 1)[0]
            .split("-qdrant", 1)[0]
            .split("_caddy", 1)[0]
        )

    def _qdrant_name(self) -> str:
        return next((name for name in self.owned["container"] if name.endswith("-qdrant")), "")

    def _postgres_name(self) -> str:
        return next((name for name in self.owned["container"] if name.endswith("-postgres")), "")


def _fixture(
    tmp_path: Path, *, restart_image_digest: str = HISTORICAL_CUTOVER_IMAGE
) -> subject.RestoreInputs:
    backup_id = "pilot-backup-1"
    source = tmp_path / "authoritative"
    backup = source / backup_id
    artifacts = backup / "artifacts"
    artifacts.mkdir(parents=True)
    artifact_names = {
        "pg-globals",
        "pgdb-ne_dbos",
        "pgdb-neutron",
        "qdrant-knowledge",
        "bundle",
        "volume-caddy-data",
    }
    artifact_rows = []
    for name in sorted(artifact_names):
        encrypted = artifacts / f"{name}.enc"
        encrypted.write_bytes(f"ciphertext:{name}".encode())
        artifact_rows.append(
            {
                "name": name,
                "relpath": f"artifacts/{name}.enc",
                "ciphertext_sha256": hashlib.sha256(encrypted.read_bytes()).hexdigest(),
                "plaintext_sha256": "c" * 64,
                "size": encrypted.stat().st_size,
            }
        )
    manifest = {
        "id": backup_id,
        "created_at": "2026-08-23T00:00:00Z",
        "kind": "on-demand",
        "format_version": 1,
        "kek_fingerprint": "b" * 16,
        "databases": ["ne_dbos", "neutron"],
        "qdrant_collections": [{"name": "knowledge", "points_count": 13}],
        "artifacts": artifact_rows,
        "volumes": ["caddy-data"],
        "config_hashes": {},
        "durations": {},
    }
    manifest_path = backup / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    journal_data = {
        "phase": Phase.COMPLETE.value,
        "backup_id": backup_id,
        "generation": "agent-transport-v1",
        "image_digest": HISTORICAL_CUTOVER_IMAGE,
        "inventory_sha256": "8" * 64,
        "manifest_sha256": manifest_hash,
        "kek_fingerprint": "b" * 16,
        "fence": "7" * 64,
    }
    producer: Any = importlib.import_module("ne.cli.agent_transport")
    historical = producer._historical_cutover_projection(
        phase=Phase.COMPLETE,
        data=journal_data,
        version=len(Phase),
    )
    assert historical == {
        "backup_id_sha256": hashlib.sha256(backup_id.encode()).hexdigest(),
        "generation": "agent-transport-v1",
        "cutover_image_digest": HISTORICAL_CUTOVER_IMAGE,
        "inventory_sha256": "8" * 64,
        "journal_path": "pilot/agent-transport-cutover",
        "journal_version": len(Phase),
        "phase": "complete",
        "manifest_sha256": manifest_hash,
        "kek_fingerprint": "b" * 16,
        "retirement_fence": "7" * 64,
    }
    pre_identity = {
        "compose_config_hash": "1" * 64,
        "container_id": "2" * 64,
        "generation": "agent-transport-v1",
        "image_digest": restart_image_digest,
        "process_epoch": "3" * 64,
    }
    post_identity = {**pre_identity, "container_id": "4" * 64, "process_epoch": "5" * 64}
    body: dict[str, Any] = {
        "schema": "neutron.agent-transport.restart-receipt.v1",
        "historical_cutover": historical,
        "pre_roots": {"ne-api": pre_identity, "ne-worker": pre_identity},
        "post_roots": {"ne-api": post_identity, "ne-worker": post_identity},
    }
    receipt = producer._canonical_restart_receipt(body)
    cutover = tmp_path / "cutover.json"
    cutover.write_text(json.dumps(receipt), encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "secrets.ne.json").write_text("{}", encoding="utf-8")
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        f"x-ne-postgres-age-image: &pg {POSTGRES_IMAGE}\n"
        f"x-ne-postgres-age-source-revision: &rev {SOURCE_REVISION}\n"
        "services:\n"
        "  qdrant:\n"
        f"    image: {QDRANT_IMAGE}\n",
        encoding="utf-8",
    )
    return subject.RestoreInputs(
        backup_id=backup_id,
        source_dest=source,
        bundle=bundle,
        cutover_receipt=cutover,
        output_receipt=tmp_path / "isolated-receipt.json",
        compose_file=compose,
        postgres_image=POSTGRES_IMAGE,
        qdrant_image=QDRANT_IMAGE,
        helper_image=HELPER_IMAGE,
        upgrade_lock=tmp_path / "upgrade.lock",
        drill_lock=tmp_path / "drill.lock",
    )


@pytest.fixture(autouse=True)
def _authenticated_archive_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    def identities(_clone: Path, _bundle: Path, workspace: Path) -> dict[str, tuple[str, int, int]]:
        expected_bundle = workspace / "fixture-expected-bundle"
        (expected_bundle / "config").mkdir(parents=True)
        (expected_bundle / "config" / "settings.toml").write_text(
            "closed metadata\n", encoding="utf-8"
        )
        expected_caddy = workspace / "fixture-expected-caddy"
        expected_caddy.mkdir()
        (expected_caddy / "root.pem").write_text("certificate\n", encoding="utf-8")
        return {
            "bundle": subject._tree_identity(expected_bundle),
            "volume-caddy-data": subject._tree_identity(expected_caddy),
        }

    monkeypatch.setattr(subject, "_authenticated_archive_identities", identities)


def test_exact_isolated_restore_argv_config_parity_cleanup_and_receipt(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    port = FakePort()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subject, "_wait_ready", lambda *_: None)
        receipt = subject.execute(inputs, port=port)

    verify = next(call for call in port.calls if "verify" in call)
    restore = next(call for call in port.calls if "restore" in call)
    assert verify[0] == "env"
    assert verify[2:6] == ("uv", "run", "--all-packages", "ne-backup")
    assert restore[:6] == verify[:6]
    clone_dest = Path(restore[restore.index("--dest") + 1])
    assert clone_dest != inputs.source_dest
    assert inputs.source_dest.as_posix() not in restore
    assert restore[-2:] == ("--no-safety", "--yes")
    detached_runs = [call for call in port.calls if call[:2] == ("docker", "run")]
    assert detached_runs
    assert all(call[2:4] == ("--pull", "never") for call in detached_runs if "--detach" in call)
    helper_runs = [call for call in detached_runs if inputs.helper_image in call]
    assert helper_runs
    assert all("--pull" in call and "--network" in call and "none" in call for call in helper_runs)
    config_call_copy = tuple(restore)
    assert "ne-pilot" not in " ".join(
        item for item in config_call_copy if item not in {str(inputs.cutover_receipt)}
    )
    assert not any("stop" in call or "restart" in call for call in port.calls)
    assert 'postgres_container = "ne-restore-' in port.config_text
    assert "stop_containers = []" in port.config_text
    assert 'volumes = ["caddy-data"]' in port.config_text
    assert 'compose_project = "ne-restore-' in port.config_text
    assert port.owned == {"container": [], "volume": [], "network": []}
    assert receipt["verified"] is True
    assert receipt["database_table_counts"] == {"ne_dbos": 7, "neutron": 11}
    assert receipt["qdrant_point_counts"] == {"knowledge": 13}
    assert receipt["age"]["version"] == "1.7.0"
    assert receipt["age"]["vertex_count"] == 45
    assert receipt["caddy"]["file_count"] == 1
    assert receipt["restore_seconds"] == 2.5
    assert receipt["cleanup_complete"] is True
    assert set(receipt) == {
        "artifact_bytes",
        "artifact_count",
        "backup_id_sha256",
        "age",
        "bundle_artifact_sha256",
        "bundle_bytes",
        "bundle_files",
        "bundle_tree_sha256",
        "caddy",
        "caddy_artifact_sha256",
        "cleanup_complete",
        "cutover_receipt_sha256",
        "database_table_counts",
        "kek_fingerprint",
        "live_root_identity_sha256",
        "manifest_sha256",
        "qdrant_point_counts",
        "receipt_hash",
        "restore_seconds",
        "schema",
        "source_clone_sha256",
        "verified",
    }
    on_disk = json.loads(inputs.output_receipt.read_text())
    assert on_disk == receipt
    assert stat.S_IMODE(inputs.output_receipt.stat().st_mode) == 0o600
    serialized = inputs.output_receipt.read_text()
    assert inputs.backup_id not in serialized
    assert str(inputs.source_dest) not in serialized
    assert "pilot.env" not in serialized


def test_postgres_readiness_uses_terminal_tcp_server_without_target_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakePort()
    port.owned["container"] = ["ne-restore-owner-postgres"]
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: nullcontext())
    subject._wait_ready(port, "ne-restore-owner-postgres", 49152)
    postgres_calls = [call for call in port.calls if call[:2] == ("docker", "exec")]
    assert postgres_calls[0][-7:] == (
        "pg_isready",
        "--host",
        "127.0.0.1",
        "--username",
        "ne",
        "--dbname",
        "postgres",
    )
    assert any("pg_is_in_recovery" in item for item in postgres_calls[1])
    assert "neutron" not in postgres_calls[0] + postgres_calls[1]


def test_postgres_readiness_retries_until_terminal_primary_is_read_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeferredTerminalPort(FakePort):
        terminal_reads = 0

        def run(self, argv: Sequence[str], *, capture: bool = False) -> str:
            if "psql" in argv and any("pg_is_in_recovery" in item for item in argv):
                self.calls.append(tuple(argv))
                self.terminal_reads += 1
                return "0\n" if self.terminal_reads == 1 else "1\n"
            return super().run(argv, capture=capture)

    port = DeferredTerminalPort()
    port.owned["container"] = ["ne-restore-owner-postgres"]
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    subject._wait_ready(port, "ne-restore-owner-postgres", 49152)
    assert port.terminal_reads == 2


@pytest.mark.parametrize("field", ["postgres_image", "qdrant_image", "helper_image"])
def test_unpinned_image_refuses_before_docker(tmp_path: Path, field: str) -> None:
    inputs = replace(_fixture(tmp_path), **{field: "latest"})
    port = FakePort()
    with pytest.raises(subject.IsolatedRestoreError, match="digest pinned"):
        subject.execute(inputs, port=port)
    assert port.calls == []


def test_foreign_manifest_kek_or_cutover_receipt_refuses_before_mutation(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    receipt = json.loads(inputs.cutover_receipt.read_text())
    receipt["historical_cutover"]["manifest_sha256"] = "0" * 64
    body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    receipt["receipt_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inputs.cutover_receipt.write_text(json.dumps(receipt), encoding="utf-8")
    port = FakePort()
    with pytest.raises(subject.IsolatedRestoreError, match="does not bind"):
        subject.execute(inputs, port=port)
    assert port.calls == []


def test_restart_receipt_admits_final_image_distinct_from_historical_cutover(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path, restart_image_digest=FINAL_RELEASE_IMAGE)

    values = subject._require_inputs(inputs)

    receipt = json.loads(inputs.cutover_receipt.read_text())
    assert receipt["historical_cutover"]["cutover_image_digest"] == HISTORICAL_CUTOVER_IMAGE
    for side in ("pre_roots", "post_roots"):
        for identity in receipt[side].values():
            assert identity["image_digest"] == FINAL_RELEASE_IMAGE
    assert values[5] == receipt["receipt_hash"]


@pytest.mark.parametrize(
    ("side", "root"),
    [("pre_roots", "ne-worker"), ("post_roots", "ne-api")],
)
def test_restart_receipt_refuses_mixed_current_root_image_digests(
    tmp_path: Path, side: str, root: str
) -> None:
    inputs = _fixture(tmp_path, restart_image_digest=FINAL_RELEASE_IMAGE)
    receipt = json.loads(inputs.cutover_receipt.read_text())
    receipt[side][root]["image_digest"] = "sha256:" + "5" * 64
    body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    receipt["receipt_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inputs.cutover_receipt.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(subject.IsolatedRestoreError, match="root image identity is inconsistent"):
        subject._require_inputs(inputs)


def test_restart_receipt_refuses_per_root_image_digest_divergence(tmp_path: Path) -> None:
    worker_image = "sha256:" + "5" * 64
    inputs = _fixture(tmp_path, restart_image_digest=FINAL_RELEASE_IMAGE)
    receipt = json.loads(inputs.cutover_receipt.read_text())
    for side in ("pre_roots", "post_roots"):
        receipt[side]["ne-worker"]["image_digest"] = worker_image
    body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    receipt["receipt_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inputs.cutover_receipt.write_text(json.dumps(receipt), encoding="utf-8")

    # Every per-root and per-side pairing is self-consistent and well formed, so only the
    # cross-root check can refuse this: the fleet is split across two current images.
    assert receipt["pre_roots"]["ne-api"]["image_digest"] == FINAL_RELEASE_IMAGE
    assert receipt["post_roots"]["ne-api"]["image_digest"] == FINAL_RELEASE_IMAGE
    assert receipt["pre_roots"]["ne-worker"]["image_digest"] == worker_image
    assert receipt["post_roots"]["ne-worker"]["image_digest"] == worker_image
    assert worker_image != FINAL_RELEASE_IMAGE

    port = FakePort()
    with pytest.raises(subject.IsolatedRestoreError, match="root image identity is inconsistent"):
        subject.execute(inputs, port=port)
    assert port.calls == []


def test_restart_receipt_refuses_uniformly_malformed_root_image_digest(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path, restart_image_digest="not-a-digest-at-all")
    receipt = json.loads(inputs.cutover_receipt.read_text())

    # All four roots carry the same malformed value, so neither the cross-root consistency
    # check nor the pre/post image equality can fire: only the FORMAT guard refuses this.
    assert {
        receipt[side][root]["image_digest"]
        for side in ("pre_roots", "post_roots")
        for root in ("ne-api", "ne-worker")
    } == {"not-a-digest-at-all"}

    port = FakePort()
    with pytest.raises(subject.IsolatedRestoreError, match="root identity is foreign"):
        subject.execute(inputs, port=port)
    assert port.calls == []


@pytest.mark.parametrize(
    "damage",
    [
        "missing-root",
        "unchanged-epoch",
        "foreign-generation",
        "missing-retirement-fence",
        "malformed-root-image",
    ],
)
def test_full_production_cutover_shape_and_advanced_roots_are_mandatory(
    tmp_path: Path, damage: str
) -> None:
    inputs = _fixture(tmp_path)
    receipt = json.loads(inputs.cutover_receipt.read_text())
    if damage == "missing-root":
        del receipt["post_roots"]["ne-api"]
    elif damage == "unchanged-epoch":
        receipt["post_roots"]["ne-api"]["process_epoch"] = receipt["pre_roots"]["ne-api"][
            "process_epoch"
        ]
    elif damage == "foreign-generation":
        receipt["historical_cutover"]["generation"] = "agent-transport-v0"
    elif damage == "missing-retirement-fence":
        del receipt["historical_cutover"]["retirement_fence"]
    else:
        receipt["pre_roots"]["ne-api"]["image_digest"] = "sha256:" + "z" * 64
    body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    receipt["receipt_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inputs.cutover_receipt.write_text(json.dumps(receipt), encoding="utf-8")
    port = FakePort()
    with pytest.raises(subject.IsolatedRestoreError, match="PRE-009C"):
        subject.execute(inputs, port=port)
    assert port.calls == []


def test_incomplete_caddy_bundle_or_age_artifact_inventory_refuses_before_mutation(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    manifest_path = inputs.source_dest / inputs.backup_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"] = [
        artifact for artifact in manifest["artifacts"] if artifact["name"] != "volume-caddy-data"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    # Keep PRE-009C bound to the modified manifest so this test discriminates the
    # complete topology inventory rather than merely tripping the hash join.
    receipt = json.loads(inputs.cutover_receipt.read_text())
    receipt["historical_cutover"]["manifest_sha256"] = hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    receipt["receipt_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inputs.cutover_receipt.write_text(json.dumps(receipt), encoding="utf-8")
    port = FakePort()
    with pytest.raises(subject.IsolatedRestoreError, match="artifact inventory"):
        subject.execute(inputs, port=port)
    assert port.calls == []


@pytest.mark.parametrize("lock_field", ["upgrade_lock", "drill_lock"])
def test_both_serial_locks_refuse_before_live_root_or_docker_mutation(
    tmp_path: Path, lock_field: str
) -> None:
    inputs = _fixture(tmp_path)
    lock = getattr(inputs, lock_field)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        port = FakePort()
        with pytest.raises(subject.IsolatedRestoreError, match="already held"):
            subject.execute(inputs, port=port)
        assert port.calls == []
    finally:
        os.close(descriptor)


def test_failure_cleans_only_owned_resources_and_never_writes_receipt(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    port = FakePort(fail="ne-backup restore")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subject, "_wait_ready", lambda *_: None)
        with pytest.raises(RuntimeError, match="controlled failure"):
            subject.execute(inputs, port=port)
    assert port.owned == {"container": [], "volume": [], "network": []}
    assert not inputs.output_receipt.exists()
    assert not any("prune" in call for call in port.calls)


def test_decrypt_verify_failure_occurs_before_any_docker_target_mutation(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    port = FakePort(fail="ne-backup verify")
    with pytest.raises(RuntimeError, match="controlled failure"):
        subject.execute(inputs, port=port)
    mutating_docker = [
        call
        for call in port.calls
        if call[:3]
        in {
            ("docker", "network", "create"),
            ("docker", "volume", "create"),
        }
        or call[:2] == ("docker", "run")
    ]
    assert mutating_docker == []
    assert not inputs.output_receipt.exists()


def test_live_root_drift_fails_closed_after_cleanup(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    port = FakePort(drift=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subject, "_wait_ready", lambda *_: None)
        with pytest.raises(subject.IsolatedRestoreError, match="live Pilot root identity"):
            subject.execute(inputs, port=port)
    assert port.owned == {"container": [], "volume": [], "network": []}
    assert not inputs.output_receipt.exists()


def test_signal_style_interruption_cleans_every_owned_resource_and_keeps_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    port = FakePort()

    def interrupt_after_targets(*_args: Any) -> None:
        raise subject.IsolatedRestoreError("isolated restore interrupted by signal")

    monkeypatch.setattr(subject, "_wait_ready", interrupt_after_targets)
    with pytest.raises(subject.IsolatedRestoreError, match="interrupted by signal"):
        subject.execute(inputs, port=port)
    assert port.owned == {"container": [], "volume": [], "network": []}
    assert not inputs.output_receipt.exists()
    assert subject._journal_path(inputs.output_receipt).exists()


def test_existing_receipt_refuses_without_overwrite(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    inputs.output_receipt.write_text("sentinel", encoding="utf-8")
    port = FakePort()
    with pytest.raises(subject.IsolatedRestoreError, match="already exists"):
        subject.execute(inputs, port=port)
    assert inputs.output_receipt.read_text() == "sentinel"
    assert port.calls == []


def test_clone_integrity_mutation_refuses_before_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    subject_module: Any = subject
    real_copytree = subject_module.shutil.copytree

    depth = 0

    def corrupting_copytree(source: Path, target: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal depth
        depth += 1
        result = real_copytree(source, target, *args, **kwargs)
        if depth == 1:
            (target / "manifest.json").write_text("{}", encoding="utf-8")
        depth -= 1
        return result

    monkeypatch.setattr(subject_module.shutil, "copytree", corrupting_copytree)
    port = FakePort()
    with pytest.raises(subject.IsolatedRestoreError, match="clone integrity"):
        subject.execute(inputs, port=port)
    assert not any("ne-backup" in call for call in port.calls)


def test_module_is_unregistered_and_has_exact_main_guardian() -> None:
    demo_init = Path(subject.__file__).with_name("__init__.py").read_text(encoding="utf-8")
    assert "isolated_restore" not in demo_init
    assert subject.__name__ == "ne.cli.demo.isolated_restore"
    invoked = subprocess.run(
        [sys.executable, "-m", "ne.cli.demo.isolated_restore", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert invoked.returncode == 0
    assert "--cutover-receipt" in invoked.stdout
    assert "--compose-file" in invoked.stdout


@pytest.mark.parametrize(
    "raw",
    [
        "progress\n{}\n{}\n",
        "progress\n{bad}\n",
        "progress\n{}\ntrailing",
        "[]",
        "",
    ],
)
def test_final_json_extractor_rejects_multiple_malformed_or_trailing_output(raw: str) -> None:
    with pytest.raises(subject.IsolatedRestoreError):
        subject._extract_final_json(raw, operation="test")


def test_final_json_extractor_accepts_progress_and_one_pretty_object() -> None:
    assert subject._extract_final_json(
        'progress one\nprogress two\n{\n  "ok": true\n}\n', operation="test"
    ) == {"ok": True}


def test_valid_but_unadmitted_or_nonlocal_images_refuse_before_target_mutation(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    wrong = replace(inputs, postgres_image="sha256:" + "6" * 64)
    with pytest.raises(subject.IsolatedRestoreError, match="admitted Pilot image"):
        subject.execute(wrong, port=FakePort())

    port = FakePort(foreign_image=inputs.qdrant_image)
    with pytest.raises(subject.IsolatedRestoreError, match="local Qdrant image"):
        subject.execute(inputs, port=port)
    assert not any(call[:2] == ("docker", "run") for call in port.calls)


@pytest.mark.parametrize(
    ("port", "message"),
    [
        (FakePort(restore_override={"databases": {"neutron": 1}}), "restore parity"),
        (FakePort(restore_override={"qdrant_collections": {}}), "restore parity"),
        (FakePort(restore_override={"restore_seconds": 0}), "restore parity"),
        (FakePort(restore_override={"restore_seconds": 1800.001}), "restore parity"),
        (FakePort(age_override={"age_version": "1.6.0"}), "AGE parity"),
        (FakePort(age_override={"vertex_count": 0}), "AGE parity"),
        (FakePort(wrong_caddy=True), "caddy parity"),
        (FakePort(wrong_bundle=True), "bundle parity"),
        (FakePort(empty_bundle=True), "bundle parity"),
    ],
)
def test_every_database_qdrant_age_bundle_caddy_and_rto_leg_is_mandatory(
    tmp_path: Path, port: FakePort, message: str
) -> None:
    inputs = _fixture(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subject, "_wait_ready", lambda *_: None)
        with pytest.raises(subject.IsolatedRestoreError, match=message):
            subject.execute(inputs, port=port)
    assert port.owned == {"container": [], "volume": [], "network": []}
    assert not inputs.output_receipt.exists()


def test_documented_restore_rto_boundary_is_inclusive(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    port = FakePort(restore_override={"restore_seconds": 1800.0})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subject, "_wait_ready", lambda *_: None)
        receipt = subject.execute(inputs, port=port)
    assert receipt["restore_seconds"] == 1800.0


def test_cleanup_failure_is_retried_leaves_zero_residue_and_no_receipt(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    port = FakePort(cleanup_failure_once=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subject, "_wait_ready", lambda *_: None)
        with pytest.raises(subject.IsolatedRestoreError, match="cleanup"):
            subject.execute(inputs, port=port)
    assert port.owned == {"container": [], "volume": [], "network": []}
    assert not inputs.output_receipt.exists()
    assert subject._journal_path(inputs.output_receipt).exists()


def test_stable_journal_recovers_sigkill_style_stopped_resources_before_new_run(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    values = subject._require_inputs(inputs)
    manifest_hash, cutover_hash = values[1], values[5]
    owner = hashlib.sha256(
        b"neutron.isolated-restore.owner.v1\0"
        + manifest_hash.encode()
        + b"\0"
        + cutover_hash.encode()
    ).hexdigest()[:32]
    journal = subject._journal_path(inputs.output_receipt)
    subject._atomic_receipt(journal, {"schema": subject._JOURNAL_SCHEMA, "owner": owner})
    port = FakePort()
    port.owned = {
        "container": [f"ne-restore-{owner}-postgres"],
        "volume": [f"ne-restore-{owner}_caddy-data"],
        "network": [f"ne-restore-{owner}"],
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subject, "_wait_ready", lambda *_: None)
        receipt = subject.execute(inputs, port=port)
    assert receipt["cleanup_complete"] is True
    assert port.owned == {"container": [], "volume": [], "network": []}
    assert not journal.exists()
    container_censuses = [call for call in port.calls if call[:3] == ("docker", "container", "ls")]
    assert container_censuses
    assert all("--all" in call for call in container_censuses)


def test_backup_helper_wrapper_is_local_no_pull_and_no_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    wrapper = subject._docker_wrapper(tmp_path, HELPER_IMAGE)
    text = wrapper.read_text(encoding="utf-8")
    assert "args[1:1] = ['--pull', 'never', '--network', 'none']" in text
    assert "if helper not in args" in text
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o700


def test_authenticated_archives_are_independently_safe_extracted_and_identified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    artifacts = clone / "artifacts"
    artifacts.mkdir(parents=True)
    archive_sources = tmp_path / "archives"
    archive_sources.mkdir()
    bundle_source = tmp_path / "bundle-source"
    (bundle_source / "config").mkdir(parents=True)
    (bundle_source / "config" / "settings.toml").write_text("closed\n", encoding="utf-8")
    caddy_source = tmp_path / "caddy-source"
    caddy_source.mkdir()
    (caddy_source / "root.pem").write_text("certificate\n", encoding="utf-8")
    bundle_tar = archive_sources / "bundle.tar"
    caddy_tar = archive_sources / "caddy.tar"
    with tarfile.open(bundle_tar, "w") as archive:
        archive.add(bundle_source, arcname="bundle")
    with tarfile.open(caddy_tar, "w") as archive:
        archive.add(caddy_source, arcname=".")
    source_by_name = {"bundle": bundle_tar, "volume-caddy-data": caddy_tar}
    rows = []
    for name, plaintext in source_by_name.items():
        encrypted = artifacts / f"{name}.neb"
        encrypted.write_bytes(b"authenticated-ciphertext-placeholder")
        rows.append(
            {
                "name": name,
                "relpath": f"artifacts/{name}.neb",
                "ciphertext_sha256": hashlib.sha256(encrypted.read_bytes()).hexdigest(),
                "plaintext_sha256": hashlib.sha256(plaintext.read_bytes()).hexdigest(),
                "size": encrypted.stat().st_size,
            }
        )
    manifest = {
        "id": "backup",
        "created_at": "2026-08-24T00:00:00Z",
        "kind": "on-demand",
        "format_version": 1,
        "kek_fingerprint": "b" * 16,
        "databases": [],
        "qdrant_collections": [],
        "artifacts": rows,
        "volumes": ["caddy-data"],
        "config_hashes": {},
        "durations": {},
    }
    (clone / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def decrypt(_dek: bytes, name: str, _source: Path, destination: Path) -> None:
        shutil.copyfile(source_by_name[name], destination)

    monkeypatch.setattr("ne.backup.keys.resolve_kek", lambda _bundle: b"k" * 32)
    monkeypatch.setattr("ne.backup.runtime._unwrap_backup_dek", lambda *_args, **_kwargs: b"d")
    monkeypatch.setattr("ne.backup.crypto.decrypt_file", decrypt)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identities = _REAL_AUTHENTICATED_ARCHIVE_IDENTITIES(
        clone,
        tmp_path / "bundle",
        workspace,
    )
    assert identities["bundle"][1:] == (1, 7)
    assert identities["volume-caddy-data"][1:] == (1, 12)

    unsafe = archive_sources / "unsafe.tar"
    with tarfile.open(unsafe, "w") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = 0
        archive.addfile(member)
    source_by_name["volume-caddy-data"] = unsafe
    rows[1]["plaintext_sha256"] = hashlib.sha256(unsafe.read_bytes()).hexdigest()
    (clone / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    unsafe_workspace = tmp_path / "unsafe-workspace"
    unsafe_workspace.mkdir()
    with pytest.raises(subject.IsolatedRestoreError, match="authenticated archive parity"):
        _REAL_AUTHENTICATED_ARCHIVE_IDENTITIES(
            clone,
            tmp_path / "bundle",
            unsafe_workspace,
        )


def test_authoritative_source_drift_after_cleanup_withholds_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    port = FakePort()
    real_cleanup = subject._cleanup
    changed = False

    def drifting_cleanup(command: FakePort, owner: str) -> bool:
        nonlocal changed
        result = real_cleanup(command, owner)
        if not changed:
            changed = True
            (inputs.source_dest / inputs.backup_id / "manifest.json").write_text(
                "{}", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(subject, "_cleanup", drifting_cleanup)
    monkeypatch.setattr(subject, "_wait_ready", lambda *_: None)
    with pytest.raises(subject.IsolatedRestoreError, match="authoritative encrypted backup"):
        subject.execute(inputs, port=port)
    assert not inputs.output_receipt.exists()


def test_cli_error_is_content_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        subject,
        "execute",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("/secret/backup-id")),
    )
    result = subject.main(
        [
            "--backup-id",
            "secret-id",
            "--source-dest",
            str(tmp_path / "secret-source"),
            "--bundle",
            str(tmp_path / "secret-bundle"),
            "--cutover-receipt",
            str(tmp_path / "secret-cutover"),
            "--output-receipt",
            str(tmp_path / "out"),
            "--compose-file",
            str(tmp_path / "compose"),
            "--postgres-image",
            POSTGRES_IMAGE,
            "--qdrant-image",
            QDRANT_IMAGE,
            "--helper-image",
            HELPER_IMAGE,
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "isolated restore refused\n"
    assert "secret" not in captured.out + captured.err
