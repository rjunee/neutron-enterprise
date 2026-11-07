# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closed, peer-authenticated transport to the non-root provider broker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import stat
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ne.contracts.inference import InferenceOperatorCode, InferenceTargetOperation

PROTOCOL_VERSION = 1
MAX_BROKER_FRAME_BYTES = 4096
VAST_TARGET_ID = "private-vast-gpu"
_SAFE_ID = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._:-")
_BROKER_CODES = frozenset(
    {
        InferenceOperatorCode.OBSERVED,
        InferenceOperatorCode.AIR_GAP_REFUSED,
        InferenceOperatorCode.DEADLINE_EXCEEDED,
        InferenceOperatorCode.READINESS_FAILED,
        InferenceOperatorCode.SCRIPT_REFUSED,
        InferenceOperatorCode.PROVIDER_UNAVAILABLE,
        InferenceOperatorCode.INTERRUPTED,
        InferenceOperatorCode.INTERNAL_ERROR,
    }
)


class ProviderBrokerRefused(ValueError):
    """The broker transport or its closed envelope is unavailable or unsafe."""


@dataclass(frozen=True)
class BrokerResult:
    code: InferenceOperatorCode
    returncode: int | None


class ProviderBackend(Protocol):
    async def invoke(
        self,
        operation: InferenceTargetOperation,
        *,
        transition_id: str,
        deadline_at: float,
    ) -> BrokerResult: ...


def _safe_id(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or any(character not in _SAFE_ID for character in value)
    ):
        raise ProviderBrokerRefused(f"{label} is invalid")
    return value


def _exact_object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProviderBrokerRefused("provider broker envelope has an invalid closed shape")
    return value


def _encode(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_BROKER_FRAME_BYTES:
        raise ProviderBrokerRefused("provider broker frame is too large")
    return struct.pack("!I", len(payload)) + payload


async def _read(reader: asyncio.StreamReader, *, deadline_s: float) -> dict[str, Any]:
    if deadline_s <= 0:
        raise TimeoutError

    async def read_frame() -> bytes:
        header = await reader.readexactly(4)
        size = struct.unpack("!I", header)[0]
        if size < 2 or size > MAX_BROKER_FRAME_BYTES:
            raise ProviderBrokerRefused("provider broker frame size is refused")
        return await reader.readexactly(size)

    payload = await asyncio.wait_for(read_frame(), timeout=deadline_s)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderBrokerRefused("provider broker frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProviderBrokerRefused("provider broker frame must be an object")
    return value


def _peer_credentials(writer: asyncio.StreamWriter) -> tuple[int, int]:
    transport_socket = writer.get_extra_info("socket")
    peer_option = getattr(socket, "SO_PEERCRED", None)
    if transport_socket is None or peer_option is None:
        raise ProviderBrokerRefused("provider broker peer credentials are unavailable")
    raw = transport_socket.getsockopt(socket.SOL_SOCKET, peer_option, 12)
    _pid, uid, gid = struct.unpack("3i", raw)
    return uid, gid


class ProviderBrokerClient:
    """Root-side AF_UNIX client with exact broker identity and bounded frames."""

    def __init__(
        self,
        socket_path: Path = Path("/run/neutron/inference-provider/provider.sock"),
        *,
        broker_uid: int = 10002,
        broker_gid: int = 10002,
        socket_gid: int = 10001,
        owner_uid: int = 0,
    ) -> None:
        self._socket_path = socket_path
        self._broker_uid = broker_uid
        self._broker_gid = broker_gid
        self._socket_gid = socket_gid
        self._owner_uid = owner_uid

    def _validate_path(self) -> None:
        path = self._socket_path
        try:
            parent = path.parent.lstat()
            leaf = path.lstat()
        except OSError as exc:
            raise ProviderBrokerRefused("provider broker socket is unavailable") from exc
        if (
            not path.is_absolute()
            or path.parent.is_symlink()
            or path.parent.resolve(strict=True) != path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != self._owner_uid
            or parent.st_gid != self._socket_gid
            or stat.S_IMODE(parent.st_mode) != 0o750
            or not stat.S_ISSOCK(leaf.st_mode)
            or leaf.st_uid != self._owner_uid
            or leaf.st_gid != self._socket_gid
            or stat.S_IMODE(leaf.st_mode) != 0o660
        ):
            raise ProviderBrokerRefused("provider broker socket metadata is unsafe")

    async def invoke(
        self,
        operation: InferenceTargetOperation,
        *,
        transition_id: str,
        deadline_s: float,
    ) -> BrokerResult:
        transition_id = _safe_id(transition_id, "transition id")
        if operation not in {
            InferenceTargetOperation.STATUS,
            InferenceTargetOperation.START,
            InferenceTargetOperation.STOP,
        }:
            raise ProviderBrokerRefused("provider operation is unsupported")
        if not 0 < deadline_s <= 900:
            raise ProviderBrokerRefused("provider deadline is outside its bound")
        deadline_at = asyncio.get_running_loop().time() + deadline_s
        self._validate_path()
        writer: asyncio.StreamWriter | None = None
        try:
            remaining = deadline_at - asyncio.get_running_loop().time()
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._socket_path), timeout=remaining
            )
            if _peer_credentials(writer) != (self._broker_uid, self._broker_gid):
                raise ProviderBrokerRefused("provider broker process identity is refused")
            self._validate_path()
            request = {
                "deadline_monotonic_ns": int(deadline_at * 1_000_000_000),
                "operation": operation.value,
                "protocol_version": PROTOCOL_VERSION,
                "target_id": VAST_TARGET_ID,
                "transition_id": transition_id,
            }
            writer.write(_encode(request))
            await asyncio.wait_for(
                writer.drain(), timeout=deadline_at - asyncio.get_running_loop().time()
            )
            remaining = deadline_at - asyncio.get_running_loop().time()
            response = _exact_object(
                await _read(reader, deadline_s=remaining),
                {"code", "protocol_version", "returncode", "transition_id"},
            )
            if response["protocol_version"] != PROTOCOL_VERSION:
                raise ProviderBrokerRefused("provider broker protocol version is refused")
            if response["transition_id"] != transition_id:
                raise ProviderBrokerRefused("provider broker transition echo is refused")
            try:
                code = InferenceOperatorCode(response["code"])
            except (TypeError, ValueError) as exc:
                raise ProviderBrokerRefused("provider broker code is refused") from exc
            returncode = response["returncode"]
            if code not in _BROKER_CODES or returncode not in {None, 0, 3}:
                raise ProviderBrokerRefused("provider broker result is refused")
            if code is InferenceOperatorCode.OBSERVED and returncode not in {0, 3}:
                raise ProviderBrokerRefused("provider broker observed result is contradictory")
            if code is not InferenceOperatorCode.OBSERVED and returncode is not None:
                raise ProviderBrokerRefused("provider broker failure carries a return code")
            return BrokerResult(code, returncode)
        except TimeoutError:
            return BrokerResult(InferenceOperatorCode.DEADLINE_EXCEEDED, None)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            return BrokerResult(InferenceOperatorCode.PROVIDER_UNAVAILABLE, None)
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.wait_closed()


