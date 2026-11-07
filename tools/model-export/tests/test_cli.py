# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CLI orchestration: AIR_GAP fail-closed refusal, injected-runner
orchestration (no real venv/subprocess/network — HARD GATE: the heavy
artifact run is out of scope for the builder), and the truth gate: a parity
FAILURE emits NO artifact/manifest/goldens."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ne.model_export import cli
from ne.model_export.specs import QWEN3_EMBEDDING_0_6B, ModelSpec


def _spec_for(tmp_path: Path) -> ModelSpec:
    # Re-point the committed paths at tmp_path-relative locations so the test
    # never touches the real repo tree.
    return replace(
        QWEN3_EMBEDDING_0_6B,
        manifest_path=Path("manifests") / "spec.json",
        golden_vectors_path=Path("golden") / "spec-vectors.json",
        golden_texts_path=Path("golden") / "spec-texts.json",
    )


def _seed_texts_file(root: Path, spec: ModelSpec) -> None:
    path = root / spec.golden_texts_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"texts": ["hello world", "bonjour le monde"]}), encoding="utf-8")


def _seed_export_env(root: Path) -> None:
    # ensure_sidecar_venv reads env_dir/requirements.txt unconditionally (even
    # on the fast path) to compute the marker's "want" hash — seed a stand-in
    # so it never touches the real tools/model-export/export_env/.
    env_dir = root / "tools" / "model-export" / "export_env"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text("# fixture\n", encoding="utf-8")


def _venv_build_runner(calls: list[list[str]]):
    """Fake sidecar-venv-build ``run`` seam: records calls and, on the ``uv
    venv`` invocation, actually creates the venv dir (so the real
    ``ensure_sidecar_venv`` marker-write step has somewhere to write)."""

    def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        calls.append(cmd)
        if cmd[:2] == ["uv", "venv"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)

    return _run


def _passing_export_runner(dim: int, resolved_revision: str):
    def _runner(cmd: list[str]) -> None:
        out_dir = Path(cmd[cmd.index("--out-dir") + 1])
        onnx_dir = out_dir / "onnx"
        onnx_dir.mkdir(parents=True, exist_ok=True)
        (onnx_dir / "model.onnx").write_bytes(b"fake-graph")
        (onnx_dir / "model.onnx_data").write_bytes(b"fake-external-data" * 3)
        report = {
            "model_key": "qwen3-embedding-0.6b",
            "resolved_revision": resolved_revision,
            "toolchain": {"torch": "2.13.0", "onnxruntime": "1.27.0"},
            "parity": {
                "golden_count": 2,
                "min_cosine": 0.99998,
                "threshold": 0.9999,
                "passed": True,
            },
            "goldens": {
                "texts": ["hello world", "bonjour le monde"],
                "vectors": [[1.0] + [0.0] * (dim - 1), [0.0, 1.0] + [0.0] * (dim - 2)],
                "dim": dim,
                "source_revision": resolved_revision,
            },
        }
        (out_dir / "build-report.json").write_text(json.dumps(report), encoding="utf-8")

    return _runner


def _failing_export_runner(resolved_revision: str):
    def _runner(cmd: list[str]) -> None:
        out_dir = Path(cmd[cmd.index("--out-dir") + 1])
        onnx_dir = out_dir / "onnx"
        onnx_dir.mkdir(parents=True, exist_ok=True)
        (onnx_dir / "model.onnx").write_bytes(b"fake-graph")
        (onnx_dir / "model.onnx_data").write_bytes(b"fake-external-data")
        report = {
            "model_key": "qwen3-embedding-0.6b",
            "resolved_revision": resolved_revision,
            "toolchain": {},
            "parity": {
                "golden_count": 2,
                "min_cosine": 0.42,
                "threshold": 0.9999,
                "passed": False,
            },
            "goldens": {"texts": [], "vectors": [], "dim": 2, "source_revision": resolved_revision},
        }
        (out_dir / "build-report.json").write_text(json.dumps(report), encoding="utf-8")

    return _runner


def test_air_gap_refuses_before_any_venv_or_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[object] = []
    spec = _spec_for(tmp_path)

    with pytest.raises(cli.AirGapExportRefusedError):
        cli.export_model(
            spec,
            root=tmp_path,
            run=lambda cmd, env=None: calls.append(("venv", cmd)),
            export_runner=lambda cmd: calls.append(("export", cmd)),
        )
    assert calls == []  # no venv build, no export subprocess — refused before any side effect


