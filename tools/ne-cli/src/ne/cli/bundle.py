# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic, versioned release bundle builder (NE-069).

``ne release bundle`` packages the release manifest (``ne.cli.fingerprint``,
which reuses the NE-CKB1 SBOM generator + the NE-CKC12 config-hash) plus every
per-member CycloneDX SBOM into a single ``neutron-platform-<tag>.tar.gz``, and
writes a sibling ``neutron-platform-<tag>.manifest.json`` (manifest + the
release fingerprint). The invariant: **same tree -> same bundle bytes** — the
tar.gz's sha256 is reproducible across builds and machines, which is what lets
the fingerprint stand in for "this exact release."

Determinism recipe (every one of these matters — get any wrong and the tar.gz
sha256 changes across otherwise-identical builds):

* Entries are added in **sorted path order** (manifest.json first, then every
  ``sbom/<name>-<version>.cdx.json`` in name order).
* Every ``TarInfo`` is normalized: ``mtime=0``, ``uid=0``, ``gid=0``,
  ``uname=""``, ``gname=""``, ``mode=0o644`` (0o755 for the directory entries
  tar implicitly needs) — a real mtime/uid/uname would make the tar bytes
  depend on WHEN and WHERE it was built.
* The gzip wrapper is built EXPLICITLY via ``gzip.GzipFile(..., mtime=0)`` and
  handed to ``tarfile.open(fileobj=..., mode="w")`` — plain ``tarfile.open(...,
  mode="w:gz")`` embeds gzip's own timestamp in the header and would silently
  break reproducibility.
* ``format=tarfile.USTAR_FORMAT`` avoids PAX extended headers (which can carry
  extra, harder-to-control metadata).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import click

from ne.cli.checks import _find_repo_root
from ne.cli.fingerprint import (
    _require_clean,
    _require_tag,
    build_manifest,
    compute_fingerprint,
)
from ne.cli.licenses import Policy, default_policy_path
from ne.cli.sbom import build_sbom


def _normalized_tarinfo(name: str, size: int, *, is_dir: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o755 if is_dir else 0o644
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    return info


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = _normalized_tarinfo(name, len(data))
    tar.addfile(info, io.BytesIO(data))


def build_bundle(root: Path, tag: str, out_dir: Path) -> tuple[Path, dict, str]:
    """Build the deterministic bundle for ``root`` at ``tag`` into ``out_dir``.

    Returns ``(tar_path, manifest, fingerprint)``. Writes both
    ``<out_dir>/neutron-platform-<tag>.tar.gz`` and the sibling
    ``.manifest.json``.
    """
    manifest = build_manifest(root, tag)
    fp = compute_fingerprint(manifest)

    lock_path = root / "uv.lock"
    policy = Policy.load(default_policy_path(root))

    prefix = f"neutron-platform-{tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / f"{prefix}.tar.gz"
    manifest_path = out_dir / f"{prefix}.manifest.json"

    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    buf = io.BytesIO()
    gz = gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0)
    with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        _add_bytes(tar, f"{prefix}/manifest.json", manifest_bytes)
        for member in sorted(manifest["members"], key=lambda m: m["name"]):
            doc = build_sbom(lock_path, policy, member["name"], deterministic_serial=True)
            content = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            if digest != member["sha256"]:  # pragma: no cover - defensive, should be unreachable
                raise ValueError(
                    f"SBOM for {member['name']} changed between manifest build and bundle write"
                )
            _add_bytes(tar, f"{prefix}/{member['sbom']}", content)
    gz.close()

    tar_bytes = buf.getvalue()
    tar_path.write_bytes(tar_bytes)
    # The sidecar manifest (outside the tar) carries the fingerprint too, so a
    # consumer can read {out}/*.manifest.json without unpacking the bundle.
    sidecar = json.dumps({**manifest, "fingerprint": fp}, indent=2, sort_keys=True) + "\n"
    manifest_path.write_text(sidecar, encoding="utf-8")

    return tar_path, manifest, fp


@click.command("bundle")
@click.option(
    "--tag",
    "tag",
    default=None,
    help="Platform version tag (default: `git describe --tags --exact-match HEAD`).",
)
@click.option(
    "--allow-dirty",
    "allow_dirty",
    is_flag=True,
    default=False,
    help="Allow a dirty worktree (default: refuse, fail-closed).",
)
@click.option(
    "--out",
    "out",
    type=click.Path(path_type=Path),
    default=Path("dist/bundle"),
    show_default=True,
    help="Output directory for the bundle + manifest.",
)
def bundle(tag: str | None, allow_dirty: bool, out: Path) -> None:
    """Build the deterministic, versioned release bundle."""
    root = _find_repo_root(Path.cwd())
    _require_clean(root, allow_dirty)
    resolved_tag = _require_tag(root, tag)
    out_resolved = out if out.is_absolute() else (Path.cwd() / out)

    tar_path, _manifest, fp = build_bundle(root, resolved_tag, out_resolved)
    tar_sha256 = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    click.echo(f"wrote {tar_path}")
    click.echo(f"fingerprint: {fp}")
    click.echo(f"tar sha256: {tar_sha256}")
