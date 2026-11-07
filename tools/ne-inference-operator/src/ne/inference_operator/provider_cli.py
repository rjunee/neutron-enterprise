# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Entry point for the non-root, network-confined provider broker."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import stat
import tempfile
from pathlib import Path

from ne.inference_operator.cli import _air_gap_from_file
from ne.inference_operator.manifest import load_private_manifest
from ne.inference_operator.provider_backend import ProviderEgressReceipt, VastProviderBackend
from ne.inference_operator.provider_broker import ProviderBrokerServer
from ne.inference_operator.runner import PinnedRunner

PROVIDER_UID = 10002
PROVIDER_GID = 10002
OPERATOR_GID = 10001


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--adapter-manifest", type=Path, required=True)
    parser.add_argument("--air-gap-file", type=Path, required=True)
    parser.add_argument("--egress-receipt", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--state-seed", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--systemd-socket", action="store_true", required=True)
    return parser


def _seed_state(source: Path, destination: Path) -> None:
    """Seed or reconcile broker state to the fresh root-authorized instance id."""
    try:
        source_info = source.lstat()
        parent_info = destination.parent.lstat()
    except OSError as exc:
        raise SystemExit("provider instance seed is unavailable") from exc
    if (
        not source.is_absolute()
        or source.is_symlink()
        or source.resolve(strict=True) != source
        or not stat.S_ISREG(source_info.st_mode)
        or source_info.st_uid != PROVIDER_UID
        or source_info.st_nlink != 1
        or stat.S_IMODE(source_info.st_mode) != 0o400
        or not destination.is_absolute()
        or destination.parent.is_symlink()
        or destination.parent.resolve(strict=True) != destination.parent
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != PROVIDER_UID
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise SystemExit("provider instance seed metadata is unsafe")
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise SystemExit("provider instance seed is unavailable") from exc
    try:
        opened_source = os.fstat(source_descriptor)
        if (source_info.st_dev, source_info.st_ino) != (
            opened_source.st_dev,
            opened_source.st_ino,
        ):
            raise SystemExit("provider instance seed metadata is unsafe")
        raw = os.read(source_descriptor, 8193)
    finally:
        os.close(source_descriptor)
    if len(raw) > 8192:
        raise SystemExit("provider instance seed is too large")
    try:
        value = json.loads(raw)
        instance_id = value["id"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SystemExit("provider instance seed is invalid") from exc
    if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0:
        raise SystemExit("provider instance seed is invalid")
    try:
        destination_info = destination.lstat()
    except FileNotFoundError:
        destination_info = None
    except OSError as exc:
        raise SystemExit("provider instance state is unavailable") from exc
    if destination_info is not None:
        try:
            destination_descriptor = os.open(
                destination,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError as exc:
            raise SystemExit("provider instance state is unavailable") from exc
        try:
            opened_destination = os.fstat(destination_descriptor)
            if (
                destination.is_symlink()
                or destination.resolve(strict=True) != destination
                or not stat.S_ISREG(destination_info.st_mode)
                or destination_info.st_uid != PROVIDER_UID
                or destination_info.st_nlink != 1
                or stat.S_IMODE(destination_info.st_mode) != 0o600
                or (destination_info.st_dev, destination_info.st_ino)
                != (opened_destination.st_dev, opened_destination.st_ino)
            ):
                raise SystemExit("provider instance state metadata is unsafe")
            existing_raw = os.read(destination_descriptor, 8193)
        finally:
            os.close(destination_descriptor)
        if len(existing_raw) > 8192:
            raise SystemExit("provider instance state is too large")
        try:
            existing = json.loads(existing_raw)
            existing_id = existing["id"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SystemExit("provider instance state is invalid") from exc
        if (
            not isinstance(existing, dict)
            or set(existing)
            not in (
                {"id"},
                {"host", "id", "map", "ssh_port", "status"},
            )
            or isinstance(existing_id, bool)
            or not isinstance(existing_id, int)
            or existing_id != instance_id
        ):
            raise SystemExit("provider instance state does not match its root seed")
        return
    raw = json.dumps({"id": instance_id}, sort_keys=True, separators=(",", ":")).encode()
    descriptor, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, destination, follow_symlinks=False)
        os.unlink(temporary)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _activated_socket(path: Path) -> socket.socket:
    if (
        os.environ.get("LISTEN_PID") != str(os.getpid())
        or os.environ.get("LISTEN_FDS") != "1"
        or os.environ.get("LISTEN_FDNAMES") != "inference-provider"
    ):
        raise SystemExit("exact provider socket activation is required")
    try:
        activated = socket.socket(fileno=os.dup(3))
    except OSError as exc:
        raise SystemExit("activated provider socket is unavailable") from exc
    if (
        activated.family != socket.AF_UNIX
        or activated.type & socket.SOCK_STREAM != socket.SOCK_STREAM
        or activated.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
        or activated.getsockname() != str(path)
    ):
        activated.close()
        raise SystemExit("activated provider socket is refused")
    return activated


def _bootstrap(args: argparse.Namespace) -> tuple[socket.socket, ProviderBrokerServer]:
    _seed_state(args.state_seed, args.state)
    manifest = load_private_manifest(
        args.registry,
        args.adapter_manifest,
        owner_uid=0,
        _document_mode=0o640,
        _document_reader_gid=PROVIDER_GID,
        _vast_reader_gid=PROVIDER_GID,
    )
    policy = manifest.adapters["vast-private-rig-v1"]

    def live_air_gap() -> bool:
        return _air_gap_from_file(
            args.air_gap_file,
            owner_uid=0,
            reader_gid=PROVIDER_GID,
        )

    live_air_gap()
    backend = VastProviderBackend(
        policy,
        runner=PinnedRunner(owner_uid=0),
        air_gap=live_air_gap,
        egress_receipt=ProviderEgressReceipt(
            args.egress_receipt,
            owner_uid=0,
            reader_gid=PROVIDER_GID,
        ),
    )
    activated = _activated_socket(args.socket)
    return activated, ProviderBrokerServer(
        backend,
        activated,
        allowed_uid=0,
        allowed_gid=OPERATOR_GID,
    )


def main() -> None:
    args = _parser().parse_args()
    activated, server = _bootstrap(args)
    try:
        asyncio.run(server.serve())
    finally:
        with contextlib.suppress(OSError):
            activated.close()


if __name__ == "__main__":
    main()


__all__ = ["main"]
