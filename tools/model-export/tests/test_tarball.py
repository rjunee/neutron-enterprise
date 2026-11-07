# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic .tar.gz pack/unpack round-trip + path-traversal rejection —
synthetic bytes only, no real onnx artifact."""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

import pytest

from ne.model_export import tarball as t


def _members(tmp_path: Path) -> dict[str, Path]:
    src = tmp_path / "src"
    src.mkdir()
    onnx = src / "model.onnx"
    data = src / "model.onnx_data"
    manifest = src / "export-manifest.json"
    onnx.write_bytes(b"onnx-graph-bytes")
    data.write_bytes(b"external-data-bytes" * 5)
    manifest.write_text('{"model_key": "k"}', encoding="utf-8")
    return {
        "onnx/model.onnx": onnx,
        "onnx/model.onnx_data": data,
        "export-manifest.json": manifest,
    }


def test_pack_unpack_round_trip(tmp_path: Path) -> None:
    members = _members(tmp_path)
    out = tmp_path / "artifact.tar.gz"
    t.pack_tar_gz(members, out)
    assert out.is_file()

    dest = tmp_path / "unpacked"
    names = t.unpack_tar_gz(out, dest)
    assert names == sorted(members)
    for arcname, src in members.items():
        assert (dest / arcname).read_bytes() == src.read_bytes()


def test_double_pack_is_byte_identical(tmp_path: Path) -> None:
    members = _members(tmp_path)
    out1 = tmp_path / "a.tar.gz"
    out2 = tmp_path / "b.tar.gz"
    t.pack_tar_gz(members, out1)
    t.pack_tar_gz(members, out2)
    assert (
        hashlib.sha256(out1.read_bytes()).hexdigest()
        == hashlib.sha256(out2.read_bytes()).hexdigest()
    )


def test_pack_is_deterministic_regardless_of_dict_insertion_order(tmp_path: Path) -> None:
    members = _members(tmp_path)
    reversed_members = dict(reversed(list(members.items())))
    out1 = tmp_path / "a.tar.gz"
    out2 = tmp_path / "b.tar.gz"
    t.pack_tar_gz(members, out1)
    t.pack_tar_gz(reversed_members, out2)
    assert out1.read_bytes() == out2.read_bytes()


def test_unpack_rejects_path_traversal_member(tmp_path: Path) -> None:
    evil_tar = tmp_path / "evil.tar.gz"
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    payload = payload_dir / "payload.txt"
    payload.write_bytes(b"pwned")

    with tarfile.open(evil_tar, "w:gz") as tar:
        info = tar.gettarinfo(str(payload), arcname="../../../etc/evil.txt")
        with payload.open("rb") as handle:
            tar.addfile(info, handle)

    dest = tmp_path / "unpack-dest"
    with pytest.raises(tarfile.TarError):  # FilterError / OutsideDestinationError subclass
        t.unpack_tar_gz(evil_tar, dest)
    assert not (tmp_path / "etc" / "evil.txt").exists()


def test_unpack_creates_dest_when_absent(tmp_path: Path) -> None:
    members = _members(tmp_path)
    out = tmp_path / "artifact.tar.gz"
    t.pack_tar_gz(members, out)
    dest = tmp_path / "does" / "not" / "exist" / "yet"
    assert not dest.exists()
    t.unpack_tar_gz(out, dest)
    assert dest.is_dir()
