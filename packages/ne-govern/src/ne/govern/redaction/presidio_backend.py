# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PresidioRedactor — the adopt-first PII backend, ISOLATED in a hash-pinned venv.

This module imports NOTHING from presidio. Presidio (and its spaCy +
``en_core_web_lg`` NER-model closure — the heavy-scientific-Python class SPEC §11
keeps OUT of the platform lock/SBOM, C4) is confined to an ISOLATED, hash-pinned
virtualenv (default ``packages/ne-govern/.presidio-venv``). Redaction therefore
runs the in-venv ``presidio_env/redact_text.py`` as a subprocess over a JSON
protocol; this module only manages that venv and adapts the subprocess I/O to the
frozen :class:`ne.contracts.governance.Redactor` contract.

This is ADOPT-VIA-ISOLATION, not reimplementation: Presidio is the primary,
industry-standard detector; :class:`~ne.govern.redaction.regex_backend.RegexRedactor`
is only an air-gap supplement. The isolation pattern mirrors the docling parser
sidecar-venv builder exactly (repo-root ``uv.lock`` walk-up for the
monorepo dev checkout, ``presidio_env_dir=``/``venv_dir=`` kwargs and
``NE_GOVERN_PRESIDIO_ENV_DIR``/``NE_GOVERN_PRESIDIO_VENV_DIR`` env vars for
installed/production deployments) — INCLUDING its NE-020 P2a lock-serialised venv
rebuild: two concurrent ``redact()`` calls could both enter the (re)build path,
so ``ensure_env_at`` serialises it behind a cross-process/thread file lock (a
SIBLING of ``venv_dir`` that survives the rebuild ``rmtree``) with an in-lock
double-checked marker fast-path.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from ne.contracts.governance import RedactionResult
from ne.envbuild import (
    ensure_sidecar_venv,
    env_lock_path,
    marker_path,
    requirements_sha256,
    sidecar_venv_dir,
    venv_python,
)
from ne.govern.errors import RedactionError
from ne.net.airgap import AirGapViolationError, air_gap_engaged

_PKG = Path("packages") / "ne-govern"
_PRESIDIO_ENV_DIR = _PKG / "presidio_env"
_VENV_DIR = _PKG / ".presidio-venv"
_ENV_MARKER = ".requirements.sha256"

# NE-020 P2a / NE-CKC8: concurrency guard around the venv (re)build. Two
# concurrent redact() calls could both enter the rebuild path — one removing/
# recreating venv_dir while another installs into it — causing intermittent
# failures on the C12 redaction path. The probe/lock/rebuild flow now lives in
# ``ne.envbuild`` (shared with the docling and codegen builders): a cross-
# process/thread file lock (SIBLING of venv_dir so it survives the rmtree of
# venv_dir) serialises the rebuild, the marker is written atomically (tmp +
# os.replace) so a partial marker is never observed, and the AUTHORITATIVE
# up-to-date decision is re-checked UNDER the lock so only the first waiter
# actually rebuilds.
#
# NE-FIX-GOVERN-EVAL (C2/C12): the C12 redaction path is now AIR_GAP fail-closed.
# ``ensure_env_at`` passes an ``_air_gap_check`` closure as ``ensure_sidecar_venv``'s
# ``pre_lock_check`` hook, so a missing/stale venv under AIR_GAP raises a typed
# ``AirGapViolationError`` BEFORE any mkdir/lock/rmtree/uv subprocess (closing the
# runtime ``uv pip install`` egress hole NE-FIX-EGRESS-C2 flagged here); an
# already-current venv is untouched by the fast-path (a silent no-op).
_ENV_LOCK_SUFFIX = ".lock"
_ENV_LOCK_TIMEOUT_S = 1800

_ENV_DIR_OVERRIDE_VAR = "NE_GOVERN_PRESIDIO_ENV_DIR"
_VENV_DIR_OVERRIDE_VAR = "NE_GOVERN_PRESIDIO_VENV_DIR"


def _find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")


