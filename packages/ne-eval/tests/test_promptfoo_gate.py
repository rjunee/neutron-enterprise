# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""promptfoo gate kit: config generation, anti-drift ratchet, pin, offline provider."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from ne.eval.promptfoo import (
    PROMPTFOO_ENV,
    PROMPTFOO_VERSION,
    build_promptfoo_config,
    check_promptfoo_config,
)
from ne.eval.thresholds import DEFAULT_THRESHOLDS

_PKG = Path(__file__).resolve().parent.parent
_COMMITTED = _PKG / "promptfoo" / "promptfooconfig.json"
_PROVIDER = _PKG / "promptfoo" / "gate_provider.py"
_RUN_GATE = _PKG / "promptfoo" / "run_gate.sh"


def test_one_assertion_per_threshold_at_bound() -> None:
    config = build_promptfoo_config(DEFAULT_THRESHOLDS)
    tests = config["tests"]
    assert isinstance(tests, list)
    assertions = tests[0]["assert"]
    assert len(assertions) == len(DEFAULT_THRESHOLDS)
    values = " ".join(a["value"] for a in assertions)
    assert 'metrics["recall_at_10"] >= 0.83' in values
    assert 'metrics["faithfulness"] >= 0.77' in values
    assert 'metrics["context_precision"] >= 0.67' in values
    assert 'metrics["context_recall"] >= 0.77' in values


def test_committed_config_matches_generated() -> None:
    assert check_promptfoo_config(_COMMITTED) is True


def test_drift_detected_on_tamper(tmp_path: Path) -> None:
    copy = tmp_path / "promptfooconfig.json"
    copy.write_text(_COMMITTED.read_text(encoding="utf-8") + "\n// tampered", encoding="utf-8")
    assert check_promptfoo_config(copy) is False


def test_version_pin() -> None:
    assert PROMPTFOO_VERSION == "0.121.17"


def test_env_disables_telemetry_and_update() -> None:
    assert PROMPTFOO_ENV["PROMPTFOO_DISABLE_TELEMETRY"] == "1"
    assert PROMPTFOO_ENV["PROMPTFOO_DISABLE_UPDATE"] == "1"


def test_run_gate_script_pins_and_disables() -> None:
    text = _RUN_GATE.read_text(encoding="utf-8")
    assert "promptfoo@0.121.17" in text
    assert "PROMPTFOO_DISABLE_TELEMETRY=1" in text
    assert "PROMPTFOO_DISABLE_UPDATE=1" in text


def _load_provider() -> object:
    spec = importlib.util.spec_from_file_location("gate_provider", _PROVIDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_provider_offline_returns_metrics_and_passed(
    monkeypatch: object,
) -> None:
    import os

    os.environ["NE_EVAL_GATE_MODE"] = "offline"
    try:
        module = _load_provider()
        result = module.call_api("rag", {}, {})  # type: ignore[attr-defined]
        payload = json.loads(result["output"])
        assert "metrics" in payload
        assert "passed" in payload
        # The offline provider must emit EVERY metric the committed config asserts,
        # so the default (offline) run_gate.sh invocation does not fail on missing
        # metrics (P1 fix). All four are green in the fixture.
        config = build_promptfoo_config(DEFAULT_THRESHOLDS)
        asserted = {spec.metric for spec in DEFAULT_THRESHOLDS}
        assert asserted <= set(payload["metrics"])
        for metric in asserted:
            assert payload["metrics"][metric] == 1.0
        assert payload["passed"] is True
        # Every asserted metric clears its gate binding (threshold - tolerance).
        for spec in DEFAULT_THRESHOLDS:
            assert payload["metrics"][spec.metric] >= spec.threshold - spec.tolerance
        assert isinstance(config, dict)
    finally:
        os.environ.pop("NE_EVAL_GATE_MODE", None)