class ProviderBrokerServer:
    """Non-root broker transport; the backend alone owns provider details."""

    def __init__(
        self,
        backend: ProviderBackend,
        activated_socket: socket.socket,
        *,
        allowed_uid: int = 0,
        allowed_gid: int = 10001,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._backend = backend
        self._socket = activated_socket
        self._allowed_uid = allowed_uid
        self._allowed_gid = allowed_gid
        self._clock = clock

    async def _reply(self, writer: asyncio.StreamWriter, value: dict[str, Any]) -> None:
        writer.write(_encode(value))
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        transition_id = "refused"
        try:
            if _peer_credentials(writer) != (self._allowed_uid, self._allowed_gid):
                raise ProviderBrokerRefused("provider broker caller identity is refused")
            request = _exact_object(
                await _read(reader, deadline_s=2.0),
                {
                    "deadline_monotonic_ns",
                    "operation",
                    "protocol_version",
                    "target_id",
                    "transition_id",
                },
            )
            transition_id = _safe_id(request["transition_id"], "transition id")
            if (
                request["protocol_version"] != PROTOCOL_VERSION
                or request["target_id"] != VAST_TARGET_ID
            ):
                raise ProviderBrokerRefused("provider broker request authority is refused")
            try:
                operation = InferenceTargetOperation(request["operation"])
            except (TypeError, ValueError) as exc:
                raise ProviderBrokerRefused("provider broker operation is refused") from exc
            deadline_ns = request["deadline_monotonic_ns"]
            if (
                isinstance(deadline_ns, bool)
                or not isinstance(deadline_ns, int)
                or deadline_ns <= 0
            ):
                raise ProviderBrokerRefused("provider broker deadline is refused")
            loop = asyncio.get_running_loop()
            now = self._clock or loop.time
            deadline_at = deadline_ns / 1_000_000_000
            if not 0 < deadline_at - now() <= 900:
                raise ProviderBrokerRefused("provider broker deadline is refused")
            result = await self._backend.invoke(
                operation,
                transition_id=transition_id,
                deadline_at=deadline_at,
            )
            await self._reply(
                writer,
                {
                    "code": result.code.value,
                    "protocol_version": PROTOCOL_VERSION,
                    "returncode": result.returncode,
                    "transition_id": transition_id,
                },
            )
        except (ProviderBrokerRefused, TimeoutError, asyncio.IncompleteReadError, OSError):
            await self._reply(
                writer,
                {
                    "code": InferenceOperatorCode.SCRIPT_REFUSED.value,
                    "protocol_version": PROTOCOL_VERSION,
                    "returncode": None,
                    "transition_id": transition_id,
                },
            )
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def serve(self) -> None:
        if sys.version_info >= (3, 13):
            server = await asyncio.start_unix_server(
                self._handle,
                sock=self._socket,
                cleanup_socket=False,
            )
        else:
            server = await asyncio.start_unix_server(self._handle, sock=self._socket)
        async with server:
            await server.serve_forever()


__all__ = [
    "MAX_BROKER_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "VAST_TARGET_ID",
    "BrokerResult",
    "ProviderBackend",
    "ProviderBrokerClient",
    "ProviderBrokerRefused",
    "ProviderBrokerServer",
]
