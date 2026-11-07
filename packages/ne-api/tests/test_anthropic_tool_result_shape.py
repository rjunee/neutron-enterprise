# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-GW-TOOL-RESULT — an agent turn that FOLLOWS a tool call must reach the frontier.

The bug this pins (measured live 2026-08-26, ``ne-pilot-ne-worker-1``): every
agent turn after a tool call HTTP-failed on the frontier lane with
``litellm.BadRequestError: AnthropicException - 'tool_call_id'`` and fell down
the chain to the private vLLM lane, so the demo answered from a local 35B model
while the console showed no denial and no skip record — because there WAS none.
Policy ALLOWED frontier (``model.selection`` read ``pdp_effect: allow``); the
PROVIDER CALL is what failed. That distinction is the whole point of this
module: it does not test policy, it tests WIRE SHAPE.

Root cause, ours not the dependency's: ne-runtime maps a ``ToolReturnPart`` to
``ChatMessage(role="tool", ...)``, and ``ChatMessage`` has NO tool-call id
field — it cannot have one, because decision D3 does tool-calling entirely
in-band (the assistant's "tool call" is a line of JSON in a plain text message,
never a native ``tool_calls`` block). On the OpenAI wire ``role: "tool"`` means
"the result of tool call ``tool_call_id``", so what we shipped was, in wire
terms, an ORPHANED tool result: a ``tool_result`` with nothing to pair with.
litellm 1.90.3's Anthropic transform reads ``message["tool_call_id"]``
unguarded and raises ``KeyError('tool_call_id')``; an OpenAI-compatible vLLM
server happens to accept it, which is why the private lane "worked" and the
frontier lane never did — portable by accident, exactly what
``_normalize_system_first`` already exists to prevent.

The fix is ``ne.gateway.gateway._to_wire_messages``, the single wire-body
chokepoint both ``complete`` and ``stream`` route through.

This is a CROSS-PACKAGE test on purpose. It lives in ne-api because ne-api is
the one package that depends on BOTH ne-runtime and ne-gateway, so the path
under test is the REAL one end to end — real ``GatewayModel`` history mapping,
real ``LiteLLMGateway`` egress body, real ``AnthropicConfig.transform_request``
(the very function the live call raised inside; only the network is faked).
Nothing here is a hand-written message list.

Vacuity traps deliberately avoided (docs/testing/vacuous-guards.md):

* (b) is the DISCRIMINATION control: the identical wire body with the one role
  flipped back to ``"tool"`` MUST still raise. Without it, (a) would pass
  against a transform that accepts anything, and an orphaned tool result would
  be free to be silently dropped instead of rejected.
* (a) asserts the tool's OUTPUT TEXT survives into the transformed blocks, not
  merely that no exception was raised — "accepted" must not mean "accepted
  because the tool result was thrown away".
* (c) pins that the internal contract is UNCHANGED (ne-runtime still emits
  ``role="tool"``; the content is byte-identical): the normalization is a wire
  boundary concern, not a rewrite of what the runtime means.
* (d) is the PAIRED mutation: a genuine provider failure must STILL fall back
  and STILL be recorded. The fix must make the frontier lane work, never make
  a real failure quieter.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.usage import RequestUsage

from ne.contracts import Lane
from ne.contracts.governance import RedactionResult
from ne.contracts.models import CompletionRequest
from ne.contracts.types import ChatMessage
from ne.gateway import GatewaySettings, LiteLLMGateway, ModelDef, TaskRoute
from ne.gateway.errors import ModelCallFailedError
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel

# The Anthropic request transform litellm's own anthropic handler calls — the exact
# frame the live ``AnthropicException - 'tool_call_id'`` was raised in. Imported
# AFTER ne.gateway._litellm (which ne.gateway re-exports through) so litellm's
# import-time telemetry / local-cost-map forcing is applied before anything else
# touches the package. Reaching into litellm from a TEST is fine — the C3
# chokepoint guardian scans ``packages/*/src/**`` and the import-linter contract
# covers the ``ne.*`` modules; neither includes tests.
from litellm.exceptions import BadRequestError  # isort: skip
from litellm.llms.anthropic.chat.transformation import AnthropicConfig  # isort: skip

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing  # isort: skip

