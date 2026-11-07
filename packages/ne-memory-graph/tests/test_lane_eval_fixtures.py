# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Per-lane eval harness in --fixtures-only mode (R1): schema + deterministic scoring.

The evals package is not importable (it has no __init__ and lives outside src), so
we load ``evals/lane_eval.py`` by path — the CI contract is that fixtures validate
and scoring is deterministic; live per-lane quality/cost numbers are LIVE-PARKABLE.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_lane_eval():
    path = Path(__file__).resolve().parents[1] / "evals" / "lane_eval.py"
    spec = importlib.util.spec_from_file_location("ne_memory_graph_lane_eval", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixtures_only_scoring_is_deterministic() -> None:
    lane_eval = _load_lane_eval()
    a = lane_eval.run_fixtures_only()
    b = lane_eval.run_fixtures_only()
    assert a == b  # deterministic
    # Recorded frontier output covers a strict subset of expected → recall in (0,1].
    assert 0.0 < a.entity_recall <= 1.0
    assert 0.0 < a.fact_recall <= 1.0
    # Precision is 1.0 (every recorded item is a true positive in the fixture).
    assert a.entity_precision == 1.0
    assert a.fact_precision == 1.0
    # Per-event cost is captured.
    assert a.prompt_tokens > 0
    assert a.cost_usd > 0.0


def test_schema_validation_rejects_bad_fixtures() -> None:
    lane_eval = _load_lane_eval()
    try:
        lane_eval.validate_fixture_schema([], {"entities": [], "facts": []})
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("empty episodes must be rejected")


def test_main_fixtures_only_returns_zero() -> None:
    lane_eval = _load_lane_eval()
    assert lane_eval.main(["--fixtures-only"]) == 0
