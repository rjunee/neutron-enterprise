# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ONE consolidated sidecar-venv env-builder (NE-CKC8).

Three platform-side drivers each isolate a heavy dependency closure inside a
hash-pinned virtualenv built with ``uv`` and skip the (re)build when a stored
``.requirements.sha256`` marker still matches the committed ``requirements.txt``:

* ``ne.brain.ingest.docling_parser`` (docling / torch / CUDA),
* ``ne.govern.redaction.presidio_backend`` (presidio / spaCy),
* ``ne.schema.codegen`` (linkml).

Each had (or, for codegen, lacked) its own copy of the probe/lock/rebuild flow.
The old fast-path probe was a TOCTOU: an ``is_file() -> read_text()`` of the
marker performed BEFORE (and, in the docling/presidio twins, again INSIDE) the
lock could observe a marker that a concurrent rebuild was writing mid-flight via
a non-atomic ``write_text``. A partial/short/stale read either produced a false
"up-to-date" skip (a venv left built against the OLD requirements) or a redundant
rebuild. ``ne.schema.codegen`` additionally had NO lock at all — two concurrent
runs raced the ``rmtree``/install.

This leaf fixes it once:

* :func:`write_marker_atomic` writes to a same-directory temp file then
  ``os.replace``\\ s it into place, so a reader can only ever observe a COMPLETE
  old or COMPLETE new marker — never a partial one.
* :func:`read_marker` is FAIL-SAFE: missing / unreadable / a directory / empty
  all return ``None``, and a short/partial hash can never equal the 64-hex want,
  so every degenerate marker state maps to needs-rebuild, never a skip and never
  an unhandled exception.
* :func:`ensure_sidecar_venv` keeps the cheap UNLOCKED double-checked fast-path
  as an advisory optimisation but re-makes the AUTHORITATIVE skip decision UNDER
  the filelock (double-checked locking).

The only third-party dependency is ``filelock`` (already a shared, vetted
direct dependency of ne-brain and ne-govern); everything else is stdlib. The
leaf-ness is enforced by the ``ne-envbuild-leaf`` import-linter contract.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import filelock

# Name of the per-venv marker file recording the sha256 of the requirements.txt
# the venv was built from.
ENV_MARKER = ".requirements.sha256"
# Suffix of the rebuild lock file (a SIBLING of the venv dir — see env_lock_path).
ENV_LOCK_SUFFIX = ".lock"
# Default seconds to wait for the rebuild lock before giving up (a cold build of
# the heaviest sidecar can take many minutes on a slow link).
DEFAULT_LOCK_TIMEOUT_S: float = 1800
# Default interpreter version `uv venv --python` requests.
DEFAULT_PYTHON_VERSION = "3.12"
# Operator-set writable root for hardened/non-root/read-only-rootfs deploys
# (NE-SVENV). When set-and-nonempty, every ML sidecar resolves its venv (and
# lightrag its workdir) to $NE_SIDECAR_VENV_ROOT/<leaf> instead of the in-package
# sibling default, so a runtime (re)build lands on a WRITABLE mount rather than
# the root-owned read-only site-packages tree of a hardened image.
SIDECAR_VENV_ROOT_VAR = "NE_SIDECAR_VENV_ROOT"
# uv's own env var naming the directory it installs MANAGED Python interpreters
# into (`uv venv --python 3.12` downloads one when no current interpreter
# satisfies the request). Operator-set values are respected verbatim; when unset,
# ensure_sidecar_venv passes a default DERIVED FROM THE VENV ROOT to the
# `uv venv` subprocess via an EXPLICIT child environment — never by mutating
# os.environ — see uv_python_install_dir.
UV_PYTHON_INSTALL_DIR_VAR = "UV_PYTHON_INSTALL_DIR"
# Leaf name of the derived default: a SIBLING of the venv dir, so the managed
# interpreter lands on the same operator-chosen writable/executable mount as the
# venv that symlinks to it (they travel together across restarts).
UV_PYTHON_DIR_LEAF = ".uv-python"
# uv's own env var naming the directory it caches downloaded wheels/sdists in
# (unset, uv derives it from $HOME: ``$XDG_CACHE_HOME/uv`` else ``~/.cache/uv``).
# Operator-set values are respected verbatim; when unset, ensure_sidecar_venv
# passes a default DERIVED FROM THE VENV ROOT to BOTH uv subprocesses via an
# EXPLICIT child environment — never by mutating os.environ — see uv_cache_dir.
UV_CACHE_DIR_VAR = "UV_CACHE_DIR"
# Leaf name of the derived default: a SIBLING of the venv dir, beside
# .uv-python, so the multi-gigabyte download lands on the same persistent,
# disk-backed mount as the venv it is being installed into.
UV_CACHE_DIR_LEAF = ".uv-cache"


