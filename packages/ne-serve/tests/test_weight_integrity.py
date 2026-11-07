# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Manifest v2 + verify: 'which weights, exactly' at the air-gap boundary.

No network — ``repo_info`` and ``snapshot_download`` are injected fakes. Covers:
resolved-SHA capture + PIN into the snapshot call, per-file sha256, stale-file
non-absorption (sibling-list enumeration, not rglob), fail-closed download on
unresolvable SHA / missing listed file, and verify_weights over pristine /
tampered / extra / missing / v1 / .cache stagings + the CLI.
"""

from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path
from typing import Any

import pytest

from ne.serve.__main__ import main
from ne.serve.download import download_weights, local_path, verify_weights
from ne.serve.errors import WeightIntegrityError

_SHA = "a" * 40
_CONTENT = {
    "config.json": b"{}",
    "model-00001.safetensors": b"weights-bytes-here",
}


def _fake_repo_info(*, siblings: list[str] | None = None, sha: str | None = _SHA):
    names = siblings if siblings is not None else list(_CONTENT)

    def repo_info(**_kwargs: Any) -> Any:
        return types.SimpleNamespace(
            sha=sha,
            siblings=[types.SimpleNamespace(rfilename=n) for n in names],
        )

    return repo_info


class _FakeSnapshot:
    """Callable snapshot_download fake that records its calls and stages files."""

    def __init__(self, *, skip: str | None = None) -> None:
        self.skip = skip
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        dest = Path(kwargs["local_dir"])
        for name, data in _CONTENT.items():
            if name == self.skip:
                continue
            (dest / name).write_bytes(data)


def _stage(tmp_path: Path, **kwargs: Any) -> tuple[Any, _FakeSnapshot]:
    snap = kwargs.pop("snapshot_download", None) or _FakeSnapshot()
    info = kwargs.pop("repo_info", None) or _fake_repo_info()
    result = download_weights(
        "Org/Repo",
        tmp_path,
        snapshot_download=snap,
        repo_info=info,
        **kwargs,
    )
    return result, snap


def test_manifest_records_resolved_sha_and_per_file_sha256(tmp_path: Path) -> None:
    result, snap = _stage(tmp_path)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["resolved_revision"] == _SHA
    assert manifest["revision"] == "main"
    assert result.resolved_revision == _SHA

    by_name = {f["name"]: f for f in manifest["files"]}
    for name, data in _CONTENT.items():
        assert by_name[name]["sha256"] == hashlib.sha256(data).hexdigest()
        assert by_name[name]["size"] == len(data)

    # The snapshot was PINNED to the resolved SHA (not the mutable 'main').
    assert snap.calls[0]["revision"] == _SHA


def test_unresolvable_sha_raises_and_never_downloads(tmp_path: Path) -> None:
    snap = _FakeSnapshot()
    with pytest.raises(WeightIntegrityError):
        download_weights(
            "Org/Repo",
            tmp_path,
            snapshot_download=snap,
            repo_info=_fake_repo_info(sha=None),
        )
    assert snap.calls == []  # fail-closed before any download


def test_stale_file_not_absorbed(tmp_path: Path) -> None:
    dest = local_path("Org/Repo", tmp_path)
    dest.mkdir(parents=True)
    (dest / "stale.bin").write_bytes(b"straggler from a prior staging")

    result, _ = _stage(tmp_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    names = {f["name"] for f in manifest["files"]}
    assert names == set(_CONTENT)  # stale.bin enumerated from siblings, never rglob


def test_missing_listed_file_raises(tmp_path: Path) -> None:
    with pytest.raises(WeightIntegrityError):
        _stage(tmp_path, snapshot_download=_FakeSnapshot(skip="config.json"))


def test_verify_passes_on_pristine(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    manifest = verify_weights(result.path)
    assert manifest["resolved_revision"] == _SHA


def test_verify_fails_on_tampered(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    (result.path / "config.json").write_bytes(b"tampered")
    with pytest.raises(WeightIntegrityError) as excinfo:
        verify_weights(result.path)
    assert "config.json" in str(excinfo.value)


def test_verify_fails_on_extra_and_stale_file(tmp_path: Path) -> None:
    # Pre-existing straggler (not absorbed into the manifest) + a fresh drop.
    dest = local_path("Org/Repo", tmp_path)
    dest.mkdir(parents=True)
    (dest / "stale.bin").write_bytes(b"straggler")
    result, _ = _stage(tmp_path)
    (result.path / "evil.bin").write_bytes(b"exfil")

    with pytest.raises(WeightIntegrityError) as excinfo:
        verify_weights(result.path)
    msg = str(excinfo.value)
    assert "evil.bin" in msg
    assert "stale.bin" in msg


def test_verify_fails_on_missing_file(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    (result.path / "config.json").unlink()
    with pytest.raises(WeightIntegrityError) as excinfo:
        verify_weights(result.path)
    assert "config.json" in str(excinfo.value)


def test_verify_fails_without_manifest(tmp_path: Path) -> None:
    with pytest.raises(WeightIntegrityError):
        verify_weights(tmp_path)


def test_verify_fails_on_v1_manifest(tmp_path: Path) -> None:
    dest = local_path("Org/Repo", tmp_path)
    dest.mkdir(parents=True)
    legacy = {
        "repo_id": "Org/Repo",
        "revision": "main",
        "downloaded_at": "2026-01-01T00:00:00+00:00",
        "files": [{"name": "config.json", "size": 2}],
    }
    (dest / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(WeightIntegrityError) as excinfo:
        verify_weights(dest)
    assert "re-stage" in str(excinfo.value)


def test_verify_ignores_dot_cache(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    cache = result.path / ".cache" / "huggingface"
    cache.mkdir(parents=True)
    (cache / "x.meta").write_text("hub metadata", encoding="utf-8")
    verify_weights(result.path)  # no raise


def test_cli_verify_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _stage(tmp_path)

    rc = main(["verify", "Org/Repo", "--dest", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verified 2 files" in out
    assert _SHA in out

    # Tamper, then the CLI must exit 1.
    (local_path("Org/Repo", tmp_path) / "config.json").write_bytes(b"tampered")
    rc = main(["verify", "Org/Repo", "--dest", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
