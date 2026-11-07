# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Export-manifest build/load/save/verify — the C11 pin/hash authority for a
model-export artifact.

This module is platform-side (imported by the dep-less ``ne-model-export``
package, which declares only ``ne-envbuild``) and therefore may NOT import
``ne.serve`` (that would make ``ne-serve`` a dependency of the export tool,
inverting the intended direction: ``ne-serve`` consumes committed manifests,
the export tool produces them). It carries its own tiny stdlib
``sha256_file`` rather than reusing ``ne.serve.download._sha256_file``.

Two manifest shapes:

* the CORE manifest (:func:`build_manifest`) — model identity, toolchain,
  per-file sha256 of the onnx pair, and the parity-report summary. This is
  what gets packed INSIDE the ``.tar.gz`` artifact (an artifact cannot record
  its own hash).
* the FULL manifest (:func:`with_artifact`) — the core manifest plus an
  ``artifact`` section (filename/sha256/size) describing the ``.tar.gz`` itself,
  computed AFTER packing. This is the manifest committed to
  ``tools/model-export/manifests/`` (and cached next to a generated artifact) —
  the one every consumer (``ne-serve stage-tei``) verifies bytes against. The
  artifact travels WITH this manifest (cache dir + sealed bundle both carry
  both); there is no hosted-release channel.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

_HASH_CHUNK: Final[int] = 1 << 20
MANIFEST_VERSION: Final[int] = 1


class ManifestIntegrityError(Exception):
    """A manifest failed to parse, or staged/packed bytes don't match it."""


def sha256_file(path: Path) -> str:
    """Stream ``path`` through sha256 in fixed chunks; return the hex digest."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    *,
    model_key: str,
    source_repo: str,
    source_revision: str,
    dtype: str,
    toolchain: Mapping[str, str],
    files: Sequence[Path],
    golden_count: int,
    min_cosine: float,
    threshold: float,
) -> dict[str, Any]:
    """Build the CORE export manifest (no ``artifact`` section yet) from the
    actually-produced ``files`` (e.g. ``onnx/model.onnx`` + ``.../model.onnx_data``).
    """
    file_entries = [
        {"name": f.name, "size": f.stat().st_size, "sha256": sha256_file(f)}
        for f in sorted(files, key=lambda p: p.name)
    ]
    return {
        "manifest_version": MANIFEST_VERSION,
        "model_key": model_key,
        "source_repo": source_repo,
        "source_revision": source_revision,
        "dtype": dtype,
        "toolchain": dict(sorted(toolchain.items())),
        "files": file_entries,
        "parity": {
            "golden_count": golden_count,
            "min_cosine": min_cosine,
            "threshold": threshold,
        },
        "created_at": datetime.now(UTC).isoformat(),
    }


def with_artifact(
    manifest: Mapping[str, Any],
    *,
    filename: str,
    path: Path,
) -> dict[str, Any]:
    """Return a COPY of ``manifest`` with an ``artifact`` section describing
    the packed ``.tar.gz`` at ``path`` (sha256/size computed now — after
    packing, since an artifact can't record its own hash)."""
    out = dict(manifest)
    out["artifact"] = {
        "filename": filename,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }
    return out


def save_manifest(manifest: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ManifestIntegrityError(f"no export manifest at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestIntegrityError(f"manifest at {path} is unparseable: {exc}") from exc


def verify_files_against_manifest(manifest: Mapping[str, Any], root: Path) -> None:
    """Re-check every ``manifest["files"]`` entry under ``root`` (tamper /
    missing / size-mismatch detection). Raises :class:`ManifestIntegrityError`
    collecting ALL problems together; extra untracked files under ``root`` are
    NOT this function's concern (callers with a full-tree contract do that
    enumeration themselves, e.g. ``ne.serve.stage_tei``)."""
    problems: list[str] = []
    for entry in manifest.get("files", []):
        name = entry["name"]
        path = root / name
        if not path.is_file():
            problems.append(f"missing file {name} listed in manifest")
            continue
        if path.stat().st_size != entry.get("size"):
            problems.append(f"size mismatch for {name} (manifest vs staged bytes)")
        if sha256_file(path) != entry.get("sha256"):
            problems.append(f"sha256 mismatch for {name} (tampered or corrupt)")
    if problems:
        raise ManifestIntegrityError("\n".join(sorted(problems)))


def verify_artifact(manifest: Mapping[str, Any], artifact_path: Path) -> None:
    """Verify a packed ``.tar.gz`` at ``artifact_path`` against the manifest's
    ``artifact`` section (sha256 + size)."""
    artifact = manifest.get("artifact")
    if not artifact:
        raise ManifestIntegrityError("manifest has no 'artifact' section to verify against")
    if not artifact_path.is_file():
        raise ManifestIntegrityError(f"no artifact at {artifact_path}")
    if artifact_path.stat().st_size != artifact.get("size"):
        raise ManifestIntegrityError(f"artifact size mismatch at {artifact_path}")
    if sha256_file(artifact_path) != artifact.get("sha256"):
        raise ManifestIntegrityError(f"artifact sha256 mismatch at {artifact_path} (tampered)")


__all__ = [
    "MANIFEST_VERSION",
    "ManifestIntegrityError",
    "build_manifest",
    "load_manifest",
    "save_manifest",
    "sha256_file",
    "verify_artifact",
    "verify_files_against_manifest",
    "with_artifact",
]
