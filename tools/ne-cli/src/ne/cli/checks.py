# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Repository policy checks exposed under `ne check`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

# A .py file passes the header check iff, within its first N lines, one line
# contains the copyright marker and one line names the owning entity.
HEADER_SCAN_LINES = 5
_COPYRIGHT_MARKER = "Copyright (c)"
_OWNER_MARKER = "Ryan Junee"


def _find_repo_root(start: Path) -> Path:
    """Walk up from `start` to the workspace root (the dir whose pyproject.toml
    declares [tool.uv.workspace]). Falls back to `start` if none is found."""
    for candidate in (start, *start.parents):
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and "[tool.uv.workspace]" in pyproject.read_text():
            return candidate
    return start


def _tracked_python_files(root: Path) -> list[Path]:
    """All git-tracked .py files under packages/, tools/, and apps/."""
    result = subprocess.run(
        ["git", "ls-files", "--", "packages/**/*.py", "tools/**/*.py", "apps/**/*.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / line for line in result.stdout.splitlines() if line]


def _has_header(path: Path) -> bool:
    seen_copyright = False
    seen_owner = False
    with path.open(encoding="utf-8") as handle:
        for _, line in zip(range(HEADER_SCAN_LINES), handle, strict=False):
            if _COPYRIGHT_MARKER in line:
                seen_copyright = True
            if _OWNER_MARKER in line:
                seen_owner = True
    return seen_copyright and seen_owner


@click.group()
def check() -> None:
    """Repository policy checks."""


@check.command("headers")
@click.argument("files", nargs=-1, type=click.Path())
def headers(files: tuple[str, ...]) -> None:
    """Verify the copyright/SPDX source header on Python files.

    With no FILES, checks every git-tracked .py file under packages/, tools/,
    and apps/ (resolved from the workspace root). Otherwise checks the given
    files. Exits 1 if any file is missing the header.
    """
    root = _find_repo_root(Path.cwd())
    paths = [Path(f) for f in files] if files else _tracked_python_files(root)

    failures = [p for p in paths if not _has_header(p)]
    if failures:
        for path in failures:
            click.echo(f"missing source header: {path}")
        raise click.exceptions.Exit(1)

    click.echo(f"header check passed: {len(paths)} file(s) checked")
