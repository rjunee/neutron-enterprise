# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Air-gap weight pre-staging tooling (C2 / AIR_GAP sovereignty story).

The sovereignty workflow: pre-stage weights to a local directory ONCE (this
module), then serve them with ``HF_HUB_OFFLINE=1`` so the vLLM container makes
ZERO runtime egress to huggingface.co. :func:`offline_env` is the exact env
block the compose profiles and the CLI export.

Manifest v2 answers "which weights, exactly" and supports verify at the air-gap
boundary — the one place C11 digest-pinning otherwise breaks. It records the
RESOLVED immutable commit SHA (not the mutable ``main`` revision string) and the
per-file sha256 of the actually-staged files, enumerated from the repo's own
sibling list (never an ``rglob`` that could absorb stragglers from a prior
staging). :func:`verify_weights` re-checks staged bytes against that manifest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeGuard

from pydantic import BaseModel, ConfigDict

from ne.serve.errors import WeightIntegrityError

# Exact env block that disables every phone-home knob on the serving side.
OFFLINE_ENV: Final[dict[str, str]] = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "VLLM_NO_USAGE_STATS": "1",
    "DO_NOT_TRACK": "1",
}

_MANIFEST_NAME: Final[str] = "manifest.json"
_MANIFEST_VERSION: Final[int] = 2
_HASH_CHUNK: Final[int] = 1 << 20
_HF_CACHE_DIR: Final[str] = ".cache"


class DownloadResult(BaseModel):
    """Result of a pre-stage download: the local weights dir and its manifest."""

    model_config = ConfigDict(frozen=True)

    path: Path
    manifest_path: Path
    resolved_revision: str


def offline_env() -> dict[str, str]:
    """Return a fresh copy of the offline/telemetry-off env block."""
    return dict(OFFLINE_ENV)


def local_path(repo_id: str, weights_root: Path | str) -> Path:
    """Local directory for ``repo_id`` under ``weights_root`` (``org/name`` dirs)."""
    return Path(weights_root) / repo_id


def _sha256_file(path: Path) -> str:
    """Stream ``path`` through sha256 in fixed chunks; return the hex digest."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_resolved_sha(value: Any) -> TypeGuard[str]:
    """True when ``value`` is a 40-char lowercase-hex git commit SHA."""
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def download_weights(
    repo_id: str,
    weights_root: Path | str,
    *,
    revision: str = "main",
    snapshot_download: Callable[..., Any] | None = None,
    repo_info: Callable[..., Any] | None = None,
) -> DownloadResult:
    """Pre-stage ``repo_id`` weights to ``weights_root/org/name`` and write a v2 manifest.

    ``snapshot_download`` and ``repo_info`` are injectable for tests; when
    ``None`` the real ``huggingface_hub`` callables are lazily imported (keeps
    import cheap and avoids requiring the hub client at module load).

    The requested (mutable) ``revision`` is first resolved to an immutable commit
    SHA via ``repo_info``; the snapshot download is then PINNED to that SHA
    (closing the resolve/download race), and the manifest records the resolved
    SHA plus a per-file sha256 of the actually-staged files enumerated from the
    repo's own sibling list.
    """
    dest = local_path(repo_id, weights_root)
    dest.mkdir(parents=True, exist_ok=True)

    if snapshot_download is None:
        from huggingface_hub import snapshot_download as _snapshot_download

        snapshot_download = _snapshot_download

    if repo_info is None:
        from huggingface_hub import HfApi

        repo_info = HfApi().repo_info

    info = repo_info(repo_id=repo_id, revision=revision)
    resolved = getattr(info, "sha", None)
    if not _is_resolved_sha(resolved):
        raise WeightIntegrityError(
            f"could not resolve {repo_id}@{revision} to an immutable commit SHA"
        )

    names = sorted(s.rfilename for s in (info.siblings or []))
    if not names:
        raise WeightIntegrityError(
            f"{repo_id}@{resolved} lists no files — nothing servable / cannot build a "
            "verifiable manifest"
        )

    snapshot_download(
        repo_id=repo_id,
        revision=resolved,
        local_dir=str(dest),
    )

    files: list[dict[str, Any]] = []
    for name in names:
        path = dest / name
        if not path.is_file():
            raise WeightIntegrityError(f"staged snapshot is missing {name} listed at {resolved}")
        files.append({"name": name, "size": path.stat().st_size, "sha256": _sha256_file(path)})

    manifest = {
        "manifest_version": _MANIFEST_VERSION,
        "repo_id": repo_id,
        "revision": revision,
        "resolved_revision": resolved,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "files": files,
    }
    manifest_path = dest / _MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    return DownloadResult(path=dest, manifest_path=manifest_path, resolved_revision=resolved)


def verify_weights(
    weights_dir: Path | str,
    *,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Re-check staged weights against their manifest (integrity at the air-gap boundary).

    Raises :class:`~ne.serve.errors.WeightIntegrityError` on a missing /
    unparseable manifest, a pre-v2 manifest, or any tampered / missing / extra
    staged file (all problems are collected and reported together). Returns the
    parsed manifest on success.
    """
    root = Path(weights_dir)
    resolved_manifest = manifest_path if manifest_path is not None else root / _MANIFEST_NAME

    if not resolved_manifest.is_file():
        raise WeightIntegrityError(f"no manifest at {resolved_manifest} — cannot verify staging")
    try:
        manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise WeightIntegrityError(
            f"manifest at {resolved_manifest} is unparseable: {exc}"
        ) from exc

    if manifest.get("manifest_version") != _MANIFEST_VERSION or not _is_resolved_sha(
        manifest.get("resolved_revision")
    ):
        raise WeightIntegrityError(
            "manifest predates weight-integrity v2 — re-stage with python -m ne.serve download"
        )

    problems: list[str] = []
    listed: set[str] = set()
    for entry in manifest.get("files", []):
        name = entry["name"]
        listed.add(name)
        path = root / name
        if not path.is_file():
            problems.append(f"missing file {name} listed in manifest")
            continue
        if path.stat().st_size != entry.get("size"):
            problems.append(f"size mismatch for {name} (manifest vs staged bytes)")
        if _sha256_file(path) != entry.get("sha256"):
            problems.append(f"sha256 mismatch for {name} (tampered or corrupt staging)")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path == resolved_manifest:
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == _HF_CACHE_DIR:
            continue  # huggingface_hub local_dir metadata, not a served weight
        if str(rel) not in listed:
            problems.append(f"unexpected file {rel} not in manifest (stale or tampered staging)")

    if problems:
        raise WeightIntegrityError("\n".join(sorted(problems)))

    return manifest


def vllm_serve_args(
    model_path: Path | str,
    served_model_name: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    backend: str = "xgrammar",
) -> list[str]:
    """Build the vLLM OpenAI-server argv pointing at LOCAL pre-staged weights.

    ``host`` is a safe-by-default loopback bind (``127.0.0.1``), mirroring the
    NE-013 compose loopback default (``NE_VLLM_BIND_ADDR``); pass an explicit
    in-VPC interface address to expose the server beyond the host. Uses the
    CURRENT structured-output flag (``--structured-outputs-config.backend``); the
    old ``--guided-decoding-backend`` was removed upstream.
    """
    return [
        "--model",
        str(model_path),
        "--served-model-name",
        served_model_name,
        "--host",
        host,
        "--port",
        str(port),
        "--structured-outputs-config.backend",
        backend,
    ]
