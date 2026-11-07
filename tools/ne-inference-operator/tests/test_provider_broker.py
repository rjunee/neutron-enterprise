# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from ne.contracts.inference import InferenceOperatorCode, InferenceTargetOperation
from ne.inference_operator import provider_broker as broker_module
from ne.inference_operator import provider_cli
from ne.inference_operator.air_gap_condition import provider_control_enabled
from ne.inference_operator.manifest import AdapterPolicy, VerifiedFile
from ne.inference_operator.provider_backend import (
    NFT_POLICY_SHA256,
    ProviderEgressReceipt,
    VastProviderBackend,
)
from ne.inference_operator.provider_broker import (
    BrokerResult,
    ProviderBrokerClient,
    ProviderBrokerRefused,
    ProviderBrokerServer,
)
from ne.inference_operator.runner import RunResult


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[InferenceTargetOperation, str, float]] = []

    async def invoke(
        self,
        operation: InferenceTargetOperation,
        *,
        transition_id: str,
        deadline_at: float,
    ) -> BrokerResult:
        self.calls.append((operation, transition_id, deadline_at))
        return BrokerResult(InferenceOperatorCode.OBSERVED, 0)


def test_broker_service_restart_preserves_activated_socket_path(tmp_path: Path) -> None:
    del tmp_path
    short_root = Path(tempfile.mkdtemp(prefix="nepb-"))
    path = short_root / "b" / "provider.sock"
    listener = _listener(path)
    server = ProviderBrokerServer(
        _Backend(),
        listener,
        allowed_uid=os.getuid(),
        allowed_gid=os.getgid(),
    )

    async def scenario() -> None:
        task = asyncio.create_task(server.serve())
        for _ in range(100):
            if task.done():
                await task
            await asyncio.sleep(0.001)
            if path.is_socket():
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
        assert path.is_socket()
    finally:
        listener.close()
        shutil.rmtree(short_root)


def test_provider_activation_condition_is_exact_and_fail_closed(tmp_path: Path) -> None:
    policy = tmp_path / "environment"
    assert provider_control_enabled(policy, owner_uid=os.getuid(), reader_gid=os.getgid()) is False
    policy.write_text("AIR_GAP=true\n", encoding="ascii")
    policy.chmod(0o640)
    assert provider_control_enabled(policy, owner_uid=os.getuid(), reader_gid=os.getgid()) is False
    policy.write_text("AIR_GAP=false\n", encoding="ascii")
    assert provider_control_enabled(policy, owner_uid=os.getuid(), reader_gid=os.getgid()) is True
    policy.write_text("AIR_GAP=false\nEXTRA=true\n", encoding="ascii")
    assert provider_control_enabled(policy, owner_uid=os.getuid(), reader_gid=os.getgid()) is False


def test_broker_envelope_refuses_extra_endpoint_or_command_fields() -> None:
    for field in ("endpoint", "command", "argv", "secret"):
        with pytest.raises(ProviderBrokerRefused, match="closed shape"):
            broker_module._exact_object(
                {"operation": "status", field: "attacker-controlled"},
                {"operation"},
            )


def _egress_receipt(tmp_path: Path) -> tuple[Path, Path, datetime]:
    namespace = tmp_path / "netns"
    namespace.write_text("namespace", encoding="utf-8")
    now = datetime(2026, 8, 20, tzinfo=UTC)
    receipt = tmp_path / "egress-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "addresses": ["1.1.1.1"],
                "authority": "console.vast.ai:443",
                "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "netns_inode": namespace.stat().st_ino,
                "policy_sha256": NFT_POLICY_SHA256,
                "schema": "ne-inference-provider-egress-v1",
            }
        ),
        encoding="utf-8",
    )
    receipt.chmod(0o640)
    return receipt, namespace, now


def test_egress_receipt_binds_fixed_authority_expiry_and_namespace(tmp_path: Path) -> None:
    receipt, namespace, now = _egress_receipt(tmp_path)
    policy = ProviderEgressReceipt(
        receipt,
        owner_uid=os.getuid(),
        reader_gid=os.getgid(),
        network_namespace=namespace,
        now=lambda: now,
    )
    policy.validate()

    document = json.loads(receipt.read_text(encoding="utf-8"))
    for field, value in (
        ("authority", "console.vast.ai.attacker.invalid:443"),
        ("addresses", ["127.0.0.1"]),
        ("netns_inode", namespace.stat().st_ino + 1),
    ):
        mutated = dict(document)
        mutated[field] = value
        receipt.write_text(json.dumps(mutated), encoding="utf-8")
        with pytest.raises(ProviderBrokerRefused):
            policy.validate()
    receipt.write_text(json.dumps(document), encoding="utf-8")
    policy.validate()


