#!/usr/bin/python3
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Root helper consuming one active-transition SSH nft authority."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

POLICY_SHA256 = "8f06a75d0c58f058d6b12293c4233bf2d05a10f009377278bbc6af4bcb7ca06d"
_REQUEST_KEYS = {
    "instance_id",
    "policy_sha256",
    "schema",
    "target_id",
    "transition_id",
}
_AUTHORITY_KEYS = {
    "expires_at",
    "instance_id",
    "operation",
    "owner_pid",
    "owner_start_ticks",
    "policy_sha256",
    "schema",
    "target_id",
    "transition_id",
}
_RECEIPT_KEYS = {
    "addresses",
    "authority",
    "expires_at",
    "netns_inode",
    "policy_sha256",
    "schema",
}
_TRANSITION = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")


def refuse() -> NoReturn:
    print('{"code":"refused"}')
    raise SystemExit(70)


def owned_file(path: Path, mode: int, *, owner_uid: int, reader_gid: int | None = None) -> bytes:
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        refuse()
    try:
        current = os.fstat(descriptor)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or path.resolve(strict=True) != path
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or (reader_gid is not None and before.st_gid != reader_gid)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != mode
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
        ):
            refuse()
        raw = b""
        while chunk := os.read(descriptor, 4096):
            raw += chunk
            if len(raw) > 4096:
                refuse()
        return raw
    finally:
        os.close(descriptor)


def exact(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        refuse()
    return value


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        refuse()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        refuse()
    return parsed.astimezone(UTC)


def _process_start_ticks(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        refuse()
    tail = raw.rsplit(")", 1)[-1].strip().split()
    if len(tail) <= 19 or not tail[19].isdigit():
        refuse()
    return tail[19]


def _authority_paths(directory: Path, transition_id: str) -> tuple[Path, Path]:
    name = hashlib.sha256(transition_id.encode()).hexdigest()
    return directory / f"{name}.json", directory / f"{name}.consumed"


def _provider_endpoint(
    instance_id: int,
    *,
    credential_path: Path = Path(
        "/run/credentials/ne-inference-provider-broker.service/vast-api-key"
    ),
    credential_owner_uid: int = 10002,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[str, int]:
    """Derive the endpoint with one fixed, capped, no-redirect HTTPS read."""
    try:
        if not isinstance(instance_id, int) or isinstance(instance_id, bool) or instance_id <= 0:
            refuse()
        credential = (
            owned_file(
                credential_path,
                0o400,
                owner_uid=credential_owner_uid,
            )
            .decode("ascii")
            .strip()
        )
        if not 8 <= len(credential) <= 4096 or any(
            character.isspace() or ord(character) < 0x21 for character in credential
        ):
            refuse()
        url = f"https://console.vast.ai/api/v0/instances/{instance_id}/"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential}",
                "User-Agent": "neutron-inference-operator/1",
            },
            method="GET",
        )
        if opener is None:

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(),
                NoRedirect(),
            )
        with opener.open(request, timeout=15) as response:
            if response.status != 200 or response.geturl() != url:
                refuse()
            raw = response.read(65537)
        if len(raw) > 65536:
            refuse()
        outer = exact(json.loads(raw), {"instances"})
        document = outer["instances"]
        if not isinstance(document, dict):
            refuse()
        ports = document.get("ports")
        mapping = ports.get("22/tcp") if isinstance(ports, dict) else None
        address = document.get("public_ipaddr") or document.get("ssh_host")
        port = mapping[0].get("HostPort") if isinstance(mapping, list) and mapping else None
        port = port or document.get("ssh_port")
        if not isinstance(address, str):
            refuse()
        if isinstance(port, str):
            if re.fullmatch(r"[1-9][0-9]{0,4}", port) is None:
                refuse()
            port = int(port)
        parsed_address = ipaddress.ip_address(address)
        if (
            document.get("id") != instance_id
            or document.get("actual_status") != "running"
            or parsed_address.version != 4
            or not parsed_address.is_global
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            refuse()
        return str(parsed_address), port
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        urllib.error.URLError,
        ValueError,
    ):
        refuse()


