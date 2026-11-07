# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LightRAG sidecar: env-builder shape (fake runner) + projector payload + LIVE (parked).

The env-builder shape is asserted with a monkeypatched ``_run`` — this test NEVER
runs a real ``uv venv`` / ``uv pip install`` (the ``test_splink_sidecar`` /
``test_ragas_env_lock`` precedent): a full hash-pinned install
(``--require-hashes``, no ``--no-deps``) of ``lightrag_env/requirements.txt`` into a
``.lightrag-venv``. The ``LightRagProjector`` payload shape is asserted over the
in-memory store. The LIVE subprocess ``project()``/``query()`` round-trip and the
T-LR-3 no-egress/pipmaster-dead assertion are skipped unless ``.lightrag-venv`` is
actually built (the ragas live-parked precedent).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from ne.brain_graph.lightrag._lightrag import LightRagSidecar, default_lightrag_env_dir
from ne.brain_graph.lightrag.projector import LightRagProjector
from ne.brain_graph.testing.fixtures import make_edge, make_node
from ne.brain_graph.testing.memory import InMemoryGraphStore


class _FakeRunner:
    """Fakes ``uv venv`` / ``uv pip install`` shapes; asserts the install contract."""

    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir
        self.install_calls = 0

    def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        if cmd[:2] == ["uv", "venv"]:
            venv_dir = Path(cmd[2])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!fake-python\n")
            return
        if cmd[:3] == ["uv", "pip", "install"]:
            python = Path(cmd[4])
            assert python.is_file(), "venv python must exist on install entry"
            assert "--require-hashes" in cmd, "the LightRAG sidecar install must be hash-pinned"
            assert "--no-deps" not in cmd, "LightRAG installs its FULL hash-pinned closure"
            assert cmd[-2:] == ["-r", str(self._env_dir / "requirements.txt")], cmd
            self.install_calls += 1
            return
        raise AssertionError(f"unexpected command: {cmd!r}")


def _write_requirements(env_dir: Path, body: str) -> str:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text(body)
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


def test_default_env_dir_points_at_in_repo_lightrag_env() -> None:
    env_dir = default_lightrag_env_dir()
    assert env_dir.name == "lightrag_env"
    assert (env_dir / "requirements.in").is_file()
    assert (env_dir / "project.py").is_file()
    assert (env_dir / "query.py").is_file()


def test_ensure_builds_hash_pinned_venv(tmp_path: Path) -> None:
    env_dir = tmp_path / "lightrag_env"
    venv_dir = tmp_path / ".lightrag-venv"
    want = _write_requirements(env_dir, "lightrag-hku==1.5.4 --hash=sha256:deadbeef\n")
    runner = _FakeRunner(env_dir)
    sidecar = LightRagSidecar(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert sidecar.available() is False  # not built yet
    sidecar.ensure()
    assert runner.install_calls == 1
    assert sidecar.available() is True
    assert (venv_dir / ".requirements.sha256").read_text() == want

    sidecar.ensure()  # a second ensure is a no-op (marker still matches)
    assert runner.install_calls == 1


def test_ensure_rebuilds_when_requirements_change(tmp_path: Path) -> None:
    env_dir = tmp_path / "lightrag_env"
    venv_dir = tmp_path / ".lightrag-venv"
    _write_requirements(env_dir, "lightrag-hku==1.5.4 --hash=sha256:aaa\n")
    runner = _FakeRunner(env_dir)
    sidecar = LightRagSidecar(env_dir=env_dir, venv_dir=venv_dir, run=runner)
    sidecar.ensure()
    assert runner.install_calls == 1

    _write_requirements(env_dir, "lightrag-hku==1.5.4 --hash=sha256:bbb\n")  # changed
    assert sidecar.available() is False
    sidecar.ensure()
    assert runner.install_calls == 2


def test_workdir_defaults_beside_the_venv(tmp_path: Path) -> None:
    sidecar = LightRagSidecar(env_dir=tmp_path / "lightrag_env")
    assert sidecar.workdir.name == ".lightrag-workdir"
    assert sidecar.workdir.parent == tmp_path


def _projector_store() -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    a = make_node("a", "Person", ("t",), display_name="Alice", source_ref="Contact:a")
    b = make_node("b", "Account", ("t",), display_name="Acme", source_ref="Account:b")
    asyncio.run(store.upsert_nodes([a, b]))
    asyncio.run(store.upsert_edges([make_edge("BELONGS_TO", "a", "b", ("t",))]))
    return store


def test_projector_payload_shape() -> None:
    payload = asyncio.run(LightRagProjector(_projector_store()).build_payload())
    assert set(payload) == {"entities", "relationships", "chunks"}

    entities = {e["entity_name"]: e for e in payload["entities"]}
    assert entities["a"]["entity_type"] == "Person"
    assert entities["a"]["source_id"] == "Contact:a"  # source_id == the node source_ref
    assert entities["b"]["entity_type"] == "Account"
    # No governance fields leak into the exported payload.
    for ent in payload["entities"]:
        assert "acl" not in ent and "content_hash" not in ent

    rels = payload["relationships"]
    assert len(rels) == 1
    assert rels[0]["src_id"] == "a" and rels[0]["tgt_id"] == "b"
    assert rels[0]["description"] == "BELONGS_TO"

    # Deterministic ordering → a rebuild is byte-stable.
    again = asyncio.run(LightRagProjector(_projector_store()).build_payload())
    assert again == payload


# --- LIVE (parked unless .lightrag-venv is built) ---------------------------

import pytest  # noqa: E402


def _venv_built() -> bool:
    return LightRagSidecar().available()


@pytest.mark.skipif(not _venv_built(), reason="LightRAG sidecar venv not built (live-parked)")
def test_live_project_query_roundtrip(tmp_path: Path) -> None:  # pragma: no cover - live-parked
    sidecar = LightRagSidecar(workdir=tmp_path / "wd")
    payload = asyncio.run(LightRagProjector(_projector_store()).build_payload())
    sidecar.project(payload)
    out = sidecar.query("Alice Acme", top_k=5)
    assert set(out) == {"entity_keys", "chunk_ids"}
    assert isinstance(out["entity_keys"], list)


def test_venv_and_workdir_honor_sidecar_venv_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV: both venv_dir and workdir resolve under NE_SIDECAR_VENV_ROOT."""
    root = tmp_path / "writable"
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(root))
    sidecar = LightRagSidecar(env_dir=tmp_path / "lightrag_env")
    assert sidecar.venv_dir == root / ".lightrag-venv"
    assert sidecar.workdir == root / ".lightrag-workdir"


def test_explicit_dirs_beat_sidecar_venv_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV: explicit venv_dir=/workdir= kwargs win over NE_SIDECAR_VENV_ROOT."""
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(tmp_path / "writable"))
    venv = tmp_path / "explicit" / ".lightrag-venv"
    workdir = tmp_path / "explicit" / ".lightrag-workdir"
    sidecar = LightRagSidecar(env_dir=tmp_path / "lightrag_env", venv_dir=venv, workdir=workdir)
    assert sidecar.venv_dir == venv
    assert sidecar.workdir == workdir
