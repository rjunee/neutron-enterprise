# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``ne-model-export`` CLI — the platform-side (dep-less) orchestrator for the
isolated ONNX export + build-time parity gate (NE-TEI-ONNX / NE-TEI-GEN).

    ne-model-export export       qwen3-embedding-0.6b [--out DIR]
    ne-model-export ensure-cached qwen3-embedding-0.6b   # generate-on-miss + cache
    ne-model-export cache-info    qwen3-embedding-0.6b   # read-only probe (never generates)

This module NEVER imports torch/optimum/onnxruntime/transformers/
sentence-transformers — those live ONLY in the hash-pinned sidecar venv
(``export_env/``, built by :func:`ne.envbuild.ensure_sidecar_venv`) and are
driven as a subprocess (``export_env/export_and_gate.py``), mirroring
``ne.govern.redaction.presidio_backend``. ``ne-model-export``'s own
``pyproject.toml`` declares exactly one dependency: ``ne-envbuild`` (C4/C11
isolation proof; see ``tests/test_env_policy.py``).

Truth gate (§0-law-5): :func:`export_model` reads the in-venv worker's
``build-report.json`` and refuses to write ANYTHING (no manifest, no golden
vectors, no ``.tar.gz``) unless ``report["parity"]["passed"]`` is true.

NE-TEI-GEN: the generated artifact is NOT hosted anywhere (owner veto of the
GitHub-release channel, 2026-07-19). ``ensure-cached`` generates on a cache
miss (egress-OK only — ``AIR_GAP=1`` fail-closes to the sealed bundle) into
``$NE_TEI_ONNX_CACHE``; the sealed deploy carries the artifact + manifest in
its bundle. The export venv honors ``$NE_SIDECAR_VENV_ROOT`` (hardened deploys).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from ne.envbuild import UvRunner, ensure_sidecar_venv, sidecar_venv_dir, venv_python
from ne.model_export import cache as cache_mod
from ne.model_export import goldens as goldens_mod
from ne.model_export import manifest as manifest_mod
from ne.model_export import tarball as tarball_mod
from ne.model_export.specs import MODEL_SPECS, ModelSpec

_TOOL_DIR = Path("tools") / "model-export"
_EXPORT_ENV_DIR = _TOOL_DIR / "export_env"
#: The venv-dir leaf name; :func:`ne.envbuild.sidecar_venv_dir` resolves it to
#: ``$NE_SIDECAR_VENV_ROOT/.export-venv`` when that var is set-and-nonempty, else
#: the historical in-package sibling ``tools/model-export/.export-venv`` (NE-SVENV).
_VENV_LEAF = ".export-venv"


class AirGapExportRefusedError(RuntimeError):
    """AIR_GAP=1 forbids building/using the isolated export sidecar venv."""


class ParityGateFailedError(RuntimeError):
    """The build-time cosine gate failed — NO artifact/manifest/goldens were emitted."""


def _find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")


def _default_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def _air_gap_check() -> None:
    """Fail-closed AIR_GAP=1 refusal — the ``pre_lock_check`` hook, but ALSO
    called explicitly BEFORE ``ensure_sidecar_venv`` so it fires even when the
    venv already exists (a stale-but-present venv could otherwise mask a
    forbidden re-export attempt; the export tool is a build-time-only
    surface, never expected to run under AIR_GAP at all)."""
    if os.environ.get("AIR_GAP") == "1":
        raise AirGapExportRefusedError(
            "AIR_GAP=1 — refusing to run the model-export tool (it requires egress "
            "to pypi.org/huggingface.co to build the sidecar venv and download the "
            "source checkpoint). This tool is a build-time-only surface; never run "
            "it inside a sealed deployment."
        )