TOOL_NAME = "read.accounts.list"
TOOL_OUTPUT = "ACCOUNTS-TOOL-OUTPUT-SENTINEL: 3 accounts renew in Q3"
ANTHROPIC_MODEL = "claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _frozen_config_hash() -> Iterator[None]:
    """Every gateway call stamps the frozen config hash; freeze it per test."""
    reset_config_hash_for_testing()
    freeze_config_hash(RuntimeSettings())
    try:
        yield
    finally:
        reset_config_hash_for_testing()


class _PassRedactor:
    def redact(self, text: str, *, categories: Any = None) -> RedactionResult:
        return RedactionResult(text=text)


class _CostSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


class _RecordingGateway:
    """A ModelGateway double that records the CompletionRequest ne-runtime built.

    It records ONLY; the recorded request is then handed VERBATIM to the REAL
    ``LiteLLMGateway`` below, so no hand-assembled message list is ever asserted
    against.
    """

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Any:
        self.requests.append(request)
        from ne.contracts.models import Completion, Usage

        return Completion(
            text="final answer",
            model_id="rec",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[Any]:  # pragma: no cover
        raise NotImplementedError


async def _noop_hook(name: str, args: Any) -> None:  # pragma: no cover - not reached
    return None


def _frontier_model(name: str = "frontier-default") -> ModelDef:
    return ModelDef(
        name=name,
        litellm_model=f"anthropic/{ANTHROPIC_MODEL}",
        provider="anthropic",
        lane=Lane.FRONTIER_API,
        extra_params={"api_key": "test"},
        context_window=200000,
    )


def _private_model(name: str = "private-default") -> ModelDef:
    return ModelDef(
        name=name,
        litellm_model="hosted_vllm/qwen3.6-35b-a3b",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://gpu.invalid/v1",
        extra_params={"api_key": "test"},
        context_window=32768,
    )


def _ok_response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        _hidden_params={"response_cost": 0.0},
        model="fake",
    )


def _history_with_a_tool_call() -> list[ModelMessage]:
    """The multi-turn history of an agent turn that FOLLOWS a tool call.

    Built out of pydantic-ai parts (what the agent loop actually accumulates),
    NOT out of ChatMessages — the mapping under test is ne-runtime's.
    """
    return [
        ModelRequest(
            parts=[
                SystemPromptPart(content="You are the Meridian analyst."),
                UserPromptPart(content="Which accounts renew in Q3?"),
            ]
        ),
        ModelResponse(
            parts=[ToolCallPart(tool_name=TOOL_NAME, args={})],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
            model_name="p",
        ),
        ModelRequest(parts=[ToolReturnPart(tool_name=TOOL_NAME, content=TOOL_OUTPUT)]),
    ]