def test_injected_runner_orchestration_emits_artifact_manifest_goldens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    spec = _spec_for(tmp_path)
    _seed_texts_file(tmp_path, spec)
    _seed_export_env(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    venv_calls: list[list[str]] = []
    out_dir = tmp_path / "dist"

    artifact_path = cli.export_model(
        spec,
        root=tmp_path,
        out_dir=out_dir,
        run=_venv_build_runner(venv_calls),
        export_runner=_passing_export_runner(dim=spec.dim, resolved_revision=spec.revision),
    )

    assert venv_calls, "the sidecar-venv build seam must be invoked"
    assert artifact_path == out_dir / spec.artifact_filename
    assert artifact_path.is_file()

    manifest_path = tmp_path / spec.manifest_path
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["model_key"] == "qwen3-embedding-0.6b"
    assert manifest["source_revision"] == spec.revision
    assert manifest["artifact"]["filename"] == spec.artifact_filename
    # NE-TEI-GEN: the artifact section is exactly {filename, sha256, size} — no
    # hosted-asset tag (the GitHub-release channel was vetoed).
    assert set(manifest["artifact"]) == {"filename", "sha256", "size"}
    names = {f["name"] for f in manifest["files"]}
    assert names == {"model.onnx", "model.onnx_data"}

    # NE-TEI-GEN: the FULL manifest + golden vectors are ALSO written next to the
    # artifact (cache consumers read these from out_dir).
    assert (out_dir / "export-manifest.json").is_file()
    assert (out_dir / "golden-vectors.json").is_file()

    goldens_path = tmp_path / spec.golden_vectors_path
    assert goldens_path.is_file()
    goldens = json.loads(goldens_path.read_text(encoding="utf-8"))
    assert goldens["texts"] == ["hello world", "bonjour le monde"]
    assert goldens["dim"] == spec.dim
    assert goldens["source_revision"] == spec.revision

    # the scratch work dir is cleaned up — only committed outputs remain
    assert not (out_dir / "_work").exists()


def test_parity_failure_emits_no_artifact_manifest_or_goldens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    spec = _spec_for(tmp_path)
    _seed_texts_file(tmp_path, spec)
    _seed_export_env(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    out_dir = tmp_path / "dist"

    with pytest.raises(cli.ParityGateFailedError, match="min_cosine"):
        cli.export_model(
            spec,
            root=tmp_path,
            out_dir=out_dir,
            run=_venv_build_runner([]),
            export_runner=_failing_export_runner(resolved_revision=spec.revision),
        )

    assert not (out_dir / spec.artifact_filename).exists()
    assert not (tmp_path / spec.manifest_path).exists()
    assert not (tmp_path / spec.golden_vectors_path).exists()


def test_resolved_revision_mismatch_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    spec = _spec_for(tmp_path)
    _seed_texts_file(tmp_path, spec)
    _seed_export_env(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    out_dir = tmp_path / "dist"

    with pytest.raises(cli.ParityGateFailedError, match="resolved"):
        cli.export_model(
            spec,
            root=tmp_path,
            out_dir=out_dir,
            run=_venv_build_runner([]),
            export_runner=_passing_export_runner(dim=spec.dim, resolved_revision="f" * 40),
        )

    assert not (out_dir / spec.artifact_filename).exists()
    assert not (tmp_path / spec.manifest_path).exists()
    assert not (tmp_path / spec.golden_vectors_path).exists()


def test_main_export_success_prints_artifact_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = _spec_for(tmp_path)
    monkeypatch.setattr(
        cli,
        "export_model",
        lambda s, **k: tmp_path / "dist" / s.artifact_filename,
    )
    monkeypatch.setitem(cli.MODEL_SPECS, spec.key, spec)

    rc = cli.main(["export", spec.key])
    assert rc == 0
    out = capsys.readouterr().out
    assert "artifact:" in out


def test_main_export_air_gap_refusal_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    rc = cli.main(["export", "qwen3-embedding-0.6b"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "AIR_GAP" in err


def test_repo_outputs_false_writes_out_dir_only_not_committed_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # NE-TEI-GEN: a generate-into-cache run (repo_outputs=False) writes the FULL
    # manifest + goldens into out_dir but NEVER touches the committed repo paths.
    monkeypatch.delenv("AIR_GAP", raising=False)
    spec = _spec_for(tmp_path)
    _seed_texts_file(tmp_path, spec)
    _seed_export_env(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    out_dir = tmp_path / "cache-entry"

    cli.export_model(
        spec,
        root=tmp_path,
        out_dir=out_dir,
        repo_outputs=False,
        run=_venv_build_runner([]),
        export_runner=_passing_export_runner(dim=spec.dim, resolved_revision=spec.revision),
    )

    assert (out_dir / spec.artifact_filename).is_file()
    assert (out_dir / "export-manifest.json").is_file()
    assert (out_dir / "golden-vectors.json").is_file()
    # committed paths NOT written
    assert not (tmp_path / spec.manifest_path).exists()
    assert not (tmp_path / spec.golden_vectors_path).exists()


def test_repo_outputs_default_writes_committed_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    spec = _spec_for(tmp_path)
    _seed_texts_file(tmp_path, spec)
    _seed_export_env(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    out_dir = tmp_path / "dist"

    cli.export_model(
        spec,
        root=tmp_path,
        out_dir=out_dir,
        run=_venv_build_runner([]),
        export_runner=_passing_export_runner(dim=spec.dim, resolved_revision=spec.revision),
    )

    assert (tmp_path / spec.manifest_path).is_file()
    assert (tmp_path / spec.golden_vectors_path).is_file()


def test_export_venv_honors_sidecar_venv_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # NE-SVENV: with NE_SIDECAR_VENV_ROOT set, the export venv builds under it
    # ($ROOT/.export-venv), not the in-package sibling (mirrors
    # test_splink_sidecar.py::test_venv_dir_honors_sidecar_venv_root).
    monkeypatch.delenv("AIR_GAP", raising=False)
    root_dir = tmp_path / "writable"
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(root_dir))
    spec = _spec_for(tmp_path)
    _seed_texts_file(tmp_path, spec)
    _seed_export_env(tmp_path)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    venv_calls: list[list[str]] = []
    cli.export_model(
        spec,
        root=tmp_path,
        out_dir=tmp_path / "dist",
        run=_venv_build_runner(venv_calls),
        export_runner=_passing_export_runner(dim=spec.dim, resolved_revision=spec.revision),
    )

    uv_venv_calls = [c for c in venv_calls if c[:2] == ["uv", "venv"]]
    assert uv_venv_calls, "the sidecar-venv build seam must be invoked"
    assert uv_venv_calls[0][2] == str(root_dir / ".export-venv")


def test_main_ensure_cached_prints_single_line_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # CLI ensure-cached prints EXACTLY one machine-readable JSON object as the
    # last stdout line (the ne-serve parsing contract). Seed a valid cache so it
    # is a HIT (no generation needed) under a redirected cache root.
    monkeypatch.setenv("NE_TEI_ONNX_CACHE", str(tmp_path / "cache"))
    spec = cli.MODEL_SPECS["qwen3-embedding-0.6b"]
    _seed_valid_cache(spec, tmp_path / "cache")

    rc = cli.main(["ensure-cached", "qwen3-embedding-0.6b"])
    assert rc == 0
    out_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    payload = json.loads(out_lines[-1])
    assert payload["model_key"] == "qwen3-embedding-0.6b"
    assert payload["repo_id"] == spec.repo_id
    assert payload["hit"] is True
    assert Path(payload["artifact"]).is_file()


def test_main_ensure_cached_air_gap_miss_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NE_TEI_ONNX_CACHE", str(tmp_path / "empty-cache"))
    monkeypatch.setenv("AIR_GAP", "1")
    rc = cli.main(["ensure-cached", "qwen3-embedding-0.6b"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "AIR_GAP" in err


def _seed_valid_cache(spec: ModelSpec, cache_root: Path) -> None:
    """Write a fixture-valid cache entry (artifact + manifest w/ matching
    sha256/size) so probe_cache HITs — no real export."""
    from ne.model_export import cache as cache_mod
    from ne.model_export import manifest as manifest_mod

    cache_dir = cache_mod.cache_dir_for(spec, cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = cache_dir / spec.artifact_filename
    artifact_path.write_bytes(b"fake-artifact-tarball-bytes")
    manifest = {
        "manifest_version": 1,
        "model_key": spec.key,
        "source_repo": spec.repo_id,
        "source_revision": spec.revision,
        "files": [],
        "artifact": {
            "filename": spec.artifact_filename,
            "sha256": manifest_mod.sha256_file(artifact_path),
            "size": artifact_path.stat().st_size,
        },
    }
    manifest_mod.save_manifest(manifest, cache_dir / cache_mod.EXPORT_MANIFEST_NAME)


def test_main_unknown_model_key_rejected_by_argparse() -> None:
    with pytest.raises(SystemExit):
        cli.main(["export", "not-a-real-model"])