class UvRunner(Protocol):
    """The ``run`` seam of :func:`ensure_sidecar_venv`.

    ``cmd`` is the full ``uv`` argv. ``env`` — when not ``None`` — is the
    COMPLETE child environment the subprocess must receive (``subprocess.run``'s
    ``env=`` semantics); ``None`` means inherit the ambient process environment.

    The builder passes ``env`` to BOTH of its subprocesses, and in both cases the
    value is ``os.environ`` PLUS the derived uv directories and nothing else, so
    everything either call inherited from the ambient environment before still
    reaches it:

    * ``uv venv`` gets ``UV_PYTHON_INSTALL_DIR`` (uv's only knob for the
      managed-interpreter directory — there is no CLI flag) and ``UV_CACHE_DIR``;
    * ``uv pip install`` gets ``UV_CACHE_DIR`` only — it addresses the
      interpreter by absolute path, so an install-dir override would only change
      unrelated uv discovery behavior.

    Never by mutating ``os.environ``: a process-global mutation raced concurrent
    builds (another build's operator-override read mistook the temporary value
    for an operator setting) and leaked into every unrelated subprocess spawned
    meanwhile.
    """

    def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None: ...


def venv_python(venv_dir: Path) -> Path:
    """Path to the venv's interpreter (``venv_dir/bin/python``)."""
    return venv_dir / "bin" / "python"


def marker_path(venv_dir: Path) -> Path:
    """Path to the venv's ``.requirements.sha256`` marker file."""
    return venv_dir / ENV_MARKER


def env_lock_path(venv_dir: Path) -> Path:
    """Path of the rebuild lock file — a SIBLING of ``venv_dir``.

    Must live OUTSIDE ``venv_dir`` (never inside it) so the lock file survives
    ``shutil.rmtree(venv_dir)`` during a rebuild.
    """
    return venv_dir.with_name(venv_dir.name + ENV_LOCK_SUFFIX)


def sidecar_venv_dir(env_dir: Path, leaf: str) -> Path:
    """Resolve a sidecar's venv dir to a WRITABLE location, default unchanged.

    Returns ``$NE_SIDECAR_VENV_ROOT/<leaf>`` when the ``NE_SIDECAR_VENV_ROOT``
    env var is set-and-nonempty, else the historical in-package sibling
    ``env_dir.parent / leaf``. ``env_dir`` is a sidecar's in-package payload dir
    (e.g. ``packages/ne-brain-graph/splink_env``) and ``leaf`` is the venv dir
    name (e.g. ``".splink-venv"``).

    Semantics:

    * The env var is read at CALL time (never import time), so tests
      ``monkeypatch.setenv``/``delenv`` it and hardened deploys set it per-run.
    * DEFAULT (env unset OR empty string — an empty value counts as unset) is
      BYTE-IDENTICAL to the historical in-package sibling ``env_dir.parent/leaf``;
      no existing behavior changes when the var is not set.
    * PRE-STAGING↔RUNTIME CONTRACT: the operator MUST pre-build the venv at the
      SAME resolved path the runtime will compute. If the runtime resolves the
      venv elsewhere than where it was pre-built, an AIR_GAP deploy fail-closes on
      the "missing" venv (C2, ``AirGapViolationError`` before any build) and a
      non-air-gap deploy attempts an unwanted ``uv pip install`` rebuild/egress.
    * :func:`env_lock_path` derived from this result stays a SIBLING INSIDE the
      writable root (``$ROOT/<leaf>.lock``), so it survives ``rmtree(venv_dir)``.
    * :func:`ensure_sidecar_venv` mkdirs ``venv_dir.parent``, so the writable root
      itself need not pre-exist for a non-air-gap build.

    This is the reason the writable-root indirection exists: the ne-app image
    installs into a root-owned, read-only site-packages tree and runs as a
    non-root ``USER``, so the in-package default is unwritable at runtime.
    """
    root = os.environ.get(SIDECAR_VENV_ROOT_VAR)
    if root:
        return Path(root) / leaf
    return env_dir.parent / leaf


