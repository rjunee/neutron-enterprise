# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SVENV-EXEC: managed interpreters must land on an exec-capable filesystem.

The live connect container runs with ``HOME=/tmp`` on a tmpfs mounted
``noexec``. ``uv venv --python 3.12`` (system Python is 3.13) downloaded a
managed cpython under ``/tmp/.local/share/uv/python/`` — successfully — and then
failed EVERY provisioning attempt for two days with an opaque ``exit status 2``
("Failed to inspect Python interpreter"), because a binary on a ``noexec`` mount
can never be executed. This suite proves the shared builder now

* hands ``uv venv`` an EXPLICIT child environment carrying
  ``UV_PYTHON_INSTALL_DIR`` (uv's only knob — ``uv venv`` has no CLI flag for
  it) pointing at a SIBLING of the venv dir, so a managed download lands on the
  same writable, exec-capable, persistent mount as the venv that symlinks to
  it — WITHOUT ever mutating ``os.environ`` (a process-global mutation raced
  concurrent builds and leaked into unrelated subprocesses);
* respects an operator-set ``UV_PYTHON_INSTALL_DIR`` verbatim;
* never lets one build's derived value reach another concurrent build's
  operator-override read, nor ``uv pip install``, nor any unrelated spawn;
* FAILS LOUDLY, in one line naming the path and the ``noexec`` mount problem,
  when the resolved install dir cannot host an executable interpreter — before
  the destructive ``rmtree`` of the old venv;
* is matched by a ``.gitignore`` entry at every repository-default venv root,
  so a cold local build never leaves the generated interpreter dir untracked.

The ``uv`` subprocess is faked — this test NEVER runs a real ``uv``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import types
from pathlib import Path

import pytest

from ne.envbuild import (
    UV_PYTHON_DIR_LEAF,
    UV_PYTHON_INSTALL_DIR_VAR,
    check_exec_capable,
    ensure_sidecar_venv,
    uv_python_install_dir,
    venv_python,
)


def _write_requirements(env_dir: Path) -> str:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text("docling==2.110.0 --hash=sha256:deadbeef\n")
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


class _EnvRecordingRunner:
    """Fakes ``uv venv``/``uv pip install`` and records ``UV_PYTHON_INSTALL_DIR``
    exactly as each subprocess would inherit it at spawn time: from the explicit
    ``env`` when one is passed, else from the process environment.

    ``process_env_at_venv`` additionally records what ``os.environ`` itself held
    during the ``uv venv`` call — it must NEVER carry the derived value (an
    unrelated subprocess spawned by another thread during the potentially long
    ``uv venv`` would inherit it).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.commands: list[list[str]] = []
        self.env_at_venv: list[str | None] = []
        self.process_env_at_venv: list[str | None] = []
        self.env_at_install: list[str | None] = []

    def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        with self._lock:
            self.commands.append(cmd)
        inherited = (env if env is not None else os.environ).get(UV_PYTHON_INSTALL_DIR_VAR)
        if cmd[:2] == ["uv", "venv"]:
            with self._lock:
                self.env_at_venv.append(inherited)
                self.process_env_at_venv.append(os.environ.get(UV_PYTHON_INSTALL_DIR_VAR))
            venv_dir = Path(cmd[2])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!fake-python\n")
            return
        if cmd[:3] == ["uv", "pip", "install"]:
            with self._lock:
                self.env_at_install.append(inherited)
            return
        raise AssertionError(f"unexpected command: {cmd!r}")


def test_uv_venv_gets_exec_capable_sibling_install_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv venv` must inherit UV_PYTHON_INSTALL_DIR pointing at the venv's
    sibling `.uv-python` — NOT uv's $HOME-derived default (noexec tmpfs on the
    live box) — via an explicit child environment, with `os.environ` untouched
    even DURING the call. FAILS ON MAIN: the builder never set the variable at
    all, so a managed 3.12 download landed wherever $HOME pointed."""
    monkeypatch.delenv(UV_PYTHON_INSTALL_DIR_VAR, raising=False)
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / "state" / ".parser-venv"
    _write_requirements(env_dir)
    runner = _EnvRecordingRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    expected = str(venv_dir.parent / UV_PYTHON_DIR_LEAF)
    assert runner.env_at_venv == [expected], (
        f"uv venv must inherit {UV_PYTHON_INSTALL_DIR_VAR}={expected}; got {runner.env_at_venv!r}"
    )
    # The value travels in the child env ONLY — the process environment never
    # holds it, not even while `uv venv` is running.
    assert runner.process_env_at_venv == [None]
    assert UV_PYTHON_INSTALL_DIR_VAR not in os.environ