def _resolve_env_dirs(
    *,
    root: Path | None,
    presidio_env_dir: Path | None,
    venv_dir: Path | None,
) -> tuple[Path, Path]:
    """Resolve the (presidio_env payload dir, venv working dir) pair.

    Priority: explicit constructor args > explicit ``root`` (dev-checkout layout)
    > ``NE_GOVERN_PRESIDIO_ENV_DIR``/``NE_GOVERN_PRESIDIO_VENV_DIR`` env vars >
    monorepo dev-checkout auto-discovery via ``uv.lock`` walk-up.

    Venv-dir precedence (NE-SVENV): explicit ``venv_dir=`` kwarg > explicit
    ``root=`` kwarg > ``NE_GOVERN_PRESIDIO_VENV_DIR`` >
    ``NE_SIDECAR_VENV_ROOT/.presidio-venv`` > in-package sibling
    ``presidio_env_dir.parent/.presidio-venv``. Only the FINAL fallback routes
    through :func:`ne.envbuild.sidecar_venv_dir` (the specific per-sidecar
    overrides beat the generic writable-root var and keep every existing
    resolution test passing unedited).
    """
    if presidio_env_dir is None:
        if root is not None:
            presidio_env_dir = root / _PRESIDIO_ENV_DIR
        elif env_override := os.environ.get(_ENV_DIR_OVERRIDE_VAR):
            presidio_env_dir = Path(env_override)
        else:
            try:
                presidio_env_dir = _find_repo_root(Path(__file__)) / _PRESIDIO_ENV_DIR
            except RuntimeError as exc:
                raise RuntimeError(
                    "PresidioRedactor could not locate the isolated presidio env "
                    "(no uv.lock found above this file, so this does not look like a "
                    "monorepo dev checkout). For installed/production deployments, pass "
                    "presidio_env_dir=/venv_dir= to PresidioRedactor(...), or set the "
                    f"{_ENV_DIR_OVERRIDE_VAR}/{_VENV_DIR_OVERRIDE_VAR} environment "
                    "variables to point at a vendored copy of presidio_env/ (requirements.txt "
                    f"+ redact_text.py) and a writable venv directory. Original error: {exc}"
                ) from exc

    if venv_dir is None:
        if root is not None:
            venv_dir = root / _VENV_DIR
        elif env_override := os.environ.get(_VENV_DIR_OVERRIDE_VAR):
            venv_dir = Path(env_override)
        else:
            venv_dir = sidecar_venv_dir(presidio_env_dir, ".presidio-venv")

    return presidio_env_dir, venv_dir


def _venv_python_at(venv_dir: Path) -> Path:
    return venv_python(venv_dir)


def _marker_path_at(venv_dir: Path) -> Path:
    return marker_path(venv_dir)


def _env_lock_path_at(venv_dir: Path) -> Path:
    """Path of the rebuild lock file — a SIBLING of ``venv_dir`` (see ne.envbuild).

    Must live OUTSIDE ``venv_dir`` (never inside it) so the lock file survives
    ``shutil.rmtree(venv_dir)`` during a rebuild.
    """
    return env_lock_path(venv_dir)


def _requirements_sha256_at(presidio_env_dir: Path) -> str:
    return requirements_sha256(presidio_env_dir)


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
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


def ensure_env_at(presidio_env_dir: Path, venv_dir: Path) -> None:
    """Create/refresh the isolated hash-pinned presidio venv if needed.

    Rebuilt when missing or when the stored marker of ``requirements.txt`` no
    longer matches the committed file. Installation is ``--require-hashes
    --no-deps`` (SPEC §2 C4/C11; see inline note). The atomic-marker + under-lock
    authoritative re-check flow lives in :func:`ne.envbuild.ensure_sidecar_venv`,
    shared with the docling and codegen builders (NE-CKC8).

    C2 AIR_GAP (NE-FIX-GOVERN-EVAL): the C12 redaction path is fail-closed. An
    already-current venv is a silent no-op (the unlocked fast-path); a missing/stale
    venv under AIR_GAP raises a typed :class:`~ne.net.airgap.AirGapViolationError`
    via the ``pre_lock_check`` hook BEFORE any lock/mkdir/rmtree/uv subprocess — the
    sanctioned path is a pre-built venv (closing the gap NE-FIX-EGRESS-C2 flagged).
    """
    # --no-deps (NE-CKC9): requirements.txt is the COMPLETE compiled closure (uv pip
    # compile --generate-hashes --override overrides.txt), so nothing needs resolving at
    # install time. Without it, uv's hash-mode installer still resolves package metadata
    # and HARD-FAILS on presidio-anonymizer 2.2.363's stale cryptography<47 ceiling, which
    # overrides.txt exists to lift (GHSA-537c-gmf6-5ccf). --override cannot be passed here:
    # --require-hashes rejects the unpinned/unhashed override line. Closure completeness is
    # proven by test_presidio_live.py. The shared env-builder unification landed via
    # ne.envbuild (NE-CKC8); --no-deps is now just this call site's install_args.
    # ``run=_run`` resolves the module global at call time so monkeypatching still works.
    ensure_sidecar_venv(
        env_dir=presidio_env_dir,
        venv_dir=venv_dir,
        install_args=("--no-deps",),
        pre_lock_check=_air_gap_check(venv_dir, "the isolated presidio venv"),
        lock_timeout_s=_ENV_LOCK_TIMEOUT_S,
        name="the isolated presidio venv",
        run=_run,
    )


