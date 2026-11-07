# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SVENV-CACHE: uv's download cache must land beside the venv, not on $HOME.

The sibling redirect proven by ``test_uv_python_exec_dir.py`` moved the managed
INTERPRETER off the container's tmpfs ``HOME=/tmp`` and left the CACHE — by far
the bigger of the two — behind. Measured on the live connect worker while it was
down: ``/tmp`` is ``tmpfs … noexec`` (31 GB, so never "full"), the container's
memory cgroup limit is 2 GiB, and the docling closure is ~3.3 GB of wheels.
tmpfs pages are charged to the cgroup, so the kernel OOM-killed the worker
mid-download every ~3 minutes, from inside uv's own ``write()`` into the tmpfs::

    tokio-runtime-w invoked oom-killer
      shmem_write_begin -> shmem_alloc_and_add_folio
        -> __mem_cgroup_charge -> try_charge_memcg
          -> mem_cgroup_out_of_memory -> oom_kill_process
    memory: usage 2097168kB, limit 2097152kB
    shmem 1780142080          # 1.78 GB of the 2 GiB is tmpfs, not process memory
    anon  306941952
    Killed process (python) ... oom_score_adj:0

Each restart wiped the tmpfs and the download began again from zero, so
``/connect/.parser-venv`` never grew past 88K and M365 ingestion stayed down.

This suite proves the shared builder now hands BOTH uv subprocesses an explicit
child environment carrying ``UV_CACHE_DIR`` pointed at a SIBLING of the venv, so
the closure lands on the same persistent, disk-backed volume as the venv it is
being installed into — and that the newly-explicit environment on
``uv pip install`` is the ambient one PLUS that single key and nothing else.

The ``uv`` subprocess is faked — this test NEVER runs a real ``uv``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from ne.envbuild import (
    UV_CACHE_DIR_LEAF,
    UV_CACHE_DIR_VAR,
    UV_PYTHON_INSTALL_DIR_VAR,
    ensure_sidecar_venv,
    uv_cache_dir,
)


def _write_requirements(env_dir: Path) -> str:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text("docling==2.110.0 --hash=sha256:deadbeef\n")
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


def _cache_dir_uv_would_use(env: Mapping[str, str] | None) -> Path:
    """The cache directory uv itself picks for a subprocess spawned with ``env``.

    ``None`` means the subprocess inherits ``os.environ`` (``subprocess.run``'s
    ``env=`` semantics). Mirrors uv's documented resolution order: an explicit
    ``UV_CACHE_DIR``, else ``$XDG_CACHE_HOME/uv``, else ``$HOME/.cache/uv``.

    Resolving this the way uv does — rather than asserting on the env dict — is
    what makes the assertions below fail on the parent commit with the actual
    ``$HOME``-derived path in the message, instead of merely observing that a
    key is absent.
    """
    source = os.environ if env is None else env
    explicit = source.get(UV_CACHE_DIR_VAR)
    if explicit:
        return Path(explicit)
    xdg = source.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "uv"
    return Path(source["HOME"]) / ".cache" / "uv"


