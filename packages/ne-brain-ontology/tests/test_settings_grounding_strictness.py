# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-HGATE-3: the two grounding-strictness knobs on OntologySettings + the
invalid ``ng1-frozen + standard`` combo validator (design §7 / D-P1-1).

Proves: defaults (strict + ng2), env binding for both NE_ONTOLOGY_GROUNDING_*
vars, the Literal type rejecting a third tier value, the combo fail-loud via
BOTH the constructor and env, and that every LEGAL combo constructs and echoes.
"""

from __future__ import annotations

import os
from typing import Literal

import pytest
from pydantic import ValidationError

from ne.brain_ontology.config import OntologySettings

_Tier = Literal["strict", "standard"]
_Profile = Literal["ng1-frozen", "ng2"]


@pytest.fixture(autouse=True)
def _scrub_ne_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ambient NE_* config so a developer's env never leaks into these tests."""
    for name in [k for k in os.environ if k.startswith("NE_")]:
        monkeypatch.delenv(name)


def test_defaults() -> None:
    s = OntologySettings()
    assert s.grounding_strictness == "strict"
    assert s.grounding_gate_profile == "ng2"


def test_env_binds_strictness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_ONTOLOGY_GROUNDING_STRICTNESS", "standard")
    s = OntologySettings()
    assert s.grounding_strictness == "standard"
    assert s.grounding_gate_profile == "ng2"


def test_env_binds_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_ONTOLOGY_GROUNDING_GATE_PROFILE", "ng1-frozen")
    s = OntologySettings()
    assert s.grounding_gate_profile == "ng1-frozen"
    assert s.grounding_strictness == "strict"


def test_invalid_tier_value_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Literal-typed, no third tier — design A-P1-5 (LENIENT was rejected for v1).
    monkeypatch.setenv("NE_ONTOLOGY_GROUNDING_STRICTNESS", "lenient")
    with pytest.raises(ValidationError):
        OntologySettings()


def test_combo_ng1_frozen_standard_rejected_constructor() -> None:
    with pytest.raises(ValidationError, match="ng1-frozen") as excinfo:
        OntologySettings(grounding_strictness="standard", grounding_gate_profile="ng1-frozen")
    message = str(excinfo.value)
    assert "NE_ONTOLOGY_GROUNDING_GATE_PROFILE" in message
    assert "NE_ONTOLOGY_GROUNDING_STRICTNESS" in message


def test_combo_ng1_frozen_standard_rejected_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_ONTOLOGY_GROUNDING_STRICTNESS", "standard")
    monkeypatch.setenv("NE_ONTOLOGY_GROUNDING_GATE_PROFILE", "ng1-frozen")
    with pytest.raises(ValidationError, match="ng1-frozen"):
        OntologySettings()


def test_all_legal_combos_construct() -> None:
    combos: list[tuple[_Tier, _Profile]] = [
        ("strict", "ng2"),
        ("strict", "ng1-frozen"),
        ("standard", "ng2"),
    ]
    for strictness, profile in combos:
        s = OntologySettings(grounding_strictness=strictness, grounding_gate_profile=profile)
        assert s.grounding_strictness == strictness
        assert s.grounding_gate_profile == profile


# --- NE-GA-RUN1: grounded_run_agent_ids -------------------------------------


def test_grounded_run_agent_ids_default_empty() -> None:
    assert OntologySettings().grounded_run_agent_ids == ()


def test_grounded_run_agent_ids_env_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "NE_ONTOLOGY_GROUNDED_RUN_AGENT_IDS",
        "agt_01k3meridiananalyst000000000, agt_01k3hardshipreview0000000000,,",
    )
    assert OntologySettings().grounded_run_agent_ids == (
        "agt_01k3meridiananalyst000000000",
        "agt_01k3hardshipreview0000000000",
    )


# --- NE-GA-RUN4: the Run-path strictness knob (U11 fix F1) -------------------
#
# The Run gate previously INHERITED the chat tier: a deployment at chat-standard
# silently ran its gated Run agents at standard too, where a digit-free
# empty-corpus draft passes with only an advisory finding — falsifying the
# "empty-corpus turns always withhold" record. The Run path now has its own
# deployment knob, defaulting to STRICT so a chat-posture choice can never
# loosen the Run gate silently.


def test_grounding_run_strictness_defaults_strict() -> None:
    assert OntologySettings().grounding_run_strictness == "strict"


def test_grounding_run_strictness_default_is_independent_of_chat_tier() -> None:
    # The deployed demo posture: chat standard, Run strict — legal, and the
    # Run tier must NOT follow the chat knob down.
    s = OntologySettings(grounding_strictness="standard")
    assert s.grounding_run_strictness == "strict"


def test_env_binds_run_strictness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS", "standard")
    s = OntologySettings()
    assert s.grounding_run_strictness == "standard"
    assert s.grounding_strictness == "strict"


def test_invalid_run_tier_value_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS", "lenient")
    with pytest.raises(ValidationError):
        OntologySettings()


def test_combo_ng1_frozen_run_standard_rejected() -> None:
    # Same D-P1-1 rationale as the chat combo: the frozen ng-1 rules cannot
    # classify fact-sentences, so 'standard' would tier fact-adjacent
    # vocabulary on the Run path too.
    with pytest.raises(ValidationError, match="ng1-frozen"):
        OntologySettings(grounding_run_strictness="standard", grounding_gate_profile="ng1-frozen")
