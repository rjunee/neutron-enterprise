# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Platform-side driver for the Docling PARSE stage (NE-020).

This module imports NOTHING from docling. docling (and its torch / proprietary
NVIDIA-CUDA-runtime transitive closure) is confined to an ISOLATED,
hash-pinned virtualenv (default ``packages/ne-brain/.parser-venv``) so it
never enters the platform lock/SBOM (SPEC §2 C4) — the same isolation pattern
``ne.schema.codegen`` uses for linkml. Parsing therefore runs the in-venv
``parser_env/parse_doc.py`` as a subprocess; this module only manages that
venv and adapts subprocess I/O to the :class:`~ne.brain.ingest.contracts.ParsedDocument`
contract.

Locating the parser env payload (``parser_env/requirements.txt`` +
``parse_doc.py``) and the venv working directory: the default resolution
walks up from this file for a monorepo dev checkout (a directory containing
``uv.lock``), which is what every current deployment shape uses (CI, `uv run`,
and the OCI-image build that copies the whole workspace — SPEC §2 C9). That
walk-up is NOT available once ``ne-brain`` is installed as a standalone wheel
outside a monorepo checkout (e.g. a slimmed production image built from
published wheels rather than a workspace copy). For that shape, callers MUST
supply an explicit override — either constructor kwargs
(``parser_env_dir=``/``venv_dir=``) or the ``NE_PARSER_ENV_DIR`` /
``NE_PARSER_VENV_DIR`` environment variables — pointing at a directory where
the ``parser_env/`` payload has been vendored in (e.g. via a Dockerfile
``COPY packages/ne-brain/parser_env /opt/ne/parser_env``) and a writable
venv location. See ``README.md`` for the exact deployment recipe.

AIR_GAP posture (C2)
--------------------
``ensure_env_at`` provisions the venv with ``uv pip install`` (pypi.org /
files.pythonhosted.org egress) on a cold/stale env. Under AIR_GAP that runtime
egress is forbidden: a CURRENT venv is a silent no-op, but an ABSENT or STALE one
fails fast with a typed :class:`~ne.brain.ingest.contracts.AirGapViolationError`
BEFORE any lock/rmtree/subprocess — the sanctioned path is a pre-built venv
(run ``ensure_env_at`` at image-build/pre-staging time, or point
``NE_PARSER_VENV_DIR`` at a pre-built venv). Separately, any NON-AIR_GAP
env-provisioning failure (a failed ``uv`` subprocess, a lock timeout, an OSError)
is wrapped as :class:`~ne.brain.ingest.contracts.ParserEnvironmentError` so the
spine treats it as FAILED/retryable (infra-caused) rather than QUARANTINE
(document-caused).

Public API:
  * ``DoclingParser``               — implements ``DocumentParser``.
  * ``parsed_document_from_json``   — thin ``ParsedDocument`` JSON loader,
    used by tests and by NE-026 to replay a recorded/committed fixture
    without the docling runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import ValidationError

from ne.brain.ingest.contracts import (
    ParsedDocument,
    ParserConversionError,
    ParserEnvironmentError,
    ParserOutputError,
    ParserResidueError,
)
from ne.brain.sovereignty import AirGapViolationError, air_gap_engaged
from ne.envbuild import (
    ensure_sidecar_venv,
    env_lock_path,
    marker_path,
    requirements_sha256,
    sidecar_venv_dir,
    venv_python,
)

_PKG = Path("packages") / "ne-brain"
_PARSER_ENV_DIR = _PKG / "parser_env"
_VENV_DIR = _PKG / ".parser-venv"
_ENV_MARKER = ".requirements.sha256"

# NE-020 P2a / NE-CKC8: concurrency guard around the venv (re)build. Two
# concurrent parse() calls could both enter the rebuild path — one removing/
# recreating venv_dir while another installs into it — causing intermittent
# failures. The probe/lock/rebuild flow now lives in ``ne.envbuild`` (shared with
# the presidio and codegen builders): a cross-process/thread file lock (SIBLING
# of venv_dir so it survives the rmtree of venv_dir) serialises the rebuild, the
# marker is written atomically (tmp + os.replace) so a partial marker is never
# observed, and the AUTHORITATIVE up-to-date decision is re-checked UNDER the
# lock so only the first waiter actually rebuilds.
_ENV_LOCK_SUFFIX = ".lock"
_ENV_LOCK_TIMEOUT_S = 1800

