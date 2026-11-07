# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closed admission for the reviewed offline Vast CLI artifact closure."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

VAST_OFFLINE_LOCK_SHA256 = "927c4289a2c7b16f0b355b12a298f1068fcea8953c2823666cd0cb2ac30d29cd"

_LOCK_KEYS = {"python", "schema", "upstream", "wheels"}
_PYTHON_KEYS = {"executable", "isolated_flags", "requires"}
_UPSTREAM_KEYS = {
    "commit",
    "installed_sha256",
    "name",
    "source_sha256",
    "tag",
    "version",
}
_WHEEL_KEYS = {"filename", "name", "sha256", "version"}


class VastBundleRefused(ValueError):
    """The installed provider artifact closure is unavailable or unsafe."""


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise VastBundleRefused(f"{label} has an invalid closed shape")
    return value


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_owned(
    path: Path,
    *,
    owner_uid: int,
    mode: int,
    reader_gid: int | None,
) -> bytes:
    reader_bits = 0o050 if mode & 0o100 else 0o040
    try:
        before = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise VastBundleRefused("Vast offline artifact is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or canonical != path
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != owner_uid
        or (reader_gid is not None and before.st_gid != reader_gid)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != (mode | (reader_bits if reader_gid is not None else 0))
    ):
        raise VastBundleRefused("Vast offline artifact ownership or mode is unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise VastBundleRefused("Vast offline artifact cannot be read") from exc


def _validate_directory(path: Path, *, owner_uid: int, reader_gid: int | None) -> None:
    try:
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise VastBundleRefused("Vast offline directory is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or canonical != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner_uid
        or (reader_gid is not None and info.st_gid != reader_gid)
        or stat.S_IMODE(info.st_mode) not in ({0o550} if reader_gid is not None else {0o500, 0o700})
    ):
        raise VastBundleRefused("Vast offline directory ownership or mode is unsafe")


def _safe_wheel_path(value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise VastBundleRefused("Vast wheel contains an unsafe path")
    return Path(*relative.parts)


def _wheel_contents(value: bytes) -> dict[Path, str]:
    expected: dict[Path, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(value)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = _safe_wheel_path(info.filename)
                if relative in expected:
                    raise VastBundleRefused("Vast wheel repeats an installed path")
                expected[relative] = _digest(archive.read(info))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, VastBundleRefused):
            raise
        raise VastBundleRefused("Vast wheel cannot be verified") from exc
    return expected


def validate_vast_bundle(
    root: Path,
    *,
    owner_uid: int = 0,
    reader_gid: int | None = None,
    accepted_lock_sha256: str = VAST_OFFLINE_LOCK_SHA256,
) -> str:
    """Validate the exact CLI, wheelhouse, and extracted import graph.

    Returns the admitted CLI digest for comparison with the adapter receipt.
    The accepted lock digest is code-owned; the installed lock cannot select a
    different version, wheel, dependency, interpreter, or executable.
    """
    _validate_directory(root, owner_uid=owner_uid, reader_gid=reader_gid)
    lock_path = root / "offline-lock.json"
    lock_raw = _read_owned(
        lock_path,
        owner_uid=owner_uid,
        mode=0o400,
        reader_gid=reader_gid,
    )
    if _digest(lock_raw) != accepted_lock_sha256:
        raise VastBundleRefused("Vast offline lock is not the reviewed closure")
    try:
        document = json.loads(lock_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VastBundleRefused("Vast offline lock is not valid JSON") from exc
    lock = _exact_dict(document, _LOCK_KEYS, "Vast offline lock")
    if lock["schema"] != "ne-vastai-offline-lock-v1":
        raise VastBundleRefused("Vast offline lock schema is unsupported")
    python = _exact_dict(lock["python"], _PYTHON_KEYS, "Vast Python policy")
    if python != {
        "executable": "/usr/bin/python3.12",
        "isolated_flags": "-IS",
        "requires": ">=3.12,<3.14",
    }:
        raise VastBundleRefused("Vast Python policy is not admitted")
    upstream = _exact_dict(lock["upstream"], _UPSTREAM_KEYS, "Vast upstream policy")
    if (
        upstream["name"] != "vast-cli"
        or upstream["version"] != "1.5.2"
        or upstream["tag"] != "v1.5.2"
        or upstream["commit"] != "bc7356483dd0f922ee17975c5357741f0d5d81fc"
    ):
        raise VastBundleRefused("Vast upstream identity is not admitted")
    installed_digest = upstream["installed_sha256"]
    if not isinstance(installed_digest, str) or len(installed_digest) != 64:
        raise VastBundleRefused("Vast installed digest is malformed")

    wheel_rows = lock["wheels"]
    if not isinstance(wheel_rows, list) or not wheel_rows:
        raise VastBundleRefused("Vast wheel closure is empty")
    wheelhouse = root / "wheelhouse"
    site_packages = root / "site-packages"
    _validate_directory(wheelhouse, owner_uid=owner_uid, reader_gid=reader_gid)
    _validate_directory(site_packages, owner_uid=owner_uid, reader_gid=reader_gid)
    expected_wheels: dict[str, str] = {}
    expected_site: dict[Path, str] = {}
    expected_names: set[str] = set()
    for value in wheel_rows:
        row = _exact_dict(value, _WHEEL_KEYS, "Vast wheel")
        filename = row["filename"]
        name = row["name"]
        version = row["version"]
        sha256 = row["sha256"]
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith("-py3-none-any.whl")
            or not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or name in expected_names
        ):
            raise VastBundleRefused("Vast wheel identity is invalid")
        expected_names.add(name)
        wheel = _read_owned(
            wheelhouse / filename,
            owner_uid=owner_uid,
            mode=0o400,
            reader_gid=reader_gid,
        )
        if _digest(wheel) != sha256:
            raise VastBundleRefused("Vast wheel digest is not admitted")
        expected_wheels[filename] = sha256
        for relative, digest in _wheel_contents(wheel).items():
            if relative in expected_site:
                raise VastBundleRefused("Vast wheels overlap an installed path")
            expected_site[relative] = digest
    actual_wheels = {path.name for path in wheelhouse.iterdir()}
    if actual_wheels != set(expected_wheels):
        raise VastBundleRefused("Vast wheelhouse does not match the closed artifact set")
    expected_directories = {
        parent for relative in expected_site for parent in relative.parents if parent != Path(".")
    }
    actual_directories = {
        path.relative_to(site_packages)
        for path in site_packages.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    if actual_directories != expected_directories:
        raise VastBundleRefused("Vast installed package directories are not exact")
    for relative in expected_directories:
        _validate_directory(
            site_packages / relative,
            owner_uid=owner_uid,
            reader_gid=reader_gid,
        )
    actual_site = {
        path.relative_to(site_packages)
        for path in site_packages.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_site != set(expected_site):
        raise VastBundleRefused("Vast installed package graph is not exact")
    for relative, digest in expected_site.items():
        value = _read_owned(
            site_packages / relative,
            owner_uid=owner_uid,
            mode=0o400,
            reader_gid=reader_gid,
        )
        if _digest(value) != digest:
            raise VastBundleRefused("Vast installed package digest is not admitted")

    executable = _read_owned(
        root / "bin/vastai",
        owner_uid=owner_uid,
        mode=0o500,
        reader_gid=reader_gid,
    )
    if _digest(executable) != installed_digest:
        raise VastBundleRefused("Vast CLI executable is not admitted")
    return installed_digest


__all__ = ["VAST_OFFLINE_LOCK_SHA256", "VastBundleRefused", "validate_vast_bundle"]
