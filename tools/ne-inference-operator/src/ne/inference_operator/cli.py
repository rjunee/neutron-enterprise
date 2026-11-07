# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Service entry point for the confined inference host operator."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path

from ne.inference_operator.journal import StateJournal
from ne.inference_operator.manifest import load_private_manifest
from ne.inference_operator.operator import HostInferenceTargetOperator
from ne.inference_operator.server import OperatorServer, acquire_lifetime_lock


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--adapter-manifest", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--lifetime-lock", type=Path, required=True)
    parser.add_argument("--systemd-socket", action="store_true")
    parser.add_argument("--allowed-uid", type=int, required=True)
    parser.add_argument("--allowed-gid", type=int, required=True)
    parser.add_argument("--air-gap-file", type=Path)
    parser.add_argument("--provider-gid", type=int)
    return parser


def _air_gap_from_environment() -> bool:
    raw_air_gap = os.environ.get("AIR_GAP", "true").strip().lower()
    if raw_air_gap in {"1", "true", "yes", "on"}:
        return True
    if raw_air_gap in {"0", "false", "no", "off"}:
        return False
    raise SystemExit("AIR_GAP must be an explicit boolean")


def _air_gap_from_file(
    path: Path,
    *,
    owner_uid: int = 0,
    reader_gid: int | None = None,
) -> bool:
    """Read the only operator override without importing arbitrary environment."""
    try:
        info = path.lstat()
        content = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return True
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit("AIR_GAP policy file is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or path.resolve(strict=True) != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != owner_uid
        or (reader_gid is not None and info.st_gid != reader_gid)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != (0o640 if reader_gid is not None else 0o600)
    ):
        raise SystemExit("AIR_GAP policy file is unsafe")
    if content == "AIR_GAP=true\n":
        return True
    if content == "AIR_GAP=false\n":
        return False
    raise SystemExit("AIR_GAP policy file must contain one exact boolean")


def _activated_socket(path: Path) -> socket.socket:
    if (
        os.environ.get("LISTEN_PID") != str(os.getpid())
        or os.environ.get("LISTEN_FDS") != "1"
        or os.environ.get("LISTEN_FDNAMES") != "inference-operator"
    ):
        raise SystemExit("exact systemd socket activation is required")
    try:
        activated = socket.socket(fileno=os.dup(3))
    except OSError as exc:
        raise SystemExit("activated socket is unavailable") from exc
    if activated.getsockname() != str(path):
        activated.close()
        raise SystemExit("activated socket path is unexpected")
    return activated


def _bootstrap(args: argparse.Namespace, *, owner_uid: int = 0) -> tuple[int, OperatorServer]:
    lock_descriptor = acquire_lifetime_lock(args.lifetime_lock, owner_uid=owner_uid)
    activated: socket.socket | None = None
    try:
        provider_gid = getattr(args, "provider_gid", None)
        air_gap: bool | Callable[[], bool]
        if args.air_gap_file is not None:
            _air_gap_from_file(
                args.air_gap_file,
                owner_uid=owner_uid,
                reader_gid=provider_gid,
            )

            def live_air_gap() -> bool:
                return _air_gap_from_file(
                    args.air_gap_file,
                    owner_uid=owner_uid,
                    reader_gid=provider_gid,
                )

            air_gap = live_air_gap
        else:
            air_gap = _air_gap_from_environment()
        manifest = load_private_manifest(
            args.registry,
            args.adapter_manifest,
            owner_uid=owner_uid,
            _document_mode=0o640 if provider_gid is not None else 0o600,
            _document_reader_gid=provider_gid,
            _vast_reader_gid=provider_gid,
        )
        journal = StateJournal(args.journal, owner_uid=owner_uid)
        operator = HostInferenceTargetOperator(
            manifest,
            journal,
            air_gap=air_gap,
            actor=f"peer-uid:{args.allowed_uid}",
        )
        if args.systemd_socket:
            activated = _activated_socket(args.socket)
        server = OperatorServer(
            operator,
            args.socket,
            activated_socket=activated,
            allowed_uid=args.allowed_uid,
            allowed_gid=args.allowed_gid,
            owner_uid=owner_uid,
        )
        return lock_descriptor, server
    except BaseException:
        if activated is not None:
            activated.close()
        os.close(lock_descriptor)
        raise


def main() -> None:
    args = _parser().parse_args()
    lock_descriptor, server = _bootstrap(args)
    try:
        asyncio.run(server.serve())
    finally:
        if server.activated_socket is not None:
            with contextlib.suppress(OSError):
                server.activated_socket.close()
        os.close(lock_descriptor)


if __name__ == "__main__":
    main()