def _publish_observation(
    path: Path,
    *,
    instance_id: int,
    address: str,
    port: int,
    owner_uid: int,
) -> None:
    parent = path.parent
    info = parent.lstat()
    if (
        not path.is_absolute()
        or parent.is_symlink()
        or parent.resolve(strict=True) != parent
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        refuse()
    payload = json.dumps(
        {
            "address": address,
            "instance_id": instance_id,
            "port": port,
            "schema": "ne-inference-provider-observation-v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    descriptor, temporary = tempfile.mkstemp(prefix=".provider-observation-", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def grant(
    request: Any,
    *,
    environment_path: Path = Path("/etc/neutron/inference-operator/environment"),
    authority_dir: Path = Path("/var/lib/neutron/inference-operator/provider-grants"),
    receipt_path: Path = Path("/run/neutron/inference-provider/egress-receipt.json"),
    observation_path: Path = Path("/var/lib/neutron/inference-operator/provider-observation.json"),
    network_namespace: Path = Path("/proc/self/ns/net"),
    owner_uid: int = 0,
    reader_gid: int = 10002,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    process_start: Callable[[int], str] = _process_start_ticks,
    endpoint_resolver: Callable[[int], tuple[str, int]] = _provider_endpoint,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    """Validate, consume, and apply one exact root-authorized endpoint grant."""
    request = exact(request, _REQUEST_KEYS)
    if (
        owned_file(
            environment_path,
            0o640,
            owner_uid=owner_uid,
            reader_gid=reader_gid,
        )
        != b"AIR_GAP=false\n"
    ):
        refuse()
    try:
        receipt = exact(
            json.loads(owned_file(receipt_path, 0o640, owner_uid=owner_uid, reader_gid=reader_gid)),
            _RECEIPT_KEYS,
        )
        transition_id = request["transition_id"]
        instance_id = request["instance_id"]
        receipt_expiry = _utc(receipt["expires_at"])
        current_namespace_inode = network_namespace.stat().st_ino
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        refuse()
    if not isinstance(transition_id, str) or _TRANSITION.fullmatch(transition_id) is None:
        refuse()
    addresses = receipt["addresses"]
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 16:
        refuse()
    try:
        normalized_addresses = [str(ipaddress.ip_address(value)) for value in addresses]
    except (TypeError, ValueError):
        refuse()
    if normalized_addresses != sorted(set(normalized_addresses)) or any(
        not ipaddress.ip_address(value).is_global for value in normalized_addresses
    ):
        refuse()
    try:
        directory_info = authority_dir.lstat()
    except OSError:
        refuse()
    if (
        not authority_dir.is_absolute()
        or authority_dir.is_symlink()
        or authority_dir.resolve(strict=True) != authority_dir
        or not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != owner_uid
        or stat.S_IMODE(directory_info.st_mode) != 0o700
    ):
        refuse()
    active, consumed = _authority_paths(authority_dir, transition_id)
    try:
        authority = exact(
            json.loads(owned_file(active, 0o600, owner_uid=owner_uid)),
            _AUTHORITY_KEYS,
        )
        authority_expiry = _utc(authority["expires_at"])
        authority_instance_id = authority["instance_id"]
        owner_pid = authority["owner_pid"]
        owner_start_ticks = authority["owner_start_ticks"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        refuse()
    current = now().astimezone(UTC)
    if (
        request["schema"] != "ne-inference-provider-ssh-grant-v1"
        or request["target_id"] != "private-vast-gpu"
        or request["policy_sha256"] != POLICY_SHA256
        or not isinstance(instance_id, int)
        or isinstance(instance_id, bool)
        or instance_id != authority_instance_id
        or not current < authority_expiry <= current + timedelta(minutes=15)
        or receipt["schema"] != "ne-inference-provider-egress-v1"
        or receipt["authority"] != "console.vast.ai:443"
        or receipt["policy_sha256"] != POLICY_SHA256
        or receipt_expiry <= current
        or receipt["netns_inode"] != current_namespace_inode
        or authority["schema"] != "ne-inference-provider-grant-authority-v1"
        or authority["target_id"] != "private-vast-gpu"
        or authority["policy_sha256"] != POLICY_SHA256
        or authority["transition_id"] != transition_id
        or authority["operation"] not in {"status", "start"}
        or not isinstance(owner_pid, int)
        or isinstance(owner_pid, bool)
        or owner_pid <= 0
        or not isinstance(owner_start_ticks, str)
        or not owner_start_ticks.isdigit()
        or process_start(owner_pid) != owner_start_ticks
    ):
        refuse()
    if (
        owned_file(
            environment_path,
            0o640,
            owner_uid=owner_uid,
            reader_gid=reader_gid,
        )
        != b"AIR_GAP=false\n"
    ):
        refuse()
    address_value, port = endpoint_resolver(instance_id)
    address = ipaddress.ip_address(address_value)
    current = now().astimezone(UTC)
    if (
        address.version != 4
        or not address.is_global
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or authority_expiry <= current
        or receipt_expiry <= current
        or process_start(owner_pid) != owner_start_ticks
    ):
        refuse()
    _publish_observation(
        observation_path,
        instance_id=instance_id,
        address=str(address),
        port=port,
        owner_uid=owner_uid,
    )
    try:
        os.link(active, consumed, follow_symlinks=False)
        active.unlink()
    except OSError:
        refuse()
    seconds = max(1, int((authority_expiry - current).total_seconds()))
    rules = (
        "update element inet ne_inference_provider ssh_grants_v4 "
        f"{{ {address} . {port} timeout {seconds}s }}\n"
    ).encode()
    result = run(
        ["/usr/sbin/nft", "-f", "-"],
        input=rules,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        refuse()
    print('{"code":"granted"}')


def main() -> None:
    if os.geteuid() != 0 or os.environ.get("SUDO_UID") != "10002":
        refuse()
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            refuse()
        request = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        refuse()
    grant(request)


if __name__ == "__main__":
    main()
