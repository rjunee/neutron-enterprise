# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Real HTTP-over-UDS deadline and endpoint-integrity controls."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.api.inference_lifecycle import (
    InferenceLifecycleConfig,
    InferenceLifecycleService,
    LifecycleUnavailable,
    UdsInferenceTargetOperator,
)
from ne.audit import ensure_audit_schema
from ne.contracts.inference import (
    InferenceManifestTarget,
    InferenceOperatorCode,
    InferenceOperatorManifest,
    InferenceOperatorObservation,
    InferenceTargetOperation,
    InferenceTargetState,
)

from .test_inference_lifecycle import MANIFEST_HASH, NOW, target


@pytest.fixture
def socket_parent():
    parent = Path(tempfile.mkdtemp(prefix="ne-uds-", dir=Path(tempfile.gettempdir()).resolve()))
    parent.chmod(0o750)
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _manifest() -> InferenceOperatorManifest:
    configured = target()
    return InferenceOperatorManifest(
        manifest_hash=MANIFEST_HASH,
        targets=(
            InferenceManifestTarget(
                target_id=configured.target_id,
                adapter_id=configured.adapter_id,
                resource_id=configured.resource_id,
                capabilities=configured.capabilities,
            ),
        ),
    )


async def _serve(
    socket_path,
    *,
    manifest_delay: float = 0,
    status_delay: float = 0,
) -> tuple[asyncio.AbstractServer, Callable[[], Awaitable[None]]]:
    manifest = _manifest()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            request_line = header.split(b"\r\n", 1)[0].decode()
            path = request_line.split(" ")[1]
            content_length = 0
            for line in header.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
            body = await reader.readexactly(content_length) if content_length else b"{}"
            if path == "/v1/manifest":
                await asyncio.sleep(manifest_delay)
                payload = manifest.model_dump(mode="json")
            else:
                await asyncio.sleep(status_delay)
                request = json.loads(body)
                payload = InferenceOperatorObservation(
                    target_id="cpu-vllm",
                    adapter_id="compose-v1",
                    resource_id="compose:vllm-cpu",
                    operation=InferenceTargetOperation.STATUS,
                    transition_id=request.get("transition_id"),
                    state=InferenceTargetState.READY,
                    code=InferenceOperatorCode.OBSERVED,
                    manifest_hash=MANIFEST_HASH,
                    observed_at=NOW,
                ).model_dump(mode="json")
            raw = json.dumps(payload).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(raw)}\r\n\r\n".encode()
                + raw
            )
            await writer.drain()
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    os.chmod(socket_path, 0o660)

    async def close() -> None:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)

    return server, close


def _operator(socket_path) -> UdsInferenceTargetOperator:
    parent_stat = socket_path.parent.stat()
    return UdsInferenceTargetOperator(
        str(socket_path),
        expected_uid=parent_stat.st_uid,
        expected_gid=parent_stat.st_gid,
        expected_parent_mode=0o750,
        expected_socket_mode=0o660,
    )


def _service(tmp_path, socket_path, configured_target) -> InferenceLifecycleService:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'uds-audit.db'}")
    ensure_audit_schema(engine)
    return InferenceLifecycleService(
        engine=engine,
        config=InferenceLifecycleConfig(targets=(configured_target,)),
        operator=_operator(socket_path),
        air_gap_provider=lambda: False,
        config_hash="b" * 64,
        clock=lambda: NOW,
    )


@pytest.mark.anyio
async def test_real_uds_status_can_exceed_ten_seconds_within_target_deadline(
    tmp_path, socket_parent
) -> None:
    socket_path = socket_parent / "operator.sock"
    _, close = await _serve(socket_path, status_delay=10.1)
    lifecycle = _service(
        tmp_path,
        socket_path,
        target().model_copy(update={"status_deadline_s": 12.0}),
    )
    try:
        row = await lifecycle.reconcile("cpu-vllm")
        assert row.state is InferenceTargetState.READY
        assert row.code is InferenceOperatorCode.OBSERVED
    finally:
        await lifecycle.aclose()
        await close()


@pytest.mark.anyio
async def test_handshake_and_status_share_one_total_monotonic_deadline(
    tmp_path, socket_parent
) -> None:
    socket_path = socket_parent / "operator.sock"
    _, close = await _serve(socket_path, manifest_delay=0.08, status_delay=0.08)
    lifecycle = _service(
        tmp_path,
        socket_path,
        target().model_copy(update={"status_deadline_s": 0.12}),
    )
    try:
        with pytest.raises(LifecycleUnavailable, match="deadline"):
            await lifecycle.reconcile("cpu-vllm")
        row = lifecycle.state("cpu-vllm")
        assert row.state is InferenceTargetState.ERROR
        assert row.code is InferenceOperatorCode.DEADLINE_EXCEEDED
    finally:
        await lifecycle.aclose()
        await close()


@pytest.mark.anyio
async def test_replaced_same_manifest_socket_is_not_retrusted(socket_parent) -> None:
    socket_path = socket_parent / "operator.sock"
    _, close_first = await _serve(socket_path)
    operator = _operator(socket_path)
    await operator.describe()
    await close_first()
    _, close_second = await _serve(socket_path)
    try:
        assert operator.is_available is False
        with pytest.raises(OSError, match="identity changed"):
            await operator.describe()
    finally:
        await operator.aclose()
        await close_second()
