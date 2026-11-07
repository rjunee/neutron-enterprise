# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PresidioRedactor: env resolution + subprocess JSON protocol (BELOW the venv seam).

The heavy real Presidio path is live-parked (test_presidio_live.py). Here the
subprocess protocol is exercised end-to-end against a FAKE in-venv worker: a tmp
venv whose ``bin/python`` symlinks to the current interpreter, a preset
requirements-sha marker so ``ensure_env_at`` skips the (network) rebuild, and a
stdlib fake ``redact_text.py`` that honours the JSON contract.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from ne.govern.errors import RedactionError
from ne.govern.redaction.presidio_backend import (
    _ENV_DIR_OVERRIDE_VAR,
    _VENV_DIR_OVERRIDE_VAR,
    PresidioRedactor,
)

# --- env-dir resolution (mirrors test_docling_parser_env_resolution.py) ----


def test_root_kwarg_resolves_standard_layout(tmp_path: Path) -> None:
    r = PresidioRedactor(tmp_path)
    presidio_env_dir, venv_dir = r._resolved_dirs()
    assert presidio_env_dir == tmp_path / "packages" / "ne-govern" / "presidio_env"
    assert venv_dir == tmp_path / "packages" / "ne-govern" / ".presidio-venv"


def test_explicit_dirs_bypass_repo_root_discovery(tmp_path: Path) -> None:
    env_dir = tmp_path / "vendored" / "presidio_env"
    venv_dir = tmp_path / "state" / "presidio-venv"
    r = PresidioRedactor(presidio_env_dir=env_dir, venv_dir=venv_dir)
    resolved_env_dir, resolved_venv_dir = r._resolved_dirs()
    assert resolved_env_dir == env_dir
    assert resolved_venv_dir == venv_dir


def test_explicit_env_dir_defaults_venv_as_sibling(tmp_path: Path) -> None:
    env_dir = tmp_path / "vendored" / "presidio_env"
    r = PresidioRedactor(presidio_env_dir=env_dir)
    _, resolved_venv_dir = r._resolved_dirs()
    assert resolved_venv_dir == env_dir.parent / ".presidio-venv"


def test_env_var_overrides_resolve_without_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "opt" / "presidio_env"
    venv_dir = tmp_path / "var" / "presidio-venv"
    monkeypatch.setenv(_ENV_DIR_OVERRIDE_VAR, str(env_dir))
    monkeypatch.setenv(_VENV_DIR_OVERRIDE_VAR, str(venv_dir))
    r = PresidioRedactor()
    resolved_env_dir, resolved_venv_dir = r._resolved_dirs()
    assert resolved_env_dir == env_dir
    assert resolved_venv_dir == venv_dir


def test_construction_is_boot_safe_when_no_repo_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NE-162 continuation: the exact air-gap boot regression.

    ``ne-api`` wires ``create_redactor`` unconditionally at the composition
    root, so constructing a ``PresidioRedactor`` must never raise even when no
    ``uv.lock`` is discoverable (a production/air-gap install with no repo
    checkout). Proven here by a RECORDING raiser: if ``__init__`` ever walked
    for the repo root, the counter would be nonzero.
    """
    monkeypatch.delenv(_ENV_DIR_OVERRIDE_VAR, raising=False)
    monkeypatch.delenv(_VENV_DIR_OVERRIDE_VAR, raising=False)
    import ne.govern.redaction.presidio_backend as mod

    calls = {"count": 0}

    def _never_found(start: Path) -> Path:
        calls["count"] += 1
        raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")

    monkeypatch.setattr(mod, "_find_repo_root", _never_found)

    PresidioRedactor()  # must not raise

    assert calls["count"] == 0


def test_redact_fails_closed_with_actionable_error_when_no_repo_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ENV_DIR_OVERRIDE_VAR, raising=False)
    monkeypatch.delenv(_VENV_DIR_OVERRIDE_VAR, raising=False)
    import ne.govern.redaction.presidio_backend as mod

    calls = {"count": 0}

    def _never_found(start: Path) -> Path:
        calls["count"] += 1
        raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")

    monkeypatch.setattr(mod, "_find_repo_root", _never_found)

    r = PresidioRedactor()
    with pytest.raises(RedactionError) as exc:
        r.redact("anything")
    message = str(exc.value)
    assert _ENV_DIR_OVERRIDE_VAR in message
    assert _VENV_DIR_OVERRIDE_VAR in message
    assert "presidio_env_dir" in message
    assert calls["count"] == 1


# --- subprocess JSON protocol via a FAKE worker ----------------------------

_FAKE_WORKER = """
import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--input", required=True, type=Path)
p.add_argument("--output", required=True, type=Path)
a = p.parse_args()
req = json.loads(a.input.read_text())
# Echo a deterministic redaction proving the round-trip.
a.output.write_text(json.dumps({"text": "<REDACTED:PERSON> was here",
                                "redactions": ["EMAIL_ADDRESS", "PERSON"]}))
