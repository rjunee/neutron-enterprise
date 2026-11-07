# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-3 DoD (c): the cross-package LOCKSTEP invariant (design §2.3 / §3.3).

The load-bearing guarantee: any request the RUNTIME trim guard reduces to fit the
PRIMARY candidate's input budget is NEVER budget-pre-flight-rejected for that
primary by the gateway. Proven END-TO-END against the REAL ``LiteLLMGateway`` (its
per-candidate pre-flight arithmetic, not a re-implementation): seeded-random
histories run through ``GatewayModel(budget=...)`` over a recording fake gateway;
every CompletionRequest that did NOT raise is fed VERBATIM into
``LiteLLMGateway.complete`` (Router.acompletion monkeypatched to record the model
kwarg) — the PRIMARY is always the model actually called, and no
``ContextBudgetExceededError`` is raised.

The lockstep is EXACT because both layers derive the estimator params, overhead
margin, and effective ``max_tokens`` from the ONE ``GatewaySettings`` (the platform
threads them into ``RuntimeSettings`` — replicated here) and serialize the same
message model_dumps. KNOWN CAVEAT (design D3): a TaskRoute whose ``params`` carry
prompt-bearing keys (``tools``/``functions``) for the agent task would add mass the
runtime estimate cannot see and break exact lockstep — D3 forbids it and no
production agent route sets them; this test deliberately sets none.
"""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.messages import InstructionPart, ModelMessage
from pydantic_ai.models import ModelRequestParameters

from ne.contracts import Lane
from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.governance import RedactionResult
from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing  # isort: skip


def _make_model(name: str, *, window: int) -> ModelDef:
    return ModelDef(
        name=name,
        litellm_model="gpt-3.5-turbo",
        provider="openai",
        lane=Lane.FRONTIER_API,
        extra_params={"api_key": "test", "mock_response": "ok"},
        context_window=window,
    )


class _PassRedactor:
    def redact(self, text: str, *, categories: Any = None) -> RedactionResult:
        return RedactionResult(text=text)


class _CostSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


def _fake_ok() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        _hidden_params={"response_cost": 0.0},
        model="fake",
    )


def _random_history(rng: random.Random) -> list[ModelMessage]:
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )
    from pydantic_ai.usage import RequestUsage

    from .conftest import ScriptedGateway  # noqa: F401 - ensure package import side-effects

    def _resp_text(c: str) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart(content=c)],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
            model_name="p",
        )

    msgs: list[ModelMessage] = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="SYS " + "s" * rng.randint(0, 400)),
                UserPromptPart(content="T0 " + "u" * rng.randint(10, 1800)),
            ]
        ),
        _resp_text("a0"),
    ]
    for i in range(rng.randint(0, 4)):
        if rng.random() < 0.5:
            msgs.append(
                ModelRequest(parts=[UserPromptPart(content=f"U{i} " + "u" * rng.randint(10, 1600))])
            )
            msgs.append(
                ModelResponse(
                    parts=[ToolCallPart(tool_name="read.docs.search", args={"q": "z"})],
                    usage=RequestUsage(input_tokens=1, output_tokens=1),
                    model_name="p",
                )
            )
            msgs.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="read.docs.search", content="R" + "r" * rng.randint(10, 1600)
                        )
                    ]
                )
            )
            msgs.append(_resp_text(f"a{i}"))
        else:
            msgs.append(
                ModelRequest(parts=[UserPromptPart(content=f"U{i} " + "u" * rng.randint(10, 1200))])
            )
            msgs.append(_resp_text(f"a{i}"))
    # current turn (protected suffix)
    from pydantic_ai.messages import ModelRequest as MR

    msgs.append(MR(parts=[UserPromptPart(content="CURRENT " + "c" * rng.randint(0, 2000))]))
    return msgs


def test_runtime_trimmed_is_never_gateway_preflight_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_config_hash_for_testing()
    freeze_config_hash(RuntimeSettings())
    try:
        gs = GatewaySettings(
            models=[_make_model("primary", window=4096), _make_model("backup", window=200000)],
            tasks={"agent": TaskRoute(model="primary", fallbacks=("backup",))},
        )
        primary = gs.models[0]
        budget = gs.context_budget_for(primary)
        # Mirror the platform model_copy: estimator params + budget from the ONE gs.
        rt = RuntimeSettings(
            task="agent",
            bytes_per_token=gs.bytes_per_token,
            per_call_overhead=gs.per_call_overhead,
            context_budget=budget,
        )
        params = ModelRequestParameters(
            function_tools=[], instruction_parts=[InstructionPart(content="INSTR")]
        )

        from .conftest import ScriptedGateway  # a recording ModelGateway double

        rng = random.Random(12345)
        checked = 0
        for _ in range(120):
            rec_gw = ScriptedGateway()
            model = GatewayModel(
                rec_gw,
                rt,
                on_unknown_tool=_noop_hook,
                budget=budget,
                sink=_AuditSink(),
            )
            messages = _random_history(rng)
            try:
                asyncio.run(model.request(list(messages), None, params))
            except ContextBudgetExceededError:
                continue  # runtime refused ⇒ nothing egressed, nothing to check

            sent = rec_gw.requests[-1]
            assert sent.task == "agent"

            gw = LiteLLMGateway(gs, _CostSink(), redactor=_PassRedactor())
            captured: list[str] = []
            monkeypatch.setattr(gw._router, "acompletion", _recording_acompletion(captured))
            # Must NOT raise the pre-flight refusal, and the PRIMARY must be called.
            asyncio.run(gw.complete(sent))
            assert captured and captured[0] == "primary"
            checked += 1

        assert checked > 0, "at least some fuzzed requests fit and reached the gateway"
    finally:
        reset_config_hash_for_testing()


def _recording_acompletion(captured: list[str]) -> Any:
    """A Router.acompletion double that records the model kwarg each call (a factory
    so the closure binds an explicit list, not a loop variable)."""

    async def _fake(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.append(model)
        return _fake_ok()

    return _fake


async def _noop_hook(name: str, args: Any) -> None:  # pragma: no cover - unused
    return None


class _AuditSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> Any:
        from ne.contracts.governance import AuditVerification

        return AuditVerification(valid=True, checked=len(self.events))
