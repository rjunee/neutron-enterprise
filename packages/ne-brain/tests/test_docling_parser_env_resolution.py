# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DoclingParser must be constructible outside a monorepo dev checkout (NE-020 fix).

The default (`DoclingParser()`, no args) resolution walks up from this file for
a directory containing `uv.lock` — fine for every current deployment shape,
but unavailable once `ne-brain` is installed as a standalone wheel outside a
monorepo checkout. This module proves the escape hatches for that shape:
explicit `parser_env_dir=`/`venv_dir=` constructor kwargs, the
`NE_PARSER_ENV_DIR`/`NE_PARSER_VENV_DIR` environment variables, and — when
none of those are available and no `uv.lock` is reachable — a clear,
actionable error naming both mechanisms instead of a bare lookup failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Never

import pytest

import ne.brain as brain
import ne.brain.ingest.docling_parser as docling_parser_module
from ne.brain.ingest.contracts import (
    ParserConversionError,
    ParserEnvironmentError,
    ParserOutputError,
    ParserResidueError,
)
from ne.brain.ingest.docling_parser import (
    _CHILD_EXIT_AVAILABILITY,
    _CHILD_EXIT_CAPACITY,
    _CHILD_EXIT_CONVERSION,
    _CHILD_EXIT_DEPENDENCY,
    _CHILD_EXIT_INFERENCE,
    _CHILD_EXIT_INTERNAL,
    _CHILD_EXIT_OUTPUT,
    _CHILD_EXIT_POLICY,
    _CHILD_EXIT_TIMEOUT,
    _CHILD_EXIT_TRANSPORT,
    _ENV_DIR_OVERRIDE_VAR,
    _VENV_DIR_OVERRIDE_VAR,
    DoclingParser,
    ParserAvailabilityError,
    ParserCapacityError,
    ParserDependencyError,
    ParserInferenceError,
    ParserInternalError,
    ParserPolicyError,
    ParserTimeoutError,
    ParserTransportError,
    _parse_sync,
    _venv_python_at,
)


def test_parser_failure_categories_are_exported_with_exact_identity() -> None:
    assert brain.ParserConversionError is ParserConversionError
    assert brain.ParserOutputError is ParserOutputError
    assert brain.ParserResidueError is ParserResidueError


def test_root_kwarg_resolves_standard_layout(tmp_path: Path) -> None:
    parser = DoclingParser(tmp_path)
    assert parser._parser_env_dir == tmp_path / "packages" / "ne-brain" / "parser_env"
    assert parser._venv_dir == tmp_path / "packages" / "ne-brain" / ".parser-venv"


def test_explicit_dirs_bypass_repo_root_discovery(tmp_path: Path) -> None:
    """No uv.lock anywhere near tmp_path — explicit dirs must still work."""
    env_dir = tmp_path / "vendored" / "parser_env"
    venv_dir = tmp_path / "state" / "parser-venv"

    parser = DoclingParser(parser_env_dir=env_dir, venv_dir=venv_dir)

    assert parser._parser_env_dir == env_dir
    assert parser._venv_dir == venv_dir


def test_explicit_parser_env_dir_defaults_venv_dir_as_sibling(tmp_path: Path) -> None:
    env_dir = tmp_path / "vendored" / "parser_env"

    parser = DoclingParser(parser_env_dir=env_dir)

    assert parser._parser_env_dir == env_dir
    assert parser._venv_dir == env_dir.parent / ".parser-venv"


def test_env_var_overrides_resolve_without_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "opt" / "parser_env"
    venv_dir = tmp_path / "var" / "parser-venv"
    monkeypatch.setenv(_ENV_DIR_OVERRIDE_VAR, str(env_dir))
    monkeypatch.setenv(_VENV_DIR_OVERRIDE_VAR, str(venv_dir))

    parser = DoclingParser()

    assert parser._parser_env_dir == env_dir
    assert parser._venv_dir == venv_dir


