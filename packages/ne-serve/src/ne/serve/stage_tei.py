# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TEI hub-cache pre-staging (NE-TEI-ONNX) — builds the EXACT hub-cache tree
TEI's hf-hub 0.4.2 client reads (cache-first ``get()``), optionally injecting
a sha256-verified, self-exported fast-ort ONNX pair so TEI boots its ort
backend instead of the slow candle CPU fallback.

    <dest>/models--{org}--{name}/
      refs/main                      # 40-hex resolved sha, NO trailing newline
      snapshots/<resolved-sha>/
        <official sibling files...>  # config.json, tokenizer.json, ...
        onnx/model.onnx  onnx/model.onnx_data   # only when onnx_artifact given
      ne-stage-manifest.json         # manifest-v2-style: full-tree sha256 coverage

Reuses :func:`ne.serve.download._sha256_file` / :func:`ne.serve.download._is_resolved_sha`
and :class:`ne.serve.errors.WeightIntegrityError` — the same resolve-then-pin,
sibling-list-enumeration, and fail-closed-on-any-tamper conventions as
:func:`ne.serve.download.download_weights` / :func:`ne.serve.download.verify_weights`.

The ONNX pair's provenance is the COMMITTED export manifest (``export_manifest``)
produced by ``tools/model-export`` — its bytes are sha256-verified against that
manifest BEFORE ever landing in the served tree; a mismatch or a missing
manifest is a hard refusal (C11: the manifest is the authority, the artifact
path/URL is convenience).

NE-TEI-GEN generation model (owner decision 2026-07-19 — no hosted release):
:func:`stage_tei` with ``generate=True`` (the default-on ``stage-tei`` CLI mode)
resolves the ONNX pair + its export manifest on demand for a KNOWN repo by
shelling out to ``python -m ne.model_export.cli ensure-cached`` (a subprocess
SEAM — ``ne-serve`` declares NO dependency on ``ne-model-export``), which
probes ``$NE_TEI_ONNX_CACHE`` and, on a miss, GENERATES + caches via the
parity-gated isolated export toolchain. An explicit ``onnx_artifact`` always
wins (air-gap bundle / manual override). A sealed (``AIR_GAP=1``) deploy
fail-closes on a cache miss: ``ensure-cached`` refuses before any egress and the
non-zero exit surfaces here as a :class:`WeightIntegrityError` pointing at the
bundle path — nothing generates or downloads at runtime (C2).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from ne.serve.download import _is_resolved_sha, _sha256_file
from ne.serve.errors import WeightIntegrityError

_STAGE_MANIFEST_NAME: Final[str] = "ne-stage-manifest.json"
_STAGE_MANIFEST_VERSION: Final[int] = 2
_ONNX_MEMBER_NAMES: Final[tuple[str, str]] = ("onnx/model.onnx", "onnx/model.onnx_data")

#: Repos this stager can GENERATE a fast-ort ONNX pair for on demand, mapping
#: repo_id -> the ``ne.model_export`` registry key. Mirrors
#: ``ne.model_export.specs.MODEL_SPECS`` — ne-serve must NOT import
#: ne.model_export (no declared dependency); the coupling is subprocess-only.
GENERATABLE_REPOS: Final[dict[str, str]] = {"Qwen/Qwen3-Embedding-0.6B": "qwen3-embedding-0.6b"}


class StageResult(BaseModel):
    """Result of a ``stage_tei`` call: the staged repo dir + its manifest."""

    model_config = ConfigDict(frozen=True)

    path: Path
    manifest_path: Path
    resolved_revision: str


def _cache_dirname(repo_id: str) -> str:
    """HF hub-cache directory name for ``repo_id`` (``org/name`` -> ``models--org--name``)."""
    org, name = repo_id.split("/", 1)
    return f"models--{org}--{name}"


def _refs_main_path(repo_dir: Path) -> Path:
    return repo_dir / "refs" / "main"


def _snapshot_dir(repo_dir: Path, resolved: str) -> Path:
    return repo_dir / "snapshots" / resolved


