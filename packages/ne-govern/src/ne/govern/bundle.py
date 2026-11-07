# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Versioned OPA policy-bundle tooling (stdlib tarfile — no new deps).

The Rego bundle under ``packages/ne-govern/policies/`` is the normative policy
(SPEC §4.2 tiers). Its version is stamped by the policy itself
(``ne/govern/version.rego``'s ``policy_version`` constant) and mirrored in the
OPA bundle ``.manifest`` revision; a test enforces the two are equal, and git
history IS the bundle version log. :func:`build_bundle` produces the deploy
artifact (excluding ``*_test.rego``, which ship only with the source).
"""

from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path

from ne.govern.errors import BundleError

_VERSION_RE = re.compile(r"policy_version\s*:=\s*\"([^\"]+)\"")


def _iter_bundle_members(policies_dir: Path) -> list[Path]:
    """Sorted bundle members: ``.manifest`` + all ``*.rego`` except ``*_test.rego``."""
    members: list[Path] = []
    manifest = policies_dir / ".manifest"
    if manifest.is_file():
        members.append(manifest)
    for rego in policies_dir.rglob("*.rego"):
        if rego.name.endswith("_test.rego"):
            continue
        members.append(rego)
    return sorted(members)


def build_bundle(policies_dir: Path, out_path: Path) -> Path:
    """Write a gzip-compressed OPA bundle tarball; return ``out_path``.

    Members are the ``.manifest`` and every non-test ``.rego`` file, in stable
    (sorted) order, with archive names relative to ``policies_dir`` (the OPA
    bundle layout). ``*_test.rego`` files are excluded.
    """
    if not policies_dir.is_dir():
        raise BundleError(f"policies dir not found: {policies_dir}")
    members = _iter_bundle_members(policies_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for member in members:
            tar.add(member, arcname=str(member.relative_to(policies_dir)))
    return out_path


def read_manifest_revision(policies_dir: Path) -> str:
    """Return the ``.manifest`` ``revision`` string, or raise :class:`BundleError`."""
    manifest = policies_dir / ".manifest"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(f"cannot read bundle manifest {manifest}: {exc}") from exc
    revision = data.get("revision")
    if not isinstance(revision, str) or not revision:
        raise BundleError(f"bundle manifest {manifest} has no string 'revision'")
    return revision


def read_policy_version(policies_dir: Path) -> str:
    """Return the ``policy_version`` constant from ``ne/govern/version.rego``."""
    version_rego = policies_dir / "ne" / "govern" / "version.rego"
    try:
        text = version_rego.read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleError(f"cannot read {version_rego}: {exc}") from exc
    match = _VERSION_RE.search(text)
    if match is None:
        raise BundleError(f'no policy_version := "..." found in {version_rego}')
    return match.group(1)