def test_env_var_override_alone_defaults_venv_as_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "opt" / "parser_env"
    monkeypatch.setenv(_ENV_DIR_OVERRIDE_VAR, str(env_dir))
    monkeypatch.delenv(_VENV_DIR_OVERRIDE_VAR, raising=False)

    parser = DoclingParser()

    assert parser._parser_env_dir == env_dir
    assert parser._venv_dir == env_dir.parent / ".parser-venv"


def test_no_override_and_no_repo_root_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate an installed wheel outside any monorepo checkout: no uv.lock
    reachable and no override configured. The constructor must fail with a
    message pointing at the override mechanisms, not a bare lookup error."""
    monkeypatch.delenv(_ENV_DIR_OVERRIDE_VAR, raising=False)
    monkeypatch.delenv(_VENV_DIR_OVERRIDE_VAR, raising=False)
    import ne.brain.ingest.docling_parser as docling_parser_module

    def _never_found(start: Path) -> Path:
        raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")

    monkeypatch.setattr(docling_parser_module, "_find_repo_root", _never_found)

    with pytest.raises(RuntimeError) as exc_info:
        DoclingParser()

    message = str(exc_info.value)
    assert _ENV_DIR_OVERRIDE_VAR in message
    assert _VENV_DIR_OVERRIDE_VAR in message
    assert "parser_env_dir" in message


def test_sidecar_venv_root_resolves_writable_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV: with only NE_PARSER_ENV_DIR + NE_SIDECAR_VENV_ROOT set (no
    NE_PARSER_VENV_DIR), the venv resolves to $NE_SIDECAR_VENV_ROOT/.parser-venv."""
    env_dir = tmp_path / "opt" / "parser_env"
    root = tmp_path / "writable"
    monkeypatch.setenv(_ENV_DIR_OVERRIDE_VAR, str(env_dir))
    monkeypatch.delenv(_VENV_DIR_OVERRIDE_VAR, raising=False)
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(root))

    parser = DoclingParser()

    assert parser._parser_env_dir == env_dir
    assert parser._venv_dir == root / ".parser-venv"


