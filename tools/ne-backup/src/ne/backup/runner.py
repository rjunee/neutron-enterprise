# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from jupyter-naas/abi @ 4b0d12f3ed0e (MIT, Copyright (C) 2025
# Naas.ai): the run_docker subprocess wrapper pattern (docker not-in-PATH /
# non-zero-exit handling). Adapted to a Runner Protocol with byte stdin/stdout so
# the whole subsystem is unit-testable with a fake runner, and to raise ne.backup
# typed errors.
"""The Docker command seam — a small ``Runner`` Protocol so every side-effecting
call is injectable (real subprocess in production, a fake in tests)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import IO, Protocol

from ne.backup.errors import NeBackupError


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class Runner(Protocol):
    """Runs ``docker <args>`` (the ``docker`` prefix is added by the impl)."""

    def run(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> RunResult: ...

    def run_stream(
        self,
        args: list[str],
        *,
        stdin_file: IO[bytes] | None = None,
        stdout_file: IO[bytes] | None = None,
        check: bool = True,
    ) -> RunResult:
        """Like :meth:`run` but plumbs stdin/stdout to OPEN FILE HANDLES so a large
        dump/restore streams through the pipe buffer (memory O(pipe buffer)) instead
        of buffering the whole payload in RAM. ``stdout`` in the result is ``b""``
        when redirected to a file."""
        ...


class DockerRunner:
    """The production :class:`Runner`: a checked ``subprocess`` around ``docker``."""

    def run(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> RunResult:
        try:
            proc = subprocess.run(
                ["docker", *args],
                input=stdin,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as error:
            raise NeBackupError("Docker is not installed or not available in PATH.") from error
        if check and proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise NeBackupError(
                f"docker {' '.join(args)} failed (exit {proc.returncode}): {stderr}"
            )
        return RunResult(proc.returncode, proc.stdout or b"", proc.stderr or b"")

    def run_stream(
        self,
        args: list[str],
        *,
        stdin_file: IO[bytes] | None = None,
        stdout_file: IO[bytes] | None = None,
        check: bool = True,
    ) -> RunResult:
        try:
            proc = subprocess.run(
                ["docker", *args],
                stdin=stdin_file,
                stdout=stdout_file if stdout_file is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as error:
            raise NeBackupError("Docker is not installed or not available in PATH.") from error
        if check and proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise NeBackupError(
                f"docker {' '.join(args)} failed (exit {proc.returncode}): {stderr}"
            )
        return RunResult(proc.returncode, proc.stdout or b"", proc.stderr or b"")