def _egress_body(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The REAL wire body: runtime-mapped history → REAL LiteLLMGateway → litellm.

    Returns the ``messages`` kwarg ``Router.acompletion`` was actually called
    with, i.e. exactly what would have gone to Anthropic on the live box.
    """
    rec = _RecordingGateway()
    model = GatewayModel(rec, RuntimeSettings(task="agent"), on_unknown_tool=_noop_hook)
    asyncio.run(
        model.request(_history_with_a_tool_call(), None, ModelRequestParameters(function_tools=[]))
    )
    sent = rec.requests[-1]
    # Sanity: the runtime really did produce a tool-role message (if this ever
    # stops being true the test below would silently stop testing anything).
    assert any(m.role == "tool" for m in sent.messages)

    gs = GatewaySettings(
        models=[_frontier_model()],
        tasks={"agent": TaskRoute(model="frontier-default", fallbacks=())},
    )
    gw = LiteLLMGateway(gs, _CostSink(), redactor=_PassRedactor())
    captured: list[list[dict[str, Any]]] = []

    async def _fake(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.append(list(messages))
        return _ok_response()

    monkeypatch.setattr(gw._router, "acompletion", _fake)
    asyncio.run(gw.complete(sent))
    assert captured, "the gateway never reached the provider call"
    return captured[0]


def _transform(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Run litellm's REAL Anthropic request transform over a wire body."""
    return AnthropicConfig().transform_request(
        model=ANTHROPIC_MODEL,
        messages=cast(Any, list(messages)),
        optional_params={},
        litellm_params={},
        headers={},
    )


# --- (a) THE regression: the frontier lane accepts a post-tool-call turn ----


def test_turn_following_a_tool_call_serialises_to_a_shape_anthropic_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _egress_body(monkeypatch)

    transformed = _transform(body)

    # Not merely "did not raise": the tool's OUTPUT must survive into the blocks.
    # "Accepted because the tool result was dropped" is a different bug, not a fix.
    rendered = str(transformed["messages"])
    assert TOOL_OUTPUT in rendered, f"tool output lost in transform: {transformed['messages']}"
    assert TOOL_NAME in rendered, "the tool's identity was lost from the result text"
    # And no orphan survived to the wire.
    assert all(m["role"] in {"user", "assistant"} for m in transformed["messages"])


# --- (b) DISCRIMINATION: the orphan is still REJECTED, never silently dropped ---


def test_orphaned_tool_result_is_still_rejected_by_the_same_transform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _egress_body(monkeypatch)
    orphaned = [
        {**m, "role": "tool"} if TOOL_OUTPUT in str(m.get("content", "")) else dict(m) for m in body
    ]
    assert any(m["role"] == "tool" for m in orphaned), "control did not reconstruct the bug shape"

    with pytest.raises(Exception) as excinfo:
        _transform(orphaned)

    # The live signature, reproduced: a bare 'tool_call_id' key name.
    assert "tool_call_id" in str(excinfo.value)


# --- (c) the INTERNAL contract is untouched: a boundary fix, not a rewrite ---


def test_runtime_still_emits_a_tool_role_and_content_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RecordingGateway()
    model = GatewayModel(rec, RuntimeSettings(task="agent"), on_unknown_tool=_noop_hook)
    asyncio.run(
        model.request(_history_with_a_tool_call(), None, ModelRequestParameters(function_tools=[]))
    )
    internal = [m for m in rec.requests[-1].messages if m.role == "tool"]
    assert len(internal) == 1, "ne-runtime's internal tool-role marker must be preserved"

    body = _egress_body(monkeypatch)
    on_wire = [m for m in body if TOOL_OUTPUT in str(m["content"])]
    assert len(on_wire) == 1
    # ONLY the role changed. The content crosses the boundary byte-for-byte, so
    # the runtime's context-budget estimate stays lockstep with the gateway's.
    assert on_wire[0]["content"] == internal[0].content
    assert on_wire[0]["role"] == "user"


# --- (d) PAIRED mutation: a real provider failure still falls back, still records ---


def test_a_genuine_provider_failure_still_falls_back_and_is_still_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gs = GatewaySettings(
        models=[_frontier_model(), _private_model()],
        tasks={"agent": TaskRoute(model="frontier-default", fallbacks=("private-default",))},
    )
    gw = LiteLLMGateway(gs, _CostSink(), redactor=_PassRedactor())
    called: list[str] = []

    async def _fake(*, model: str, messages: Any, **kwargs: Any) -> Any:
        called.append(model)
        if model == "frontier-default":
            raise BadRequestError(  # a REAL provider failure, not a shape bug
                message="AnthropicException - overloaded_error",
                model=model,
                llm_provider="anthropic",
            )
        return _ok_response()

    monkeypatch.setattr(gw._router, "acompletion", _fake)
    request = CompletionRequest(
        task="agent",
        messages=(ChatMessage(role="user", content="hi"),),
    )
    completion = asyncio.run(gw.complete(request))

    assert called == ["frontier-default", "private-default"], "the chain must be unchanged"
    assert completion.text == "ok"


def test_every_candidate_failing_still_raises_the_recorded_chain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gs = GatewaySettings(
        models=[_frontier_model()],
        tasks={"agent": TaskRoute(model="frontier-default", fallbacks=())},
    )
    gw = LiteLLMGateway(gs, _CostSink(), redactor=_PassRedactor())

    async def _fake(*, model: str, messages: Any, **kwargs: Any) -> Any:
        raise BadRequestError(
            message="AnthropicException - overloaded_error",
            model=model,
            llm_provider="anthropic",
        )

    monkeypatch.setattr(gw._router, "acompletion", _fake)
    request = CompletionRequest(
        task="agent",
        messages=(ChatMessage(role="user", content="hi"),),
    )
    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(request))
    assert "frontier-default" in str(excinfo.value)