def test_parser_venv_dir_var_beats_sidecar_venv_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV: the specific NE_PARSER_VENV_DIR beats the generic
    NE_SIDECAR_VENV_ROOT (specific-beats-generic precedence)."""
    env_dir = tmp_path / "opt" / "parser_env"
    venv_dir = tmp_path / "var" / "parser-venv"
    monkeypatch.setenv(_ENV_DIR_OVERRIDE_VAR, str(env_dir))
    monkeypatch.setenv(_VENV_DIR_OVERRIDE_VAR, str(venv_dir))
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(tmp_path / "writable"))

    parser = DoclingParser()

    assert parser._venv_dir == venv_dir


@pytest.mark.parametrize("runtime_shape", ["missing", "not-executable", "corrupt"])
def test_runtime_environment_os_errors_are_typed_retryable_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_shape: str,
) -> None:
    parser_env_dir = tmp_path / "parser-env"
    venv_dir = tmp_path / "parser-venv"
    input_path = tmp_path / "source.docx"
    parser_env_dir.mkdir()
    input_path.write_bytes(b"fixture")
    python = _venv_python_at(venv_dir)
    if runtime_shape != "missing":
        python.parent.mkdir(parents=True)
        python.write_text("CORRUPT-RUNTIME-SENTINEL", encoding="utf-8")
        python.chmod(0o600 if runtime_shape == "not-executable" else 0o700)
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    with pytest.raises(ParserEnvironmentError) as excinfo:
        _parse_sync(parser_env_dir, venv_dir, input_path, "doc-1")

    assert type(excinfo.value) is ParserEnvironmentError
    assert str(excinfo.value) == "parser runtime environment failed"
    assert "CORRUPT-RUNTIME-SENTINEL" not in str(excinfo.value)


def _use_stage(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage.mkdir(mode=0o700)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kwargs: str(stage))


def _fail_first_unlink(monkeypatch: pytest.MonkeyPatch) -> None:
    original_unlink = os.unlink
    calls = 0

    def _unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("UNLINK-PATH-MUST-NOT-LEAK")
        if dir_fd is None:
            original_unlink(path)
        else:
            original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", _unlink)


def _fail_first_rmdir(monkeypatch: pytest.MonkeyPatch) -> None:
    original_rmdir = os.rmdir
    calls = 0

    def _rmdir(path: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("RMDIR-PATH-MUST-NOT-LEAK")
        if dir_fd is None:
            original_rmdir(path)
        else:
            original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", _rmdir)


def _block_all_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("DELETE-PATH-MUST-NOT-LEAK")

    monkeypatch.setattr(os, "unlink", _refuse)
    monkeypatch.setattr(os, "rmdir", _refuse)


def _write_child_output(cmd: list[str], payload: str) -> None:
    output_fd = int(cmd[cmd.index("--output-fd") + 1])
    remaining = memoryview(payload.encode("utf-8"))
    while remaining:
        written = os.write(output_fd, remaining)
        assert written > 0
        remaining = remaining[written:]
    os.fsync(output_fd)


def test_admitted_conversion_exit_remains_document_caused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser_env_dir = tmp_path / "parser-env"
    venv_dir = tmp_path / "parser-venv"
    input_path = tmp_path / "source.docx"
    parser_env_dir.mkdir()
    input_path.write_bytes(b"fixture")
    python = _venv_python_at(venv_dir)
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!/bin/sh\nprintf 'DOCUMENT-FAILURE-SENTINEL\\n' >&2\nexit {_CHILD_EXIT_CONVERSION}\n",
        encoding="utf-8",
    )
    python.chmod(0o700)
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    with pytest.raises(ParserConversionError) as excinfo:
        _parse_sync(parser_env_dir, venv_dir, input_path, "doc-1")

    assert type(excinfo.value) is ParserConversionError
    assert str(excinfo.value) == "docling rejected the document"
    assert "DOCUMENT-FAILURE-SENTINEL" not in str(excinfo.value)


@pytest.mark.parametrize("exit_code", [1, 7, 69, 81, 255])
def test_unknown_child_exit_is_environment_not_document_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    parser_env_dir = tmp_path / "parser-env"
    venv_dir = tmp_path / "parser-venv"
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd, exit_code, "", "UNKNOWN-CHILD-DETAIL-MUST-NOT-LEAK"
        ),
    )

    with pytest.raises(ParserEnvironmentError) as excinfo:
        _parse_sync(parser_env_dir, venv_dir, input_path, "doc-1")

    assert type(excinfo.value) is ParserEnvironmentError
    assert str(excinfo.value) == "parser child runtime failed"
    assert "UNKNOWN-CHILD-DETAIL-MUST-NOT-LEAK" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("exit_code", "error_type", "message"),
    [
        (_CHILD_EXIT_DEPENDENCY, ParserDependencyError, "parser dependency unavailable"),
        (_CHILD_EXIT_POLICY, ParserPolicyError, "parser runtime policy refused execution"),
        (_CHILD_EXIT_CAPACITY, ParserCapacityError, "parser runtime capacity unavailable"),
        (_CHILD_EXIT_AVAILABILITY, ParserAvailabilityError, "parser runtime unavailable"),
        (_CHILD_EXIT_TIMEOUT, ParserTimeoutError, "parser runtime timed out"),
        (_CHILD_EXIT_INFERENCE, ParserInferenceError, "parser inference failed"),
        (_CHILD_EXIT_INTERNAL, ParserInternalError, "parser internal runtime failed"),
        (_CHILD_EXIT_TRANSPORT, ParserTransportError, "parser private transport failed"),
    ],
)
def test_enumerated_child_runtime_exit_maps_to_exact_typed_content_free_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    error_type: type[ParserEnvironmentError],
    message: str,
) -> None:
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd, exit_code, "", "CHILD-DETAIL-MUST-NOT-LEAK"
        ),
    )

    with pytest.raises(error_type) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is error_type
    assert str(excinfo.value) == message
    assert "CHILD-DETAIL-MUST-NOT-LEAK" not in str(excinfo.value)


def test_child_output_exit_maps_to_parser_output_without_stderr_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd, _CHILD_EXIT_OUTPUT, "", "OUTPUT-DETAIL-MUST-NOT-LEAK"
        ),
    )

    with pytest.raises(ParserOutputError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserOutputError
    assert str(excinfo.value) == "parser returned invalid document output"
    assert "OUTPUT-DETAIL-MUST-NOT-LEAK" not in str(excinfo.value)


def test_invalid_parser_output_remains_document_caused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser_env_dir = tmp_path / "parser-env"
    venv_dir = tmp_path / "parser-venv"
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _write_invalid_output(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _write_child_output(cmd, "not-json")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_invalid_output)

    with pytest.raises(ParserOutputError) as excinfo:
        _parse_sync(parser_env_dir, venv_dir, input_path, "doc-1")

    assert type(excinfo.value) is ParserOutputError
    assert str(excinfo.value) == "parser returned invalid document output"
    assert "not-json" not in str(excinfo.value)


def test_parse_sync_passes_the_preopened_private_output_fd_to_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _write_output(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output_fd = int(cmd[cmd.index("--output-fd") + 1])
        assert "--output" not in cmd
        assert kwargs["pass_fds"] == (output_fd,)
        opened = os.fstat(output_fd)
        assert stat.S_ISREG(opened.st_mode)
        assert stat.S_IMODE(opened.st_mode) == 0o600
        _write_child_output(cmd, _valid_parser_output())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_output)

    parsed = _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert parsed.document_id == "doc-1"
    assert not stage.exists()


def test_parse_sync_passes_only_inherited_input_and_output_fds_to_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    input_fd = os.open(input_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    inherited_output_fd: int | None = None

    def _write_output(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal inherited_output_fd
        output_fd = int(cmd[cmd.index("--output-fd") + 1])
        inherited_output_fd = output_fd
        assert "--input" not in cmd
        assert "--output" not in cmd
        assert cmd[cmd.index("--input-fd") + 1] == str(input_fd)
        assert cmd[cmd.index("--source-name") + 1] == "source.docx"
        assert kwargs["pass_fds"] == (input_fd, output_fd)
        _write_child_output(cmd, _valid_parser_output())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_output)

    try:
        parsed = _parse_sync(
            tmp_path / "env",
            tmp_path / "venv",
            input_path,
            "doc-1",
            input_fd=input_fd,
            source_name="source.docx",
        )
    finally:
        os.close(input_fd)

    assert parsed.document_id == "doc-1"
    assert not stage.exists()
    assert inherited_output_fd is not None
    with pytest.raises(OSError):
        os.fstat(inherited_output_fd)


def test_parse_fd_launch_failure_closes_owned_duplicate_but_not_caller_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"caller-owned")
    caller_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    submitted_fd: int | None = None

    def _fail_submit(
        _loop: asyncio.AbstractEventLoop,
        _parser_env_dir: Path,
        _venv_dir: Path,
        _source_name: str,
        _document_id: str,
        owned_fd: int,
    ) -> Never:
        nonlocal submitted_fd
        submitted_fd = owned_fd
        raise RuntimeError("executor unavailable")

    monkeypatch.setattr(docling_parser_module, "_submit_owned_fd_parse", _fail_submit)
    parser = DoclingParser(parser_env_dir=tmp_path / "env", venv_dir=tmp_path / "venv")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="executor unavailable"):
            await parser.parse_fd(
                caller_fd,
                document_id="doc-1",
                source_name="source.docx",
            )

    try:
        asyncio.run(scenario())
        assert submitted_fd is not None
        assert submitted_fd != caller_fd
        with pytest.raises(OSError):
            os.fstat(submitted_fd)
        assert os.fstat(caller_fd).st_ino == source.stat().st_ino
    finally:
        os.close(caller_fd)


def test_parse_fd_worker_failure_closes_owned_duplicate_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"caller-owned")
    caller_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    closed_fds: list[int] = []
    original_close_owned = docling_parser_module._close_owned_input_fd

    def _fail_parse(*_args: object, **_kwargs: object) -> Never:
        raise ParserEnvironmentError("worker failed")

    def _record_close(file_fd: int) -> None:
        original_close_owned(file_fd)
        closed_fds.append(file_fd)

    monkeypatch.setattr(docling_parser_module, "_parse_sync", _fail_parse)
    monkeypatch.setattr(docling_parser_module, "_close_owned_input_fd", _record_close)
    parser = DoclingParser(parser_env_dir=tmp_path / "env", venv_dir=tmp_path / "venv")

    async def scenario() -> None:
        with pytest.raises(ParserEnvironmentError, match="worker failed"):
            await parser.parse_fd(
                caller_fd,
                document_id="doc-1",
                source_name="source.docx",
            )

    try:
        asyncio.run(scenario())
        assert len(closed_fds) == 1
        assert closed_fds[0] != caller_fd
        with pytest.raises(OSError):
            os.fstat(closed_fds[0])
        assert os.fstat(caller_fd).st_ino == source.stat().st_ino
    finally:
        os.close(caller_fd)


def test_docling_preplanted_output_symlink_is_never_opened_or_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "outside-target"
    target.write_bytes(b"outside-bytes-must-not-change")
    target.chmod(0o600)
    before = hashlib.sha256(target.read_bytes()).digest()
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    original_capture = docling_parser_module._capture_temporary_root

    def _capture(path: Path) -> object:
        root = original_capture(path)
        (stage / "parsed.json").symlink_to(target)
        return root

    def _must_not_run(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("parser child must not run for a preplanted output symlink")

    monkeypatch.setattr(docling_parser_module, "_capture_temporary_root", _capture)
    monkeypatch.setattr(subprocess, "run", _must_not_run)

    with pytest.raises(ParserEnvironmentError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserEnvironmentError
    assert str(excinfo.value) == "parser runtime environment failed"
    assert hashlib.sha256(target.read_bytes()).digest() == before
    assert not stage.exists()


def test_docling_open_to_child_root_replacement_writes_only_bound_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    displaced = tmp_path / "displaced-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "outside-sentinel"
    sentinel.write_bytes(b"outside-bytes-must-not-change")
    before = hashlib.sha256(sentinel.read_bytes()).digest()
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _replace_root(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output_fd = int(cmd[cmd.index("--output-fd") + 1])
        assert kwargs["pass_fds"] == (output_fd,)
        stage.rename(displaced)
        stage.symlink_to(outside, target_is_directory=True)
        _write_child_output(cmd, _valid_parser_output())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _replace_root)

    with pytest.raises(ParserResidueError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserResidueError
    assert str(excinfo.value) == "parser temporary data cleanup incomplete"
    assert hashlib.sha256(sentinel.read_bytes()).digest() == before
    assert not (outside / "parsed.json").exists()
    assert stage.is_symlink()
    assert displaced.is_dir()
    bound_output = displaced / "parsed.json"
    assert bound_output.read_text(encoding="utf-8") == _valid_parser_output()
    assert stat.S_IMODE(bound_output.stat().st_mode) == 0o600


def test_non_owner_only_output_root_is_refused_before_child_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "output-stage"
    stage.mkdir(mode=0o755)
    stage.chmod(0o755)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kwargs: str(stage))
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _must_not_run(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("parser child must not run")

    monkeypatch.setattr(subprocess, "run", _must_not_run)

    with pytest.raises(ParserResidueError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserResidueError
    assert str(excinfo.value) == "parser temporary root identity invalid"
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize("drifted_mode", [0o755, 0o777])
def test_docling_capture_to_first_write_mode_drift_exposes_no_parsed_bytes(
    drifted_mode: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    original_open = os.open

    def _open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "parsed.json" or (isinstance(path, Path) and path.name == "parsed.json"):
            stage.chmod(drifted_mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def _must_not_run(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("parser child must not run after root mode drift")

    monkeypatch.setattr(os, "open", _open)
    monkeypatch.setattr(subprocess, "run", _must_not_run)

    with pytest.raises(ParserResidueError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserResidueError
    assert str(excinfo.value) == "parser temporary data cleanup incomplete"
    output = stage / "parsed.json"
    assert output.read_bytes() == b""
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(stage.stat().st_mode) == drifted_mode


def test_docling_capture_to_first_write_rename_never_writes_outside_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    displaced = tmp_path / "displaced-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "OUTSIDE-SENTINEL-MUST-SURVIVE"
    sentinel.write_text("safe", encoding="utf-8")
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    original_open = os.open

    def _open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "parsed.json" or (isinstance(path, Path) and path.name == "parsed.json"):
            stage.rename(displaced)
            stage.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def _must_not_run(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("parser child must not run after root replacement")

    monkeypatch.setattr(os, "open", _open)
    monkeypatch.setattr(subprocess, "run", _must_not_run)

    with pytest.raises(ParserResidueError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserResidueError
    assert str(excinfo.value) == "parser temporary data cleanup incomplete"
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert not (outside / "parsed.json").exists()
    output = displaced / "parsed.json"
    assert output.read_bytes() == b""
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_cleanup_failure_preserves_conversion_and_removes_output_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "output-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    _fail_first_rmdir(monkeypatch)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd, _CHILD_EXIT_CONVERSION, "", "CONVERSION-DETAIL-MUST-NOT-LEAK"
        ),
    )

    with pytest.raises(ParserConversionError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserConversionError
    assert str(excinfo.value) == "docling rejected the document"
    assert "CONVERSION-DETAIL-MUST-NOT-LEAK" not in str(excinfo.value)
    assert not stage.exists()


def test_cleanup_failure_preserves_output_error_and_removes_output_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "output-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    _fail_first_unlink(monkeypatch)

    def _write_invalid_output(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _write_child_output(cmd, "OUTPUT-DETAIL-MUST-NOT-LEAK")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_invalid_output)

    with pytest.raises(ParserOutputError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserOutputError
    assert str(excinfo.value) == "parser returned invalid document output"
    assert "OUTPUT-DETAIL-MUST-NOT-LEAK" not in str(excinfo.value)
    assert not stage.exists()


def test_cleanup_only_failure_is_environment_and_removes_output_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "output-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    _fail_first_unlink(monkeypatch)

    def _write_valid_output(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _write_child_output(
            cmd,
            '{"schema_version":"ne.parsed_document/1","document_id":"doc-1",'
            '"source_name":"source.docx","source_format":"docx","blocks":[],'
            '"metadata":{}}',
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_valid_output)

    with pytest.raises(ParserEnvironmentError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserEnvironmentError
    assert str(excinfo.value) == "parser output cleanup failed"
    assert not stage.exists()


@pytest.mark.parametrize("primary_failure", [False, True])
def test_unremovable_output_residue_supersedes_success_or_conversion_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_failure: bool,
) -> None:
    stage = tmp_path / "output-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)
    _block_all_deletion(monkeypatch)

    def _write_output(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _write_child_output(
            cmd,
            "PARSED-RESIDUE-MUST-NOT-LEAK"
            if primary_failure
            else '{"schema_version":"ne.parsed_document/1","document_id":"doc-1",'
            '"source_name":"source.docx","source_format":"docx","blocks":[],'
            '"metadata":{}}',
        )
        return subprocess.CompletedProcess(
            cmd, _CHILD_EXIT_CONVERSION if primary_failure else 0, "", ""
        )

    monkeypatch.setattr(subprocess, "run", _write_output)

    with pytest.raises(ParserResidueError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserResidueError
    assert isinstance(excinfo.value, ParserEnvironmentError)
    assert str(excinfo.value) == "parser temporary data cleanup incomplete"
    assert "MUST-NOT-LEAK" not in str(excinfo.value)
    assert (stage / "parsed.json").exists()


def _valid_parser_output() -> str:
    return (
        '{"schema_version":"ne.parsed_document/1","document_id":"doc-1",'
        '"source_name":"source.docx","source_format":"docx","blocks":[],'
        '"metadata":{}}'
    )


def test_replaced_docling_output_root_is_never_followed_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    displaced = tmp_path / "displaced-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "OUTSIDE-SENTINEL-MUST-SURVIVE"
    sentinel.write_text("safe", encoding="utf-8")
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _replace_root(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _write_child_output(cmd, _valid_parser_output())
        stage.rename(displaced)
        stage.symlink_to(outside, target_is_directory=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _replace_root)

    with pytest.raises(ParserResidueError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserResidueError
    assert str(excinfo.value) == "parser temporary data cleanup incomplete"
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert stage.is_symlink()
    assert (displaced / "parsed.json").exists()


def test_renamed_docling_output_root_is_cleaned_by_fd_but_never_reported_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    displaced = tmp_path / "displaced-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _rename_root(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _write_child_output(cmd, _valid_parser_output())
        stage.rename(displaced)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _rename_root)

    with pytest.raises(ParserResidueError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserResidueError
    assert str(excinfo.value) == "parser temporary data cleanup incomplete"
    assert not stage.exists()
    assert displaced.is_dir()
    assert list(displaced.iterdir()) == []


def test_docling_output_root_replacement_at_cleanup_guard_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    displaced = tmp_path / "displaced-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "OUTSIDE-SENTINEL-MUST-SURVIVE"
    sentinel.write_text("safe", encoding="utf-8")
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _write_output(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _write_child_output(cmd, _valid_parser_output())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_output)
    original_stat = os.stat
    guarded_calls = 0

    def _stat(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal guarded_calls
        if path == stage.name and dir_fd is not None and not follow_symlinks:
            guarded_calls += 1
            if guarded_calls == 3:
                stage.rename(displaced)
                stage.symlink_to(outside, target_is_directory=True)
        if dir_fd is None:
            return original_stat(path, follow_symlinks=follow_symlinks)
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", _stat)

    with pytest.raises(ParserResidueError):
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert guarded_calls >= 3
    assert sentinel.read_text(encoding="utf-8") == "safe"
    assert stage.is_symlink()
    assert (displaced / "parsed.json").exists()


def test_docling_output_root_rename_at_cleanup_guard_is_never_reported_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    displaced = tmp_path / "displaced-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _write_output(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _write_child_output(cmd, _valid_parser_output())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_output)
    original_stat = os.stat
    guarded_calls = 0

    def _stat(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal guarded_calls
        if path == stage.name and dir_fd is not None and not follow_symlinks:
            guarded_calls += 1
            if guarded_calls == 3:
                stage.rename(displaced)
        if dir_fd is None:
            return original_stat(path, follow_symlinks=follow_symlinks)
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", _stat)

    with pytest.raises(ParserResidueError):
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert guarded_calls >= 3
    assert not stage.exists()
    assert displaced.is_dir()
    assert list(displaced.iterdir()) == []


@pytest.mark.parametrize("drifted_mode", [0o755, 0o777])
def test_docling_output_root_mode_drift_is_refused_without_deleting_bytes(
    drifted_mode: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _write_output(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _write_child_output(cmd, _valid_parser_output())
        stage.chmod(drifted_mode)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_output)

    with pytest.raises(ParserResidueError) as excinfo:
        _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert type(excinfo.value) is ParserResidueError
    assert str(excinfo.value) == "parser temporary data cleanup incomplete"
    assert stat.S_IMODE(stage.stat().st_mode) == drifted_mode
    assert (stage / "parsed.json").exists()


def test_docling_fd_cleanup_removes_nested_files_and_link_not_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "output-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "OUTSIDE-SENTINEL-MUST-SURVIVE"
    sentinel.write_text("safe", encoding="utf-8")
    _use_stage(stage, monkeypatch)
    input_path = tmp_path / "source.docx"
    input_path.write_bytes(b"fixture")
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    def _write_nested_output(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _write_child_output(cmd, _valid_parser_output())
        nested = stage / "nested"
        nested.mkdir()
        (nested / "ordinary.txt").write_text("delete", encoding="utf-8")
        (stage / "outside-link").symlink_to(outside, target_is_directory=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _write_nested_output)

    parsed = _parse_sync(tmp_path / "env", tmp_path / "venv", input_path, "doc-1")

    assert parsed.document_id == "doc-1"
    assert not stage.exists()
    assert sentinel.read_text(encoding="utf-8") == "safe"
