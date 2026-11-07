# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``SplinkSidecar`` — the ONLY seam to Splink (out-of-process, C4).

Splink drags GPL igraph UNCONDITIONALLY, so it runs in a hash-pinned sidecar venv
(``ne.envbuild.ensure_sidecar_venv``; the ragas_env precedent) and is invoked as a
SUBPROCESS over temp JSON files. NOTHING in this module (or anywhere platform-side)
imports splink/igraph/duckdb — the whole closure lives only in the sidecar venv and
never enters ``uv.lock`` (proven by ``test_no_gpl_sidecar_leak.py`` + the
``t2-sidecars-out-of-process`` import-linter contract).
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

__all__ = ["SplinkSidecar", "default_splink_env_dir"]


def default_splink_env_dir() -> Path:
    """The in-repo ``packages/ne-brain-graph/splink_env`` directory."""
    # er/_splink.py -> er -> brain_graph -> ne -> src -> ne-brain-graph
    return Path(__file__).resolve().parents[4] / "splink_env"


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


class SplinkSidecar:
    """Build/refresh the Splink sidecar venv and run ``resolve.py`` as a subprocess."""

    def __init__(
        self,
        *,
        env_dir: Path | None = None,
        venv_dir: Path | None = None,
        run: UvRunner | None = None,
    ) -> None:
        self._env_dir = env_dir or default_splink_env_dir()
        self._venv_dir = venv_dir or sidecar_venv_dir(self._env_dir, ".splink-venv")
        self._run = run

    @property
    def env_dir(self) -> Path:
        return self._env_dir

    @property
    def venv_dir(self) -> Path:
        return self._venv_dir

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
            pre_lock_check=_air_gap_check(self._venv_dir, "the Splink ER sidecar venv"),
            name="the Splink ER sidecar venv",
            run=self._run,
        )

    def resolve(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Run the sidecar deduper over ``payload``; return sorted candidate pairs.

        ``payload`` = ``{entity_class, records, compare_fields, blocking_fields,
        threshold, seed}``. The subprocess reads the input JSON and writes a
        ``[{left_key, right_key, match_probability, match_weight, blocking_rule}]``
        JSON — no splink import ever happens platform-side.
        """
        self.ensure()
        python = venv_python(self._venv_dir)
        script = self._env_dir / "resolve.py"
        with tempfile.TemporaryDirectory(prefix="ne-splink-") as scratch:
            in_path = Path(scratch) / "in.json"
            out_path = Path(scratch) / "out.json"
            in_path.write_text(json.dumps(payload), encoding="utf-8")
            subprocess.run(
                [str(python), str(script), "--input", str(in_path), "--output", str(out_path)],
                check=True,
            )
            return list(json.loads(out_path.read_text(encoding="utf-8")))
