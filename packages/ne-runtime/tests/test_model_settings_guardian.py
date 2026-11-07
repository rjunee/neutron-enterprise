# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-3 SPEC-row item 4: the ModelSettings key-set guardian + max_tokens honoring.

Pins the FULL pydantic-ai 2.5.0 ``ModelSettings`` annotation set so a NEW upstream
key is a LOUD supported-vs-ignored DECISION (the same silent-drop class as the
instructions bug), asserts ``SUPPORTED_MODEL_SETTINGS_KEYS == {'max_tokens'}`` with
every other key explicitly ENUMERATED as ignored (the two sets PARTITION the
annotations), and proves ``max_tokens`` is honored into the outbound params AND the
effective-reserve arithmetic (widening the input budget), while an oversized
``max_tokens`` collapses the budget to 0 (mirrors the gateway pre-flight), and an
ignored-only settings dict leaves ``params`` byte-identical to the legacy path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic_ai.messages import InstructionPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings

from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.frozen import FrozenDict
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import SUPPORTED_MODEL_SETTINGS_KEYS, GatewayModel

from .conftest import (
    RecordingAuditSink,
    ScriptedGateway,
    make_budget,
    req_user,
    text_completion,
)

# The EXACT pydantic-ai 2.5.0 ModelSettings key set (componentVerification). A bump
# that adds/removes a key trips this pin → a deliberate supported-vs-ignored review.
_PINNED_MODEL_SETTINGS_KEYS = sorted(
    [
        "extra_body",
        "extra_headers",
        "frequency_penalty",
        "logit_bias",
        "max_tokens",
        "parallel_tool_calls",
        "presence_penalty",
        "seed",
        "service_tier",
        "stop_sequences",
        "temperature",
        "thinking",
        "timeout",
        "tool_choice",
        "top_k",
        "top_p",
    ]
)
# Every key we DELIBERATELY ignore — the complement of SUPPORTED within the pin.
_IGNORED_KEYS = frozenset(_PINNED_MODEL_SETTINGS_KEYS) - SUPPORTED_MODEL_SETTINGS_KEYS


async def _noop_hook(name: str, args: Mapping[str, Any]) -> None:  # pragma: no cover - unused
    return None


def _params() -> ModelRequestParameters:
    return ModelRequestParameters(
        function_tools=[], instruction_parts=[InstructionPart(content="I")]
    )


def _run(model: GatewayModel, messages: list, settings: ModelSettings | None) -> Any:
    return asyncio.run(model.request(list(messages), settings, _params()))


def test_model_settings_annotation_set_is_pinned() -> None:
    actual = sorted(ModelSettings.__annotations__)
    assert actual == _PINNED_MODEL_SETTINGS_KEYS, (
        "pydantic-ai ModelSettings keys changed: a new/removed key must be a "
        "deliberate supported-vs-ignored decision (update SUPPORTED_MODEL_SETTINGS_KEYS "
        "and _IGNORED_KEYS), not a silent drop"
    )


def test_supported_and_ignored_partition_the_annotation_set() -> None:
    assert {"max_tokens"} == SUPPORTED_MODEL_SETTINGS_KEYS
    # The two sets are disjoint and together cover every pinned key.
    assert SUPPORTED_MODEL_SETTINGS_KEYS.isdisjoint(_IGNORED_KEYS)
    assert set(_PINNED_MODEL_SETTINGS_KEYS) == SUPPORTED_MODEL_SETTINGS_KEYS | _IGNORED_KEYS


def test_max_tokens_honored_into_params_budget_none() -> None:
    gw = ScriptedGateway([text_completion("done")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_noop_hook)
    _run(model, [req_user("go")], {"max_tokens": 64})
    assert gw.requests[-1].params == FrozenDict({"max_tokens": 64})


def test_max_tokens_widens_input_budget_where_default_raises() -> None:
    # A single over-budget request (nothing droppable). With the default reserve
    # (4096) the input budget is too small and the request RAISES; an explicit
    # max_tokens=64 widens the budget by ~4032 tokens and the SAME request fits.
    budget = make_budget(context_window=10000, max_output_tokens=4096, overhead_margin=1024)
    # input_budget(default reserve) = 10000-4096-1024 = 4880
    # input_budget(max_tokens=64)   = 10000-64-1024   = 8912
    messages = [req_user("Q " + "x" * 8000)]  # ~5.3k tokens: between the two budgets

    gw_default = ScriptedGateway([text_completion("done")])
    m_default = GatewayModel(
        gw_default,
        RuntimeSettings(),
        on_unknown_tool=_noop_hook,
        budget=budget,
        sink=RecordingAuditSink(),
    )
    with pytest.raises(ContextBudgetExceededError):
        _run(m_default, messages, None)

    gw_explicit = ScriptedGateway([text_completion("done")])
    m_explicit = GatewayModel(
        gw_explicit,
        RuntimeSettings(),
        on_unknown_tool=_noop_hook,
        budget=budget,
        sink=RecordingAuditSink(),
    )
    _run(m_explicit, messages, {"max_tokens": 64})
    assert gw_explicit.requests  # it fit and reached the gateway
    assert gw_explicit.requests[-1].params == FrozenDict({"max_tokens": 64})


def test_oversized_max_tokens_collapses_budget_to_zero() -> None:
    # max_tokens >= window - margin => input_budget == 0 => any prompt refuses
    # (mirrors the gateway test_explicit_oversized_max_tokens_shrinks_budget_into_skip).
    budget = make_budget(context_window=10000, max_output_tokens=4096, overhead_margin=1024)
    gw = ScriptedGateway([text_completion("done")])
    model = GatewayModel(
        gw, RuntimeSettings(), on_unknown_tool=_noop_hook, budget=budget, sink=RecordingAuditSink()
    )
    with pytest.raises(ContextBudgetExceededError) as ei:
        _run(model, [req_user("small")], {"max_tokens": 9000})
    assert ei.value.input_budget == 0
    assert gw.requests == []


def test_ignored_only_settings_leave_params_byte_identical() -> None:
    gw = ScriptedGateway([text_completion("done")])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_noop_hook)
    _run(model, [req_user("go")], {"temperature": 0.5, "top_p": 0.9, "seed": 7})
    assert gw.requests[-1].params == FrozenDict()