def export_model(
    spec: ModelSpec,
    *,
    root: Path | None = None,
    out_dir: Path | None = None,
    repo_outputs: bool = True,
    run: UvRunner | None = None,
    export_runner: Callable[[list[str]], None] | None = None,
) -> Path:
    """Run the export + build-time parity gate for ``spec``.

    On a PASSING parity gate: writes the ``.tar.gz`` artifact (containing the
    onnx pair + the core manifest), the FULL export manifest
    (``out_dir/export-manifest.json``), and the golden vectors
    (``out_dir/golden-vectors.json``) into ``out_dir`` — ALWAYS (cache consumers
    read these). When ``repo_outputs`` is true (the default, the committed-repo
    ``export`` command), ALSO writes the FULL manifest to ``root/spec.manifest_path``
    and the golden vectors to ``root/spec.golden_vectors_path`` — the two
    COMMITTED paths. ``ensure_cached`` passes ``repo_outputs=False`` (a cache
    entry never touches the committed repo tree).

    On a FAILING parity gate: raises :class:`ParityGateFailedError` and emits
    NOTHING (no artifact, no manifest, no goldens) — the truth gate.

    ``run`` is the sidecar-venv-BUILD seam (forwarded to
    :func:`ne.envbuild.ensure_sidecar_venv`); ``export_runner`` is the seam for
    invoking ``export_env/export_and_gate.py`` itself. Both are injectable so
    tests never touch a real venv/subprocess/network (HARD GATE: the heavy
    artifact run is out of scope for the builder).
    """
    _air_gap_check()

    repo_root = root if root is not None else _find_repo_root(Path(__file__))
    env_dir = repo_root / _EXPORT_ENV_DIR
    venv_dir = sidecar_venv_dir(env_dir, _VENV_LEAF)
    dest = out_dir if out_dir is not None else repo_root / _TOOL_DIR / "dist"
    dest.mkdir(parents=True, exist_ok=True)

    ensure_sidecar_venv(
        env_dir=env_dir,
        venv_dir=venv_dir,
        pre_lock_check=_air_gap_check,
        name="the isolated model-export venv",
        run=run if run is not None else _default_run,
    )

    python = venv_python(venv_dir)
    texts_path = repo_root / spec.golden_texts_path
    work_dir = dest / "_work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    cmd = [
        str(python),
        str(env_dir / "export_and_gate.py"),
        "--repo-id",
        spec.repo_id,
        "--revision",
        spec.revision,
        "--model-key",
        spec.key,
        "--texts-file",
        str(texts_path),
        "--out-dir",
        str(work_dir),
        "--dim",
        str(spec.dim),
        "--threshold",
        str(spec.threshold),
    ]
    runner = export_runner if export_runner is not None else _default_run
    runner(cmd)

    report_path = work_dir / "build-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    parity = report["parity"]

    if report.get("resolved_revision") != spec.revision:
        raise ParityGateFailedError(
            f"export resolved {report.get('resolved_revision')!r} but the spec pins "
            f"{spec.revision!r} — refusing to emit artifact/manifest/goldens "
            "(the checkpoint moved out from under a pinned revision)."
        )

    if not parity.get("passed"):
        raise ParityGateFailedError(
            f"build-time parity gate FAILED for {spec.key}: "
            f"min_cosine={parity.get('min_cosine')!r} < threshold={spec.threshold!r} "
            "— refusing to emit artifact/manifest/goldens (§0-law-5 truth gate)."
        )

    onnx_dir = work_dir / "onnx"
    onnx_files = [onnx_dir / "model.onnx", onnx_dir / "model.onnx_data"]
    missing = [f for f in onnx_files if not f.is_file()]
    if missing:
        raise ParityGateFailedError(
            f"parity gate reported PASS but expected onnx file(s) missing: {missing} "
            "— refusing to emit artifact/manifest/goldens."
        )

    core_manifest = manifest_mod.build_manifest(
        model_key=spec.key,
        source_repo=spec.repo_id,
        source_revision=report["resolved_revision"],
        dtype="fp32",
        toolchain=report["toolchain"],
        files=onnx_files,
        golden_count=parity["golden_count"],
        min_cosine=parity["min_cosine"],
        threshold=spec.threshold,
    )
    inner_manifest_path = work_dir / "export-manifest.json"
    manifest_mod.save_manifest(core_manifest, inner_manifest_path)

    artifact_path = dest / spec.artifact_filename
    tarball_mod.pack_tar_gz(
        {
            "onnx/model.onnx": onnx_files[0],
            "onnx/model.onnx_data": onnx_files[1],
            "export-manifest.json": inner_manifest_path,
        },
        artifact_path,
    )

    full_manifest = manifest_mod.with_artifact(
        core_manifest,
        filename=spec.artifact_filename,
        path=artifact_path,
    )
    goldens = report["goldens"]
    golden_payload = goldens_mod.build_golden_vectors(
        texts=goldens["texts"],
        vectors=goldens["vectors"],
        dim=goldens["dim"],
        source_revision=goldens["source_revision"],
    )

    # ALWAYS write the FULL manifest + golden vectors next to the artifact — cache
    # consumers (ne.model_export.cache) read these from out_dir, and a cache entry
    # never touches the committed repo tree.
    manifest_mod.save_manifest(full_manifest, dest / cache_mod.EXPORT_MANIFEST_NAME)
    goldens_mod.save_golden_vectors(golden_payload, dest / cache_mod.GOLDEN_VECTORS_NAME)

    # The committed-repo copies are the C11 pin — written only for the `export`
    # command (repo_outputs=True), never for a generate-into-cache run.
    if repo_outputs:
        manifest_mod.save_manifest(full_manifest, repo_root / spec.manifest_path)
        goldens_mod.save_golden_vectors(golden_payload, repo_root / spec.golden_vectors_path)

    shutil.rmtree(work_dir)
    return artifact_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ne-model-export",
        description="Export + build-time parity-gate an embedder to ONNX (isolated sidecar venv).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="export, parity-gate, and emit the committed artifact")
    export.add_argument(
        "model_key", choices=sorted(MODEL_SPECS), help="registry key (see specs.py)"
    )
    export.add_argument(
        "--out",
        type=Path,
        default=None,
        help="artifact output dir (default: tools/model-export/dist)",
    )

    ensure = sub.add_parser(
        "ensure-cached",
        help="probe the local cache and generate-on-miss (egress-OK; AIR_GAP fail-closes)",
    )
    ensure.add_argument(
        "model_key", choices=sorted(MODEL_SPECS), help="registry key (see specs.py)"
    )

    info = sub.add_parser(
        "cache-info",
        help="print the cache probe result as JSON (read-only; never generates)",
    )
    info.add_argument("model_key", choices=sorted(MODEL_SPECS), help="registry key (see specs.py)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "export":
        spec = MODEL_SPECS[args.model_key]
        try:
            artifact_path = export_model(spec, out_dir=args.out)
        except (AirGapExportRefusedError, ParityGateFailedError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"artifact: {artifact_path}")
        print(f"manifest: {spec.manifest_path} (commit this)")
        print(f"golden vectors: {spec.golden_vectors_path} (commit this)")
        return 0

    if args.command == "ensure-cached":
        spec = MODEL_SPECS[args.model_key]
        # Probe FIRST so the emitted JSON's "hit" reflects the state BEFORE this
        # call (was it already cached?), then ensure (generate-on-miss).
        was_hit = cache_mod.probe_cache(spec).hit
        try:
            status = cache_mod.ensure_cached(spec)
        except (
            cache_mod.AirGapGenerationRefusedError,
            AirGapExportRefusedError,
            ParityGateFailedError,
        ) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # Human progress went to stderr (export_model's subprocess); the LAST
        # stdout line is the single machine-readable JSON object (parsing contract
        # for ne.serve.stage_tei.ensure_generated_onnx).
        print(
            json.dumps(
                {
                    "model_key": spec.key,
                    "repo_id": spec.repo_id,
                    "revision": spec.revision,
                    "cache_dir": str(status.cache_dir),
                    "artifact": str(status.artifact_path),
                    "manifest": str(status.manifest_path),
                    "hit": was_hit,
                }
            )
        )
        return 0

    if args.command == "cache-info":
        spec = MODEL_SPECS[args.model_key]
        status = cache_mod.probe_cache(spec)
        print(
            json.dumps(
                {
                    "model_key": spec.key,
                    "repo_id": spec.repo_id,
                    "revision": spec.revision,
                    "cache_dir": str(status.cache_dir),
                    "artifact": str(status.artifact_path),
                    "manifest": str(status.manifest_path),
                    "hit": status.hit,
                    "reason": status.reason,
                }
            )
        )
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover — argparse.error exits before this


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
