# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CLI exit-code tests (main([...]) invoked directly)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ne.eval.cli import main

_PKG = Path(__file__).resolve().parent.parent
_COMMITTED = _PKG / "promptfoo" / "promptfooconfig.json"
_FIXTURE_CAL = Path(__file__).resolve().parent / "fixtures" / "calibration"


def test_validate_golden_ok(sample_golden_path: Path) -> None:
    assert main(["validate-golden", str(sample_golden_path)]) == 0


def test_validate_golden_bad(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    assert main(["validate-golden", str(bad)]) == 1


def test_hash_golden_prints_hex(
    sample_golden_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["hash-golden", str(sample_golden_path)]) == 0
    out = capsys.readouterr().out.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", out)


def test_promptfoo_config_check_ok() -> None:
    assert main(["promptfoo-config", "--check", "--path", str(_COMMITTED)]) == 0


def test_promptfoo_config_check_drift(tmp_path: Path) -> None:
    copy = tmp_path / "promptfooconfig.json"
    copy.write_text(_COMMITTED.read_text(encoding="utf-8") + "x", encoding="utf-8")
    assert main(["promptfoo-config", "--check", "--path", str(copy)]) == 1


def test_check_calibration_ok() -> None:
    code = main(
        [
            "check-calibration",
            "--dir",
            str(_FIXTURE_CAL),
            "--rubric",
            "context_precision",
            "--judge-model",
            "qwen3.5-judge",
        ]
    )
    assert code == 0


def test_check_calibration_missing_judge() -> None:
    code = main(
        [
            "check-calibration",
            "--dir",
            str(_FIXTURE_CAL),
            "--rubric",
            "context_precision",
            "--judge-model",
            "nonexistent-judge",
        ]
    )
    assert code == 1


def test_run_delegates_to_worker_composition_and_preserves_exit_code(monkeypatch) -> None:
    seen: list[list[str]] = []

    def call(command: list[str]) -> int:
        seen.append(command)
        return 1

    monkeypatch.setattr("ne.eval.cli.subprocess.call", call)
    code = main(
        [
            "run",
            "--principal-id",
            "user:operator",
            "--suite",
            "meridian-route-a",
            "--golden",
            "/inputs/golden.jsonl",
        ]
    )

    assert code == 1
    assert seen == [
        [
            __import__("sys").executable,
            "-m",
            "ne.worker",
            "eval-run",
            "--principal-id",
            "user:operator",
            "--suite",
            "meridian-route-a",
            "--golden",
            "/inputs/golden.jsonl",
        ]
    ]
