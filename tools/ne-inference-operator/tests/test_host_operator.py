# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import socket
import stat
import struct
import sys
import tempfile
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from ne.api.inference_lifecycle import UdsInferenceTargetOperator
from ne.contracts.inference import (
    InferenceOperatorCode,
    InferenceOperatorObservation,
    InferenceStartRequest,
    InferenceStatusRequest,
    InferenceStopRequest,
    InferenceTargetOperation,
    InferenceTargetState,
)
from ne.inference_operator import cli as cli_module
from ne.inference_operator.cli import _air_gap_from_environment, _air_gap_from_file
from ne.inference_operator.journal import JournalRefused, StateJournal
from ne.inference_operator.manifest import (
    AdapterPolicy,
    ManifestRefused,
    PrivateManifest,
    RegistryTarget,
    VerifiedFile,
)
from ne.inference_operator.manifest import (
    load_private_manifest as _load_private_manifest,
)
from ne.inference_operator.operator import HostInferenceTargetOperator
from ne.inference_operator.provider_broker import BrokerResult
from ne.inference_operator.runner import PinnedRunner, RunResult
from ne.inference_operator.server import OperatorServer, acquire_lifetime_lock
from ne.inference_operator.vast_bundle import VastBundleRefused, validate_vast_bundle


def _write(path: Path, content: bytes, mode: int) -> None:
    if path.exists() and not path.is_symlink():
        path.chmod(0o700)
    path.write_bytes(content)
    path.chmod(mode)