def _load_json(path: Path, *, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise WeightIntegrityError(f"no {what} at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise WeightIntegrityError(f"{what} at {path} is unparseable: {exc}") from exc


def _write_stage_manifest(
    repo_dir: Path,
    *,
    repo_id: str,
    revision: str,
    resolved: str,
    manifest_path: Path,
) -> dict[str, Any]:
    """Rebuild the full-tree stage manifest by walking EVERY file currently
    under ``repo_dir`` (refs/main + every staged snapshot file, including any
    injected onnx pair) — the manifest always reflects exactly what is on
    disk right now."""
    files: list[dict[str, Any]] = []
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file() or path == manifest_path:
            continue
        rel = path.relative_to(repo_dir).as_posix()
        files.append({"name": rel, "size": path.stat().st_size, "sha256": _sha256_file(path)})
    manifest = {
        "manifest_version": _STAGE_MANIFEST_VERSION,
        "repo_id": repo_id,
        "revision": revision,
        "resolved_revision": resolved,
        "staged_at": datetime.now(UTC).isoformat(),
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def _verify_repo_dir(repo_dir: Path) -> dict[str, Any]:
    """Re-check ONE staged ``models--*`` dir against its own manifest: every
    listed file present/untampered, no extra untracked files, and
    ``refs/main`` is a byte-exact 40-hex sha (no trailing newline) matching
    the manifest's resolved revision. Raises :class:`WeightIntegrityError`
    (all problems collected together) on ANY mismatch; returns the parsed
    manifest on success."""
    manifest_path = repo_dir / _STAGE_MANIFEST_NAME
    manifest = _load_json(manifest_path, what=f"stage manifest for {repo_dir.name}")

    if manifest.get("manifest_version") != _STAGE_MANIFEST_VERSION:
        raise WeightIntegrityError(
            f"{repo_dir.name}: stage manifest predates v{_STAGE_MANIFEST_VERSION} "
            "— re-stage with `python -m ne.serve stage-tei`"
        )
    resolved = manifest.get("resolved_revision")
    if not _is_resolved_sha(resolved):
        raise WeightIntegrityError(f"{repo_dir.name}: manifest resolved_revision is not a sha")

    problems: list[str] = []
    listed: set[str] = set()
    for entry in manifest.get("files", []):
        name = entry["name"]
        listed.add(name)
        path = repo_dir / name
        if not path.is_file():
            problems.append(f"{repo_dir.name}: missing file {name} listed in manifest")
            continue
        if path.stat().st_size != entry.get("size"):
            problems.append(f"{repo_dir.name}: size mismatch for {name}")
        if _sha256_file(path) != entry.get("sha256"):
            problems.append(f"{repo_dir.name}: sha256 mismatch for {name} (tampered or corrupt)")

    for path in repo_dir.rglob("*"):
        if not path.is_file() or path == manifest_path:
            continue
        rel = path.relative_to(repo_dir).as_posix()
        if rel not in listed:
            problems.append(f"{repo_dir.name}: unexpected file {rel} (stale or tampered staging)")

    refs_path = _refs_main_path(repo_dir)
    if not refs_path.is_file():
        problems.append(f"{repo_dir.name}: missing refs/main")
    else:
        raw = refs_path.read_bytes()
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            text = ""
        if raw.endswith((b"\n", b"\r")) or not _is_resolved_sha(text):
            problems.append(
                f"{repo_dir.name}: refs/main is not a byte-exact 40-hex sha "
                "(must carry NO trailing newline)"
            )
        elif text != resolved:
            problems.append(
                f"{repo_dir.name}: refs/main {text!r} != manifest resolved_revision {resolved!r}"
            )

    if problems:
        raise WeightIntegrityError("\n".join(sorted(problems)))
    return manifest


def _stage_onnx_pair(
    onnx_artifact: Path,
    export_manifest_data: dict[str, Any],
    snapshot_dir: Path,
) -> None:
    """Unpack ``onnx_artifact`` (a ``.tar.gz``, ``filter='data'`` — path-
    traversal safe), sha256-verify ``model.onnx``/``model.onnx_data`` against
    ``export_manifest_data['files']`` BEFORE moving them into
    ``snapshot_dir/onnx/``."""
    files_by_name = {f["name"]: f for f in export_manifest_data.get("files", [])}
    for onnx_name in ("model.onnx", "model.onnx_data"):
        if onnx_name not in files_by_name:
            raise WeightIntegrityError(
                f"export manifest has no entry for {onnx_name} — cannot verify the artifact"
            )

    with tempfile.TemporaryDirectory(dir=snapshot_dir.parent.parent) as tmp_str:
        extract_dir = Path(tmp_str)
        try:
            with tarfile.open(onnx_artifact, mode="r:gz") as tar:
                tar.extractall(extract_dir, filter="data")
        except tarfile.TarError as exc:
            raise WeightIntegrityError(
                f"onnx artifact {onnx_artifact} is unreadable: {exc}"
            ) from exc

        onnx_dir_src = extract_dir / "onnx"
        for onnx_name in ("model.onnx", "model.onnx_data"):
            src = onnx_dir_src / onnx_name
            entry = files_by_name[onnx_name]
            if not src.is_file():
                raise WeightIntegrityError(
                    f"onnx artifact {onnx_artifact} does not contain onnx/{onnx_name}"
                )
            if src.stat().st_size != entry.get("size") or _sha256_file(src) != entry.get("sha256"):
                raise WeightIntegrityError(
                    f"onnx artifact {onnx_artifact}: {onnx_name} failed sha256 verification "
                    "against the export manifest (tampered or wrong artifact) — refusing to stage"
                )

        onnx_target_dir = snapshot_dir / "onnx"
        onnx_target_dir.mkdir(parents=True, exist_ok=True)
        for onnx_name in ("model.onnx", "model.onnx_data"):
            shutil.move(str(onnx_dir_src / onnx_name), str(onnx_target_dir / onnx_name))


def ensure_generated_onnx(
    repo_id: str,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[Path, Path]:
    """Ensure a generated + cached fast-ort ONNX pair for ``repo_id`` exists;
    return ``(artifact_path, export_manifest_path)``.

    Shells out to ``python -m ne.model_export.cli ensure-cached <model_key>`` (a
    subprocess SEAM — ``ne-serve`` has NO import dependency on ``ne-model-export``,
    only the workspace runtime env has both). ``ensure-cached`` probes
    ``$NE_TEI_ONNX_CACHE`` and, on a miss, GENERATES via the parity-gated isolated
    export toolchain — or, under ``AIR_GAP=1`` + miss, refuses (non-zero exit).

    A non-zero exit (including the AIR_GAP fail-closed refusal) raises
    :class:`WeightIntegrityError` carrying a stderr tail so the failure is
    diagnosable. The LAST non-empty stdout line is parsed as the JSON contract
    ``{"artifact": ..., "manifest": ...}`` (progress is stderr-only by contract;
    last-line parsing is the belt).
    """
    try:
        model_key = GENERATABLE_REPOS[repo_id]
    except KeyError as exc:
        raise WeightIntegrityError(
            f"{repo_id} is not a generatable repo (known: {sorted(GENERATABLE_REPOS)})"
        ) from exc

    cmd = [sys.executable, "-m", "ne.model_export.cli", "ensure-cached", model_key]
    run = runner if runner is not None else _default_ensure_cached_runner
    result = run(cmd)

    if result.returncode != 0:
        stderr_tail = "\n".join((result.stderr or "").splitlines()[-20:])
        raise WeightIntegrityError(
            f"generate-on-demand for {repo_id} failed (exit {result.returncode}) via "
            f"`{' '.join(cmd)}`. This is how an AIR_GAP=1 cache-miss fail-close surfaces "
            f"— a sealed deploy must supply the bundle artifact via --onnx-artifact. "
            f"stderr tail:\n{stderr_tail}"
        )

    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise WeightIntegrityError(
            f"generate-on-demand for {repo_id} produced no stdout JSON line to parse"
        )
    try:
        payload = json.loads(lines[-1])
        artifact = Path(payload["artifact"])
        manifest = Path(payload["manifest"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise WeightIntegrityError(
            f"generate-on-demand for {repo_id} returned an unparseable last stdout line: "
            f"{lines[-1]!r} ({exc})"
        ) from exc
    return artifact, manifest


def _default_ensure_cached_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _verified_staged_result(
    repo_dir: Path,
    *,
    repo_id: str,
    revision: str,
    repo_info: Callable[..., Any],
) -> StageResult | None:
    """Idempotency fast-path for auto-generate mode: return a :class:`StageResult`
    when ``repo_dir`` is ALREADY validly staged (clean against its own manifest)
    with its onnx pair present at the resolved ``revision`` — else ``None`` (never
    raises; a dirty/absent/revision-mismatched tree falls through to a full
    generate+stage).

    Runs BEFORE the generation resolver so a sealed (``AIR_GAP=1``) redeploy from
    a previously bundle-staged tree restages idempotently instead of the
    resolver's cache-miss fail-close firing when nothing needs (re)generating.
    """
    stage_manifest_path = repo_dir / _STAGE_MANIFEST_NAME
    if not stage_manifest_path.is_file():
        return None
    try:
        existing = _verify_repo_dir(repo_dir)
    except WeightIntegrityError:
        return None
    info = repo_info(repo_id=repo_id, revision=revision)
    resolved = getattr(info, "sha", None)
    if not _is_resolved_sha(resolved) or existing.get("resolved_revision") != resolved:
        return None
    if not (_snapshot_dir(repo_dir, resolved) / "onnx" / "model.onnx").is_file():
        return None
    return StageResult(path=repo_dir, manifest_path=stage_manifest_path, resolved_revision=resolved)


def stage_tei(
    repo_id: str,
    dest: Path | str,
    *,
    revision: str = "main",
    onnx_artifact: Path | str | None = None,
    export_manifest: Path | str | None = None,
    skip_safetensors: bool = False,
    generate: bool = False,
    ensure_onnx: Callable[[str], tuple[Path, Path]] | None = None,
    snapshot_download: Callable[..., Any] | None = None,
    repo_info: Callable[..., Any] | None = None,
) -> StageResult:
    """Build/refresh the hub-cache tree for ``repo_id`` under ``dest``.

    ``snapshot_download``/``repo_info`` are injectable (the
    :func:`ne.serve.download.download_weights` pattern) for tests; when
    ``None`` the real ``huggingface_hub`` callables are lazily imported.

    When ``onnx_artifact`` is given, ``export_manifest`` is REQUIRED (the C11
    pin/hash authority). Staging then resolves/pins to the export manifest's
    OWN ``source_revision`` — NOT the (possibly mutable, e.g. ``"main"``)
    ``revision`` argument — so a caller that never overrides the default
    ``revision="main"`` does not fail once upstream's mutable branch head
    drifts past the exact commit the ONNX artifact was exported from. The
    resolved sha is still compared against ``source_revision`` as a
    belt-and-suspenders check: a mismatch refuses staging rather than
    silently pairing an ONNX export with the wrong checkpoint revision.

    ``generate`` (default False): when True AND no explicit ``onnx_artifact`` was
    given AND ``repo_id`` is a known generatable repo, resolve the ONNX pair +
    export manifest via ``ensure_onnx`` (default :func:`ensure_generated_onnx`,
    the ``ne.model_export.cli ensure-cached`` subprocess seam) — generate-on-miss
    + cache for egress-OK hosts; AIR_GAP + miss fail-closes as a
    :class:`WeightIntegrityError`. An explicit ``onnx_artifact`` ALWAYS wins (the
    generate branch is gated on it being None). ``generate=True`` with an UNKNOWN
    repo proceeds snapshot-only (auto mode is best-effort, no error). When the
    tree is ALREADY validly staged with its onnx pair at the resolved revision,
    the resolver is skipped entirely (a sealed ``AIR_GAP=1`` redeploy from a
    previously bundle-staged tree restages idempotently without the resolver's
    cache-miss fail-close firing when nothing needs (re)generating).

    IDEMPOTENT: if the existing staged tree already verifies clean against
    its own manifest at the SAME resolved revision, and (when an artifact was
    requested) the onnx pair is already present, returns early without any
    download/subprocess.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    repo_dir = dest / _cache_dirname(repo_id)

    if snapshot_download is None:
        from huggingface_hub import snapshot_download as _snapshot_download

        snapshot_download = _snapshot_download
    if repo_info is None:
        from huggingface_hub import HfApi

        repo_info = HfApi().repo_info

    # NE-TEI-GEN generate-on-demand: only when no explicit artifact override is
    # given (an explicit --onnx-artifact always wins) and the repo is generatable.
    # The existing-tree verification runs BEFORE the generation resolver so an
    # already-staged (e.g. bundle-staged) tree restages idempotently without the
    # resolver's AIR_GAP cache-miss fail-close firing when nothing needs
    # (re)generating.
    if onnx_artifact is None and generate and repo_id in GENERATABLE_REPOS:
        early = _verified_staged_result(
            repo_dir, repo_id=repo_id, revision=revision, repo_info=repo_info
        )
        if early is not None:
            return early
        resolver = ensure_onnx if ensure_onnx is not None else ensure_generated_onnx
        onnx_artifact, export_manifest = resolver(repo_id)

    if onnx_artifact is not None and export_manifest is None:
        raise WeightIntegrityError(
            f"onnx_artifact given for {repo_id} but export_manifest is required "
            "(the C11 pin/hash authority) — pass export_manifest=..."
        )
    onnx_artifact_path = Path(onnx_artifact) if onnx_artifact is not None else None
    export_manifest_path = Path(export_manifest) if export_manifest is not None else None

    export_manifest_data: dict[str, Any] | None = None
    export_source_revision: str | None = None
    if export_manifest_path is not None:
        export_manifest_data = _load_json(export_manifest_path, what="export manifest")
        export_source_revision = export_manifest_data.get("source_revision")
        if not _is_resolved_sha(export_source_revision):
            raise WeightIntegrityError(
                f"export manifest at {export_manifest_path} has no valid "
                f"source_revision (got {export_source_revision!r})"
            )

    # When an ONNX artifact is requested, resolve/pin to the export
    # manifest's OWN source_revision — never the (possibly mutable, e.g.
    # "main") `revision` argument. Otherwise a caller that leaves `revision`
    # at its default would fail once upstream's mutable branch head moves
    # past the exact commit the artifact was exported from, despite the
    # pinned artifact still being perfectly valid.
    resolve_revision = export_source_revision if export_source_revision is not None else revision

    info = repo_info(repo_id=repo_id, revision=resolve_revision)
    resolved = getattr(info, "sha", None)
    if not _is_resolved_sha(resolved):
        raise WeightIntegrityError(
            f"could not resolve {repo_id}@{resolve_revision} to an immutable commit SHA"
        )

    if export_source_revision is not None and export_source_revision != resolved:
        raise WeightIntegrityError(
            f"export manifest source_revision {export_source_revision!r} != resolved "
            f"{repo_id}@{resolve_revision} = {resolved!r} — refusing to pair a mismatched "
            "ONNX artifact with this checkpoint revision"
        )

    stage_manifest_path = repo_dir / _STAGE_MANIFEST_NAME
    if stage_manifest_path.is_file():
        try:
            existing = _verify_repo_dir(repo_dir)
        except WeightIntegrityError:
            existing = None
        if existing is not None and existing.get("resolved_revision") == resolved:
            onnx_expected = onnx_artifact_path is not None
            onnx_present = (_snapshot_dir(repo_dir, resolved) / "onnx" / "model.onnx").is_file()
            if not onnx_expected or onnx_present:
                return StageResult(
                    path=repo_dir, manifest_path=stage_manifest_path, resolved_revision=resolved
                )

    names = sorted(s.rfilename for s in (info.siblings or []))
    if not names:
        raise WeightIntegrityError(
            f"{repo_id}@{resolved} lists no files — nothing servable / cannot build a "
            "verifiable manifest"
        )

    snapshot_dir = _snapshot_dir(repo_dir, resolved)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=dest) as tmp_str:
        tmp_dir = Path(tmp_str)
        snapshot_download(repo_id=repo_id, revision=resolved, local_dir=str(tmp_dir))
        for name in names:
            if skip_safetensors and name == "model.safetensors":
                continue
            src = tmp_dir / name
            if not src.is_file():
                raise WeightIntegrityError(
                    f"staged snapshot is missing {name} listed at {resolved}"
                )
            target = snapshot_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))

    _refs_main_path(repo_dir).parent.mkdir(parents=True, exist_ok=True)
    _refs_main_path(repo_dir).write_bytes(resolved.encode("ascii"))

    if onnx_artifact_path is not None and export_manifest_data is not None:
        _stage_onnx_pair(onnx_artifact_path, export_manifest_data, snapshot_dir)

    _write_stage_manifest(
        repo_dir,
        repo_id=repo_id,
        revision=revision,
        resolved=resolved,
        manifest_path=stage_manifest_path,
    )

    return StageResult(path=repo_dir, manifest_path=stage_manifest_path, resolved_revision=resolved)


def stage_tei_verify(cache_root: Path | str) -> dict[str, dict[str, Any]]:
    """Re-check every staged ``models--*`` dir under ``cache_root`` at the
    air-gap boundary — tamper/missing/extra files + refs/main formatting.
    Raises :class:`WeightIntegrityError` (all problems, across all repos,
    collected together) on ANY mismatch; returns ``{dirname: manifest}`` on
    success."""
    cache_root = Path(cache_root)
    repo_dirs = sorted(
        p for p in cache_root.iterdir() if p.is_dir() and p.name.startswith("models--")
    )
    if not repo_dirs:
        raise WeightIntegrityError(f"no staged models--* directories found under {cache_root}")

    problems: list[str] = []
    results: dict[str, dict[str, Any]] = {}
    for repo_dir in repo_dirs:
        try:
            results[repo_dir.name] = _verify_repo_dir(repo_dir)
        except WeightIntegrityError as exc:
            problems.append(str(exc))
    if problems:
        raise WeightIntegrityError("\n".join(problems))
    return results


__all__ = [
    "GENERATABLE_REPOS",
    "StageResult",
    "ensure_generated_onnx",
    "stage_tei",
    "stage_tei_verify",
]