def test_backend_rereads_air_gap_and_receipt_before_direct_runner(tmp_path: Path) -> None:
    receipt, namespace, now = _egress_receipt(tmp_path)
    egress = ProviderEgressReceipt(
        receipt,
        owner_uid=os.getuid(),
        reader_gid=os.getgid(),
        network_namespace=namespace,
        now=lambda: now,
    )
    policy = AdapterPolicy(
        adapter_id="vast-private-rig-v1",
        executable=VerifiedFile(Path("/unused"), "a" * 64),
        verified_files=(),
        operations=MappingProxyType(
            {operation: (operation.value,) for operation in InferenceTargetOperation}
        ),
        environment=MappingProxyType({}),
        provider_egress=True,
    )
    air_gap = False

    class _Runner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, *args: object, **kwargs: object) -> RunResult:
            self.calls += 1
            return RunResult(InferenceOperatorCode.OBSERVED, 0)

    runner = _Runner()
    backend = VastProviderBackend(
        policy,
        runner=runner,  # type: ignore[arg-type]
        air_gap=lambda: air_gap,
        egress_receipt=egress,
    )

    async def scenario() -> None:
        nonlocal air_gap
        deadline = asyncio.get_running_loop().time() + 1
        result = await backend.invoke(
            InferenceTargetOperation.STATUS,
            transition_id="status-one",
            deadline_at=deadline,
        )
        assert result == BrokerResult(InferenceOperatorCode.OBSERVED, 0)
        assert runner.calls == 1
        air_gap = True
        refused = await backend.invoke(
            InferenceTargetOperation.STATUS,
            transition_id="status-two",
            deadline_at=deadline,
        )
        assert refused == BrokerResult(InferenceOperatorCode.AIR_GAP_REFUSED, None)
        assert runner.calls == 1

    asyncio.run(scenario())


def test_backend_erases_private_child_returncode_from_typed_failure(tmp_path: Path) -> None:
    receipt, namespace, now = _egress_receipt(tmp_path)
    policy = AdapterPolicy(
        adapter_id="vast-private-rig-v1",
        executable=VerifiedFile(Path("/unused"), "a" * 64),
        verified_files=(),
        operations=MappingProxyType(
            {operation: (operation.value,) for operation in InferenceTargetOperation}
        ),
        environment=MappingProxyType({}),
        provider_egress=True,
    )

    class _FailingRunner:
        async def run(self, *args: object, **kwargs: object) -> RunResult:
            return RunResult(InferenceOperatorCode.READINESS_FAILED, 17)

    backend = VastProviderBackend(
        policy,
        runner=_FailingRunner(),  # type: ignore[arg-type]
        air_gap=lambda: False,
        egress_receipt=ProviderEgressReceipt(
            receipt,
            owner_uid=os.getuid(),
            reader_gid=os.getgid(),
            network_namespace=namespace,
            now=lambda: now,
        ),
    )

    async def scenario() -> None:
        result = await backend.invoke(
            InferenceTargetOperation.START,
            transition_id="start-failed",
            deadline_at=asyncio.get_running_loop().time() + 1,
        )
        assert result == BrokerResult(InferenceOperatorCode.READINESS_FAILED, None)

    asyncio.run(scenario())


