# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for `ne dev` (NE-006), with `subprocess.run` monkeypatched.

Mocking the compose invocation is sanctioned here ONLY because CI has no
docker daemon (orchestrator-granted exception, plan §1.4) — compose content
itself is validated statically in test_dev_stack.py.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from ne.cli import main

# NOTE: `import ne.cli.dev as dev_module` is unsafe here — ne/cli/__init__.py
# does `from ne.cli.dev import dev`, which (because the imported name matches
# the submodule name) rebinds the `dev` attribute on the `ne.cli` package to
# the click Group, shadowing the submodule for dotted "as" imports. Pulling
# the module straight out of sys.modules sidesteps that footgun.
dev_module = sys.modules["ne.cli.dev"]

REPO_ROOT = Path(__file__).resolve().parents[3]


class _Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], check: bool = False) -> types.SimpleNamespace:
        self.calls.append(list(cmd))
        return types.SimpleNamespace(returncode=self.returncode)


@pytest.fixture
def in_repo_root():
    cwd = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(cwd)


def _compose_file_str() -> str:
    return str(REPO_ROOT / "deploy" / "compose" / "dev" / "compose.yaml")


def test_dev_group_registered_alongside_existing() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "dev" in result.output
    assert "check" in result.output
    assert "release" in result.output
    assert "version" in result.output


def test_dev_up_invokes_compose(monkeypatch, in_repo_root) -> None:
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(dev_module.subprocess, "run", recorder)

    result = CliRunner().invoke(main, ["dev", "up"])

    assert result.exit_code == 0, result.output
    assert len(recorder.calls) == 1
    assert recorder.calls[0] == [
        "docker",
        "compose",
        "--project-name",
        "ne-dev",
        "-f",
        _compose_file_str(),
        "up",
        "--detach",
        "--wait",
    ]


def test_dev_up_langfuse_adds_profile(monkeypatch, in_repo_root) -> None:
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(dev_module.subprocess, "run", recorder)

    result = CliRunner().invoke(main, ["dev", "up", "--langfuse"])

    assert result.exit_code == 0, result.output
    argv = recorder.calls[0]
    profile_idx = argv.index("--profile")
    up_idx = argv.index("up")
    assert argv[profile_idx : profile_idx + 2] == ["--profile", "langfuse"]
    assert profile_idx < up_idx


def test_dev_down_always_includes_profile(monkeypatch, in_repo_root) -> None:
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(dev_module.subprocess, "run", recorder)

    result = CliRunner().invoke(main, ["dev", "down"])

    assert result.exit_code == 0, result.output
    argv = recorder.calls[0]
    assert "--profile" in argv
    assert "langfuse" in argv
    assert "down" in argv
    assert "--volumes" not in argv


def test_dev_down_volumes_flag(monkeypatch, in_repo_root) -> None:
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(dev_module.subprocess, "run", recorder)

    result = CliRunner().invoke(main, ["dev", "down", "--volumes"])

    assert result.exit_code == 0, result.output
    assert "--volumes" in recorder.calls[0]


def test_dev_ps_invokes_compose(monkeypatch, in_repo_root) -> None:
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(dev_module.subprocess, "run", recorder)

    result = CliRunner().invoke(main, ["dev", "ps"])

    assert result.exit_code == 0, result.output
    argv = recorder.calls[0]
    assert argv[-1] == "ps"
    assert "--profile" in argv


def test_dev_seed_runs_script(monkeypatch, in_repo_root) -> None:
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(dev_module.subprocess, "run", recorder)

    result = CliRunner().invoke(main, ["dev", "seed"])

    assert result.exit_code == 0, result.output
    expected_script = str(REPO_ROOT / "deploy" / "compose" / "dev" / "seed" / "seed.sh")
    assert recorder.calls[0] == ["bash", expected_script]


def test_dev_up_docker_missing_errors(monkeypatch, in_repo_root) -> None:
    def raise_not_found(cmd: list[str], check: bool = False):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(dev_module.subprocess, "run", raise_not_found)

    result = CliRunner().invoke(main, ["dev", "up"])

    assert result.exit_code == 1
    assert "docker not found" in result.output


def test_dev_up_nonzero_compose_exit_propagates(monkeypatch, in_repo_root) -> None:
    recorder = _Recorder(returncode=7)
    monkeypatch.setattr(dev_module.subprocess, "run", recorder)

    result = CliRunner().invoke(main, ["dev", "up"])

    assert result.exit_code != 0
    assert result.exit_code == 7


def test_dev_up_rebac_adds_profile(monkeypatch, in_repo_root) -> None:
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(dev_module.subprocess, "run", recorder)

    result = CliRunner().invoke(main, ["dev", "up", "--rebac"])

    assert result.exit_code == 0, result.output
    argv = recorder.calls[0]
    profiles = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--profile"]
    assert "rebac" in profiles
    assert argv.index("--profile") < argv.index("up")


def test_dev_down_and_ps_sweep_rebac(monkeypatch, in_repo_root) -> None:
    for sub in ("down", "ps"):
        recorder = _Recorder(returncode=0)
        monkeypatch.setattr(dev_module.subprocess, "run", recorder)
        result = CliRunner().invoke(main, ["dev", sub])
        assert result.exit_code == 0, result.output
        argv = recorder.calls[0]
        profiles = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--profile"]
        assert "langfuse" in profiles and "rebac" in profiles