def test_operator_override_is_respected_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "operator-python-dir"
    monkeypatch.setenv(UV_PYTHON_INSTALL_DIR_VAR, str(override))
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    _write_requirements(env_dir)
    runner = _EnvRecordingRunner()

    assert uv_python_install_dir(venv_dir) == override
    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert runner.env_at_venv == [str(override)]
    # The pre-existing operator value survives untouched.
    assert os.environ[UV_PYTHON_INSTALL_DIR_VAR] == str(override)


def test_empty_env_var_counts_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(UV_PYTHON_INSTALL_DIR_VAR, "")
    venv_dir = tmp_path / ".venv"
    assert uv_python_install_dir(venv_dir) == venv_dir.parent / UV_PYTHON_DIR_LEAF


def test_concurrent_cold_builds_never_cross_contaminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two CONCURRENT cold builds under DIFFERENT venv parents must each bind
    their venv to their OWN sibling `.uv-python` — and neither may ever see the
    other's derived value as an "operator override".

    FAILS ON 085e86a5: the builder temporarily mutated ``os.environ`` around
    ``uv venv``, and ``uv_python_install_dir()``'s operator-override read
    happens BEFORE the mutation mutex is acquired — so build B, entering while
    build A sat inside its (potentially minutes-long) ``uv venv``, read A's
    temporary process-global value, mistook it for an operator override, and
    bound B's venv to A's managed-Python volume.
    """
    monkeypatch.delenv(UV_PYTHON_INSTALL_DIR_VAR, raising=False)
    a_env, a_venv = tmp_path / "a" / "env", tmp_path / "a" / ".venv-a"
    b_env, b_venv = tmp_path / "b" / "env", tmp_path / "b" / ".venv-b"
    _write_requirements(a_env)
    _write_requirements(b_env)

    a_inside_uv_venv = threading.Event()
    b_finished = threading.Event()

    class _HoldingRunner(_EnvRecordingRunner):
        """Build A's runner: parks inside `uv venv` until build B has finished,
        so B's entire cold build runs while A's venv creation is in flight —
        the live-box interleaving (a managed-cpython download takes minutes).
        The timeout is a backstop for a REGRESSED implementation that
        serialises the two builds; a correct one never waits it out."""

        def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
            super().__call__(cmd, env=env)
            if cmd[:2] == ["uv", "venv"]:
                a_inside_uv_venv.set()
                b_finished.wait(timeout=10)

    runner_a = _HoldingRunner()
    runner_b = _EnvRecordingRunner()
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

    # Each build bound its venv to its OWN sibling — B never inherited A's dir.
    assert runner_a.env_at_venv == [str(a_venv.parent / UV_PYTHON_DIR_LEAF)]
    assert runner_b.env_at_venv == [str(b_venv.parent / UV_PYTHON_DIR_LEAF)], (
        f"build B bound its venv to another build's managed-Python dir: {runner_b.env_at_venv!r}"
    )
    # The derived values never reached the PROCESS environment at any point —
    # the operator-override read can only ever see the real ambient value.
    assert runner_a.process_env_at_venv == [None]
    assert runner_b.process_env_at_venv == [None]
    assert UV_PYTHON_INSTALL_DIR_VAR not in os.environ


@pytest.mark.skipif(not hasattr(os, "ST_NOEXEC"), reason="ST_NOEXEC not available")
def test_noexec_install_dir_fails_loudly_before_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A noexec install dir must produce ONE LINE naming the path and the mount
    problem — never uv's `CalledProcessError: exit status 2` — and must fire
    BEFORE the old venv is destroyed. FAILS ON MAIN: no probe existed; the
    builder ran `uv venv` straight into the opaque exit-2."""
    monkeypatch.delenv(UV_PYTHON_INSTALL_DIR_VAR, raising=False)
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".parser-venv"
    want = _write_requirements(env_dir)
    # Seed a STALE venv (marker mismatch) so the rebuild path is taken.
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("#!fake-python\n")
    (venv_dir / ".requirements.sha256").write_text("0" * 64)
    assert want != "0" * 64
    runner = _EnvRecordingRunner()

    noexec = types.SimpleNamespace(f_flag=os.ST_NOEXEC)
    monkeypatch.setattr(os, "statvfs", lambda _path: noexec)

    with pytest.raises(RuntimeError) as excinfo:
        ensure_sidecar_venv(
            env_dir=env_dir, venv_dir=venv_dir, name="the isolated parser venv", run=runner
        )

    message = str(excinfo.value)
    install_dir = venv_dir.parent / UV_PYTHON_DIR_LEAF
    assert str(install_dir) in message
    assert "noexec" in message
    assert UV_PYTHON_INSTALL_DIR_VAR in message
    assert "the isolated parser venv" in message
    # Diagnosed BEFORE any destructive/subprocess step: no uv ran, venv intact.
    assert runner.commands == []
    assert venv_python(venv_dir).is_file()


