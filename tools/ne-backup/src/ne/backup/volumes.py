# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi @ 4b0d12f3ed0e (MIT, Copyright (C) 2025
# Naas.ai): archive_volume / extract_volume / reset_volume (the helper-container
# cold-tar-a-volume pattern, --mount long-form). Adapted to an injected Runner, a
# pinned tag@sha256 helper image, and volume names built ONLY from the manifest
# allowlist plus the compose project prefix.
"""Cold-tar backup/restore of allowlisted Docker volumes via a pinned helper image."""

from __future__ import annotations

from pathlib import Path

from ne.backup.errors import NeBackupError
from ne.backup.manifest import VOLUME_ALLOWLIST
from ne.backup.runner import Runner


def _full_name(project: str, key: str) -> str:
    return f"{project}_{key}"


class VolumeArchiver:
    def __init__(self, runner: Runner, project: str, helper_image: str) -> None:
        self._runner = runner
        self._project = project
        self._helper = helper_image

    def _check_key(self, key: str) -> str:
        # Never touch a volume outside the managed allowlist, even if a caller
        # (or a crafted manifest that slipped a validator) names one.
        if key not in VOLUME_ALLOWLIST:
            raise NeBackupError(f"refusing to operate on unmanaged volume: {key!r}")
        return key

    def _vol_mount(self, key: str, dest: str, *, readonly: bool) -> str:
        mount = f"type=volume,source={_full_name(self._project, key)},destination={dest}"
        return mount + ",readonly" if readonly else mount

    @staticmethod
    def _bind_mount(host_dir: Path, dest: str, *, readonly: bool) -> str:
        mount = f"type=bind,source={host_dir.resolve()},destination={dest}"
        return mount + ",readonly" if readonly else mount

    def archive(self, key: str, dest_tar: Path) -> None:
        self._check_key(key)
        dest_tar.parent.mkdir(parents=True, exist_ok=True)
        self._runner.run(
            [
                "run",
                "--rm",
                "--mount",
                self._vol_mount(key, "/from", readonly=True),
                "--mount",
                self._bind_mount(dest_tar.parent, "/to", readonly=False),
                self._helper,
                "tar",
                "cf",
                f"/to/{dest_tar.name}",
                "-C",
                "/from",
                ".",
            ]
        )

    def reset(self, key: str) -> None:
        self._check_key(key)
        self._runner.run(
            [
                "run",
                "--rm",
                "--mount",
                self._vol_mount(key, "/to", readonly=False),
                self._helper,
                "sh",
                "-c",
                "find /to -mindepth 1 -delete",
            ]
        )

    def extract(self, key: str, src_tar: Path) -> None:
        self._check_key(key)
        self._runner.run(
            [
                "run",
                "--rm",
                "--mount",
                self._vol_mount(key, "/to", readonly=False),
                "--mount",
                self._bind_mount(src_tar.parent, "/from", readonly=True),
                self._helper,
                "sh",
                "-c",
                f"cd /to && tar xf /from/{src_tar.name}",
            ]
        )
