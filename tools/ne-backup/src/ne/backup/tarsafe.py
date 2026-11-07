# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi @ 4b0d12f3ed0e (MIT, Copyright (C) 2025
# Naas.ai): the tar-traversal / symlink / hardlink escape guards
# (_is_unsafe_component, _assert_safe_members, _safe_extract) and the cold-tar
# archive/extract pattern. Adapted to raise ne.backup typed errors instead of
# click.ClickException (the core stays click-free) and to add archive_dir with a
# top-level-directory exclusion set (the bundle's ephemeral runtime/ dir).
"""Hardened tar helpers: traversal/symlink/hardlink-escape guards MANDATORY.

An attacker-crafted archive must not write outside the extraction root. Every
extract path routes through :func:`safe_extract`, which first rejects any member
(or link target) that escapes ``dest`` and then extracts with the stdlib
``data`` filter as defense in depth.
"""

from __future__ import annotations

import os
import tarfile
from collections.abc import Iterable
from pathlib import Path

from ne.backup.errors import UnsafeArchiveError


def is_unsafe_component(name: str) -> bool:
    """True if ``name`` is not a single, plain path component.

    Anything absolute ('/'), a parent ref ('..'), '.', empty, or containing a
    separator would make ``base / name`` resolve outside the target dir.
    """
    return (not name) or name in (".", "..") or Path(name).name != name


def _assert_safe_members(tar: tarfile.TarFile, dest: Path) -> None:
    """Reject members — and link targets — that escape ``dest``.

    Guards plain path traversal AND symlink/hardlink members whose target points
    outside ``dest``, kept as defense in depth even though extraction also uses
    the ``data`` filter.
    """
    dest_resolved = dest.resolve()

    def _within(path: Path) -> bool:
        resolved = path.resolve()
        return resolved == dest_resolved or dest_resolved in resolved.parents

    for member in tar.getmembers():
        if not _within(dest_resolved / member.name):
            raise UnsafeArchiveError(f"Refusing to extract unsafe path from archive: {member.name}")
        if member.issym() or member.islnk():
            if member.issym():
                link_target = dest_resolved / os.path.dirname(member.name) / member.linkname
            else:  # hardlink target is relative to the archive root
                link_target = dest_resolved / member.linkname
            if os.path.isabs(member.linkname) or not _within(link_target):
                raise UnsafeArchiveError(
                    "Refusing to extract unsafe link from archive: "
                    f"{member.name} -> {member.linkname}"
                )


def safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract with the traversal/link guard, then the stdlib ``data`` filter."""
    _assert_safe_members(tar, dest)
    tar.extractall(dest, filter="data")


def archive_dir(
    src: Path,
    dest_tar: Path,
    arcname: str,
    *,
    exclude_top_dirs: Iterable[str] = frozenset({"runtime"}),
) -> None:
    """Cold-tar ``src`` into ``dest_tar`` under ``arcname``, dropping any listed
    top-level subdirectory.

    The default exclusion is the bundle's ``runtime/`` dir — the installer's
    ephemeral 0600 env file of resolved plaintext secrets must never enter an
    object backup (C12). Resolves a symlinked ``src`` so contents (not the link)
    are captured.
    """
    excluded = frozenset(exclude_top_dirs)
    source = src.resolve() if src.is_symlink() else src

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        rel = Path(info.name)
        parts = rel.parts
        # parts[0] is arcname; parts[1] is the top-level entry beneath src.
        if len(parts) >= 2 and parts[1] in excluded:
            return None
        return info

    with tarfile.open(dest_tar, "w") as tar:
        tar.add(source, arcname=arcname, filter=_filter)
