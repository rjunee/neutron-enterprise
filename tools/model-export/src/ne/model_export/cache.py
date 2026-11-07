# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Local generate-on-demand cache for the fast-ort ONNX artifact (NE-TEI-GEN).

The single source of truth for WHERE a generated ONNX pair + its export manifest
+ golden vectors live on an egress-OK host, and the fail-closed policy that
decides whether a cache MISS may trigger a (heavy, egress-requiring) generation.

Owner decision (SPEC §11 "TEI-ONNX DISTRIBUTION CHANGE", 2026-07-19): the
~2.2 GB fast-ort ONNX for ``Qwen/Qwen3-Embedding-0.6B`` is a regenerable,
deterministic, parity-gated build artifact — it does NOT belong on GitHub
Releases. Instead:

* egress-OK consumers (demo/dev/CI) GENERATE it on demand from the pinned
  source and CACHE it locally, keyed by ``repo_id`` + resolved ``revision`` (so
  a ``specs.py`` revision bump regenerates rather than serving a stale entry);
* a sealed (AIR_GAP) deploy NEVER generates at runtime — the sealed bundle must
  carry the pre-staged artifact + export manifest, and a cache MISS under
  ``AIR_GAP=1`` fail-closes with :class:`AirGapGenerationRefusedError` BEFORE
  any venv/subprocess/egress side effect (C2).

The parity gate (cosine ≥ 0.9999) is enforced on EVERY generation — generation
is only reachable through :func:`ne.model_export.cli.export_model`, whose truth
gate (§0-law-5) emits NOTHING on a failing gate; :func:`ensure_cached` publishes
to the cache ONLY from a completed export out-dir and re-probes (sha256 vs
manifest) before returning, so an unverified ONNX can never become a cache hit.

Concurrency: there is deliberately NO cross-process lock. Two concurrent
generations are each parity-gated and each publish atomically via
``os.replace`` of a per-attempt ``.tmp`` dir; last-writer-wins is acceptable
because every writer produced a verified, byte-identical-provenance artifact.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ne.envbuild import UvRunner
from ne.model_export import manifest as manifest_mod
from ne.model_export.specs import ModelSpec

#: Env override for the cache root (read at CALL time; an empty string counts as
#: unset — mirrors ``ne.envbuild.sidecar.sidecar_venv_dir`` semantics).
CACHE_ROOT_VAR = "NE_TEI_ONNX_CACHE"
#: Default cache root when ``$NE_TEI_ONNX_CACHE`` is unset/empty.
DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "neutron-enterprise" / "tei-onnx"

#: The cached export-manifest filename inside a cache entry (the C11 authority).
EXPORT_MANIFEST_NAME = "export-manifest.json"
#: The cached golden-vectors filename inside a cache entry.
GOLDEN_VECTORS_NAME = "golden-vectors.json"


class AirGapGenerationRefusedError(RuntimeError):
    """AIR_GAP=1 + cache MISS: generation needs egress and is refused.

    A sealed deploy must supply the pre-staged bundle artifact + export manifest
    via ``--onnx-artifact``/``--export-manifest``; nothing generates at runtime.
    """


@dataclass(frozen=True)
class CacheStatus:
    """The result of probing (or ensuring) a cache entry."""

    cache_dir: Path
    artifact_path: Path
    manifest_path: Path
    hit: bool
    reason: str | None


def _cache_root(cache_root: Path | None) -> Path:
    if cache_root is not None:
        return cache_root
    env = os.environ.get(CACHE_ROOT_VAR)
    if env:
        return Path(env)
    return DEFAULT_CACHE_ROOT


def cache_dir_for(spec: ModelSpec, cache_root: Path | None = None) -> Path:
    """Resolve the cache entry dir for ``spec``.

    ``<root>/<repo_id with '/'→'--'>@<revision>/`` where ``root`` is (in order)
    the explicit ``cache_root`` arg, then ``$NE_TEI_ONNX_CACHE`` (empty ==
    unset), then :data:`DEFAULT_CACHE_ROOT`. Keying on the resolved revision
    means a ``specs.py`` revision bump lands in a NEW dir (auto-regenerate),
    never a stale hit.
    """
    root = _cache_root(cache_root)
    key = f"{spec.repo_id.replace('/', '--')}@{spec.revision}"
    return root / key


def _artifact_path(cache_dir: Path, spec: ModelSpec) -> Path:
    return cache_dir / spec.artifact_filename


def _manifest_path(cache_dir: Path) -> Path:
    return cache_dir / EXPORT_MANIFEST_NAME


