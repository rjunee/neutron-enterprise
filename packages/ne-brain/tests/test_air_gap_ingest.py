# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AIR_GAP fail-closed on the ingest egress surfaces (NE-CKC2, audit #2).

Two seams:

* ``ensure_env_at`` — under AIR_GAP an absent/stale isolated parser venv must
  fail with a typed ``AirGapViolationError`` and NEVER attempt the ``uv``
  build/egress (spy-proven zero ``_run`` invocations); a current venv is a
  silent no-op.
* ``FastEmbedBM25Encoder`` — under AIR_GAP ``HF_HUB_OFFLINE=1`` +
  ``HF_HUB_DISABLE_TELEMETRY=1`` are FORCED into the environment BEFORE the model
  loader runs (so a cold cache raises with no network attempt), and a cold-cache
  load failure is wrapped as ``AirGapViolationError``. Without AIR_GAP only
  telemetry-off is set.

All env mutation goes through ``monkeypatch`` (delenv of the keys the code itself
writes is registered first) so global state restores between tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import fastembed
import pytest

from ne.brain.ingest import docling_parser
from ne.brain.sovereignty import AirGapViolationError


def _write_requirements(env_dir) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text("docling==2.110.0\n", encoding="utf-8")


def _build_current_venv(env_dir, venv_dir, *, marker: str | None = None) -> None:
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
    want = marker
    if want is None:
        want = hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()
    (venv_dir / ".requirements.sha256").write_text(want, encoding="utf-8")


def test_ensure_env_at_air_gap_missing_venv_fails_closed_no_build_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "parser_env"
    _write_requirements(env_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(docling_parser, "_run", lambda cmd: calls.append(cmd))

    with pytest.raises(AirGapViolationError):
        docling_parser.ensure_env_at(env_dir, tmp_path / "venv-absent")

    assert calls == []  # no `uv venv` / `uv pip install` attempted


def test_ensure_env_at_air_gap_current_venv_is_noop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "parser_env"
    _write_requirements(env_dir)
    venv_dir = tmp_path / "venv"
    _build_current_venv(env_dir, venv_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(docling_parser, "_run", lambda cmd: calls.append(cmd))

    assert docling_parser.ensure_env_at(env_dir, venv_dir) is None
    assert calls == []


def test_ensure_env_at_air_gap_stale_marker_fails_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "parser_env"
    _write_requirements(env_dir)
    venv_dir = tmp_path / "venv"
    _build_current_venv(env_dir, venv_dir, marker="deadbeef-not-the-real-hash")
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(docling_parser, "_run", lambda cmd: calls.append(cmd))

    with pytest.raises(AirGapViolationError):
        docling_parser.ensure_env_at(env_dir, venv_dir)
    assert calls == []


def _install_sparse_stub(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, str]], *, fail: bool
) -> None:
    class _StubSparse:
        def __init__(self, model_name: str) -> None:
            recorded.append(dict(os.environ))
            if fail:
                raise RuntimeError("no cached model")

        def embed(self, texts):  # pragma: no cover - unused in these tests
            return []

        def query_embed(self, texts):  # pragma: no cover - unused
            return []

    monkeypatch.setattr(fastembed, "SparseTextEmbedding", _StubSparse)


def test_bm25_air_gap_forces_offline_env_and_wraps_cold_cache_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ne.brain.sparse import FastEmbedBM25Encoder

    monkeypatch.setenv("AIR_GAP", "1")
    # Register these keys with monkeypatch so the code's direct os.environ writes
    # are restored at teardown (no leak to sibling tests).
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)

    recorded: list[dict[str, str]] = []
    _install_sparse_stub(monkeypatch, recorded, fail=True)

    encoder = FastEmbedBM25Encoder()
    with pytest.raises(AirGapViolationError):
        asyncio.run(encoder.encode(["x"]))

    assert len(recorded) == 1
    # Offline forced BEFORE the loader ran — the fail-closed mechanism; no network
    # attempt was possible.
    assert recorded[0].get("HF_HUB_OFFLINE") == "1"
    assert recorded[0].get("HF_HUB_DISABLE_TELEMETRY") == "1"


def test_bm25_no_air_gap_sets_telemetry_off_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from ne.brain.sparse import FastEmbedBM25Encoder

    monkeypatch.delenv("AIR_GAP", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)

    recorded: list[dict[str, str]] = []
    _install_sparse_stub(monkeypatch, recorded, fail=False)

    encoder = FastEmbedBM25Encoder()
    model = asyncio.run(encoder._get_model())
    assert model is not None

    # No AIR_GAP: offline is NOT forced; telemetry-off IS set.
    assert "HF_HUB_OFFLINE" not in os.environ
    assert os.environ.get("HF_HUB_DISABLE_TELEMETRY") == "1"
