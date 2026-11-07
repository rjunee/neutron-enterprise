# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Offline regression tests for the NE-CKC6 fail-closed Checkpoint-C gate.

These run the REAL Checkpoint-C test modules as pytest subprocesses (no mocking
past the fixture seam, plan §1.4) under forced-offline / missing-asset env, and
prove all three semantics without any network:

* fail-closed: ``NE_REQUIRE_PUBLIC_BASELINE=1`` + an unavailable BM25 asset (or
  ragas venv) → the subprocess exits nonzero — the exact regression the NE-025
  audit found, where a missing asset could silently green the gate.
* skip-open default: flag unset + the same unavailable asset → exit 0 with an
  explicit ``skipped`` report (local-dev behaviour preserved).
* contradiction is loud: ``NE_REQUIRE_PUBLIC_BASELINE=1`` +
  ``NE_SKIP_RAGAS_ORACLE=1`` → hard failure (REQUIRE wins).

The forced-offline trick (``HF_HUB_OFFLINE=1`` + an empty ``FASTEMBED_CACHE_PATH``)
is verified locally to make ``SparseTextEmbedding('Qdrant/bm25')`` raise on
fastembed 0.8.0.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: Repo root (holds ``uv.lock``). Verified depth: tests/ -> ne-eval/ -> packages/
#: -> repo root == parents[3]. The assert fails loudly if the tree ever drifts.
_REPO_ROOT = Path(__file__).resolve().parents[3]
assert (_REPO_ROOT / "uv.lock").is_file(), f"repo-root depth drift: {_REPO_ROOT}"

_PUBLIC_BASELINE = "packages/ne-eval/tests/test_retrieval_baseline_public.py"
_RAGAS_ORACLE = "packages/ne-eval/tests/test_ragas_oracle.py"

#: Env vars the child must NOT inherit from the parent process (esp. when this
#: suite itself runs under CI with NE_REQUIRE_PUBLIC_BASELINE=1 set job-wide).
_CONTROLLED = (
    "NE_REQUIRE_PUBLIC_BASELINE",
    "NE_SKIP_RAGAS_ORACLE",
    "HF_HUB_OFFLINE",
    "FASTEMBED_CACHE_PATH",
    "NE_RAGAS_ENV_DIR",
    "NE_RAGAS_VENV_DIR",
)


def _run(
    target: str,
    env_overrides: dict[str, str],
    *,
    drop: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run ``pytest target`` in a subprocess with a controlled environment."""
    env = dict(os.environ)
    for key in (*_CONTROLLED, *drop):
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-rs"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=300,
    )


def test_public_baseline_fails_closed_when_required_and_asset_missing(tmp_path: Path) -> None:
    proc = _run(
        _PUBLIC_BASELINE,
        {
            "NE_REQUIRE_PUBLIC_BASELINE": "1",
            "HF_HUB_OFFLINE": "1",
            "FASTEMBED_CACHE_PATH": str(tmp_path / "empty_fastembed_cache"),
        },
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "NE_REQUIRE_PUBLIC_BASELINE=1" in proc.stdout, proc.stdout + proc.stderr


def test_public_baseline_skips_open_by_default(tmp_path: Path) -> None:
    proc = _run(
        _PUBLIC_BASELINE,
        {
            "HF_HUB_OFFLINE": "1",
            "FASTEMBED_CACHE_PATH": str(tmp_path / "empty_fastembed_cache"),
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The bm25-dependent tests skip; test_calibration_artifacts_agree_with_humans
    # still passes offline — so both outcomes appear (skip-if-unavailable intact).
    assert "skipped" in proc.stdout, proc.stdout + proc.stderr
    assert "passed" in proc.stdout, proc.stdout + proc.stderr


def test_ragas_oracle_fails_closed_when_required_and_venv_missing(tmp_path: Path) -> None:
    proc = _run(
        _RAGAS_ORACLE,
        {
            "NE_REQUIRE_PUBLIC_BASELINE": "1",
            "NE_RAGAS_ENV_DIR": str(tmp_path / "no_env"),
            "NE_RAGAS_VENV_DIR": str(tmp_path / "no_venv"),
        },
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "ragas dev venv unavailable" in proc.stdout, proc.stdout + proc.stderr


def test_ragas_oracle_contradictory_flags_fail(tmp_path: Path) -> None:
    proc = _run(
        _RAGAS_ORACLE,
        {
            "NE_REQUIRE_PUBLIC_BASELINE": "1",
            "NE_SKIP_RAGAS_ORACLE": "1",
            # env dirs pointed at tmp so no venv build is attempted; the SKIP
            # branch runs first anyway.
            "NE_RAGAS_ENV_DIR": str(tmp_path / "no_env"),
            "NE_RAGAS_VENV_DIR": str(tmp_path / "no_venv"),
        },
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