# Overrides for deployments where ``ne-brain`` runs outside a monorepo dev
# checkout (no reachable ``uv.lock``) — e.g. an installed wheel in a
# production image. See module docstring.
_ENV_DIR_OVERRIDE_VAR = "NE_PARSER_ENV_DIR"
_VENV_DIR_OVERRIDE_VAR = "NE_PARSER_VENV_DIR"

# Fixed, content-free protocol emitted by ``parser_env/parse_doc.py``. Unknown
# positive codes and signal exits fail closed as infrastructure/runtime errors;
# they must never silently become a document quarantine.
_CHILD_EXIT_ENVIRONMENT = 70
_CHILD_EXIT_CONVERSION = 71
_CHILD_EXIT_OUTPUT = 72
_CHILD_EXIT_DEPENDENCY = 73
_CHILD_EXIT_POLICY = 74
_CHILD_EXIT_CAPACITY = 75
_CHILD_EXIT_AVAILABILITY = 76
_CHILD_EXIT_TIMEOUT = 77
_CHILD_EXIT_INFERENCE = 78
_CHILD_EXIT_INTERNAL = 79
_CHILD_EXIT_TRANSPORT = 80


class ParserDependencyError(ParserEnvironmentError):
    """A required isolated-parser dependency or model artifact is unavailable."""


class ParserPolicyError(ParserEnvironmentError):
    """The isolated parser refused execution for a runtime policy reason."""


class ParserCapacityError(ParserEnvironmentError):
    """The isolated parser could not admit work within capacity."""


class ParserAvailabilityError(ParserEnvironmentError):
    """A parser runtime source or target was unavailable."""


class ParserTimeoutError(ParserEnvironmentError):
    """The isolated parser exceeded its runtime deadline."""


class ParserInferenceError(ParserEnvironmentError):
    """The isolated parser reported an inference-runtime failure."""


class ParserInternalError(ParserEnvironmentError):
    """The isolated parser reported an internal or unclassified failure."""


class ParserTransportError(ParserEnvironmentError):
    """The isolated parser could not read or write its private transport."""


_CHILD_RUNTIME_ERRORS: dict[int, tuple[type[ParserEnvironmentError], str]] = {
    _CHILD_EXIT_ENVIRONMENT: (ParserEnvironmentError, "parser child runtime failed"),
    _CHILD_EXIT_DEPENDENCY: (ParserDependencyError, "parser dependency unavailable"),
    _CHILD_EXIT_POLICY: (ParserPolicyError, "parser runtime policy refused execution"),
    _CHILD_EXIT_CAPACITY: (ParserCapacityError, "parser runtime capacity unavailable"),
    _CHILD_EXIT_AVAILABILITY: (ParserAvailabilityError, "parser runtime unavailable"),
    _CHILD_EXIT_TIMEOUT: (ParserTimeoutError, "parser runtime timed out"),
    _CHILD_EXIT_INFERENCE: (ParserInferenceError, "parser inference failed"),
    _CHILD_EXIT_INTERNAL: (ParserInternalError, "parser internal runtime failed"),
    _CHILD_EXIT_TRANSPORT: (ParserTransportError, "parser private transport failed"),
}


class _TemporaryRoot(NamedTuple):
    path: Path
    root_fd: int
    parent_fd: int
    device: int
    inode: int
    owner: int