def test_provider_state_seed_retains_only_fixed_instance_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_cli, "PROVIDER_UID", os.getuid())
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    source = tmp_path / "credential"
    source.write_text(
        json.dumps({"host": "attacker.invalid", "id": 42, "ssh_port": 65535}),
        encoding="utf-8",
    )
    source.chmod(0o400)
    destination = state / "instance.json"
    provider_cli._seed_state(source, destination)
    assert destination.read_text(encoding="utf-8") == '{"id":42}'
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    destination.write_text(
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
    destination.chmod(0o600)
    provider_cli._seed_state(source, destination)
    wrong = json.loads(destination.read_text(encoding="utf-8"))
    wrong["id"] = 43
    destination.write_text(json.dumps(wrong), encoding="utf-8")
    destination.chmod(0o600)
    with pytest.raises(SystemExit, match="does not match"):
        provider_cli._seed_state(source, destination)
    destination.unlink()
    source.chmod(0o440)
    with pytest.raises(SystemExit, match="metadata"):
        provider_cli._seed_state(source, destination)


def _listener(path: Path) -> socket.socket:
    path.parent.mkdir(mode=0o750)
    path.parent.chmod(0o750)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o660)
    listener.listen(8)
    listener.setblocking(False)
    return listener


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is a Linux boundary")
def test_broker_round_trip_is_closed_and_transition_bound(tmp_path: Path) -> None:
    path = tmp_path / "broker" / "provider.sock"
    listener = _listener(path)
    backend = _Backend()
    server = ProviderBrokerServer(
        backend,
        listener,
        allowed_uid=os.getuid(),
        allowed_gid=os.getgid(),
    )
    client = ProviderBrokerClient(
        path,
        broker_uid=os.getuid(),
        broker_gid=os.getgid(),
        socket_gid=os.getgid(),
        owner_uid=os.getuid(),
    )

    async def scenario() -> None:
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0)
        try:
            result = await client.invoke(
                InferenceTargetOperation.START,
                transition_id="start-123",
                deadline_s=1,
            )
            assert result == BrokerResult(InferenceOperatorCode.OBSERVED, 0)
            assert [(call[0], call[1]) for call in backend.calls] == [
                (InferenceTargetOperation.START, "start-123")
            ]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        asyncio.run(scenario())
        assert path.is_socket(), "broker restart unlinked systemd's stable listener"
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is a Linux boundary")
def test_backend_server_client_preserve_typed_failure_without_child_code(tmp_path: Path) -> None:
    receipt, namespace, now = _egress_receipt(tmp_path)
    policy = AdapterPolicy(
        adapter_id="vast-private-rig-v1",
        executable=VerifiedFile(Path("/unused"), "a" * 64),
        verified_files=(),
        operations=MappingProxyType(
            {operation: (operation.value,) for operation in InferenceTargetOperation}
        ),
        environment=MappingProxyType({}),
        provider_egress=True,
    )

    class _FailingRunner:
        async def run(self, *args: object, **kwargs: object) -> RunResult:
            return RunResult(InferenceOperatorCode.READINESS_FAILED, 17)

    backend = VastProviderBackend(
        policy,
        runner=_FailingRunner(),  # type: ignore[arg-type]
        air_gap=lambda: False,
        egress_receipt=ProviderEgressReceipt(
            receipt,
            owner_uid=os.getuid(),
            reader_gid=os.getgid(),
            network_namespace=namespace,
            now=lambda: now,
        ),
    )
    path = tmp_path / "broker" / "provider.sock"
    listener = _listener(path)
    server = ProviderBrokerServer(
        backend,
        listener,
        allowed_uid=os.getuid(),
        allowed_gid=os.getgid(),
    )
    client = ProviderBrokerClient(
        path,
        broker_uid=os.getuid(),
        broker_gid=os.getgid(),
        socket_gid=os.getgid(),
        owner_uid=os.getuid(),
    )

    async def scenario() -> None:
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0)
        try:
            result = await client.invoke(
                InferenceTargetOperation.START,
                transition_id="start-failed",
                deadline_s=1,
            )
            assert result == BrokerResult(InferenceOperatorCode.READINESS_FAILED, None)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        asyncio.run(scenario())
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is a Linux boundary")
def test_broker_refuses_endpoint_field_before_backend(tmp_path: Path) -> None:
    path = tmp_path / "broker" / "provider.sock"
    listener = _listener(path)
    backend = _Backend()
    server = ProviderBrokerServer(
        backend,
        listener,
        allowed_uid=os.getuid(),
        allowed_gid=os.getgid(),
    )

    async def scenario() -> None:
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0)
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            payload = json.dumps(
                {
                    "deadline_monotonic_ns": int(
                        (asyncio.get_running_loop().time() + 1) * 1_000_000_000
                    ),
                    "endpoint": "https://attacker.invalid",
                    "operation": "start",
                    "protocol_version": 1,
                    "target_id": "private-vast-gpu",
                    "transition_id": "start-123",
                }
            ).encode()
            writer.write(struct.pack("!I", len(payload)) + payload)
            await writer.drain()
            size = struct.unpack("!I", await reader.readexactly(4))[0]
            response = json.loads(await reader.readexactly(size))
            assert response == {
                "code": "script-refused",
                "protocol_version": 1,
                "returncode": None,
                "transition_id": "refused",
            }
            assert backend.calls == []
            writer.close()
            await writer.wait_closed()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        asyncio.run(scenario())
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is a Linux boundary")
def test_client_refuses_wrong_broker_process_identity(tmp_path: Path) -> None:
    path = tmp_path / "broker" / "provider.sock"
    listener = _listener(path)
    backend = _Backend()
    server = ProviderBrokerServer(
        backend,
        listener,
        allowed_uid=os.getuid(),
        allowed_gid=os.getgid(),
    )
    client = ProviderBrokerClient(
        path,
        broker_uid=os.getuid() + 1,
        broker_gid=os.getgid(),
        socket_gid=os.getgid(),
        owner_uid=os.getuid(),
    )

    async def scenario() -> None:
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0)
        try:
            with pytest.raises(ProviderBrokerRefused, match="process identity"):
                await client.invoke(
                    InferenceTargetOperation.STATUS,
                    transition_id="status-123",
                    deadline_s=1,
                )
            assert backend.calls == []
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        asyncio.run(scenario())
    finally:
        listener.close()