class PresidioRedactor:
    """PII-redaction backend over Presidio, isolated in a dedicated venv.

    Implements the frozen :class:`ne.contracts.governance.Redactor` Protocol.
    See the module docstring for the env-payload/venv resolution priority.

    NE-162 boot-safety: ``ne-api`` wires ``create_redactor`` unconditionally at
    the composition root (``_default_gateway``), and a production/air-gap
    install has no ``uv.lock`` to walk up to — construction must therefore
    NEVER raise. Resolution of the env-payload/venv dirs (including the
    ``uv.lock`` repo-root walk-up and the ``NE_GOVERN_PRESIDIO_*`` env var
    reads) is deferred to the FIRST ``redact()`` call instead of happening in
    ``__init__``; a resolution failure at that point fails closed with a
    :class:`~ne.govern.errors.RedactionError` (unredacted text is never
    processed or returned). The resolution PRIORITY itself is unchanged — only
    the TIMING moved.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        presidio_env_dir: Path | None = None,
        venv_dir: Path | None = None,
    ) -> None:
        self._root = root
        self._presidio_env_dir_arg = presidio_env_dir
        self._venv_dir_arg = venv_dir
        self._dirs: tuple[Path, Path] | None = None

    def _resolved_dirs(self) -> tuple[Path, Path]:
        """Resolve (and cache) the (presidio_env_dir, venv_dir) pair.

        Called lazily from :meth:`redact` (never from ``__init__``) so that
        constructing a ``PresidioRedactor`` can never crash at boot in a
        production/air-gap install with no repo checkout. A resolution
        failure here fails closed with :class:`RedactionError` rather than
        propagating the underlying :class:`RuntimeError` — no text is
        processed when this raises.
        """
        dirs = self._dirs
        if dirs is None:
            try:
                dirs = _resolve_env_dirs(
                    root=self._root,
                    presidio_env_dir=self._presidio_env_dir_arg,
                    venv_dir=self._venv_dir_arg,
                )
            except RuntimeError as exc:
                raise RedactionError(
                    f"presidio env resolution failed (fail-closed, no text was processed): {exc}"
                ) from exc
            self._dirs = dirs
        return dirs

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        presidio_env_dir, venv_dir = self._resolved_dirs()
        ensure_env_at(presidio_env_dir, venv_dir)
        python = _venv_python_at(venv_dir)
        request: dict[str, object] = {"text": text}
        if categories is not None:
            request["categories"] = list(categories)

        with tempfile.TemporaryDirectory() as tmp:
            in_path = Path(tmp) / "input.json"
            out_path = Path(tmp) / "output.json"
            in_path.write_text(json.dumps(request), encoding="utf-8")
            result = subprocess.run(
                [
                    str(python),
                    str(presidio_env_dir / "redact_text.py"),
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
                raise RedactionError(
                    f"presidio redact subprocess failed (exit {result.returncode}):\n{stderr_tail}"
                )
            payload = json.loads(out_path.read_text(encoding="utf-8"))

        return RedactionResult(
            text=payload["text"],
            redactions=tuple(payload.get("redactions", ())),
        )