def _documents(tmp_path: Path) -> tuple[Path, Path, Path]:
    (tmp_path / "bin").mkdir()
    vast_root = tmp_path / "vendor/vastai"
    (vast_root / "bin").mkdir(parents=True)
    (vast_root / "wheelhouse").mkdir()
    (vast_root / "site-packages/fake").mkdir(parents=True)
    executable = tmp_path / "bin/cpu-vllm-target.sh"
    _write(executable, b"#!/bin/sh\nexit 0\n", 0o500)
    vast = tmp_path / "bin/vast-rig.sh"
    cli = tmp_path / "vendor/vastai/bin/vastai"
    _write(vast, b"#!/bin/sh\nexit 0\n", 0o500)
    cli_bytes = b"#!/bin/sh\nexit 0\n"
    _write(cli, cli_bytes, 0o500)
    package = b"SAFE = True\n"
    wheel_name = "fake-1.0-py3-none-any.whl"
    wheel = vast_root / "wheelhouse" / wheel_name
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fake/__init__.py", package)
    wheel.chmod(0o400)
    _write(vast_root / "site-packages/fake/__init__.py", package, 0o400)
    lock = vast_root / "offline-lock.json"
    lock.write_text(
        json.dumps(
            {
                "python": {
                    "executable": "/usr/bin/python3.12",
                    "isolated_flags": "-IS",
                    "requires": ">=3.12,<3.14",
                },
                "schema": "ne-vastai-offline-lock-v1",
                "upstream": {
                    "commit": "bc7356483dd0f922ee17975c5357741f0d5d81fc",
                    "installed_sha256": hashlib.sha256(cli_bytes).hexdigest(),
                    "name": "vast-cli",
                    "source_sha256": "a" * 64,
                    "tag": "v1.5.2",
                    "version": "1.5.2",
                },
                "wheels": [
                    {
                        "filename": wheel_name,
                        "name": "fake",
                        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                        "version": "1.0",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    lock.chmod(0o400)
    for directory in (
        vast_root,
        vast_root / "bin",
        vast_root / "wheelhouse",
        vast_root / "site-packages",
        vast_root / "site-packages/fake",
    ):
        directory.chmod(0o500)
    registry = tmp_path / "registry.json"
    adapters = tmp_path / "adapters.json"
    registry.write_text(
        json.dumps(
            {
                "schema": "ne-inference-target-registry-v1",
                "targets": [
                    {
                        "target_id": "local-vllm-cpu",
                        "display_name": "Local CPU vLLM",
                        "adapter_id": "compose-vllm-cpu-v1",
                        "resource_id": "compose:vllm-cpu",
                        "served_models": ["qwen3-0.6b"],
                        "secret_refs": [],
                        "capabilities": ["status", "start", "stop"],
                        "deadlines_s": {"status": 2, "start": 2, "stop": 2},
                        "cooldown_s": 1,
                        "reconcile_s": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    adapters.write_text(
        json.dumps(
            {
                "schema": "ne-inference-adapter-digests-v1",
                "digests": {
                    "cpu-script": hashlib.sha256(executable.read_bytes()).hexdigest(),
                    "vast-script": hashlib.sha256(vast.read_bytes()).hexdigest(),
                    "vast-cli": hashlib.sha256(cli.read_bytes()).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    registry.chmod(0o600)
    adapters.chmod(0o600)
    return registry, adapters, executable


def _load_test_manifest(
    registry: Path,
    adapters: Path,
    *,
    owner_uid: int,
    _closed_root: Path,
) -> PrivateManifest:
    lock = _closed_root / "vendor/vastai/offline-lock.json"
    return _load_private_manifest(
        registry,
        adapters,
        owner_uid=owner_uid,
        _closed_root=_closed_root,
        _accepted_vast_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
    )


def test_registry_cannot_select_executable_policy(tmp_path: Path) -> None:
    registry, adapters, _ = _documents(tmp_path)
    document = json.loads(registry.read_text(encoding="utf-8"))
    for forbidden in ("command", "path", "digest", "argv", "env", "endpoint", "secret"):
        mutated = json.loads(json.dumps(document))
        mutated["targets"][0][forbidden] = "attacker-controlled"
        registry.write_text(json.dumps(mutated), encoding="utf-8")
        registry.chmod(0o600)
        with pytest.raises(ManifestRefused, match="closed shape"):
            _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)


def test_digest_receipt_cannot_select_command_path_argv_env_or_secret(tmp_path: Path) -> None:
    registry, adapters, _ = _documents(tmp_path)
    document = json.loads(adapters.read_text(encoding="utf-8"))
    for forbidden in ("command", "path", "argv", "env", "endpoint", "secret"):
        mutated = json.loads(json.dumps(document))
        mutated[forbidden] = "attacker-controlled"
        adapters.write_text(json.dumps(mutated), encoding="utf-8")
        adapters.chmod(0o600)
        with pytest.raises(ManifestRefused, match="closed shape"):
            _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)


def test_registry_secret_references_are_typed_and_closed_by_adapter(tmp_path: Path) -> None:
    registry, adapters, _ = _documents(tmp_path)
    document = json.loads(registry.read_text(encoding="utf-8"))
    for value in (["/etc/passwd"], ["attacker-secret"], "vast-api-key"):
        mutated = json.loads(json.dumps(document))
        mutated["targets"][0]["secret_refs"] = value
        registry.write_text(json.dumps(mutated), encoding="utf-8")
        registry.chmod(0o600)
        with pytest.raises(ManifestRefused):
            _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)


def test_vast_ssh_identity_is_fixed_policy_not_registry_or_home(tmp_path: Path) -> None:
    registry, adapters, _ = _documents(tmp_path)
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["targets"][0].update(
        {
            "target_id": "private-vast-gpu",
            "display_name": "Private GPU rig",
            "adapter_id": "vast-private-rig-v1",
            "resource_id": "provider:vast-private-rig",
            "served_models": ["qwen3.6-35b-a3b"],
            "secret_refs": ["vast-api-key", "vast-ssh-private-key"],
        }
    )
    registry.write_text(json.dumps(document), encoding="utf-8")
    registry.chmod(0o600)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    environment = manifest.adapters["vast-private-rig-v1"].environment
    assert environment["NE_VAST_SSH_KEY_FILE"] == (
        "/run/credentials/ne-inference-provider-broker.service/vast-ssh-private-key"
    )
    assert environment["NE_VAST_KNOWN_HOSTS_FILE"] == (
        "/var/lib/neutron/inference-provider/vast-known-hosts"
    )
    assert environment["NE_VAST_OPERATOR_TMP_ROOT"] == ("/var/lib/neutron/inference-provider")
    assert environment["NE_PROVIDER_GRANT_CLIENT"] == (
        "/opt/neutron/inference-operator/bin/provider-ssh-grant-client"
    )
    assert "HOME" not in environment and "SSH_AUTH_SOCK" not in environment

    document["targets"][0]["secret_refs"] = ["vast-api-key"]
    registry.write_text(json.dumps(document), encoding="utf-8")
    registry.chmod(0o600)
    with pytest.raises(ManifestRefused, match="secret references"):
        _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)


@pytest.mark.parametrize("drift", ["binary", "version", "package"])
def test_vast_bundle_refuses_unreviewed_binary_version_or_package(
    tmp_path: Path, drift: str
) -> None:
    registry, adapters, _ = _documents(tmp_path)
    vast_root = tmp_path / "vendor/vastai"
    if drift == "binary":
        _write(vast_root / "bin/vastai", b"#!/bin/sh\nexit 7\n", 0o500)
    elif drift == "version":
        lock = vast_root / "offline-lock.json"
        document = json.loads(lock.read_text(encoding="utf-8"))
        document["upstream"]["version"] = "9.9.9"
        lock.chmod(0o600)
        lock.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
        lock.chmod(0o400)
    else:
        package = vast_root / "site-packages/fake/__init__.py"
        package.chmod(0o600)
        package.write_bytes(b"UNREVIEWED = True\n")
        package.chmod(0o400)
    with pytest.raises(ManifestRefused, match="offline bundle"):
        _load_test_manifest(
            registry,
            adapters,
            owner_uid=os.getuid(),
            _closed_root=tmp_path,
        )


_COMMITTED_VAST_LOCK = (
    Path(__file__).resolve().parents[3] / "deploy/inference-operator/vastai-offline-lock.json"
)


def test_validate_vast_bundle_default_digest_admits_the_committed_lock(tmp_path: Path) -> None:
    # Binds the DEFAULT accepted digest (the one manifest.py and install-policy.sh use)
    # to the committed lock bytes through the production function, not a copy of it.
    root = tmp_path / "vendor/vastai"
    root.mkdir(parents=True)
    lock = root / "offline-lock.json"
    lock.write_bytes(_COMMITTED_VAST_LOCK.read_bytes())
    lock.chmod(0o400)
    root.chmod(0o500)
    # Code-owned default: the committed lock passes the digest gate and the walk
    # fails later, at the absent wheelhouse (no wheels are committed to the repo).
    with pytest.raises(VastBundleRefused, match="Vast offline directory is unavailable"):
        validate_vast_bundle(root, owner_uid=os.getuid())
    # Control: any other accepted digest refuses the committed lock at the gate itself.
    with pytest.raises(VastBundleRefused, match="not the reviewed closure"):
        validate_vast_bundle(root, owner_uid=os.getuid(), accepted_lock_sha256="0" * 64)


def test_nonroot_broker_projection_reads_only_exact_group_closed_bundle(tmp_path: Path) -> None:
    registry, adapters, _ = _documents(tmp_path)
    vast_root = tmp_path / "vendor/vastai"
    for path in vast_root.rglob("*"):
        path.chmod(0o550 if path.is_dir() or path == vast_root / "bin/vastai" else 0o440)
    vast_root.chmod(0o550)
    registry.chmod(0o640)
    adapters.chmod(0o640)
    manifest = _load_private_manifest(
        registry,
        adapters,
        owner_uid=os.getuid(),
        _closed_root=tmp_path,
        _accepted_vast_lock_sha256=hashlib.sha256(
            (vast_root / "offline-lock.json").read_bytes()
        ).hexdigest(),
        _document_mode=0o640,
        _document_reader_gid=os.getgid(),
        _vast_reader_gid=os.getgid(),
    )
    assert "vast-private-rig-v1" in manifest.adapters
    registry.chmod(0o644)
    with pytest.raises(ManifestRefused, match="ownership or mode"):
        _load_private_manifest(
            registry,
            adapters,
            owner_uid=os.getuid(),
            _closed_root=tmp_path,
            _accepted_vast_lock_sha256=hashlib.sha256(
                (vast_root / "offline-lock.json").read_bytes()
            ).hexdigest(),
            _document_mode=0o640,
            _document_reader_gid=os.getgid(),
            _vast_reader_gid=os.getgid(),
        )


@pytest.mark.parametrize("drift", ["mode", "symlink", "digest"])
def test_adapter_file_integrity_fails_closed(tmp_path: Path, drift: str) -> None:
    registry, adapters, executable = _documents(tmp_path)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    policy = manifest.adapters["compose-vllm-cpu-v1"]
    if drift == "mode":
        executable.chmod(0o522)
    elif drift == "symlink":
        original = tmp_path / "original.sh"
        executable.chmod(0o700)
        executable.rename(original)
        executable.symlink_to(original)
    else:
        executable.chmod(0o700)
        executable.write_bytes(executable.read_bytes() + b"# drift\n")
    result = asyncio.run(
        PinnedRunner(owner_uid=os.getuid()).run(
            policy,
            InferenceTargetOperation.STATUS,
            deadline_s=1,
            transition_id="status-integrity",
        )
    )
    assert result.code is InferenceOperatorCode.SCRIPT_REFUSED


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
def test_runner_uses_fixed_argv_environment_and_discards_output(tmp_path: Path) -> None:
    registry, adapters, executable = _documents(tmp_path)
    marker = tmp_path / "marker"
    body = (
        '#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = status ] '
        '&& [ "${PATH}" = /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin ] '
        f"|| exit 9\nprintf ok > {marker}\n"
        "printf secret-output\n"
    )
    _write(executable, body.encode(), 0o500)
    adapter_doc = json.loads(adapters.read_text(encoding="utf-8"))
    adapter_doc["digests"]["cpu-script"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    adapters.write_text(json.dumps(adapter_doc), encoding="utf-8")
    adapters.chmod(0o600)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    result = asyncio.run(
        PinnedRunner(owner_uid=os.getuid()).run(
            manifest.adapters["compose-vllm-cpu-v1"],
            InferenceTargetOperation.STATUS,
            deadline_s=1,
            transition_id="cpu-status",
        )
    )
    assert result == RunResult(InferenceOperatorCode.OBSERVED, 0)
    assert marker.read_text(encoding="utf-8") == "ok"


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
def test_runner_total_deadline_kills_process_group(tmp_path: Path) -> None:
    registry, adapters, executable = _documents(tmp_path)
    _write(executable, b"#!/bin/sh\nsleep 30\n", 0o500)
    adapter_doc = json.loads(adapters.read_text(encoding="utf-8"))
    adapter_doc["digests"]["cpu-script"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    adapters.write_text(json.dumps(adapter_doc), encoding="utf-8")
    adapters.chmod(0o600)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    result = asyncio.run(
        PinnedRunner(owner_uid=os.getuid()).run(
            manifest.adapters["compose-vllm-cpu-v1"],
            InferenceTargetOperation.START,
            deadline_s=0.05,
            transition_id="cpu-start",
        )
    )
    assert result.code is InferenceOperatorCode.DEADLINE_EXCEEDED


@pytest.mark.parametrize(
    "operation",
    [InferenceTargetOperation.STATUS, InferenceTargetOperation.START],
)
def test_provider_operation_upgrade_lock_wait_is_bounded_and_precedes_execution(
    tmp_path: Path, operation: InferenceTargetOperation
) -> None:
    registry, adapters, _ = _documents(tmp_path)
    vast = tmp_path / "bin/vast-rig.sh"
    marker = tmp_path / "provider-called"
    _write(vast, f"#!/bin/sh\nprintf called > {marker}\n".encode(), 0o500)
    adapter_doc = json.loads(adapters.read_text(encoding="utf-8"))
    adapter_doc["digests"]["vast-script"] = hashlib.sha256(vast.read_bytes()).hexdigest()
    adapters.write_text(json.dumps(adapter_doc), encoding="utf-8")
    adapters.chmod(0o600)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    lock_path = tmp_path / "live-upgrade.lock"

    class _Client:
        async def invoke(self, *args: object, **kwargs: object) -> BrokerResult:
            marker.write_text("called", encoding="utf-8")
            return BrokerResult(InferenceOperatorCode.OBSERVED, 0)

    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        result = asyncio.run(
            PinnedRunner(
                owner_uid=os.getuid(),
                upgrade_lock=lock_path,
                provider_client=_Client(),  # type: ignore[arg-type]
            ).run(
                manifest.adapters["vast-private-rig-v1"],
                operation,
                deadline_s=0.05,
                transition_id="provider-lock",
            )
        )
    finally:
        os.close(lock_descriptor)
    assert result == RunResult(InferenceOperatorCode.DEADLINE_EXCEEDED, None)
    assert not marker.exists()


def test_deploy_exclusive_lock_precedes_every_adapter_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, adapters, executable = _documents(tmp_path)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    policy = manifest.adapters["compose-vllm-cpu-v1"]
    lock_path = tmp_path / "live-upgrade.lock"
    runner = PinnedRunner(owner_uid=os.getuid(), upgrade_lock=lock_path)
    opened = asyncio.Event()
    original_open = runner._open_verified

    def observed_open(expected: VerifiedFile) -> int:
        opened.set()
        return original_open(expected)

    monkeypatch.setattr(runner, "_open_verified", observed_open)

    async def scenario() -> None:
        deployment = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(deployment, fcntl.LOCK_EX)
        task = asyncio.create_task(
            runner.run(
                policy,
                InferenceTargetOperation.STATUS,
                deadline_s=1,
                transition_id="cpu-generation",
            )
        )
        await asyncio.sleep(0.05)
        assert not opened.is_set(), "adapter was pinned before deployment generation unlocked"
        executable.chmod(0o700)
        executable.write_text("#!/bin/sh\nexit 0\n# replacement\n", encoding="utf-8")
        executable.chmod(0o500)
        fcntl.flock(deployment, fcntl.LOCK_UN)
        os.close(deployment)
        assert await task == RunResult(InferenceOperatorCode.SCRIPT_REFUSED, None)
        assert opened.is_set()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "linux", reason="grant authority binds Linux /proc identity")
def test_root_runner_publishes_and_cleans_provider_transition_authority(tmp_path: Path) -> None:
    registry, adapters, _ = _documents(tmp_path)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    grants = tmp_path / "grants"
    grants.mkdir(mode=0o700)
    provider_state = tmp_path / "provider-instance.json"
    provider_state.write_text(
        json.dumps(
            {
                "host": "8.8.8.8",
                "id": 42,
                "map": {"22": 2222},
                "ssh_port": 2222,
                "status": "stopped",
            }
        ),
        encoding="utf-8",
    )
    provider_state.chmod(0o600)
    observed: dict[str, object] = {}

    class _Client:
        async def invoke(
            self,
            operation: InferenceTargetOperation,
            *,
            transition_id: str,
            deadline_s: float,
        ) -> BrokerResult:
            name = hashlib.sha256(transition_id.encode()).hexdigest()
            authority_path = grants / f"{name}.json"
            authority = json.loads(authority_path.read_text(encoding="utf-8"))
            observed.update(authority)
            assert authority_path.stat().st_mode & 0o777 == 0o600
            assert not (grants / f"{name}.consumed").exists()
            assert deadline_s > 0
            return BrokerResult(InferenceOperatorCode.OBSERVED, 0)

    result = asyncio.run(
        PinnedRunner(
            owner_uid=os.getuid(),
            upgrade_lock=tmp_path / "upgrade.lock",
            provider_grant_dir=grants,
            provider_state=provider_state,
            provider_client=_Client(),  # type: ignore[arg-type]
        ).run(
            manifest.adapters["vast-private-rig-v1"],
            InferenceTargetOperation.START,
            deadline_s=2,
            transition_id="gpu-authority",
        )
    )
    assert result == RunResult(InferenceOperatorCode.OBSERVED, 0)
    assert observed["transition_id"] == "gpu-authority"
    assert observed["operation"] == "start"
    assert observed["target_id"] == "private-vast-gpu"
    assert observed["instance_id"] == 42
    assert "address" not in observed and "port" not in observed
    assert list(grants.iterdir()) == []


@pytest.mark.skipif(sys.platform != "linux", reason="grant authority binds Linux /proc identity")
def test_provider_mutation_excludes_deployment_for_its_full_execution(tmp_path: Path) -> None:
    registry, adapters, _ = _documents(tmp_path)
    vast = tmp_path / "bin/vast-rig.sh"
    marker = tmp_path / "provider-called"
    release = tmp_path / "release-provider"
    _write(
        vast,
        (
            f"#!/bin/sh\nprintf called > {marker}\nwhile [ ! -e {release} ]; do sleep 0.01; done\n"
        ).encode(),
        0o500,
    )
    adapter_doc = json.loads(adapters.read_text(encoding="utf-8"))
    adapter_doc["digests"]["vast-script"] = hashlib.sha256(vast.read_bytes()).hexdigest()
    adapters.write_text(json.dumps(adapter_doc), encoding="utf-8")
    adapters.chmod(0o600)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    lock_path = tmp_path / "live-upgrade.lock"
    provider_state = tmp_path / "provider-instance.json"
    provider_state.write_text(
        json.dumps(
            {
                "host": "8.8.8.8",
                "id": 42,
                "map": {"22": 2222},
                "ssh_port": 2222,
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    provider_state.chmod(0o600)
    grant_dir = tmp_path / "grants"
    grant_dir.mkdir(mode=0o700)

    class _Client:
        async def invoke(self, *args: object, **kwargs: object) -> BrokerResult:
            marker.write_text("called", encoding="utf-8")
            while not release.exists():
                await asyncio.sleep(0.01)
            return BrokerResult(InferenceOperatorCode.OBSERVED, 0)

    async def scenario() -> None:
        task = asyncio.create_task(
            PinnedRunner(
                owner_uid=os.getuid(),
                upgrade_lock=lock_path,
                provider_grant_dir=grant_dir,
                provider_state=provider_state,
                provider_client=_Client(),  # type: ignore[arg-type]
            ).run(
                manifest.adapters["vast-private-rig-v1"],
                InferenceTargetOperation.START,
                deadline_s=2,
                transition_id="provider-start",
            )
        )
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists()
        deployment_descriptor = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(deployment_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(deployment_descriptor)
        release.touch()
        assert await task == RunResult(InferenceOperatorCode.OBSERVED, 0)

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
def test_slow_gpu_mutation_does_not_consume_independent_cpu_deadline(tmp_path: Path) -> None:
    registry, adapters, cpu = _documents(tmp_path)
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["targets"].append(
        {
            "target_id": "private-vast-gpu",
            "display_name": "Private GPU rig",
            "adapter_id": "vast-private-rig-v1",
            "resource_id": "provider:vast-private-rig",
            "served_models": ["qwen3.6-35b-a3b"],
            "secret_refs": ["vast-api-key", "vast-ssh-private-key"],
            "capabilities": ["status", "start", "stop"],
            "deadlines_s": {"status": 2, "start": 2, "stop": 2},
            "cooldown_s": 1,
            "reconcile_s": 1,
        }
    )
    registry.write_text(json.dumps(document), encoding="utf-8")
    registry.chmod(0o600)
    lock_path = tmp_path / "live-upgrade.lock"
    gpu = tmp_path / "bin/vast-rig.sh"
    gpu_entered = tmp_path / "gpu-entered"
    release_gpu = tmp_path / "release-gpu"
    _write(
        gpu,
        (
            f"#!/bin/sh\nprintf entered > '{gpu_entered}'\n"
            f"while [ ! -e '{release_gpu}' ]; do sleep 0.01; done\n"
        ).encode(),
        0o500,
    )
    cpu_called = tmp_path / "cpu-called"
    _write(
        cpu,
        (f"#!/bin/sh\nexec 9>'{lock_path}'\nflock -s 9\nprintf called > '{cpu_called}'\n").encode(),
        0o500,
    )
    adapter_document = json.loads(adapters.read_text(encoding="utf-8"))
    adapter_document["digests"]["cpu-script"] = hashlib.sha256(cpu.read_bytes()).hexdigest()
    adapter_document["digests"]["vast-script"] = hashlib.sha256(gpu.read_bytes()).hexdigest()
    adapters.write_text(json.dumps(adapter_document), encoding="utf-8")
    adapters.chmod(0o600)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)
    provider_state = tmp_path / "provider-instance.json"
    provider_state.write_text(json.dumps({"id": 42}), encoding="utf-8")
    provider_state.chmod(0o600)
    grant_dir = tmp_path / "grants"
    grant_dir.mkdir(mode=0o700)

    class _Client:
        async def invoke(self, *args: object, **kwargs: object) -> BrokerResult:
            gpu_entered.write_text("entered", encoding="utf-8")
            while not release_gpu.exists():
                await asyncio.sleep(0.01)
            return BrokerResult(InferenceOperatorCode.OBSERVED, 0)

    runner = PinnedRunner(
        owner_uid=os.getuid(),
        upgrade_lock=lock_path,
        provider_grant_dir=grant_dir,
        provider_state=provider_state,
        provider_client=_Client(),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        gpu_task = asyncio.create_task(
            runner.run(
                manifest.adapters["vast-private-rig-v1"],
                InferenceTargetOperation.START,
                deadline_s=2,
                transition_id="gpu-start",
            )
        )
        for _ in range(100):
            if gpu_entered.exists():
                break
            await asyncio.sleep(0.01)
        assert gpu_entered.exists()
        cpu_result = await runner.run(
            manifest.adapters["compose-vllm-cpu-v1"],
            InferenceTargetOperation.START,
            deadline_s=0.25,
            transition_id="cpu-start",
        )
        assert cpu_result == RunResult(InferenceOperatorCode.OBSERVED, 0)
        assert cpu_called.exists()
        release_gpu.touch()
        assert await gpu_task == RunResult(InferenceOperatorCode.OBSERVED, 0)

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "linux", reason="production FD execution uses Linux /proc")
def test_runner_cancellation_escalates_past_term_ignoring_child(tmp_path: Path) -> None:
    registry, adapters, executable = _documents(tmp_path)
    _write(executable, b"#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n", 0o500)
    adapter_doc = json.loads(adapters.read_text(encoding="utf-8"))
    adapter_doc["digests"]["cpu-script"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    adapters.write_text(json.dumps(adapter_doc), encoding="utf-8")
    adapters.chmod(0o600)
    manifest = _load_test_manifest(registry, adapters, owner_uid=os.getuid(), _closed_root=tmp_path)

    async def scenario() -> None:
        task = asyncio.create_task(
            PinnedRunner(owner_uid=os.getuid()).run(
                manifest.adapters["compose-vllm-cpu-v1"],
                InferenceTargetOperation.START,
                deadline_s=30,
                transition_id="cpu-start",
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=4)

    asyncio.run(scenario())


class _FakeRunner:
    def __init__(self, *, block: bool = False) -> None:
        self.calls: list[tuple[str, InferenceTargetOperation]] = []
        self.deadlines: list[tuple[InferenceTargetOperation, float, str]] = []
        self.block = block
        self.release = asyncio.Event()

    async def run(
        self,
        policy: AdapterPolicy,
        operation: InferenceTargetOperation,
        *,
        deadline_s: float,
        transition_id: str,
    ) -> RunResult:
        self.calls.append((policy.adapter_id, operation))
        self.deadlines.append((operation, deadline_s, transition_id))
        if self.block and operation is not InferenceTargetOperation.STATUS:
            await self.release.wait()
        return RunResult(
            InferenceOperatorCode.OBSERVED,
            3 if operation is InferenceTargetOperation.STATUS else 0,
        )


def _manifest(*, provider_egress: bool = False, second: bool = False) -> PrivateManifest:
    operations = MappingProxyType(
        {operation: (operation.value,) for operation in InferenceTargetOperation}
    )
    policy = AdapterPolicy(
        adapter_id="adapter-v1",
        executable=VerifiedFile(Path("/unused"), "a" * 64),
        verified_files=(),
        operations=operations,
        environment=MappingProxyType({}),
        provider_egress=provider_egress,
    )
    targets = [
        RegistryTarget(
            target_id="target-one",
            display_name="Target one",
            adapter_id=policy.adapter_id,
            resource_id="compose:one",
            served_models=("model-one",),
            secret_refs=(),
            capabilities=tuple(InferenceTargetOperation),
            deadlines_s=MappingProxyType(
                {operation: 2.0 for operation in InferenceTargetOperation}
            ),
            cooldown_s=1,
            reconcile_s=1,
        )
    ]
    if second:
        targets.append(
            RegistryTarget(
                target_id="target-two",
                display_name="Target two",
                adapter_id=policy.adapter_id,
                resource_id="compose:two",
                served_models=("model-two",),
                secret_refs=(),
                capabilities=tuple(InferenceTargetOperation),
                deadlines_s=MappingProxyType(
                    {operation: 2.0 for operation in InferenceTargetOperation}
                ),
                cooldown_s=1,
                reconcile_s=1,
            )
        )
    return PrivateManifest("b" * 64, tuple(targets), MappingProxyType({policy.adapter_id: policy}))


def test_air_gap_defaults_fail_closed_and_refuses_provider_before_runner(tmp_path: Path) -> None:
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(provider_egress=True),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        result = await operator.start(
            InferenceStartRequest(target_id="target-one", transition_id="transition-one")
        )
        assert result.code is InferenceOperatorCode.AIR_GAP_REFUSED
        assert result.state is InferenceTargetState.UNKNOWN

    asyncio.run(scenario())
    assert runner.calls == []


@pytest.mark.parametrize("value", [None, "", "garbage"])
def test_air_gap_environment_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("AIR_GAP", raising=False)
        assert _air_gap_from_environment() is True
    else:
        monkeypatch.setenv("AIR_GAP", value)
        with pytest.raises(SystemExit, match="explicit boolean"):
            _air_gap_from_environment()


def test_air_gap_file_is_exact_owner_only_policy(tmp_path: Path) -> None:
    policy = tmp_path / "environment"
    assert _air_gap_from_file(policy, owner_uid=os.getuid()) is True
    for content, expected in (("AIR_GAP=true\n", True), ("AIR_GAP=false\n", False)):
        policy.write_text(content, encoding="ascii")
        policy.chmod(0o600)
        assert _air_gap_from_file(policy, owner_uid=os.getuid()) is expected
    for content in ("AIR_GAP=false\nOTHER=value\n", "AIR_GAP=0\n", ""):
        policy.write_text(content, encoding="ascii")
        policy.chmod(0o600)
        with pytest.raises(SystemExit, match="one exact boolean"):
            _air_gap_from_file(policy, owner_uid=os.getuid())
    policy.write_text("AIR_GAP=false\n", encoding="ascii")
    policy.chmod(0o644)
    with pytest.raises(SystemExit, match="unsafe"):
        _air_gap_from_file(policy, owner_uid=os.getuid())


@pytest.mark.parametrize("changed_policy", ["true", "missing", "malformed", "unsafe"])
def test_air_gap_is_reread_before_async_provider_mutation(
    tmp_path: Path, changed_policy: str
) -> None:
    policy_file = tmp_path / "environment"
    policy_file.write_text("AIR_GAP=false\n", encoding="ascii")
    policy_file.chmod(0o600)
    checks = 0

    def live_air_gap() -> bool:
        nonlocal checks
        checks += 1
        result = _air_gap_from_file(policy_file, owner_uid=os.getuid())
        if checks == 1:
            if changed_policy == "true":
                policy_file.write_text("AIR_GAP=true\n", encoding="ascii")
            elif changed_policy == "missing":
                policy_file.unlink()
            elif changed_policy == "malformed":
                policy_file.write_text("AIR_GAP=maybe\n", encoding="ascii")
            else:
                policy_file.chmod(0o644)
        return result

    runner = _FakeRunner()
    journal = StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid())
    operator = HostInferenceTargetOperator(
        _manifest(provider_egress=True),
        journal,
        runner=runner,  # type: ignore[arg-type]
        air_gap=live_air_gap,
    )

    async def scenario() -> None:
        request = InferenceStartRequest(
            target_id="target-one", transition_id="live-policy-transition"
        )
        accepted = await operator.start(request)
        assert accepted.code is InferenceOperatorCode.ACCEPTED
        await operator.drain()
        terminal = await operator.start(request)
        assert terminal.code is InferenceOperatorCode.AIR_GAP_REFUSED
        assert terminal.state is InferenceTargetState.UNKNOWN

    asyncio.run(scenario())
    assert checks >= 2
    assert runner.calls == []
    assert journal.records[-1].payload["phase"] == "completed"
    assert (
        journal.records[-1].payload["observation"]["code"]
        == InferenceOperatorCode.AIR_GAP_REFUSED.value
    )


def test_same_transition_is_idempotent_and_conflict_is_single_flight(tmp_path: Path) -> None:
    runner = _FakeRunner(block=True)
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        request = InferenceStartRequest(target_id="target-one", transition_id="same-transition")
        first = await operator.start(request)
        retry = await operator.start(request)
        conflict = await operator.stop(
            InferenceStopRequest(target_id="target-one", transition_id="other-transition")
        )
        assert retry == first
        assert conflict.code is InferenceOperatorCode.TRANSITION_CONFLICT
        assert runner.calls == [("adapter-v1", InferenceTargetOperation.START)]
        runner.release.set()
        await operator.drain()

    asyncio.run(scenario())


def test_transition_id_cannot_be_reused_for_opposite_operation(tmp_path: Path) -> None:
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        started = await operator.start(
            InferenceStartRequest(target_id="target-one", transition_id="same-transition")
        )
        assert started.code is InferenceOperatorCode.ACCEPTED
        await operator.drain()
        operator._last_mutation_at.clear()
        stopped = await operator.stop(
            InferenceStopRequest(target_id="target-one", transition_id="same-transition")
        )
        assert stopped.code is InferenceOperatorCode.TRANSITION_CONFLICT
        assert sum(call[1] is InferenceTargetOperation.STOP for call in runner.calls) == 0

    asyncio.run(scenario())


def test_completed_transition_retry_returns_latest_without_second_mutation(tmp_path: Path) -> None:
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )
    request = InferenceStartRequest(target_id="target-one", transition_id="transition-one")

    async def scenario() -> None:
        accepted = await operator.start(request)
        assert accepted.code is InferenceOperatorCode.ACCEPTED
        await operator.drain()
        completed = await operator.start(request)
        assert completed.code is InferenceOperatorCode.OBSERVED
        assert completed.state is InferenceTargetState.READY
        assert sum(call[1] is InferenceTargetOperation.START for call in runner.calls) == 1

    asyncio.run(scenario())


def test_status_during_transition_preserves_single_flight(tmp_path: Path) -> None:
    runner = _FakeRunner(block=True)
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        await operator.start(InferenceStartRequest(target_id="target-one", transition_id="t-one"))
        observed = await operator.status(
            InferenceStatusRequest(target_id="target-one", transition_id="t-one")
        )
        assert observed.state is InferenceTargetState.STARTING
        assert observed.transition_id == "t-one"
        conflict = await operator.start(
            InferenceStartRequest(target_id="target-one", transition_id="t-two")
        )
        assert conflict.code is InferenceOperatorCode.TRANSITION_CONFLICT
        assert runner.calls == [("adapter-v1", InferenceTargetOperation.START)]
        runner.release.set()
        await operator.drain()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "cancel"])
def test_transition_failure_is_terminally_journaled_and_clears_single_flight(
    tmp_path: Path, failure: str
) -> None:
    class _FailingRunner(_FakeRunner):
        async def run(
            self,
            policy: AdapterPolicy,
            operation: InferenceTargetOperation,
            *,
            deadline_s: float,
            transition_id: str,
        ) -> RunResult:
            self.calls.append((policy.adapter_id, operation))
            if failure == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("redacted")

    runner = _FailingRunner()
    journal = StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid())
    operator = HostInferenceTargetOperator(_manifest(), journal, runner=runner)  # type: ignore[arg-type]

    async def scenario() -> None:
        await operator.start(InferenceStartRequest(target_id="target-one", transition_id="t-one"))
        await operator.drain()
        assert "target-one" not in operator._active

    asyncio.run(scenario())
    terminal = journal.records[-1].payload["observation"]
    assert terminal["state"] == "error"
    assert terminal["code"] == ("interrupted" if failure == "cancel" else "internal-error")


def test_accepted_journal_fsync_failure_never_launches_runner_and_restart_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path = tmp_path / "state.jsonl"
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(journal_path, owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )
    real_fsync = os.fsync
    calls = 0

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected accepted fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first_fsync)
    request = InferenceStartRequest(target_id="target-one", transition_id="accepted-fsync-failure")

    async def fail_accept() -> None:
        result = await operator.start(request)
        assert result.code is InferenceOperatorCode.INTERNAL_ERROR
        assert result.state is InferenceTargetState.UNKNOWN
        retry = await operator.start(request)
        assert retry.code is InferenceOperatorCode.INTERNAL_ERROR

    asyncio.run(fail_accept())
    assert runner.calls == []
    assert operator._active == {}
    assert operator._versions["target-one"] == 0

    monkeypatch.setattr(os, "fsync", real_fsync)
    durable = StateJournal(journal_path, owner_uid=os.getuid())
    assert [row.payload["phase"] for row in durable.records] == ["accepted"]
    recovered_runner = _FakeRunner()
    recovered = HostInferenceTargetOperator(
        _manifest(),
        durable,
        runner=recovered_runner,  # type: ignore[arg-type]
    )

    async def retry_after_restart() -> None:
        result = await recovered.start(request)
        assert result.code is InferenceOperatorCode.INTERRUPTED
        assert result.state is InferenceTargetState.UNKNOWN

    asyncio.run(retry_after_restart())
    assert recovered_runner.calls == []
    assert [row.payload["phase"] for row in durable.records] == [
        "accepted",
        "recovered-interrupted",
    ]


def test_terminal_journal_fsync_failure_clears_active_and_restart_uses_durable_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path = tmp_path / "state.jsonl"
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(journal_path, owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )
    real_fsync = os.fsync
    calls = 0

    def fail_terminal_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected terminal fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_terminal_fsync)
    request = InferenceStartRequest(target_id="target-one", transition_id="terminal-fsync-failure")

    async def complete_with_failed_fsync() -> None:
        accepted = await operator.start(request)
        assert accepted.code is InferenceOperatorCode.ACCEPTED
        await operator.drain()
        assert operator._active == {}
        assert operator._states["target-one"] is InferenceTargetState.UNKNOWN
        retry = await operator.start(request)
        assert retry.code is InferenceOperatorCode.INTERNAL_ERROR
        assert retry.state is InferenceTargetState.UNKNOWN

    asyncio.run(complete_with_failed_fsync())
    assert runner.calls == [("adapter-v1", InferenceTargetOperation.START)]

    monkeypatch.setattr(os, "fsync", real_fsync)
    durable = StateJournal(journal_path, owner_uid=os.getuid())
    assert [row.payload["phase"] for row in durable.records] == ["accepted", "completed"]
    recovered_runner = _FakeRunner()
    recovered = HostInferenceTargetOperator(
        _manifest(),
        durable,
        runner=recovered_runner,  # type: ignore[arg-type]
    )

    async def retry_after_restart() -> None:
        result = await recovered.start(request)
        assert result.code is InferenceOperatorCode.OBSERVED
        assert result.state is InferenceTargetState.READY

    asyncio.run(retry_after_restart())
    assert recovered_runner.calls == []


def test_start_exit_three_is_not_misreported_ready(tmp_path: Path) -> None:
    class _WrongExitRunner(_FakeRunner):
        async def run(
            self,
            policy: AdapterPolicy,
            operation: InferenceTargetOperation,
            *,
            deadline_s: float,
            transition_id: str,
        ) -> RunResult:
            return RunResult(InferenceOperatorCode.OBSERVED, 3)

    journal = StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid())
    operator = HostInferenceTargetOperator(_manifest(), journal, runner=_WrongExitRunner())  # type: ignore[arg-type]

    async def scenario() -> None:
        await operator.start(InferenceStartRequest(target_id="target-one", transition_id="t-one"))
        await operator.drain()

    asyncio.run(scenario())
    assert journal.records[-1].payload["observation"]["code"] == "readiness-failed"
    assert journal.records[-1].payload["state"] == "error"


def test_stop_exit_zero_is_persisted_stopped(tmp_path: Path) -> None:
    journal = StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid())
    operator = HostInferenceTargetOperator(_manifest(), journal, runner=_FakeRunner())  # type: ignore[arg-type]

    async def scenario() -> None:
        await operator.stop(InferenceStopRequest(target_id="target-one", transition_id="t-one"))
        await operator.drain()

    asyncio.run(scenario())
    assert journal.records[-1].payload["observation"]["code"] == "observed"
    assert journal.records[-1].payload["state"] == "stopped"


def test_cooldown_refuses_a_second_mutation_after_completion(tmp_path: Path) -> None:
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        await operator.start(
            InferenceStartRequest(target_id="target-one", transition_id="transition-one")
        )
        await operator.drain()
        refused = await operator.stop(
            InferenceStopRequest(target_id="target-one", transition_id="transition-two")
        )
        assert refused.code is InferenceOperatorCode.TRANSITION_CONFLICT
        assert runner.calls == [("adapter-v1", InferenceTargetOperation.START)]

    asyncio.run(scenario())


def test_already_proven_requires_fresh_status_evidence(tmp_path: Path) -> None:
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )
    operator._states["target-one"] = InferenceTargetState.READY

    async def scenario() -> None:
        result = await operator.start(
            InferenceStartRequest(target_id="target-one", transition_id="transition-one")
        )
        # Fake status proves stopped, so stale READY cannot produce ALREADY_PROVEN.
        assert result.code is InferenceOperatorCode.ACCEPTED
        await operator.drain()

    asyncio.run(scenario())
    assert runner.calls == [
        ("adapter-v1", InferenceTargetOperation.STATUS),
        ("adapter-v1", InferenceTargetOperation.START),
    ]


def test_status_cache_bounds_completion_probe_and_rebinds_transition(tmp_path: Path) -> None:
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        first = await operator.status(
            InferenceStatusRequest(target_id="target-one", transition_id="t-one")
        )
        second = await operator.status(
            InferenceStatusRequest(target_id="target-one", transition_id="t-two")
        )
        assert first.state == second.state
        assert second.transition_id == "t-two"
        assert runner.calls == [("adapter-v1", InferenceTargetOperation.STATUS)]

    asyncio.run(scenario())


def test_separate_targets_transition_concurrently(tmp_path: Path) -> None:
    runner = _FakeRunner(block=True)
    operator = HostInferenceTargetOperator(
        _manifest(second=True),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        await asyncio.gather(
            operator.start(
                InferenceStartRequest(target_id="target-one", transition_id="transition-one")
            ),
            operator.start(
                InferenceStartRequest(target_id="target-two", transition_id="transition-two")
            ),
        )
        assert len(runner.calls) == 2
        runner.release.set()
        await operator.drain()

    asyncio.run(scenario())


def test_restart_reconciles_pending_without_replaying_mutation(tmp_path: Path) -> None:
    journal_path = tmp_path / "state.jsonl"
    first_runner = _FakeRunner(block=True)
    first = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(journal_path, owner_uid=os.getuid()),
        runner=first_runner,  # type: ignore[arg-type]
    )

    async def accept() -> None:
        await first.start(
            InferenceStartRequest(target_id="target-one", transition_id="transition-one")
        )

    asyncio.run(accept())
    second_runner = _FakeRunner()
    recovered = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(journal_path, owner_uid=os.getuid()),
        runner=second_runner,  # type: ignore[arg-type]
    )

    async def reconcile() -> None:
        result = await recovered.status(InferenceStatusRequest(target_id="target-one"))
        assert result.state is InferenceTargetState.STOPPED

    asyncio.run(reconcile())
    assert second_runner.calls == [("adapter-v1", InferenceTargetOperation.STATUS)]


def test_restart_collapses_active_status_rows_and_retries_terminal_interrupted(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "state.jsonl"
    journal = StateJournal(journal_path, owner_uid=os.getuid())
    accepted = InferenceOperatorObservation(
        target_id="target-one",
        adapter_id="adapter-v1",
        resource_id="compose:one",
        operation=InferenceTargetOperation.START,
        transition_id="transition-one",
        state=InferenceTargetState.STARTING,
        code=InferenceOperatorCode.ACCEPTED,
        manifest_hash="b" * 64,
        observed_at=datetime.now(UTC),
    )
    active_status = accepted.model_copy(
        update={
            "operation": InferenceTargetOperation.STATUS,
            "code": InferenceOperatorCode.OBSERVED,
        }
    )
    for version, phase, observation in (
        (1, "accepted", accepted),
        (2, "active-observed", active_status),
    ):
        journal.append(
            {
                "actor": "local-operator",
                "desired_state": "ready" if phase == "accepted" else None,
                "manifest_hash": "b" * 64,
                "observation": observation.model_dump(mode="json"),
                "phase": phase,
                "state": observation.state.value,
                "target_id": observation.target_id,
                "version": version,
            }
        )

    runner = _FakeRunner()
    recovered = HostInferenceTargetOperator(_manifest(), journal, runner=runner)  # type: ignore[arg-type]
    assert len(journal.records) == 3
    terminal = journal.records[-1].payload["observation"]
    assert terminal["operation"] == "start"
    assert terminal["state"] == "unknown"
    assert terminal["code"] == "interrupted"

    async def retry() -> None:
        result = await recovered.start(
            InferenceStartRequest(target_id="target-one", transition_id="transition-one")
        )
        assert result.code is InferenceOperatorCode.INTERRUPTED
        assert result.state is InferenceTargetState.UNKNOWN

    asyncio.run(retry())
    assert runner.calls == []
    assert len(journal.records) == 3


@pytest.mark.parametrize("change_kind", ["removed", "changed"])
@pytest.mark.parametrize("terminal", [False, True])
def test_restart_scopes_history_to_exact_manifest_generation(
    tmp_path: Path, change_kind: str, terminal: bool
) -> None:
    old_manifest = _manifest(second=True)
    journal_path = tmp_path / "state.jsonl"
    journal = StateJournal(journal_path, owner_uid=os.getuid())
    old_target = old_manifest.targets[0]
    accepted = InferenceOperatorObservation(
        target_id=old_target.target_id,
        adapter_id=old_target.adapter_id,
        resource_id=old_target.resource_id,
        operation=InferenceTargetOperation.START,
        transition_id="old-generation-transition",
        state=InferenceTargetState.STARTING,
        code=InferenceOperatorCode.ACCEPTED,
        manifest_hash=old_manifest.manifest_hash,
        observed_at=datetime.now(UTC),
    )
    rows = [("accepted", accepted)]
    if terminal:
        rows.append(
            (
                "completed",
                accepted.model_copy(
                    update={
                        "state": InferenceTargetState.READY,
                        "code": InferenceOperatorCode.OBSERVED,
                    }
                ),
            )
        )
    for version, (phase, observation) in enumerate(rows, start=1):
        journal.append(
            {
                "actor": "old-operator",
                "desired_state": "ready",
                "manifest_hash": old_manifest.manifest_hash,
                "observation": observation.model_dump(mode="json"),
                "phase": phase,
                "state": observation.state.value,
                "target_id": observation.target_id,
                "version": version,
            }
        )

    if change_kind == "removed":
        current_targets = (old_manifest.targets[1],)
    else:
        current_targets = (
            replace(old_target, resource_id="compose:replacement"),
            old_manifest.targets[1],
        )
    current_manifest = PrivateManifest(
        "c" * 64,
        current_targets,
        old_manifest.adapters,
    )
    runner = _FakeRunner()
    recovered = HostInferenceTargetOperator(
        current_manifest,
        journal,
        runner=runner,  # type: ignore[arg-type]
    )
    expected_rows = len(rows) if terminal else len(rows) + 1
    assert len(journal.records) == expected_rows
    assert recovered._states == {}
    assert recovered._versions["target-one"] == 0
    assert recovered._idempotent == {}
    if not terminal:
        historical = journal.records[-1].payload
        assert historical["phase"] == "stale-generation-interrupted"
        assert historical["manifest_hash"] == old_manifest.manifest_hash
        assert historical["observation"]["resource_id"] == old_target.resource_id

    # A second boot does not append another interruption for the old generation.
    recovered_again = HostInferenceTargetOperator(
        current_manifest,
        StateJournal(journal_path, owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )
    assert len(StateJournal(journal_path, owner_uid=os.getuid()).records) == expected_rows

    async def exercise_current_projection() -> None:
        request = InferenceStartRequest(
            target_id="target-one", transition_id="old-generation-transition"
        )
        result = await recovered_again.start(request)
        if change_kind == "removed":
            assert result.code is InferenceOperatorCode.TARGET_UNKNOWN
            return
        assert result.code is InferenceOperatorCode.ACCEPTED
        await recovered_again.drain()
        latest = await recovered_again.start(request)
        assert latest.code is InferenceOperatorCode.OBSERVED
        assert latest.manifest_hash == current_manifest.manifest_hash
        assert latest.resource_id == "compose:replacement"

    asyncio.run(exercise_current_projection())
    expected_calls = (
        [] if change_kind == "removed" else [("adapter-v1", InferenceTargetOperation.START)]
    )
    assert runner.calls == expected_calls


def test_journal_detects_tamper(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    journal = StateJournal(path, owner_uid=os.getuid())
    journal.append({"safe": "value"})
    path.write_text(path.read_text(encoding="utf-8").replace("value", "other"), encoding="utf-8")
    with pytest.raises(JournalRefused, match="hash"):
        StateJournal(path, owner_uid=os.getuid())


def test_journal_retries_short_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write

    def short_write(descriptor: int, value: bytes | memoryview) -> int:
        return real_write(descriptor, value[: max(1, len(value) // 2)])

    monkeypatch.setattr(os, "write", short_write)
    path = tmp_path / "state.jsonl"
    StateJournal(path, owner_uid=os.getuid()).append({"safe": "value"})
    assert StateJournal(path, owner_uid=os.getuid()).records[0].payload == {"safe": "value"}


def test_unix_peer_credentials_require_exact_uid_and_gid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _PeerSocket:
        def __init__(self, uid: int, gid: int) -> None:
            self.uid = uid
            self.gid = gid

        def getsockopt(self, level: int, option: int, length: int) -> bytes:
            assert level == socket.SOL_SOCKET and length == 12
            return struct.pack("3i", 123, self.uid, self.gid)

    class _Writer:
        def __init__(self, peer: _PeerSocket) -> None:
            self.peer = peer

        def get_extra_info(self, name: str) -> _PeerSocket | None:
            return self.peer if name == "socket" else None

    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=_FakeRunner(),  # type: ignore[arg-type]
    )
    server = OperatorServer(
        operator,
        tmp_path / "operator.sock",
        allowed_uid=10001,
        allowed_gid=10002,
    )
    assert server._peer_allowed(_Writer(_PeerSocket(10001, 10002)))  # type: ignore[arg-type]
    assert not server._peer_allowed(_Writer(_PeerSocket(10001, 7)))  # type: ignore[arg-type]
    assert not server._peer_allowed(_Writer(_PeerSocket(7, 10002)))  # type: ignore[arg-type]


def test_lifetime_lock_is_singleton_and_owner_bound(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    descriptor = acquire_lifetime_lock(state / "operator.lock", owner_uid=os.getuid())
    try:
        with pytest.raises(ValueError, match="another operator"):
            acquire_lifetime_lock(state / "operator.lock", owner_uid=os.getuid())
    finally:
        os.close(descriptor)
    (state / "operator.lock").unlink()
    state.chmod(0o750)
    with pytest.raises(ValueError, match="lock directory is unsafe"):
        acquire_lifetime_lock(state / "operator.lock", owner_uid=os.getuid())


def test_concurrent_boot_is_locked_before_pending_journal_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir(mode=0o700)
    runtime.mkdir(mode=0o750)
    os.chown(runtime, os.getuid(), os.getgid())
    journal_path = state / "state.jsonl"
    journal = StateJournal(journal_path, owner_uid=os.getuid())
    accepted = InferenceOperatorObservation(
        target_id="target-one",
        adapter_id="adapter-v1",
        resource_id="compose:one",
        operation=InferenceTargetOperation.START,
        transition_id="pending-transition",
        state=InferenceTargetState.STARTING,
        code=InferenceOperatorCode.ACCEPTED,
        manifest_hash="b" * 64,
        observed_at=datetime.now(UTC),
    )
    journal.append(
        {
            "actor": "peer-uid:10001",
            "desired_state": InferenceTargetState.READY.value,
            "manifest_hash": "b" * 64,
            "observation": accepted.model_dump(mode="json"),
            "phase": "accepted",
            "state": accepted.state.value,
            "target_id": accepted.target_id,
            "version": 1,
        }
    )
    loads = 0

    def load_manifest(*_args: object, **_kwargs: object) -> PrivateManifest:
        nonlocal loads
        loads += 1
        return _manifest()

    monkeypatch.setattr(cli_module, "load_private_manifest", load_manifest)
    args = argparse.Namespace(
        lifetime_lock=state / "operator.lock",
        air_gap_file=None,
        registry=tmp_path / "registry.json",
        adapter_manifest=tmp_path / "adapter-manifest.json",
        journal=journal_path,
        socket=runtime / "operator.sock",
        systemd_socket=False,
        allowed_uid=os.getuid(),
        allowed_gid=os.getgid(),
    )
    first = acquire_lifetime_lock(args.lifetime_lock, owner_uid=os.getuid())
    try:
        with pytest.raises(ValueError, match="another operator"):
            cli_module._bootstrap(args, owner_uid=os.getuid())
        assert loads == 0
        assert len(StateJournal(journal_path, owner_uid=os.getuid()).records) == 1
    finally:
        os.close(first)

    second, _server = cli_module._bootstrap(args, owner_uid=os.getuid())
    os.close(second)
    recovered = StateJournal(journal_path, owner_uid=os.getuid()).records
    assert loads == 1 and len(recovered) == 2
    assert recovered[-1].payload["phase"] == "recovered-interrupted"


def test_transport_dispatch_rejects_extra_or_command_shaped_fields(tmp_path: Path) -> None:
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=_FakeRunner(),  # type: ignore[arg-type]
    )
    server = OperatorServer(
        operator,
        tmp_path / "operator.sock",
        allowed_uid=os.getuid(),
        allowed_gid=os.getgid(),
    )

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await server._dispatch(
                "POST",
                "/v1/start",
                {
                    "target_id": "target-one",
                    "transition_id": "transition-one",
                    "command": "docker down",
                },
            )

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "linux", reason="production peer credentials are Linux-only")
def test_http_over_uds_matches_api_client_routes_and_echoes_transition(tmp_path: Path) -> None:
    runner = _FakeRunner()
    operator = HostInferenceTargetOperator(
        _manifest(),
        StateJournal(tmp_path / "state.jsonl", owner_uid=os.getuid()),
        runner=runner,  # type: ignore[arg-type]
    )
    socket_dir = Path(tempfile.mkdtemp(prefix="ne-op-", dir=tmp_path))
    socket_dir.chmod(0o750)
    os.chown(socket_dir, os.getuid(), os.getgid())
    socket_path = socket_dir / "operator.sock"
    activation_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    activation_socket.bind(str(socket_path))
    activation_socket.listen()
    os.chown(socket_path, os.getuid(), os.getgid())
    os.chmod(socket_path, 0o660)

    def activated_server() -> OperatorServer:
        duplicate = socket.socket(fileno=os.dup(activation_socket.fileno()))
        return OperatorServer(
            operator,
            socket_path,
            activated_socket=duplicate,
            allowed_uid=os.getuid(),
            allowed_gid=os.getgid(),
            owner_uid=os.getuid(),
        )

    def client() -> UdsInferenceTargetOperator:
        return UdsInferenceTargetOperator(
            str(socket_path),
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            expected_parent_mode=0o750,
            expected_socket_mode=0o660,
        )

    async def wait_running(server: OperatorServer, task: asyncio.Task[None]) -> None:
        ready = asyncio.create_task(server.ready.wait())
        done, pending = await asyncio.wait(
            {ready, task},
            timeout=2,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for item in pending - {task}:
            item.cancel()
        if task in done:
            await task
        assert ready in done and ready.result() is True

    async def scenario() -> None:
        server = activated_server()
        task = asyncio.create_task(server.serve())
        await wait_running(server, task)
        api_client = client()
        try:
            for _ in range(100):
                if socket_path.is_socket():
                    break
                await asyncio.sleep(0.01)
            parent_info = socket_dir.lstat()
            socket_info = socket_path.lstat()
            assert (
                parent_info.st_uid,
                parent_info.st_gid,
                stat.S_IMODE(parent_info.st_mode),
            ) == (os.getuid(), os.getgid(), 0o750)
            assert (
                socket_info.st_uid,
                socket_info.st_gid,
                stat.S_IMODE(socket_info.st_mode),
            ) == (os.getuid(), os.getgid(), 0o660)
            assert {path.name for path in socket_dir.iterdir()} == {"operator.sock"}
            first_inode = socket_info.st_ino
            manifest = await api_client.describe()
            assert manifest.manifest_hash == "b" * 64
            started = await api_client.start(
                InferenceStartRequest(target_id="target-one", transition_id="t-one")
            )
            assert started.transition_id == "t-one"
            assert started.code is InferenceOperatorCode.ACCEPTED
            await operator.drain()
            status = await api_client.status(
                InferenceStatusRequest(target_id="target-one", transition_id="t-one")
            )
            assert status.transition_id == "t-one"
            assert runner.deadlines == [
                (InferenceTargetOperation.START, 2.0, "t-one"),
                (InferenceTargetOperation.STATUS, 2.0, "t-one"),
            ]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # The service process restarts behind systemd's still-open listener.
            # The API client keeps its #660 O_PATH pin and reconnects without
            # trusting a new pathname object.
            assert socket_path.stat().st_ino == first_inode
            replacement_server = activated_server()
            replacement = asyncio.create_task(replacement_server.serve())
            try:
                await wait_running(replacement_server, replacement)
                assert socket_path.stat().st_ino == first_inode
                assert {path.name for path in socket_dir.iterdir()} == {"operator.sock"}
                assert (await api_client.describe()).manifest_hash == "b" * 64
            finally:
                replacement.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await replacement

            # Replacing the systemd listener/path is a separate activation
            # generation. Even if a privileged actor recreates the exact mode,
            # the still-running API process must fail closed until it restarts.
            socket_path.unlink()
            rogue_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                rogue_listener.bind(str(socket_path))
                rogue_listener.listen()
                os.chown(socket_path, os.getuid(), os.getgid())
                os.chmod(socket_path, 0o660)
                assert socket_path.stat().st_ino != first_inode
                assert api_client.is_available is False
                with pytest.raises(OSError, match="identity changed"):
                    await api_client.describe()
            finally:
                rogue_listener.close()
                socket_path.unlink(missing_ok=True)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await api_client.aclose()
            activation_socket.close()
            socket_path.unlink(missing_ok=True)
            socket_dir.rmdir()

    asyncio.run(scenario())