class _CacheRecordingRunner:
    """Fakes ``uv venv``/``uv pip install``, recording for each call the cache
    directory uv would actually use and the COMPLETE env dict it was handed.

    ``process_env_at_call`` records what ``os.environ`` itself held during each
    call — it must NEVER carry the derived value (an unrelated subprocess
    spawned by another thread mid-build would inherit it).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.commands: list[list[str]] = []
        self.cache_at_venv: list[Path] = []
        self.cache_at_install: list[Path] = []
        self.env_at_install: list[dict[str, str] | None] = []
        self.process_env_at_call: list[str | None] = []

    def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        resolved = _cache_dir_uv_would_use(env)
        with self._lock:
            self.commands.append(cmd)
            self.process_env_at_call.append(os.environ.get(UV_CACHE_DIR_VAR))
        if cmd[:2] == ["uv", "venv"]:
            with self._lock:
                self.cache_at_venv.append(resolved)
            venv_dir = Path(cmd[2])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!fake-python\n")
            return
        if cmd[:3] == ["uv", "pip", "install"]:
            with self._lock:
                self.cache_at_install.append(resolved)
                self.env_at_install.append(None if env is None else dict(env))
            return
        raise AssertionError(f"unexpected command: {cmd!r}")


@pytest.fixture
def _neutral_cache_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A hermetic ``$HOME`` with no cache overrides — so an unredirected uv
    resolves its cache under ``$HOME``, exactly as it does on the live box."""
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(UV_CACHE_DIR_VAR, raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv(UV_PYTHON_INSTALL_DIR_VAR, raising=False)
    return home


def test_pip_install_downloads_into_the_venv_sibling_not_home(
    tmp_path: Path, _neutral_cache_env: Path
) -> None:
    """⭐ THE OUTAGE. ``uv pip install`` is where the ~3.3 GB of wheels lands;
    it must download into the venv's sibling ``.uv-cache`` on the venv's own
    persistent volume, NOT under ``$HOME``.

    FAILS ON MAIN: the install call passed no ``env=`` at all, so it inherited
    the ambient environment and uv resolved its cache to ``$HOME/.cache/uv`` —
    ``/tmp/.cache/uv`` on the live box, a tmpfs charged to a 2 GiB cgroup.
    """
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / "state" / ".parser-venv"
    _write_requirements(env_dir)
    runner = _CacheRecordingRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    expected = venv_dir.parent / UV_CACHE_DIR_LEAF
    home_derived = _neutral_cache_env / ".cache" / "uv"
    assert runner.cache_at_install == [expected], (
        f"uv pip install must download into {expected}; it would use "
        f"{runner.cache_at_install!r} (the $HOME-derived default is {home_derived})"
    )
    assert expected != home_derived


def test_uv_venv_gets_the_same_sibling_cache(tmp_path: Path, _neutral_cache_env: Path) -> None:
    """``uv venv`` shares the redirect — it too writes into uv's cache, and both
    calls must agree on one directory. FAILS ON MAIN: ``uv venv`` was handed an
    env carrying only ``UV_PYTHON_INSTALL_DIR``, so its cache stayed on $HOME."""
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / "state" / ".parser-venv"
    _write_requirements(env_dir)
    runner = _CacheRecordingRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    expected = venv_dir.parent / UV_CACHE_DIR_LEAF
    assert runner.cache_at_venv == [expected], (
        f"uv venv must use {expected}; it would use {runner.cache_at_venv!r} "
        f"(the $HOME-derived default is {_neutral_cache_env / '.cache' / 'uv'})"
    )
    assert runner.cache_at_venv == runner.cache_at_install


def test_install_env_is_ambient_plus_exactly_the_cache_dir(
    tmp_path: Path, _neutral_cache_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Giving ``uv pip install`` an explicit ``env=`` must not take anything
    away from it. It previously inherited the ambient environment wholesale
    (proxies, CA bundles, netrc, index credentials, TMPDIR, ...); the new env
    must be ``os.environ`` PLUS ``UV_CACHE_DIR`` and NOTHING else — in
    particular not ``UV_PYTHON_INSTALL_DIR``, which addresses interpreter
    DISCOVERY that this call does not use (it names the interpreter by absolute
    path), so injecting it would only perturb unrelated uv behavior."""
    monkeypatch.setenv("NE_AMBIENT_PROBE", "must-survive")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-bundle.crt")
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / "state" / ".parser-venv"
    _write_requirements(env_dir)
    runner = _CacheRecordingRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    (install_env,) = runner.env_at_install
    assert install_env is not None
    ambient = dict(os.environ)
    assert set(install_env) - set(ambient) == {UV_CACHE_DIR_VAR}, (
        "uv pip install's env must add exactly UV_CACHE_DIR to the ambient "
        f"environment; it adds {sorted(set(install_env) - set(ambient))}"
    )
    assert set(ambient) - set(install_env) == set(), (
        "uv pip install's env must not DROP anything it used to inherit; it drops "
        f"{sorted(set(ambient) - set(install_env))}"
    )
    assert {k: v for k, v in install_env.items() if k != UV_CACHE_DIR_VAR} == ambient
    assert install_env["NE_AMBIENT_PROBE"] == "must-survive"
    assert install_env["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-bundle.crt"
    assert UV_PYTHON_INSTALL_DIR_VAR not in install_env


def test_operator_override_is_respected_verbatim(
    tmp_path: Path, _neutral_cache_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-set ``UV_CACHE_DIR`` wins verbatim for BOTH calls — that is
    the seam a deploy uses to point at an already-warm cache volume."""
    override = tmp_path / "operator-cache"
    monkeypatch.setenv(UV_CACHE_DIR_VAR, str(override))
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    _write_requirements(env_dir)
    runner = _CacheRecordingRunner()

    assert uv_cache_dir(venv_dir) == override
    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert runner.cache_at_venv == [override]
    assert runner.cache_at_install == [override]
    # The pre-existing operator value survives untouched.
    assert os.environ[UV_CACHE_DIR_VAR] == str(override)


def test_empty_env_var_counts_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(UV_CACHE_DIR_VAR, "")
    venv_dir = tmp_path / ".venv"
    assert uv_cache_dir(venv_dir) == venv_dir.parent / UV_CACHE_DIR_LEAF


def test_derived_cache_never_reaches_the_process_environment(
    tmp_path: Path, _neutral_cache_env: Path
) -> None:
    """The derived value travels in the child env ONLY: ``os.environ`` must not
    hold it, not even DURING either uv call, or an unrelated subprocess spawned
    meanwhile would inherit it (cf. the frozen-config-hash incident)."""
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    _write_requirements(env_dir)
    runner = _CacheRecordingRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert runner.process_env_at_call == [None, None]
    assert UV_CACHE_DIR_VAR not in os.environ


def test_concurrent_cold_builds_never_share_a_cache_dir(
    tmp_path: Path, _neutral_cache_env: Path
) -> None:
    """Two CONCURRENT cold builds under DIFFERENT venv parents must each resolve
    their OWN sibling cache — neither may read the other's derived value as an
    "operator override" (the failure mode a process-global mutation would
    reintroduce here exactly as it did for the interpreter dir)."""
    a_env, a_venv = tmp_path / "a" / "env", tmp_path / "a" / ".venv-a"
    b_env, b_venv = tmp_path / "b" / "env", tmp_path / "b" / ".venv-b"
    _write_requirements(a_env)
    _write_requirements(b_env)

    a_inside_uv_venv = threading.Event()
    b_finished = threading.Event()

    class _HoldingRunner(_CacheRecordingRunner):
        """Build A parks inside ``uv venv`` until build B has finished, so B's
        whole cold build runs while A's is in flight — the live-box
        interleaving. The timeout is a backstop for a REGRESSED implementation
        that serialises the two; a correct one never waits it out."""

        def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
            super().__call__(cmd, env=env)
            if cmd[:2] == ["uv", "venv"]:
                a_inside_uv_venv.set()
                b_finished.wait(timeout=10)

    runner_a = _HoldingRunner()
    runner_b = _CacheRecordingRunner()
    error_a: list[BaseException] = []

    def _build_a() -> None:
        try:
            ensure_sidecar_venv(env_dir=a_env, venv_dir=a_venv, run=runner_a)
        except BaseException as exc:  # pragma: no cover - surfaced below
            error_a.append(exc)

    thread_a = threading.Thread(target=_build_a)
    thread_a.start()
    try:
        assert a_inside_uv_venv.wait(timeout=30), "build A never reached uv venv"
        ensure_sidecar_venv(env_dir=b_env, venv_dir=b_venv, run=runner_b)
    finally:
        b_finished.set()
        thread_a.join(timeout=60)
    assert not thread_a.is_alive(), "build A never finished"
    assert error_a == []

    assert runner_a.cache_at_venv == [a_venv.parent / UV_CACHE_DIR_LEAF]
    assert runner_b.cache_at_venv == [b_venv.parent / UV_CACHE_DIR_LEAF], (
        f"build B resolved another build's cache dir: {runner_b.cache_at_venv!r}"
    )
    assert runner_b.cache_at_install == [b_venv.parent / UV_CACHE_DIR_LEAF]
    assert UV_CACHE_DIR_VAR not in os.environ


# The same eight repository-default sidecar venv roots enumerated in
# test_uv_python_exec_dir.py — the derived `.uv-cache` lands beside `.uv-python`
# at every one of them.
_DEFAULT_SIDECAR_VENV_ROOTS = [
    "packages/ne-brain",
    "packages/ne-govern",
    "packages/ne-schema",
    "packages/ne-eval",
    "packages/ne-mcp",
    "packages/ne-brain-graph",
    "packages/ne-memory-graph",
    "tools/model-export",
]


@pytest.mark.parametrize("root", _DEFAULT_SIDECAR_VENV_ROOTS)
def test_generated_cache_dir_is_git_ignored_at_every_default_root(root: str) -> None:
    """A cold local build creates `.uv-cache` beside the repository-default
    venv; git must ignore it at EVERY default root or the build leaves a
    multi-gigabyte download cache untracked (breaking the
    clean-candidate-state requirement). FAILS ON MAIN: no
    `.gitignore` excluded the generated directory anywhere."""
    repo_root = Path(__file__).resolve().parents[3]
    assert (repo_root / root).is_dir(), f"stale root enumeration: {root} does not exist"
    probe = f"{root}/{UV_CACHE_DIR_LEAF}/wheels-v6/pypi/torch/torch-2.9.0.whl"
    result = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "-q", "--", probe],
        check=False,
        capture_output=True,
    )
    if result.returncode not in (0, 1):  # pragma: no cover - environment guard
        pytest.skip(f"git check-ignore unavailable: {result.stderr.decode(errors='replace')}")
    assert result.returncode == 0, (
        f"{root}/{UV_CACHE_DIR_LEAF} is not git-ignored — a cold build would "
        "leave a multi-gigabyte uv download cache untracked"
    )
