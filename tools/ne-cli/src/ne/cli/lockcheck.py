# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Dependency pin + hash policy over uv.lock (`ne check lock`).

uv.lock is the pin+hash substrate: every third-party sdist/wheel carries a
sha256. This gate asserts (a) every artifact carries a structurally valid
sha256:<64-hex> digest, and (b) `uv lock --check` confirms the lockfile is
current.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import click

from ne.cli.checks import _find_repo_root

_SHA256_RE = re.compile(r"sha256:[0-9a-fA-F]{64}")


def _is_first_party(pkg: dict) -> bool:
    src = pkg.get("source", {})
    return any(k in src for k in ("editable", "virtual", "directory"))


def _has_sha256(pkg: dict) -> bool:
    """True only when EVERY artifact on the entry carries a valid sha256 digest.

    All-artifacts, structural-validity semantics: the sdist (when present) and
    *every* wheel must carry a digest matching exactly `sha256:` followed by
    64 hex characters (uv.lock's hash schema). An entry with no artifacts at
    all, any single artifact lacking a hash, or a malformed/non-sha256 digest
    (e.g. 'not-a-real-sha256' or 'md5:...sha256...', which merely contain the
    substring 'sha256') is unhashed (fails closed).
    """
    artifacts: list[dict] = []
    sdist = pkg.get("sdist")
    if isinstance(sdist, dict):
        artifacts.append(sdist)
    artifacts.extend(w for w in pkg.get("wheels", []) if isinstance(w, dict))
    if not artifacts:
        return False
    return all(
        isinstance(a.get("hash"), str) and _SHA256_RE.fullmatch(a["hash"]) is not None
        for a in artifacts
    )


def find_unhashed(lock_path: Path) -> list[str]:
    """Return names of third-party lock entries lacking any sha256 hash."""
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for pkg in data.get("package", []):
        if _is_first_party(pkg):
            continue
        if not _has_sha256(pkg):
            missing.append(f"{pkg['name']} {pkg.get('version', '')}")
    return missing


@click.command("lock")
def lock() -> None:
    """Verify uv.lock pin+hash policy: every third-party entry hashed, lock current."""
    root = _find_repo_root(Path.cwd())
    lock_path = root / "uv.lock"

    missing = find_unhashed(lock_path)
    ok = True
    if missing:
        ok = False
        click.echo("Third-party lock entries missing a sha256 hash:")
        for m in missing:
            click.echo(f"  ✗ {m}")

    result = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        ok = False
        click.echo("uv.lock is not current (`uv lock --check` failed):")
        click.echo(result.stderr.strip() or result.stdout.strip())

    if ok:
        click.echo("PASS — uv.lock pin+hash policy satisfied.")
        return
    raise click.exceptions.Exit(1)
