# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-TEI-GEN local cache: key/layout + env override, probe miss modes,
generate-on-miss + populate + hit, hit-skips-generation, and the AIR_GAP
fail-closed refusal (zero seam invocations). The heavy export is fully mocked
(a fake exporter writes tiny bytes + a matching manifest) — no real
export/venv/subprocess/network anywhere."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.model_export import cache as cache_mod
from ne.model_export import manifest as manifest_mod
from ne.model_export.specs import QWEN3_EMBEDDING_0_6B, ModelSpec


def _spec() -> ModelSpec:
    return QWEN3_EMBEDDING_0_6B


def _fake_exporter(*, calls: list[dict[str, object]], artifact_bytes: bytes = b"onnx-tarball"):
    """A stand-in for cli.export_model: records the call and writes a valid cache
    entry (artifact + full manifest w/ matching sha256/size + goldens) into
    out_dir. NEVER runs a real export."""

    def _export(spec: ModelSpec, *, out_dir: Path, repo_outputs: bool = True, **kw: object) -> Path:
        calls.append({"spec": spec, "out_dir": out_dir, "repo_outputs": repo_outputs, **kw})
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = out_dir / spec.artifact_filename
        artifact_path.write_bytes(artifact_bytes)
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
        manifest_mod.save_manifest(manifest, out_dir / cache_mod.EXPORT_MANIFEST_NAME)
        (out_dir / cache_mod.GOLDEN_VECTORS_NAME).write_text('{"vectors": []}', encoding="utf-8")
        return artifact_path

    return _export


