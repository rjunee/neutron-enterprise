# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi @ 4b0d12f3ed0e (MIT, Copyright (C) 2025
# Naas.ai): the snapshot_runtime_test.py tar-escape guard test ideas.
"""THE MANDATORY tar-guard suite: no crafted archive may escape the restore root."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from ne.backup.errors import UnsafeArchiveError
from ne.backup.tarsafe import archive_dir, safe_extract


def _build_tar(path: Path, build: Callable[[tarfile.TarFile], None]) -> Path:
    with tarfile.open(path, "w") as tar:
        build(tar)
    return path


def _extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r") as tar:
        safe_extract(tar, dest)


@pytest.fixture
def canary(tmp_path: Path) -> Path:
    c = tmp_path / "canary.txt"
    c.write_text("DO NOT TOUCH", encoding="utf-8")
    return c


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    d = tmp_path / "restore-root"
    d.mkdir()
    return d


def test_traversal_member_rejected(tmp_path: Path, dest: Path, canary: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("../../canary.txt")
        data = b"pwned"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    archive = _build_tar(tmp_path / "a.tar", build)
    with pytest.raises(UnsafeArchiveError):
        _extract(archive, dest)
    assert canary.read_text() == "DO NOT TOUCH"
    assert list(dest.iterdir()) == []


def test_absolute_member_rejected(tmp_path: Path, dest: Path) -> None:
    outside = tmp_path / "outside-abs.txt"

    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(str(outside))
        data = b"pwned"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    archive = _build_tar(tmp_path / "abs.tar", build)
    with pytest.raises(UnsafeArchiveError):
        _extract(archive, dest)
    assert not outside.exists()
    assert list(dest.iterdir()) == []


def test_symlink_escape_rejected(tmp_path: Path, dest: Path, canary: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../canary.txt"
        tar.addfile(info)

    archive = _build_tar(tmp_path / "sym.tar", build)
    with pytest.raises(UnsafeArchiveError):
        _extract(archive, dest)
    assert canary.read_text() == "DO NOT TOUCH"
    assert list(dest.iterdir()) == []


def test_absolute_symlink_rejected(tmp_path: Path, dest: Path, canary: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = str(canary)
        tar.addfile(info)

    archive = _build_tar(tmp_path / "sym-abs.tar", build)
    with pytest.raises(UnsafeArchiveError):
        _extract(archive, dest)
    assert canary.read_text() == "DO NOT TOUCH"


def test_hardlink_escape_rejected(tmp_path: Path, dest: Path, canary: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("hl")
        info.type = tarfile.LNKTYPE
        info.linkname = "../../canary.txt"
        tar.addfile(info)

    archive = _build_tar(tmp_path / "hl.tar", build)
    with pytest.raises(UnsafeArchiveError):
        _extract(archive, dest)
    assert canary.read_text() == "DO NOT TOUCH"
    assert list(dest.iterdir()) == []


def test_symlink_dir_write_through_rejected(tmp_path: Path, dest: Path, canary: Path) -> None:
    # A symlink dir pointing outside, then a write through it.
    def build(tar: tarfile.TarFile) -> None:
        link = tarfile.TarInfo("d")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../"
        tar.addfile(link)
        thru = tarfile.TarInfo("d/canary.txt")
        data = b"pwned"
        thru.size = len(data)
        tar.addfile(thru, io.BytesIO(data))

    archive = _build_tar(tmp_path / "symdir.tar", build)
    with pytest.raises(UnsafeArchiveError):
        _extract(archive, dest)
    assert canary.read_text() == "DO NOT TOUCH"
    assert list(dest.iterdir()) == []


def test_benign_archive_extracts(tmp_path: Path, dest: Path) -> None:
    def build(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("sub/ok.txt")
        data = b"hello"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    archive = _build_tar(tmp_path / "ok.tar", build)
    _extract(archive, dest)
    assert (dest / "sub" / "ok.txt").read_bytes() == b"hello"


def test_archive_dir_excludes_runtime(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "config").mkdir(parents=True)
    (bundle / "config" / "app.toml").write_text("k = 1\n", encoding="utf-8")
    (bundle / "runtime").mkdir()
    secret = b"NE_PILOT_POSTGRES_PASSWORD=super-secret-value"
    (bundle / "runtime" / "pilot.env").write_bytes(secret)

    out = tmp_path / "bundle.tar"
    archive_dir(bundle, out, arcname="bundle")

    with tarfile.open(out, "r") as tar:
        names = tar.getnames()
        blob = b"".join(
            tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        )
    assert any(n.endswith("config/app.toml") for n in names)
    assert not any("runtime" in Path(n).parts for n in names)
    assert b"super-secret-value" not in blob
