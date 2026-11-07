# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic ``.tar.gz`` pack/unpack for the model-export artifact.

C11 note: the design doc's original filename was ``.tar.zst`` — orchestrator-
approved deviation (2026-07-17): ``zstandard`` would be a NEW platform-runtime
dependency just to unpack a build artifact, so this uses stdlib
``tarfile``/``gzip`` on both the isolated producer (this module) and the
consumer (``ne.serve.stage_tei``).

Deterministic pack: sorted member names, ``mtime=0`` on both the gzip stream
AND every ``TarInfo`` (uid/gid/uname/gname zeroed) — packing the same input
bytes twice yields byte-IDENTICAL ``.tar.gz`` output (proven by
``test_tarball.py``'s double-pack sha equality test), which keeps the C11
artifact hash reproducible across re-runs of the export tool.

Unpack uses ``filter="data"`` (Python 3.12+ stdlib, PEP 706) — path-traversal
and special-file members are rejected, never silently sanitized.
"""

from __future__ import annotations

import gzip
import tarfile
from pathlib import Path


def pack_tar_gz(members: dict[str, Path], out_path: Path) -> None:
    """Pack ``members`` (arcname -> source file path) into a deterministic
    ``.tar.gz`` at ``out_path``. Members are added in SORTED arcname order
    with zeroed mtime/uid/gid/uname/gname so identical input bytes always
    produce identical output bytes.

    ``tarfile.open(..., mode="w:gz")`` does NOT forward an ``mtime=`` kwarg to
    the underlying ``gzip.GzipFile`` (it lands on ``TarFile.__init__`` instead
    and raises ``TypeError``) — the gzip stream is built by hand here
    (``mtime=0``, empty embedded ``filename``) so the gzip header itself is
    also reproducible, not just the tar member metadata.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        out_path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=raw) as gz,
        tarfile.open(fileobj=gz, mode="w:") as tar,
    ):
        for arcname in sorted(members):
            src = members[arcname]
            info = tar.gettarinfo(str(src), arcname=arcname)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with src.open("rb") as handle:
                tar.addfile(info, handle)


def unpack_tar_gz(tar_path: Path, dest: Path) -> list[str]:
    """Extract ``tar_path`` into ``dest`` (created if absent); returns the
    sorted list of extracted member names. Path-traversal / device / symlink-
    escape members are rejected by ``filter="data"`` (raises on the first bad
    member — never a silent partial extraction of only the safe members)."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers())
        tar.extractall(dest, filter="data")
    return names


__all__ = ["pack_tar_gz", "unpack_tar_gz"]