"""

_FAILING_WORKER = """
import sys
print("boom: presidio model failed to load", file=sys.stderr)
sys.exit(2)
"""


def _fake_env(tmp_path: Path, worker_src: str) -> tuple[Path, Path]:
    """Build a fake (presidio_env_dir, venv_dir) that skips the real rebuild."""
    env_dir = tmp_path / "presidio_env"
    env_dir.mkdir()
    (env_dir / "requirements.txt").write_text("presidio-analyzer==2.2.363\n")
    (env_dir / "redact_text.py").write_text(worker_src)

    venv_dir = tmp_path / ".presidio-venv"
    (venv_dir / "bin").mkdir(parents=True)
    py = venv_dir / "bin" / "python"
    py.symlink_to(sys.executable)
    # Preset the sha marker so ensure_env_at treats the env as already built.
    want = hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()
    (venv_dir / ".requirements.sha256").write_text(want)
    # No chmod: ``sys.executable`` is by definition executable, and the symlink
    # inherits its target's mode. A ``chmod(py.resolve(), ...)`` here would
    # RESOLVE THE SYMLINK and land on the host interpreter running this suite —
    # outside tmp_path entirely. On this box ``resolve()`` chains through the
    # venv to the SHARED uv-managed toolchain
    # (``~/.local/share/uv/python/cpython-*/bin/python3.13``), which every
    # project and worktree on the machine uses. On a read-only toolchain that
    # raises OSError (EROFS); on a writable one it silently mutates the host's
    # permission bits and the test still passes — the silent case being the
    # worse of the two. Nothing in this module may touch a path outside
    # ``tmp_path``.
    return env_dir, venv_dir


def test_subprocess_round_trip_into_redaction_result(tmp_path: Path) -> None:
    env_dir, venv_dir = _fake_env(tmp_path, _FAKE_WORKER)
    redactor = PresidioRedactor(presidio_env_dir=env_dir, venv_dir=venv_dir)
    result = redactor.redact(
        "Alice emailed alice@example.com", categories=["PERSON", "EMAIL_ADDRESS"]
    )
    assert result.text == "<REDACTED:PERSON> was here"
    assert result.redactions == ("EMAIL_ADDRESS", "PERSON")


def test_worker_nonzero_exit_raises_redaction_error_with_stderr(tmp_path: Path) -> None:
    env_dir, venv_dir = _fake_env(tmp_path, _FAILING_WORKER)
    redactor = PresidioRedactor(presidio_env_dir=env_dir, venv_dir=venv_dir)
    with pytest.raises(RedactionError) as exc:
        redactor.redact("anything")
    assert "boom: presidio model failed to load" in str(exc.value)


def test_env_dirs_resolved_once_and_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir, venv_dir = _fake_env(tmp_path, _FAKE_WORKER)
    import ne.govern.redaction.presidio_backend as mod

    calls = {"count": 0}
    real_resolve = mod._resolve_env_dirs

    def _counting_resolve(**kwargs: object) -> tuple[Path, Path]:
        calls["count"] += 1
        return real_resolve(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mod, "_resolve_env_dirs", _counting_resolve)
    redactor = PresidioRedactor(presidio_env_dir=env_dir, venv_dir=venv_dir)
    redactor.redact("first")
    redactor.redact("second")
    assert calls["count"] == 1


def test_create_redactor_default_backend_constructs_without_repo_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition-root proof: create_redactor(GovernSettings()) with the
    default 'presidio' backend must construct boot-safe even when no repo
    checkout is discoverable; the resulting RedactionStage passes
    RedactionError through unwrapped at actual redact() time.
    """
    monkeypatch.delenv(_ENV_DIR_OVERRIDE_VAR, raising=False)
    monkeypatch.delenv(_VENV_DIR_OVERRIDE_VAR, raising=False)
    import ne.govern.redaction.presidio_backend as mod

    def _never_found(start: Path) -> Path:
        raise RuntimeError(f"could not locate repo root (uv.lock) above {start}")

    monkeypatch.setattr(mod, "_find_repo_root", _never_found)

    from ne.govern import create_redactor
    from ne.govern.config import GovernSettings

    stage = create_redactor(GovernSettings())  # must not raise

    with pytest.raises(RedactionError):
        stage.redact("anything")


def test_sidecar_venv_root_resolves_writable_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV: with NE_GOVERN_PRESIDIO_ENV_DIR + NE_SIDECAR_VENV_ROOT set (no
    NE_GOVERN_PRESIDIO_VENV_DIR), the venv resolves under the writable root."""
    env_dir = tmp_path / "opt" / "presidio_env"
    root = tmp_path / "writable"
    monkeypatch.setenv(_ENV_DIR_OVERRIDE_VAR, str(env_dir))
    monkeypatch.delenv(_VENV_DIR_OVERRIDE_VAR, raising=False)
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(root))

    r = PresidioRedactor()
    resolved_env_dir, resolved_venv_dir = r._resolved_dirs()

    assert resolved_env_dir == env_dir
    assert resolved_venv_dir == root / ".presidio-venv"


def test_presidio_venv_dir_var_beats_sidecar_venv_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV: the specific NE_GOVERN_PRESIDIO_VENV_DIR beats the generic
    NE_SIDECAR_VENV_ROOT (specific-beats-generic precedence)."""
    env_dir = tmp_path / "opt" / "presidio_env"
    venv_dir = tmp_path / "var" / "presidio-venv"
    monkeypatch.setenv(_ENV_DIR_OVERRIDE_VAR, str(env_dir))
    monkeypatch.setenv(_VENV_DIR_OVERRIDE_VAR, str(venv_dir))
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(tmp_path / "writable"))

    r = PresidioRedactor()
    _, resolved_venv_dir = r._resolved_dirs()

    assert resolved_venv_dir == venv_dir
