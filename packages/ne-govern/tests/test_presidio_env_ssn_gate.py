# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Always-on unit tests for the pure stdlib helpers in presidio_env/redact_text.py.

These test the deterministic SSN context gate + patterns BELOW the presidio seam
(NE-CKC11). ``redact_text.py`` imports presidio at module level (it normally runs
inside the isolated hash-pinned venv), so the module is loaded here with minimal
stand-in modules injected into ``sys.modules`` solely to reach the pure helpers
``_ssn_context_nearby`` and ``_NE_SSN_PATTERNS``. The REAL presidio seam is
covered by test_presidio_live.py under NE_PRESIDIO_LIVE=1 (per plan §1.4 — mock
freely below the seam).

Brittleness note: this loads redact_text.py under sys.modules stand-ins; if a
later unit adds new top-level presidio imports to redact_text.py, the stub
fixture below needs a matching attribute.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path
from types import ModuleType

import pytest

_REDACT_TEXT = Path(__file__).resolve().parents[1] / "presidio_env" / "redact_text.py"


@pytest.fixture()
def redact_text_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    pa = types.ModuleType("presidio_analyzer")

    class Pattern:
        def __init__(self, name: object, regex: object, score: object) -> None:
            self.name, self.regex, self.score = name, regex, score

    class PatternRecognizer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class AnalyzerEngine:
        pass

    class RecognizerResult:
        pass

    pa.Pattern = Pattern  # type: ignore[attr-defined]
    pa.PatternRecognizer = PatternRecognizer  # type: ignore[attr-defined]
    pa.AnalyzerEngine = AnalyzerEngine  # type: ignore[attr-defined]
    pa.RecognizerResult = RecognizerResult  # type: ignore[attr-defined]

    pan = types.ModuleType("presidio_anonymizer")

    class AnonymizerEngine:
        pass

    pan.AnonymizerEngine = AnonymizerEngine  # type: ignore[attr-defined]

    pane = types.ModuleType("presidio_anonymizer.entities")

    class OperatorConfig:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    pane.OperatorConfig = OperatorConfig  # type: ignore[attr-defined]
    pan.entities = pane  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "presidio_analyzer", pa)
    monkeypatch.setitem(sys.modules, "presidio_anonymizer", pan)
    monkeypatch.setitem(sys.modules, "presidio_anonymizer.entities", pane)

    spec = importlib.util.spec_from_file_location(
        "ne_redact_text_ssn_gate_under_test", _REDACT_TEXT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_context_gate_accepts_ssn_keyword_before_match(redact_text_module: ModuleType) -> None:
    text = "his SSN is 123-45-6789"
    assert redact_text_module._ssn_context_nearby(text, text.index("123")) is True


def test_context_gate_accepts_social_security_phrase(redact_text_module: ModuleType) -> None:
    text = "social security number: 123.45.6789"
    assert redact_text_module._ssn_context_nearby(text, text.index("123")) is True


def test_context_gate_rejects_text_without_context(redact_text_module: ModuleType) -> None:
    text = "Tracking ref 123-45-6789 was closed"
    assert redact_text_module._ssn_context_nearby(text, text.index("123")) is False


def test_context_gate_rejects_ssid_network_identifier(redact_text_module: ModuleType) -> None:
    # 'SSID' (WiFi network identifier) is NOT an SSN context word — must not
    # trigger SSN redaction of a delimited number in a network-config line
    # (Argus P3 / Codex P2: 'ssid' was wrongly in the keyword list).
    text = "SSID: 123-45-6789 is the guest network"
    assert redact_text_module._ssn_context_nearby(text, text.index("123")) is False


def test_context_gate_rejects_context_beyond_window(redact_text_module: ModuleType) -> None:
    text = "SSN" + ("x" * 70) + "123-45-6789"
    assert redact_text_module._ssn_context_nearby(text, text.index("123")) is False


def test_context_gate_rejects_context_only_after_match(redact_text_module: ModuleType) -> None:
    # Prefix-only by design (mirrors presidio's prefix-only enhancer).
    text = "123-45-6789 is his SSN"
    assert redact_text_module._ssn_context_nearby(text, 0) is False


def test_patterns_match_dashed_spaced_dotted(redact_text_module: ModuleType) -> None:
    patterns = redact_text_module._NE_SSN_PATTERNS
    compiled = [re.compile(p.regex) for p in patterns]
    for value in ("123-45-6789", "123 45 6789", "123.45.6789"):
        hits = [c for c in compiled if c.search(value)]
        assert len(hits) == 1
    for p in patterns:
        assert p.score == 0.9


def test_patterns_reject_undelimited_and_mixed_forms(redact_text_module: ModuleType) -> None:
    compiled = [re.compile(p.regex) for p in redact_text_module._NE_SSN_PATTERNS]
    for value in ("123456789", "123-45 6789", "123.45-6789", "1234-56-789"):
        assert not any(c.search(value) for c in compiled)