_ROOT_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _capture_temporary_root(path: Path) -> _TemporaryRoot:
    """Open and bind an owner-only real directory before any document bytes land."""
    with contextlib.ExitStack() as close_on_error:
        parent_fd = os.open(path.parent, _ROOT_OPEN_FLAGS)
        close_on_error.callback(os.close, parent_fd)
        root_fd = os.open(path.name, _ROOT_OPEN_FLAGS, dir_fd=parent_fd)
        close_on_error.callback(os.close, root_fd)
        path_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        fd_stat = os.fstat(root_fd)
        identity = (fd_stat.st_dev, fd_stat.st_ino, fd_stat.st_uid)
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or not stat.S_ISDIR(fd_stat.st_mode)
            or stat.S_IMODE(fd_stat.st_mode) != 0o700
            or fd_stat.st_uid != os.geteuid()
            or identity != (path_stat.st_dev, path_stat.st_ino, path_stat.st_uid)
        ):
            raise OSError("unsafe parser temporary root")
        close_on_error.pop_all()
        return _TemporaryRoot(path, root_fd, parent_fd, *identity)


def _open_new_file_at(root: _TemporaryRoot, name: str) -> int:
    """Create one private regular file through the retained root descriptor."""
    if _temporary_root_state(root) != "same":
        raise ParserResidueError("parser temporary root identity changed")
    file_fd: int | None = None
    try:
        file_fd = os.open(name, _FILE_OPEN_FLAGS, 0o600, dir_fd=root.root_fd)
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != root.owner
            or opened.st_dev != root.device
            or _temporary_root_state(root) != "same"
        ):
            raise ParserResidueError("parser temporary root identity changed")
        return file_fd
    except Exception:
        if file_fd is not None:
            with contextlib.suppress(OSError):
                os.close(file_fd)
        raise


