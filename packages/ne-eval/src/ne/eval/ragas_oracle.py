# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Platform-side driver for the dev-only RAGAS cross-validation oracle (SPEC §11).

This module imports NOTHING from ragas. ragas (and its heavy langchain/scipy/
openai-SDK closure — the exact closure the C4 license gate keeps out of the
platform lock) is confined to an ISOLATED, hash-pinned virtualenv (default
``packages/ne-eval/.ragas-venv``), built from ``ragas_env/requirements.txt`` with
``--require-hashes`` — the SAME isolation pattern
``ne.brain.ingest.docling_parser`` uses for docling's parser env. The cross-check
therefore runs the in-venv ``ragas_env/cross_check.py`` as a subprocess; this
module only manages that venv (via the shared ``ne.envbuild`` sidecar-venv leaf,
NE-CKC8/NE-CKC13) and shuttles JSON in/out.

The oracle is dev/test tooling only: it validates that our native
``ne.eval.metrics`` arithmetic matches ragas's on identical fixed verdicts. It is
never on any runtime path and never a platform dependency.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ne.envbuild import ensure_sidecar_venv, venv_python
from ne.net.airgap import AirGapViolationError, air_gap_engaged

_PKG = Path("packages") / "ne-eval"
_RAGAS_ENV_DIR = _PKG / "ragas_env"
_VENV_DIR = _PKG / ".ragas-venv"

# NE-CKC13: the probe/lock/rebuild flow now lives in ``ne.envbuild`` (shared with
# the docling, presidio, and codegen builders). The rebuild is serialised behind
# a cross-process/thread file lock (a SIBLING of venv_dir so it survives the
# rebuild's rmtree), the marker is written atomically (tmp + os.replace) so a
# partial marker is never observed, and the AUTHORITATIVE up-to-date decision is
# re-checked UNDER the lock so only the first waiter actually rebuilds.
#
# NE-FIX-GOVERN-EVAL (C2): the ragas venv builder is now AIR_GAP fail-closed.
# ``ensure_ragas_env_at`` passes an ``_air_gap_check`` closure as
# ``ensure_sidecar_venv``'s ``pre_lock_check`` hook, so a missing/stale venv under
# AIR_GAP raises a typed ``AirGapViolationError`` before any uv egress. The oracle
# is dev/test-only, but its builder must still never egress under AIR_GAP (C2).
_ENV_LOCK_TIMEOUT_S = 1800

# Overrides for deployments/CI runners that vendor the payload elsewhere.
_ENV_DIR_OVERRIDE_VAR = "NE_RAGAS_ENV_DIR"
_VENV_DIR_OVERRIDE_VAR = "NE_RAGAS_VENV_DIR"


def _find_repo_root(start: Path) -> Path:
    """Walk up from ``start`` until a directory containing ``uv.lock`` is found."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")


def _resolve_dirs() -> tuple[Path, Path]:
    env_override = os.environ.get(_ENV_DIR_OVERRIDE_VAR)
    venv_override = os.environ.get(_VENV_DIR_OVERRIDE_VAR)
    root = _find_repo_root(Path(__file__))
    env_dir = Path(env_override) if env_override else root / _RAGAS_ENV_DIR
    venv_dir = Path(venv_override) if venv_override else root / _VENV_DIR
    return env_dir, venv_dir


def _venv_python_at(venv_dir: Path) -> Path:
    return venv_python(venv_dir)


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Module global so tests can monkeypatch it (see presidio_backend)."""
    subprocess.run(cmd, check=True, env=env)


def _air_gap_check(venv_dir: Path, name: str) -> Callable[[], None]:
    """Build the fail-closed AIR_GAP ``pre_lock_check`` hook for ``venv_dir``.

    The returned closure is handed to :func:`ne.envbuild.ensure_sidecar_venv` as
    its ``pre_lock_check``: it fires AFTER the unlocked up-to-date fast-path but
    BEFORE any mkdir/lock/rmtree/subprocess. ``air_gap_engaged()`` is read at CALL
    time (not factory-build time) so a test's ``monkeypatch.setenv("AIR_GAP", ...)``
    takes effect. Reaching the hook means the venv is absent or stale, which would
    otherwise trigger a ``uv pip install`` egress from inside the customer VPC —
    forbidden under AIR_GAP, so fail typed+closed before any build side effect.
    """

    def _check() -> None:
        if air_gap_engaged():
            raise AirGapViolationError(
                f"AIR_GAP is engaged and {name} at {venv_dir} is missing or stale; "
                "runtime uv pip install egress (pypi.org/files.pythonhosted.org) is "
                "forbidden — pre-build the venv at image build/pre-staging time."
            )

    return _check


def ensure_ragas_env_at(env_dir: Path, venv_dir: Path) -> None:
    """Create/refresh the isolated hash-pinned ragas venv if needed.

    Rebuilt when missing or when the stored requirements marker no longer matches
    the committed ``requirements.txt``. Installation is ``--require-hashes``. The
    atomic-marker + under-lock authoritative re-check flow lives in
    :func:`ne.envbuild.ensure_sidecar_venv`, shared with the docling, presidio,
    and codegen builders (NE-CKC8/NE-CKC13).

    C2 AIR_GAP (NE-FIX-GOVERN-EVAL): the builder is fail-closed. An already-current
    venv is a silent no-op (the unlocked fast-path); a missing/stale venv under
    AIR_GAP raises a typed :class:`~ne.net.airgap.AirGapViolationError` via
    the ``pre_lock_check`` hook before any uv egress. The oracle is dev/test-only,
    but its builder must still never egress under AIR_GAP.
    """
    ensure_sidecar_venv(
        env_dir=env_dir,
        venv_dir=venv_dir,
        install_args=(),
        pre_lock_check=_air_gap_check(venv_dir, "the isolated ragas venv"),
        lock_timeout_s=_ENV_LOCK_TIMEOUT_S,
        name="the isolated ragas venv",
        run=_run,
    )


def ensure_ragas_env() -> tuple[Path, Path]:
    """Resolve + ensure the default ragas env; return ``(env_dir, venv_dir)``."""
    env_dir, venv_dir = _resolve_dirs()
    ensure_ragas_env_at(env_dir, venv_dir)
    return env_dir, venv_dir


def run_cross_check(fixture: dict[str, Any]) -> dict[str, Any]:
    """Run ``cross_check.py`` in the ragas venv over ``fixture``; return its JSON."""
    env_dir, venv_dir = ensure_ragas_env()
    python = _venv_python_at(venv_dir)
    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / "in.json"
        out_path = Path(tmp) / "out.json"
        in_path.write_text(json.dumps(fixture), encoding="utf-8")
        result = subprocess.run(
            [
                str(python),
                str(env_dir / "cross_check.py"),
                "--input",
                str(in_path),
                "--output",
                str(out_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr_tail = "\n".join(result.stderr.strip().splitlines()[-40:])
            raise RuntimeError(
                f"ragas cross_check subprocess failed (exit {result.returncode}):\n{stderr_tail}"
            )
        return json.loads(out_path.read_bytes())