def test_exec_capable_dir_passes_silently(tmp_path: Path) -> None:
    # Real statvfs on a real (exec-capable) test filesystem: no raise, even for
    # a not-yet-created install dir (the deepest existing ancestor is probed).
    check_exec_capable(tmp_path / "does" / "not" / "exist" / ".uv-python", name="a sidecar venv")


@pytest.mark.skipif(not hasattr(os, "ST_NOEXEC"), reason="ST_NOEXEC not available")
def test_noexec_check_names_operator_override_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-set UV_PYTHON_INSTALL_DIR on a noexec mount is diagnosed too,
    naming the operator's path."""
    override = tmp_path / "operator-noexec"
    monkeypatch.setenv(UV_PYTHON_INSTALL_DIR_VAR, str(override))
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    _write_requirements(env_dir)

    noexec = types.SimpleNamespace(f_flag=os.ST_NOEXEC)
    monkeypatch.setattr(os, "statvfs", lambda _path: noexec)

    with pytest.raises(RuntimeError, match="noexec") as excinfo:
        ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=_EnvRecordingRunner())
    assert str(override) in str(excinfo.value)


def test_export_never_leaks_into_pip_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install-dir override is scoped to `uv venv` only: `uv pip install
    --python <venv python>` addresses the interpreter by absolute path and needs
    no discovery override, so nothing may leak into it (an inherited override
    changes unrelated uv behavior — cf. the frozen-config-hash incident)."""
    monkeypatch.delenv(UV_PYTHON_INSTALL_DIR_VAR, raising=False)
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    _write_requirements(env_dir)
    runner = _EnvRecordingRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert runner.env_at_install == [None]


# Every repository-default sidecar venv root, enumerated from the ten
# ensure_sidecar_venv call sites (default = venv_dir.parent, where the derived
# `.uv-python` lands when no suitable system Python exists):
#   packages/ne-brain        — docling parser        (.parser-venv)
#   packages/ne-govern       — presidio backend      (.presidio-venv)
#   packages/ne-schema       — linkml codegen        (.codegen-venv)
#   packages/ne-eval         — ragas oracle          (.ragas-venv)
#   packages/ne-mcp          — langgraph + crewai    (.langgraph-venv, .crewai-venv)
#   packages/ne-brain-graph  — lightrag + splink     (.lightrag-venv, .splink-venv)
#   packages/ne-memory-graph — graphiti              (.graphiti-venv)
#   tools/model-export       — model export          (.export-venv)
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
def test_generated_install_dir_is_git_ignored_at_every_default_root(root: str) -> None:
    """A cold local build with no suitable system Python creates `.uv-python`
    beside the repository-default venv; git must ignore it at EVERY default
    root or the build leaves an entire managed interpreter untracked (breaking
    the clean-candidate-state requirement). FAILS ON 085e86a5: no
    `.gitignore` excluded the generated directory anywhere."""
    repo_root = Path(__file__).resolve().parents[3]
    assert (repo_root / root).is_dir(), f"stale root enumeration: {root} does not exist"
    probe = f"{root}/{UV_PYTHON_DIR_LEAF}/cpython-3.12/bin/python3"
    result = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "-q", "--", probe],
        check=False,
        capture_output=True,
    )
    if result.returncode not in (0, 1):  # pragma: no cover - environment guard
        pytest.skip(f"git check-ignore unavailable: {result.stderr.decode(errors='replace')}")
    assert result.returncode == 0, (
        f"{root}/{UV_PYTHON_DIR_LEAF} is not git-ignored — a cold build would "
        "leave the generated managed interpreter untracked"
    )
