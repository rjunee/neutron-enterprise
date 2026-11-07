# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bounded HTTP-over-UDS transport with Linux peer authentication."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import socket
import stat
import struct
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ne.contracts.inference import (
    InferenceStartRequest,
    InferenceStatusRequest,
    InferenceStopRequest,
)
from ne.inference_operator.operator import HostInferenceTargetOperator

MAX_FRAME_BYTES = 16 * 1024
MAX_HEADER_BYTES = 8 * 1024


class OperatorServer:
    def __init__(
        self,
        operator: HostInferenceTargetOperator,
        socket_path: Path,
        *,
        activated_socket: socket.socket | None = None,
        allowed_uid: int,
        allowed_gid: int,
        owner_uid: int = 0,
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("operator socket path must be absolute")
        self.operator = operator
        self.socket_path = socket_path
        self.activated_socket = activated_socket
        self.allowed_uid = allowed_uid
        self.allowed_gid = allowed_gid
        self.owner_uid = owner_uid
        self.ready = asyncio.Event()

    def _peer_allowed(self, writer: asyncio.StreamWriter) -> bool:
        transport_socket = writer.get_extra_info("socket")
        peer_option = getattr(socket, "SO_PEERCRED", None)
        if transport_socket is None or peer_option is None:
            return False
        raw = transport_socket.getsockopt(socket.SOL_SOCKET, peer_option, 12)
        _pid, uid, gid = struct.unpack("3i", raw)
        return uid == self.allowed_uid and gid == self.allowed_gid

    async def _dispatch(self, method: str, path: str, request: Any) -> dict[str, Any]:
        if method == "GET" and path == "/v1/manifest" and request is None:
            result = await self.operator.describe()
        elif method == "POST" and path == "/v1/status":
            result = await self.operator.status(InferenceStatusRequest.model_validate(request))
        elif method == "POST" and path == "/v1/start":
            result = await self.operator.start(InferenceStartRequest.model_validate(request))
        elif method == "POST" and path == "/v1/stop":
            result = await self.operator.stop(InferenceStopRequest.model_validate(request))
        else:
            raise ValueError("unsupported route")
        return result.model_dump(mode="json")

    @staticmethod
    async def _response(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_FRAME_BYTES:
            status, encoded = 500, b'{"code":"internal-error"}'
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 500: "Internal Server Error"}[
            status
        ]
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(headers + encoded)
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if not self._peer_allowed(writer):
                await self._response(writer, 403, {"code": "transport-unavailable"})
                return
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2.0)
            if len(header) > MAX_HEADER_BYTES:
                raise ValueError("headers too large")
            lines = header[:-4].split(b"\r\n")
            request_line = lines[0].decode("ascii").split(" ")
            if len(request_line) != 3 or request_line[2] != "HTTP/1.1":
                raise ValueError("invalid request line")
            method, path, _version = request_line
            headers: dict[str, str] = {}
            for raw_line in lines[1:]:
                name, separator, value = raw_line.partition(b":")
                if not separator:
                    raise ValueError("invalid header")
                key = name.decode("ascii").strip().lower()
                if key in headers:
                    raise ValueError("duplicate header")
                headers[key] = value.decode("ascii").strip()
            if method == "GET":
                if headers.get("content-length", "0") != "0":
                    raise ValueError("GET body refused")
                document = None
            elif method == "POST":
                raw_length = headers.get("content-length")
                if raw_length is None or not raw_length.isdigit():
                    raise ValueError("content length required")
                size = int(raw_length)
                if size < 2 or size > MAX_FRAME_BYTES:
                    raise ValueError("body size refused")
                document = json.loads(await asyncio.wait_for(reader.readexactly(size), timeout=2.0))
            else:
                raise ValueError("method refused")
            await self._response(writer, 200, await self._dispatch(method, path, document))
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ):
            await self._response(writer, 400, {"code": "transport-unavailable"})
        finally:
            writer.close()
            await writer.wait_closed()

    def _prepare_directory(self) -> None:
        parent = self.socket_path.parent
        try:
            parent.mkdir(mode=0o750, parents=False)
            os.chown(parent, self.owner_uid, self.allowed_gid)
            os.chmod(parent, 0o750)
        except FileExistsError:
            pass
        info = parent.lstat()
        if (
            parent.is_symlink()
            or parent.resolve(strict=True) != parent
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != self.owner_uid
            or info.st_gid != self.allowed_gid
            or stat.S_IMODE(info.st_mode) != 0o750
        ):
            raise ValueError("operator socket directory is unsafe")

    def _validate_socket(self) -> None:
        current = self.socket_path.lstat()
        if (
            not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != self.owner_uid
            or current.st_gid != self.allowed_gid
            or stat.S_IMODE(current.st_mode) != 0o660
        ):
            raise ValueError("operator socket ownership could not be established")

    def _validate_activated_socket(self) -> None:
        activated = self.activated_socket
        if activated is None:
            raise ValueError("activated socket is unavailable")
        if (
            activated.family != socket.AF_UNIX
            or activated.type & socket.SOCK_STREAM != socket.SOCK_STREAM
            or activated.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
            or activated.getsockname() != str(self.socket_path)
        ):
            raise ValueError("activated socket does not match the closed endpoint")
        self._validate_socket()

    async def serve(self) -> None:
        self._prepare_directory()
        if self.activated_socket is not None:
            self._validate_activated_socket()
            if sys.version_info >= (3, 13):
                # Python 3.13 defaults cleanup_socket=True even for an
                # inherited systemd listener. Never unlink systemd's stable
                # pathname when only this service process restarts.
                server = await asyncio.start_unix_server(
                    self._handle,
                    sock=self.activated_socket,
                    cleanup_socket=False,
                )
            else:
                server = await asyncio.start_unix_server(
                    self._handle,
                    sock=self.activated_socket,
                )
        else:
            try:
                existing = self.socket_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    not stat.S_ISSOCK(existing.st_mode)
                    or existing.st_uid != self.owner_uid
                    or existing.st_gid != self.allowed_gid
                    or stat.S_IMODE(existing.st_mode) != 0o660
                ):
                    raise ValueError("operator socket path is occupied by an unsafe file")
                self.socket_path.unlink()
            server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
            os.chown(self.socket_path, self.owner_uid, self.allowed_gid)
            os.chmod(self.socket_path, 0o660)
            try:
                self._validate_socket()
            except ValueError:
                server.close()
                await server.wait_closed()
                raise
        self.ready.set()
        try:
            async with server:
                await server.serve_forever()
        finally:
            self.ready.clear()


def acquire_lifetime_lock(path: Path, *, owner_uid: int = 0) -> int:
    """Acquire the process singleton before any journal read or recovery."""
    if not path.is_absolute():
        raise ValueError("operator lifetime lock path must be absolute")
    parent = path.parent
    parent_info = parent.lstat()
    if (
        parent.is_symlink()
        or parent.resolve(strict=True) != parent
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != owner_uid
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise ValueError("operator lifetime lock directory is unsafe")
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != owner_uid
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or path.resolve(strict=True) != path
    ):
        os.close(descriptor)
        raise ValueError("operator lifetime lock is unsafe")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise ValueError("another operator instance owns the socket") from None
    return descriptor


__all__ = ["MAX_FRAME_BYTES", "OperatorServer", "acquire_lifetime_lock"]