def requirements_sha256(env_dir: Path) -> str:
    """Hex sha256 of ``env_dir/requirements.txt`` — the marker's target value."""
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


def read_marker(marker: Path) -> str | None:
    """Read a marker value, FAIL-SAFE.

    Returns the stripped marker text, or ``None`` if the marker is missing,
    unreadable, a directory, or empty. A short/partial hash is returned verbatim
    (it can never equal the 64-hex want, so it too maps to needs-rebuild). The
    contract: a degenerate marker NEVER raises and NEVER yields a false skip.
    """
    try:
        text = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    value = text.strip()
    return value or None


def write_marker_atomic(marker: Path, value: str) -> None:
    """Write ``value`` to ``marker`` atomically.

    Writes to a temp file in the SAME directory (so ``os.replace`` is a same-
    filesystem rename), fsyncs, then atomically replaces the marker. A concurrent
    reader can therefore only ever observe a COMPLETE old or COMPLETE new marker,
    never a partial one. On ANY failure the temp file is removed and the error
    re-raised, leaving any pre-existing marker untouched (a plain ``write_text``
    would truncate it).
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(marker.parent), prefix=marker.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, marker)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def uv_python_install_dir(venv_dir: Path) -> Path:
    """Where uv may install a MANAGED interpreter for this sidecar's venv.

    An operator-set ``UV_PYTHON_INSTALL_DIR`` (set-and-nonempty, read at CALL
    time) wins verbatim. Otherwise the default is ``venv_dir.parent /
    ".uv-python"`` — a SIBLING of the venv, NOT uv's own ``$HOME``-derived
    default (``~/.local/share/uv/python``).

    Why the sibling (NE-SVENV-EXEC): hardened containers commonly run with
    ``HOME`` pointed at a tmpfs mounted ``noexec`` (the live connect container:
    ``HOME=/tmp``). uv then downloads cpython there successfully but the binary
    CAN NEVER BE EXECUTED — ``uv venv --python 3.12`` fails forever with an
    opaque "Failed to inspect Python interpreter" exit 2. The venv root the
    caller supplies is by contract a writable, exec-capable mount (it hosts the
    venv's real files), so its parent is the one directory we can trust. A
    sibling also keeps the interpreter on the same PERSISTENT volume as the venv
    that symlinks to it: an interpreter under a tmpfs ``HOME`` vanishes on
    container restart, dangling every venv symlink and forcing a full rebuild.
    """
    override = os.environ.get(UV_PYTHON_INSTALL_DIR_VAR)
    if override:
        return Path(override)
    return venv_dir.parent / UV_PYTHON_DIR_LEAF


def uv_cache_dir(venv_dir: Path) -> Path:
    """Where uv may cache the wheels/sdists it downloads to build this venv.

    An operator-set ``UV_CACHE_DIR`` (set-and-nonempty, read at CALL time) wins
    verbatim. Otherwise the default is ``venv_dir.parent / ".uv-cache"`` — a
    SIBLING of the venv, beside ``.uv-python``, NOT uv's own ``$HOME``-derived
    default (``$XDG_CACHE_HOME/uv``, else ``~/.cache/uv``).

    Why the sibling (NE-SVENV-CACHE): the sibling redirect in
    :func:`uv_python_install_dir` moved the managed INTERPRETER off the tmpfs
    ``$HOME`` and left the CACHE — by far the bigger of the two — behind. The
    same hardened containers that point ``HOME`` at a tmpfs (the live connect
    worker: ``HOME=/tmp``) thereby put every byte uv downloads into RAM, and
    tmpfs pages are charged to the container's MEMORY CGROUP. So the closure
    size is charged against the container's memory LIMIT: the live worker has a
    2 GiB limit and the docling/torch/CUDA closure is ~3.3 GB of wheels, and
    the kernel OOM-killed the worker mid-download every ~3 minutes — inside uv's
    own ``write()`` into the tmpfs (``shmem_write_begin`` ->
    ``__mem_cgroup_charge`` -> ``mem_cgroup_out_of_memory``), with 1.78 GB of
    the cgroup's 2 GiB held as ``shmem`` and only 0.31 GB as real process
    memory. Each restart wipes the tmpfs and the download starts from zero, so
    ``/connect/.parser-venv`` never populated and M365 ingestion stayed down.
    Note the tmpfs itself was nowhere near full (31 GB) — the limit that binds
    is the cgroup's, which no ``df`` shows.

    The venv root the caller supplies is by contract a writable, PERSISTENT,
    disk-backed mount (it hosts the venv's real files), so its parent is the one
    directory we can trust. Landing the cache there also means uv can hardlink
    from cache into ``site-packages`` instead of copying across filesystems, and
    a build interrupted for any other reason resumes warm instead of
    re-downloading the whole closure.

    Deliberately NOT checked here, unlike :func:`check_exec_capable`: free space
    and RAM-backing. ``noexec`` is unconditionally fatal and cheap to prove, so
    it earns a guard; "enough space" is not — the ten sidecar closures span
    megabytes (linkml) to gigabytes (docling), so any fixed threshold would
    either miss the real case or fire on a healthy box, and a tmpfs cache is
    perfectly correct wherever the cgroup can hold it. uv's own out-of-space
    error already names the path.
    """
    override = os.environ.get(UV_CACHE_DIR_VAR)
    if override:
        return Path(override)
    return venv_dir.parent / UV_CACHE_DIR_LEAF


def _nearest_existing(path: Path) -> Path:
    """The deepest existing ancestor of ``path`` (or ``path`` itself)."""
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return probe


def _mount_point_of(path: Path) -> Path:
    """The mount point of the filesystem holding ``path`` (must exist)."""
    probe = path.resolve()
    while not os.path.ismount(probe):
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return probe


def check_exec_capable(install_dir: Path, *, name: str) -> None:
    """Fail LOUDLY if ``install_dir``'s filesystem can never execute a binary.

    A managed interpreter installed onto a ``noexec`` mount downloads fine and
    then fails every ``uv venv`` with an opaque ``exit status 2`` ("Failed to
    inspect Python interpreter") — which is how a two-day production outage went
    undiagnosed (NE-SVENV-EXEC). This probes the mount flags of the deepest
    existing ancestor via ``statvfs`` and raises ONE LINE naming the path and
    the mount problem instead. Indeterminate probes (``statvfs`` failure, or a
    platform without ``ST_NOEXEC``) stay silent — uv remains the authority.
    """
    probe = _nearest_existing(install_dir)
    try:
        flags = os.statvfs(probe).f_flag
    except OSError:
        return
    if flags & getattr(os, "ST_NOEXEC", 0):
        mount = _mount_point_of(probe)
        raise RuntimeError(
            f"uv Python install dir {install_dir} for {name} is unusable: its filesystem "
            f"(mount point {mount}) is mounted noexec, so a managed interpreter installed "
            f"there can never be executed — set {UV_PYTHON_INSTALL_DIR_VAR} to a directory "
            "on an exec-capable mount."
        )


def _default_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def ensure_sidecar_venv(
    *,
    env_dir: Path,
    venv_dir: Path,
    install_args: Sequence[str] = (),
    pre_lock_check: Callable[[], None] | None = None,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    python_version: str = DEFAULT_PYTHON_VERSION,
    name: str = "the isolated sidecar venv",
    run: UvRunner | None = None,
) -> None:
    """Create/refresh an isolated hash-pinned sidecar venv if needed.

    The venv is (re)built when it is missing or when the stored marker of
    ``env_dir/requirements.txt`` no longer matches the committed file. Install is
    always ``uv pip install --require-hashes`` (SPEC §2 C4/C11); call sites pass
    their divergences (e.g. ``--no-deps`` or ``--overrides ...``) via
    ``install_args``.

    Concurrency: the rebuild is serialised behind a cross-process/thread
    :class:`filelock.FileLock` (a SIBLING of ``venv_dir`` so it survives the
    rebuild ``rmtree``). The unlocked fast-path is an ADVISORY optimisation only;
    the authoritative up-to-date decision is re-made UNDER the lock.

    ``pre_lock_check`` (if given) runs AFTER the unlocked fast-path but BEFORE any
    ``mkdir``/lock/``rmtree``/subprocess — this is where a fail-closed policy hook
    (e.g. docling's AIR_GAP guard) plugs in so it fires on the stale/absent path
    without ever attempting a build.
    """
    runner = run if run is not None else _default_run
    want = requirements_sha256(env_dir)
    marker = marker_path(venv_dir)
    python = venv_python(venv_dir)

    def _up_to_date() -> bool:
        return python.is_file() and read_marker(marker) == want

    # Cheap, UNLOCKED fast-path: an already-current venv needs no lock. ADVISORY
    # ONLY — the authoritative decision is re-made under the lock below.
    if _up_to_date():
        return

    # Fail-closed policy hook (e.g. AIR_GAP): runs on the stale/absent path BEFORE
    # any mkdir/lock/rmtree/subprocess, so a forbidden build is refused with no
    # side effects.
    if pre_lock_check is not None:
        pre_lock_check()

    # Serialise the (re)build across threads and processes. The lock file is a
    # sibling of venv_dir so it survives the rmtree below. The parent of venv_dir
    # may not exist yet, and neither may the parent of the lock file.
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = env_lock_path(venv_dir)
    try:
        with filelock.FileLock(str(lock_path), timeout=lock_timeout_s):
            # AUTHORITATIVE re-check under the lock (double-checked locking): a
            # concurrent process may have finished the rebuild while we blocked.
            if _up_to_date():
                return
            # NE-SVENV-EXEC: a managed interpreter must land on an exec-capable
            # filesystem — verify BEFORE the destructive rmtree, and fail with a
            # one-line diagnostic naming the path and the mount problem rather
            # than uv's opaque "Failed to inspect Python interpreter" exit 2.
            python_install_dir = uv_python_install_dir(venv_dir)
            check_exec_capable(python_install_dir, name=name)
            # NE-SVENV-CACHE: the download cache must land on the same
            # persistent, disk-backed volume as the venv. Left at uv's
            # $HOME-derived default it lands on the container's tmpfs $HOME,
            # where the multi-gigabyte closure is charged to the memory cgroup
            # and OOM-kills the process mid-download, forever. See uv_cache_dir.
            cache_dir = uv_cache_dir(venv_dir)
            if venv_dir.exists():
                shutil.rmtree(venv_dir)
            # Both derived directories travel to uv in an EXPLICIT child
            # environment, scoped to each subprocess. Never mutate os.environ
            # instead: uv_python_install_dir()/uv_cache_dir()'s operator-override
            # reads in a CONCURRENT build (under a different venv parent, so a
            # different file lock) would mistake the temporary process-global
            # value for an operator setting and bind its venv to THIS build's
            # volumes — and every unrelated subprocess spawned during the
            # potentially minutes-long build would inherit them.
            #
            # Each env is os.environ PLUS these keys and nothing else, so both
            # calls still inherit everything ambient they ever did (proxies,
            # CA bundles, netrc, index credentials, TMPDIR, ...).
            runner(
                ["uv", "venv", str(venv_dir), "--python", python_version],
                env={
                    **os.environ,
                    UV_PYTHON_INSTALL_DIR_VAR: str(python_install_dir),
                    UV_CACHE_DIR_VAR: str(cache_dir),
                },
            )
            # `uv pip install` gets the cache dir but deliberately NOT the
            # install dir: it addresses the interpreter by absolute path, so an
            # install-dir override would only perturb unrelated uv discovery.
            # This is the call the outage hinges on — the ~3.3 GB of wheels
            # lands here, not in `uv venv`.
            runner(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--require-hashes",
                    *install_args,
                    "-r",
                    str(env_dir / "requirements.txt"),
                ],
                env={**os.environ, UV_CACHE_DIR_VAR: str(cache_dir)},
            )
            write_marker_atomic(marker, want)
    except filelock.Timeout as exc:
        raise RuntimeError(
            f"timed out after {lock_timeout_s:g}s waiting for the rebuild lock at "
            f"{lock_path} while ensuring {name}. Another process is likely building "
            "it; if none is, remove the stale lock file and retry."
        ) from exc