def probe_cache(spec: ModelSpec, cache_root: Path | None = None) -> CacheStatus:
    """Probe the cache for ``spec`` — NEVER raises; a MISS carries a human reason.

    HIT requires ALL of: the export manifest parses as JSON; its
    ``source_revision`` == ``spec.revision``; its ``artifact.filename`` ==
    ``spec.artifact_filename``; the artifact file exists with size ==
    ``artifact.size`` AND sha256 == ``artifact.sha256`` (the parity-gate
    provenance carried through). Any failure is a MISS.
    """
    cache_dir = cache_dir_for(spec, cache_root)
    artifact_path = _artifact_path(cache_dir, spec)
    manifest_path = _manifest_path(cache_dir)

    def _miss(reason: str) -> CacheStatus:
        return CacheStatus(
            cache_dir=cache_dir,
            artifact_path=artifact_path,
            manifest_path=manifest_path,
            hit=False,
            reason=reason,
        )

    try:
        manifest = manifest_mod.load_manifest(manifest_path)
    except manifest_mod.ManifestIntegrityError as exc:
        return _miss(str(exc))

    if manifest.get("source_revision") != spec.revision:
        return _miss(
            f"cached manifest source_revision {manifest.get('source_revision')!r} != "
            f"spec revision {spec.revision!r}"
        )
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        return _miss("cached manifest has no 'artifact' section")
    if artifact.get("filename") != spec.artifact_filename:
        return _miss(
            f"cached artifact filename {artifact.get('filename')!r} != "
            f"spec artifact_filename {spec.artifact_filename!r}"
        )
    if not artifact_path.is_file():
        return _miss(f"cached artifact missing at {artifact_path}")
    if artifact_path.stat().st_size != artifact.get("size"):
        return _miss(f"cached artifact size mismatch at {artifact_path}")
    if manifest_mod.sha256_file(artifact_path) != artifact.get("sha256"):
        return _miss(f"cached artifact sha256 mismatch at {artifact_path} (tampered or corrupt)")

    return CacheStatus(
        cache_dir=cache_dir,
        artifact_path=artifact_path,
        manifest_path=manifest_path,
        hit=True,
        reason=None,
    )


def ensure_cached(
    spec: ModelSpec,
    *,
    cache_root: Path | None = None,
    exporter: Callable[..., Path] | None = None,
    run: UvRunner | None = None,
    export_runner: Callable[[list[str]], None] | None = None,
) -> CacheStatus:
    """Return a verified cache HIT for ``spec``, generating it on MISS.

    * HIT (probe passes): returned unchanged — works under ``AIR_GAP=1`` (a hit
      needs no egress).
    * MISS under ``AIR_GAP=1``: raise :class:`AirGapGenerationRefusedError`
      BEFORE any ``exporter``/venv/subprocess/egress side effect (C2).
    * MISS otherwise: run the parity-gated isolated export into a per-attempt
      ``<cache_dir>.tmp`` dir, then atomically publish it (``os.replace``
      after removing any stale ``cache_dir``), re-probe, and return the HIT.

    ``exporter`` defaults to a lazy import of
    :func:`ne.model_export.cli.export_model` (avoids an import cycle) invoked as
    ``export_model(spec, out_dir=tmp, repo_outputs=False, run=..., export_runner=...)``.
    ``run``/``export_runner`` are the venv-build / in-venv-worker seams forwarded
    to it so tests never touch a real venv/subprocess/network.
    """
    status = probe_cache(spec, cache_root)
    if status.hit:
        return status

    if os.environ.get("AIR_GAP") == "1":
        raise AirGapGenerationRefusedError(
            f"AIR_GAP=1 and no cached ONNX for {spec.repo_id}@{spec.revision} "
            f"({status.reason}) — refusing to generate (generation requires egress to "
            "pypi.org/huggingface.co). A sealed deployment must supply the pre-staged "
            "bundle artifact + export manifest via --onnx-artifact/--export-manifest; "
            "nothing is generated or downloaded at runtime."
        )

    cache_dir = status.cache_dir
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    # UNIQUE per-attempt temp dir on the SAME filesystem as cache_dir (so os.replace
    # is atomic). A deterministic "<cache>.tmp" would let two concurrent generators of
    # the SAME model race — one rmtree'ing / moving the dir the other is still writing
    # (Codex P2). mkdtemp gives each attempt its own dir; publish stays last-writer-wins.
    tmp_dir = Path(tempfile.mkdtemp(dir=cache_dir.parent, prefix=cache_dir.name + ".tmp-"))
    try:
        if exporter is None:
            from ne.model_export.cli import export_model as exporter  # lazy: avoid import cycle

        exporter(spec, out_dir=tmp_dir, repo_outputs=False, run=run, export_runner=export_runner)

        # Atomic publish: os.replace fails on a NON-EMPTY existing target, so remove
        # any stale cache_dir first (the rmtree is load-bearing).
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        os.replace(tmp_dir, cache_dir)
    except BaseException:
        # never leave a half-written attempt dir behind on failure
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    published = probe_cache(spec, cache_root)
    if not published.hit:
        raise RuntimeError(
            f"generation completed but the published cache entry at {cache_dir} still "
            f"does not verify ({published.reason}) — refusing to return an unverified entry"
        )
    return published


__all__ = [
    "CACHE_ROOT_VAR",
    "DEFAULT_CACHE_ROOT",
    "EXPORT_MANIFEST_NAME",
    "GOLDEN_VECTORS_NAME",
    "AirGapGenerationRefusedError",
    "CacheStatus",
    "cache_dir_for",
    "ensure_cached",
    "probe_cache",
]