def _seed_valid_cache(
    spec: ModelSpec, cache_root: Path, *, artifact_bytes: bytes = b"onnx"
) -> Path:
    cache_dir = cache_mod.cache_dir_for(spec, cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = cache_dir / spec.artifact_filename
    artifact_path.write_bytes(artifact_bytes)
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
    return cache_dir


# --------------------------------------------------------------------------- #
# cache key / layout / env override
# --------------------------------------------------------------------------- #
def test_cache_dir_key_is_org_name_at_rev(tmp_path: Path) -> None:
    spec = _spec()
    cache_dir = cache_mod.cache_dir_for(spec, tmp_path)
    assert cache_dir == tmp_path / f"Qwen--Qwen3-Embedding-0.6B@{spec.revision}"


def test_cache_root_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec = _spec()
    monkeypatch.setenv(cache_mod.CACHE_ROOT_VAR, str(tmp_path / "env-root"))
    cache_dir = cache_mod.cache_dir_for(spec)
    assert cache_dir.parent == tmp_path / "env-root"


def test_cache_root_empty_env_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec()
    monkeypatch.setenv(cache_mod.CACHE_ROOT_VAR, "")
    cache_dir = cache_mod.cache_dir_for(spec)
    assert cache_dir.parent == cache_mod.DEFAULT_CACHE_ROOT


def test_explicit_cache_root_beats_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec = _spec()
    monkeypatch.setenv(cache_mod.CACHE_ROOT_VAR, str(tmp_path / "env-root"))
    cache_dir = cache_mod.cache_dir_for(spec, tmp_path / "explicit")
    assert cache_dir.parent == tmp_path / "explicit"


# --------------------------------------------------------------------------- #
# probe miss modes (never raises)
# --------------------------------------------------------------------------- #
def test_probe_miss_on_empty_dir(tmp_path: Path) -> None:
    status = cache_mod.probe_cache(_spec(), tmp_path)
    assert status.hit is False
    assert status.reason


def test_probe_hit_on_valid_cache(tmp_path: Path) -> None:
    spec = _spec()
    _seed_valid_cache(spec, tmp_path)
    status = cache_mod.probe_cache(spec, tmp_path)
    assert status.hit is True
    assert status.reason is None
    assert status.artifact_path.is_file()


def test_probe_miss_on_revision_mismatch(tmp_path: Path) -> None:
    from dataclasses import replace

    spec = _spec()
    _seed_valid_cache(spec, tmp_path)
    # A revision bump lands in a NEW cache dir → miss (auto-regenerate).
    bumped = replace(spec, revision="f" * 40)
    status = cache_mod.probe_cache(bumped, tmp_path)
    assert status.hit is False


def test_probe_miss_on_sha_tamper(tmp_path: Path) -> None:
    spec = _spec()
    cache_dir = _seed_valid_cache(spec, tmp_path, artifact_bytes=b"onnx-original")
    # Same length as the seed (13 bytes) but different content → sha256 miss, not size.
    (cache_dir / spec.artifact_filename).write_bytes(b"onnx-TAMPERD!")
    status = cache_mod.probe_cache(spec, tmp_path)
    assert status.hit is False
    assert "sha256" in (status.reason or "")


def test_probe_miss_on_size_tamper(tmp_path: Path) -> None:
    spec = _spec()
    cache_dir = _seed_valid_cache(spec, tmp_path, artifact_bytes=b"original-bytes")
    (cache_dir / spec.artifact_filename).write_bytes(b"short")
    status = cache_mod.probe_cache(spec, tmp_path)
    assert status.hit is False


# --------------------------------------------------------------------------- #
# ensure_cached
# --------------------------------------------------------------------------- #
def test_ensure_cached_generates_on_miss_and_hits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    spec = _spec()
    calls: list[dict[str, object]] = []
    status = cache_mod.ensure_cached(
        spec, cache_root=tmp_path, exporter=_fake_exporter(calls=calls)
    )
    assert len(calls) == 1  # generated exactly once
    assert calls[0]["repo_outputs"] is False  # a cache entry never touches committed paths
    assert status.hit is True
    assert status.artifact_path.is_file()
    assert (status.cache_dir / cache_mod.EXPORT_MANIFEST_NAME).is_file()
    # the per-attempt .tmp dir was consumed by the atomic publish
    assert not status.cache_dir.with_name(status.cache_dir.name + ".tmp").exists()

    # A second ensure is a pure HIT — no regeneration.
    status2 = cache_mod.ensure_cached(
        spec, cache_root=tmp_path, exporter=_fake_exporter(calls=calls)
    )
    assert len(calls) == 1  # unchanged
    assert status2.hit is True


def test_ensure_cached_hit_skips_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AIR_GAP", raising=False)
    spec = _spec()
    _seed_valid_cache(spec, tmp_path)

    def _exploding_exporter(*a: object, **k: object) -> Path:
        raise AssertionError("exporter must NOT be called on a cache hit")

    status = cache_mod.ensure_cached(spec, cache_root=tmp_path, exporter=_exploding_exporter)
    assert status.hit is True


def test_ensure_cached_air_gap_miss_refuses_before_any_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    spec = _spec()
    seam_calls: list[str] = []

    def _exporter(*a: object, **k: object) -> Path:
        seam_calls.append("exporter")
        raise AssertionError("unreachable")

    def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        seam_calls.append("run")

    def _export_runner(cmd: list[str]) -> None:
        seam_calls.append("export_runner")

    with pytest.raises(cache_mod.AirGapGenerationRefusedError, match="bundle"):
        cache_mod.ensure_cached(
            spec,
            cache_root=tmp_path,
            exporter=_exporter,
            run=_run,
            export_runner=_export_runner,
        )
    assert seam_calls == []  # refused before ANY generation side effect
    # no cache/tmp dirs created
    cache_dir = cache_mod.cache_dir_for(spec, tmp_path)
    assert not cache_dir.exists()
    assert not cache_dir.with_name(cache_dir.name + ".tmp").exists()


def test_ensure_cached_air_gap_hit_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A cache HIT needs no egress — allowed even under AIR_GAP=1.
    monkeypatch.setenv("AIR_GAP", "1")
    spec = _spec()
    _seed_valid_cache(spec, tmp_path)

    def _exploding_exporter(*a: object, **k: object) -> Path:
        raise AssertionError("exporter must NOT be called on a cache hit")

    status = cache_mod.ensure_cached(spec, cache_root=tmp_path, exporter=_exploding_exporter)
    assert status.hit is True
