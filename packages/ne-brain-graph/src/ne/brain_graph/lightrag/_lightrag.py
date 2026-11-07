# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``LightRagSidecar`` — the ONLY seam to LightRAG (library-only, out-of-process).

LightRAG runs in a hash-pinned sidecar venv (``ne.envbuild.ensure_sidecar_venv``;
the ragas_env / splink_env precedent) and is invoked as a SUBPROCESS over JSON
files. NOTHING in this module (or anywhere platform-side) imports lightrag/pipmaster
— the whole closure lives only in the sidecar venv and never enters ``uv.lock``
(proven by ``test_no_gpl_sidecar_leak.py`` + the ``t2-sidecars-out-of-process``
import-linter contract). The bundled LightRAG API server is BANNED (CVEs; see
``lightrag_env/README.md``); only ``project.py`` / ``query.py`` are driven here.

The sidecar is UNTRUSTED: every candidate it returns is re-validated platform-side,
trim-before-return, against the principal-scoped ``EntityGraph`` + T1 retriever
(C7). This module is the transport only.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ne.envbuild import (
    UvRunner,
    ensure_sidecar_venv,
    marker_path,
    read_marker,
    requirements_sha256,
    sidecar_venv_dir,
    venv_python,
)
from ne.net.airgap import AirGapViolationError, air_gap_engaged

__all__ = ["LightRagSidecar", "default_lightrag_env_dir"]


def default_lightrag_env_dir() -> Path:
    """The in-repo ``packages/ne-brain-graph/lightrag_env`` directory."""
    # lightrag/_lightrag.py -> lightrag -> brain_graph -> ne -> src -> ne-brain-graph
    return Path(__file__).resolve().parents[4] / "lightrag_env"


def _air_gap_check(venv_dir: Path, name: str) -> Callable[[], None]:
    """Build the fail-closed AIR_GAP hook for :func:`ensure_sidecar_venv`.

    Passed as ``pre_lock_check`` so it fires AFTER the unlocked up-to-date
    fast-path but BEFORE any mkdir/lock/rmtree/subprocess. Reaching it means the
    venv is absent or stale, which would trigger a ``uv pip install`` egress to
    pypi.org/files.pythonhosted.org from inside the customer VPC — forbidden under
    AIR_GAP. ``air_gap_engaged()`` is read at CALL TIME inside the closure so tests
    can toggle it via ``monkeypatch.setenv``.
    """

    def _check() -> None:
        if air_gap_engaged():
            raise AirGapViolationError(
                f"AIR_GAP is engaged and {name} at {venv_dir} is missing or stale; "
                "runtime uv pip install egress (pypi.org/files.pythonhosted.org) is "
                "forbidden — pre-build the venv at image build/pre-staging time."
            )

    return _check


class LightRagSidecar:
    """Build/refresh the LightRAG sidecar venv and drive ``project.py``/``query.py``."""

    def __init__(
        self,
        *,
        env_dir: Path | None = None,
        venv_dir: Path | None = None,
        workdir: Path | None = None,
        run: UvRunner | None = None,
    ) -> None:
        self._env_dir = env_dir or default_lightrag_env_dir()
        self._venv_dir = venv_dir or sidecar_venv_dir(self._env_dir, ".lightrag-venv")
        # A PERSISTENT workdir: project.py writes the KG here, query.py reads it.
        # It is a runtime-write path too (insert()'s mkdir fails on a read-only
        # rootfs identically to the venv), so it follows NE_SIDECAR_VENV_ROOT for
        # the same writable-root reason — keeping "workdir sits beside the venv"
        # true in BOTH branches; default (env unset) is byte-identical.
        self._workdir = workdir or sidecar_venv_dir(self._env_dir, ".lightrag-workdir")
        self._run = run

    @property
    def env_dir(self) -> Path:
        return self._env_dir

    @property
    def venv_dir(self) -> Path:
        return self._venv_dir

    @property
    def workdir(self) -> Path:
        return self._workdir

    def available(self) -> bool:
        """True iff the venv python exists AND the marker matches the committed reqs."""
        python = venv_python(self._venv_dir)
        if not python.is_file():
            return False
        try:
            want = requirements_sha256(self._env_dir)
        except OSError:
            return False
        return read_marker(marker_path(self._venv_dir)) == want

    def ensure(self) -> None:
        """(Re)build the hash-pinned sidecar venv if missing/stale — fail-closed under AIR_GAP.

        A CURRENT venv is a silent fast-path no-op. An ABSENT or STALE venv would
        trigger a ``uv pip install`` egress; under AIR_GAP that is refused with a
        typed :class:`~ne.net.airgap.AirGapViolationError` BEFORE any
        lock/mkdir/rmtree/subprocess (the ``pre_lock_check`` hook). The sanctioned
        path is a pre-built venv (build it at image build/pre-staging time).
        """
        ensure_sidecar_venv(
            env_dir=self._env_dir,
            venv_dir=self._venv_dir,
            pre_lock_check=_air_gap_check(
                self._venv_dir, "the LightRAG graph-retrieval sidecar venv"
            ),
            name="the LightRAG graph-retrieval sidecar venv",
            run=self._run,
        )

    def project(self, payload: dict[str, Any]) -> None:
        """Insert an exported custom-KG ``payload`` into the persistent workdir."""
        self.ensure()
        python = venv_python(self._venv_dir)
        script = self._env_dir / "project.py"
        self._workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ne-lightrag-") as scratch:
            in_path = Path(scratch) / "kg.json"
            in_path.write_text(json.dumps(payload), encoding="utf-8")
            subprocess.run(
                [
                    str(python),
                    str(script),
                    "--input",
                    str(in_path),
                    "--workdir",
                    str(self._workdir),
                ],
                check=True,
            )

    def query(self, query_text: str, *, top_k: int = 10) -> dict[str, list[str]]:
        """Return ``{entity_keys, chunk_ids}`` candidates for ``query_text``.

        These are CANDIDATES ONLY — the caller re-validates every key/chunk id
        against the trimmed ``EntityGraph`` + T1 retriever (trim-before-return).
        """
        self.ensure()
        python = venv_python(self._venv_dir)
        script = self._env_dir / "query.py"
        with tempfile.TemporaryDirectory(prefix="ne-lightrag-") as scratch:
            out_path = Path(scratch) / "out.json"
            subprocess.run(
                [
                    str(python),
                    str(script),
                    "--query",
                    query_text,
                    "--workdir",
                    str(self._workdir),
                    "--top-k",
                    str(top_k),
                    "--output",
                    str(out_path),
                ],
                check=True,
            )
            data = json.loads(out_path.read_text(encoding="utf-8"))
        return {
            "entity_keys": [str(k) for k in data.get("entity_keys", [])],
            "chunk_ids": [str(c) for c in data.get("chunk_ids", [])],
        }