def _read_all_at_start(file_fd: int) -> bytes:
    os.lseek(file_fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(file_fd, 64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _temporary_root_state(root: _TemporaryRoot) -> Literal["same", "missing", "changed"]:
    expected = (root.device, root.inode, root.owner)
    try:
        current = os.stat(root.path.name, dir_fd=root.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            retained = os.fstat(root.root_fd)
        except OSError:
            return "changed"
        if (
            stat.S_ISDIR(retained.st_mode)
            and stat.S_IMODE(retained.st_mode) == 0o700
            and (retained.st_dev, retained.st_ino, retained.st_uid) == expected
        ):
            return "missing"
        return "changed"
    except OSError:
        return "changed"
    try:
        retained = os.fstat(root.root_fd)
    except OSError:
        return "changed"
    if (
        stat.S_ISDIR(current.st_mode)
        and stat.S_ISDIR(retained.st_mode)
        and stat.S_IMODE(current.st_mode) == 0o700
        and stat.S_IMODE(retained.st_mode) == 0o700
        and (current.st_dev, current.st_ino, current.st_uid) == expected
        and (retained.st_dev, retained.st_ino, retained.st_uid) == expected
    ):
        return "same"
    return "changed"


def _same_child(current: os.stat_result, expected: os.stat_result) -> bool:
    return (
        current.st_dev,
        current.st_ino,
        current.st_uid,
        stat.S_IFMT(current.st_mode),
    ) == (
        expected.st_dev,
        expected.st_ino,
        expected.st_uid,
        stat.S_IFMT(expected.st_mode),
    )


def _clean_directory_fd(
    root: _TemporaryRoot,
    directory_fd: int,
    *,
    expected_root_state: Literal["same", "missing"],
) -> tuple[bool, bool]:
    """Remove one anchored directory tree without following any path symlink."""
    had_issue = False
    if _temporary_root_state(root) != expected_root_state:
        return False, True
    if directory_fd != root.root_fd:
        with contextlib.suppress(OSError):
            os.fchmod(directory_fd, 0o700)
    try:
        names = os.listdir(directory_fd)
    except OSError:
        return False, True
    for name in names:
        if _temporary_root_state(root) != expected_root_state:
            return False, True
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            had_issue = True
            continue
        if before.st_uid != root.owner:
            had_issue = True
            continue
        if stat.S_ISDIR(before.st_mode):
            if before.st_dev != root.device:
                had_issue = True
                continue
            child_fd: int | None = None
            try:
                child_fd = os.open(name, _ROOT_OPEN_FLAGS, dir_fd=directory_fd)
                if not _same_child(os.fstat(child_fd), before):
                    had_issue = True
                    continue
                child_clean, child_issue = _clean_directory_fd(
                    root,
                    child_fd,
                    expected_root_state=expected_root_state,
                )
                had_issue |= child_issue
            except OSError:
                had_issue = True
                child_clean = False
            finally:
                if child_fd is not None:
                    os.close(child_fd)
            if child_clean and _temporary_root_state(root) == expected_root_state:
                try:
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (
                        _same_child(current, before)
                        and _temporary_root_state(root) == expected_root_state
                    ):
                        os.rmdir(name, dir_fd=directory_fd)
                    else:
                        had_issue = True
                except FileNotFoundError:
                    pass
                except OSError:
                    had_issue = True
        else:
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    _same_child(current, before)
                    and _temporary_root_state(root) == expected_root_state
                ):
                    os.unlink(name, dir_fd=directory_fd)
                else:
                    had_issue = True
            except FileNotFoundError:
                pass
            except OSError:
                had_issue = True
    if _temporary_root_state(root) != expected_root_state:
        return False, True
    try:
        return not os.listdir(directory_fd), had_issue
    except OSError:
        return False, True


def _cleanup_temporary_directory(root: _TemporaryRoot) -> tuple[bool, bool]:
    """Bounded fd-anchored cleanup returning ``(had_issue, residue_remains)``."""
    had_issue = False
    try:
        initial_state = _temporary_root_state(root)
        if initial_state == "changed":
            return True, True
        for _attempt in range(2):
            clean, attempt_issue = _clean_directory_fd(
                root,
                root.root_fd,
                expected_root_state=initial_state,
            )
            had_issue |= attempt_issue
            state = _temporary_root_state(root)
            if state != initial_state:
                return True, True
            if initial_state == "missing":
                if clean:
                    return True, True
                continue
            if clean:
                try:
                    current = os.stat(root.path.name, dir_fd=root.parent_fd, follow_symlinks=False)
                    if stat.S_ISDIR(current.st_mode) and (
                        current.st_dev,
                        current.st_ino,
                        current.st_uid,
                    ) == (root.device, root.inode, root.owner):
                        os.rmdir(root.path.name, dir_fd=root.parent_fd)
                    else:
                        return True, True
                except FileNotFoundError:
                    return True, True
                except OSError:
                    had_issue = True
                else:
                    return had_issue, False
        return True, True
    finally:
        with contextlib.suppress(OSError):
            os.close(root.root_fd)
        with contextlib.suppress(OSError):
            os.close(root.parent_fd)


def _find_repo_root(start: Path) -> Path:
    """Walk up from ``start`` until a directory containing ``uv.lock`` is found."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "uv.lock").is_file():
            return candidate
    raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")


def _resolve_env_dirs(
    *,
    root: Path | None,
    parser_env_dir: Path | None,
    venv_dir: Path | None,
) -> tuple[Path, Path]:
    """Resolve the (parser_env payload dir, venv working dir) pair.

    Priority: explicit constructor args > explicit ``root`` (dev-checkout
    layout) > ``NE_PARSER_ENV_DIR``/``NE_PARSER_VENV_DIR`` env vars >
    monorepo dev-checkout auto-discovery via ``uv.lock`` walk-up.

    Venv-dir precedence (NE-SVENV): explicit ``venv_dir=`` kwarg > explicit
    ``root=`` kwarg > ``NE_PARSER_VENV_DIR`` > ``NE_SIDECAR_VENV_ROOT/.parser-venv``
    > in-package sibling ``parser_env_dir.parent/.parser-venv``. Only the FINAL
    fallback routes through :func:`ne.envbuild.sidecar_venv_dir` (the specific
    per-sidecar ``root=``/``NE_PARSER_VENV_DIR`` overrides beat the generic
    writable-root var and keep every existing resolution test passing unedited).
    """
    if parser_env_dir is None:
        if root is not None:
            parser_env_dir = root / _PARSER_ENV_DIR
        elif env_override := os.environ.get(_ENV_DIR_OVERRIDE_VAR):
            parser_env_dir = Path(env_override)
        else:
            try:
                parser_env_dir = _find_repo_root(Path(__file__)) / _PARSER_ENV_DIR
            except RuntimeError as exc:
                raise RuntimeError(
                    "DoclingParser could not locate the isolated parser env "
                    "(no uv.lock found above this file, so this does not look like a "
                    "monorepo dev checkout). For installed/production deployments, "
                    "pass parser_env_dir=/venv_dir= to DoclingParser(...), or set the "
                    f"{_ENV_DIR_OVERRIDE_VAR}/{_VENV_DIR_OVERRIDE_VAR} environment "
                    "variables to point at a vendored copy of parser_env/ (requirements.txt "
                    f"+ parse_doc.py) and a writable venv directory. Original error: {exc}"
                ) from exc

    if venv_dir is None:
        if root is not None:
            venv_dir = root / _VENV_DIR
        elif env_override := os.environ.get(_VENV_DIR_OVERRIDE_VAR):
            venv_dir = Path(env_override)
        else:
            venv_dir = sidecar_venv_dir(parser_env_dir, ".parser-venv")

    return parser_env_dir, venv_dir


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


def _requirements_sha256_at(parser_env_dir: Path) -> str:
    return requirements_sha256(parser_env_dir)


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def ensure_env_at(parser_env_dir: Path, venv_dir: Path) -> None:
    """Create/refresh the isolated hash-pinned parser venv if needed.

    The venv is (re)built when it is missing or when a stored marker of
    requirements.txt no longer matches the committed file. Installation is
    ``--require-hashes`` (SPEC §2 C4/C11). The atomic-marker + under-lock
    authoritative re-check flow lives in :func:`ne.envbuild.ensure_sidecar_venv`,
    shared with the presidio and codegen builders (NE-CKC8).

    C2 AIR_GAP: an already-current venv is a silent no-op (the unlocked
    fast-path); an absent/stale venv fails typed+closed via ``_air_gap_check``,
    passed as the ``pre_lock_check`` hook so it fires BEFORE any
    lock/mkdir/rmtree/subprocess — the sanctioned path is a pre-built venv.
    """

    def _air_gap_check() -> None:
        # Reaching this hook means the unlocked fast-path failed: the venv is
        # absent or stale, which would trigger a `uv pip install` egress to
        # pypi.org/files.pythonhosted.org from inside the customer VPC. That is
        # forbidden under AIR_GAP — fail typed+closed before any build side effect.
        if air_gap_engaged():
            raise AirGapViolationError(
                f"AIR_GAP is engaged and the isolated parser venv at {venv_dir} is missing "
                "or stale; runtime uv pip install egress (pypi.org/files.pythonhosted.org) "
                "is forbidden — pre-build the venv (run ensure_env_at at image "
                "build/pre-staging time, or point NE_PARSER_VENV_DIR at a pre-built venv)."
            )

    # ``run=_run`` resolves the module global at call time, so tests that
    # monkeypatch ``docling_parser._run`` still take effect.
    ensure_sidecar_venv(
        env_dir=parser_env_dir,
        venv_dir=venv_dir,
        install_args=(),
        pre_lock_check=_air_gap_check,
        lock_timeout_s=_ENV_LOCK_TIMEOUT_S,
        name="the isolated docling parser venv",
        run=_run,
    )


def ensure_env(root: Path) -> None:
    """Back-compat wrapper: build/refresh the venv for the standard dev-checkout layout.

    Equivalent to ``ensure_env_at(root / "packages/ne-brain/parser_env", root /
    "packages/ne-brain/.parser-venv")``. Prefer ``ensure_env_at`` for non-monorepo
    (installed/production) layouts.
    """
    ensure_env_at(root / _PARSER_ENV_DIR, root / _VENV_DIR)


def parsed_document_from_json(data: str | bytes) -> ParsedDocument:
    """Load a :class:`ParsedDocument` from ``ne.parsed_document/1`` JSON.

    Thin ``model_validate_json`` wrapper used by tests and by NE-026 to replay
    a recorded/committed fixture without the docling runtime.
    """
    try:
        return ParsedDocument.model_validate_json(data)
    except ValidationError:
        raise ParserOutputError("parser returned invalid document output") from None


def _parse_sync(
    parser_env_dir: Path,
    venv_dir: Path,
    path: Path,
    document_id: str,
    *,
    input_fd: int | None = None,
    source_name: str | None = None,
) -> ParsedDocument:
    # Env provisioning is INFRA-caused: AIR_GAP is a policy fail-closed (propagate
    # as-is), and any other provisioning failure (uv subprocess CalledProcessError,
    # the lock-timeout RuntimeError wrap, OSError) becomes ParserEnvironmentError
    # so the spine records it FAILED/retryable — NOT quarantine (which is reserved
    # for document-caused failures, i.e. the parse subprocess rejecting the file).
    try:
        ensure_env_at(parser_env_dir, venv_dir)
    except AirGapViolationError:
        raise
    except Exception as exc:
        raise ParserEnvironmentError(f"parser venv provisioning failed: {exc}") from exc
    temporary: _TemporaryRoot | None = None
    output_fd: int | None = None
    try:
        try:
            python = _venv_python_at(venv_dir)
            temporary_path = Path(tempfile.mkdtemp(prefix="ne-docling-output-"))
            try:
                temporary = _capture_temporary_root(temporary_path)
            except OSError:
                raise ParserResidueError("parser temporary root identity invalid") from None
            output_fd = _open_new_file_at(temporary, "parsed.json")
            if input_fd is None:
                input_args = ["--input", str(path)]
                inherited_fds = (output_fd,)
            else:
                if source_name is None:
                    raise ParserEnvironmentError("parser fd source name missing")
                os.lseek(input_fd, 0, os.SEEK_SET)
                input_args = ["--input-fd", str(input_fd), "--source-name", source_name]
                inherited_fds = (input_fd, output_fd)
            result = subprocess.run(
                [
                    str(python),
                    str(parser_env_dir / "parse_doc.py"),
                    *input_args,
                    "--output-fd",
                    str(output_fd),
                    "--document-id",
                    document_id,
                ],
                check=False,
                capture_output=True,
                text=True,
                pass_fds=inherited_fds,
            )
            if result.returncode == _CHILD_EXIT_CONVERSION:
                raise ParserConversionError("docling rejected the document")
            if result.returncode == _CHILD_EXIT_OUTPUT:
                raise ParserOutputError("parser returned invalid document output")
            if result.returncode in _CHILD_RUNTIME_ERRORS:
                error_type, message = _CHILD_RUNTIME_ERRORS[result.returncode]
                raise error_type(message)
            if result.returncode != 0:
                raise ParserEnvironmentError("parser child runtime failed")
            if _temporary_root_state(temporary) != "same":
                raise ParserResidueError("parser temporary root identity changed")
            raw_output = _read_all_at_start(output_fd)
        except OSError as exc:
            raise ParserEnvironmentError("parser runtime environment failed") from exc
        return parsed_document_from_json(raw_output)
    finally:
        if output_fd is not None:
            with contextlib.suppress(OSError):
                os.close(output_fd)
        if temporary is not None:
            active_error = sys.exc_info()[1]
            cleanup_issue, residue_remains = _cleanup_temporary_directory(temporary)
            if cleanup_issue:
                if residue_remains:
                    raise ParserResidueError("parser temporary data cleanup incomplete") from None
                if active_error is None:
                    raise ParserEnvironmentError("parser output cleanup failed") from None
                active_error.add_note(
                    "parser output cleanup incomplete"
                    if residue_remains
                    else "parser output cleanup recovered after an error"
                )


def _duplicate_regular_input_fd(file_fd: int) -> int:
    """Return one close-on-exec descriptor bound to the caller's regular file."""
    duplicate_fd: int | None = None
    try:
        source = os.fstat(file_fd)
        duplicate_fd = fcntl.fcntl(file_fd, fcntl.F_DUPFD_CLOEXEC, 0)
        duplicate = os.fstat(duplicate_fd)
        inheritable = os.get_inheritable(duplicate_fd)
    except OSError:
        if duplicate_fd is not None:
            with contextlib.suppress(OSError):
                os.close(duplicate_fd)
        raise ParserEnvironmentError("parser input descriptor duplication failed") from None
    if (
        not stat.S_ISREG(source.st_mode)
        or not stat.S_ISREG(duplicate.st_mode)
        or (duplicate.st_dev, duplicate.st_ino) != (source.st_dev, source.st_ino)
        or inheritable
    ):
        with contextlib.suppress(OSError):
            os.close(duplicate_fd)
        raise ParserEnvironmentError("parser input descriptor duplication failed")
    return duplicate_fd


def _close_owned_input_fd(file_fd: int) -> None:
    with contextlib.suppress(OSError):
        os.close(file_fd)


def _parse_owned_fd_sync(
    parser_env_dir: Path,
    venv_dir: Path,
    source_name: str,
    document_id: str,
    owned_fd: int,
) -> ParsedDocument:
    try:
        return _parse_sync(
            parser_env_dir,
            venv_dir,
            Path(source_name),
            document_id,
            input_fd=owned_fd,
            source_name=source_name,
        )
    finally:
        _close_owned_input_fd(owned_fd)


def _submit_owned_fd_parse(
    loop: asyncio.AbstractEventLoop,
    parser_env_dir: Path,
    venv_dir: Path,
    source_name: str,
    document_id: str,
    owned_fd: int,
) -> asyncio.Future[ParsedDocument]:
    return loop.run_in_executor(
        None,
        _parse_owned_fd_sync,
        parser_env_dir,
        venv_dir,
        source_name,
        document_id,
        owned_fd,
    )


def _consume_background_parse(future: asyncio.Future[ParsedDocument]) -> None:
    """Retrieve a cancelled caller's eventual worker result without exposing it."""
    if not future.cancelled():
        with contextlib.suppress(Exception):
            future.exception()


class DoclingParser:
    """PARSE stage adapter over docling, isolated in a dedicated parser venv.

    Implements ``ne.brain.ingest.contracts.DocumentParser``.

    Resolution of the parser env payload + venv working directory (see the
    module docstring for the full priority order): pass ``root`` for the
    standard monorepo dev-checkout layout (``packages/ne-brain/parser_env`` +
    ``packages/ne-brain/.parser-venv`` under ``root``); pass
    ``parser_env_dir``/``venv_dir`` directly for an installed/production
    deployment where the payload has been vendored to an arbitrary path; or
    rely on the ``NE_PARSER_ENV_DIR``/``NE_PARSER_VENV_DIR`` environment
    variables / monorepo auto-discovery when none of the above are given.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        parser_env_dir: Path | None = None,
        venv_dir: Path | None = None,
    ) -> None:
        self._parser_env_dir, self._venv_dir = _resolve_env_dirs(
            root=root, parser_env_dir=parser_env_dir, venv_dir=venv_dir
        )

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        return await asyncio.to_thread(
            _parse_sync, self._parser_env_dir, self._venv_dir, path, document_id
        )

    async def parse_fd(
        self,
        file_fd: int,
        *,
        document_id: str,
        source_name: str,
    ) -> ParsedDocument:
        owned_fd = _duplicate_regular_input_fd(file_fd)
        try:
            future = _submit_owned_fd_parse(
                asyncio.get_running_loop(),
                self._parser_env_dir,
                self._venv_dir,
                source_name,
                document_id,
                owned_fd,
            )
        except BaseException:
            _close_owned_input_fd(owned_fd)
            raise
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            future.add_done_callback(_consume_background_parse)
            raise


def main(argv: list[str] | None = None) -> int:
    """Tiny CLI shim: ``python -m ne.brain.ingest.docling_parser INPUT OUTPUT --document-id ID``."""
    parser = argparse.ArgumentParser(description="Parse a document with the isolated docling env.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--document-id", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = _find_repo_root(Path.cwd())
    doc = asyncio.run(DoclingParser(root).parse(args.input, document_id=args.document_id))
    args.output.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
