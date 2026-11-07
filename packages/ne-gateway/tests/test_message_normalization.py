# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-AGENT-MSG-ORDER — the C3 chokepoint: EVERY caller, EVERY provider.

``LiteLLMGateway.complete()``/``_stream()`` normalize the incoming
``CompletionRequest.messages`` to AT MOST ONE leading system message BEFORE
token estimation, redaction, and the provider call — so a caller that
assembles a non-conforming sequence (any upstream bug, not just ne-runtime's)
still reaches a strict chat template (Qwen3.6-35B-A3B-FP8's) in a shape it
accepts. The exact rig-measured 400 shapes are reproduced here directly:

* ``(user, system)`` — the model swap's second measured 400.
* ``(system, user, system)`` — the SHIPPED agent shape, the model swap's
  third measured 400 and the one that killed every governed turn.

Nothing is mocked past the litellm network boundary (plan §1.4): the REAL
``Router`` never actually egresses here — ``Router.acompletion`` is
monkeypatched to CAPTURE the exact kwargs forwarded to litellm, mirroring
``test_redaction_seam.py``'s ``_install_recorder`` pattern. The PRIVATE_VLLM
lane is used throughout (sovereign, no redactor requirement) so normalization
is observed in isolation from NE-162 redaction.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.gateway import LiteLLMGateway, TaskRoute

from .conftest import RecordingCostSink, make_model, make_settings


def _gateway(sink: RecordingCostSink) -> LiteLLMGateway:
    """A PRIVATE_VLLM-only gateway — no redactor required, so normalization is
    observed independent of NE-162 pre-egress redaction."""
    settings = make_settings(
        [
            make_model(
                "priv",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
            )
        ],
        {"draft": TaskRoute(model="priv")},
    )
    return LiteLLMGateway(settings, sink)


def _install_recorder(gw: LiteLLMGateway) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append({"model": model, "messages": messages, **kwargs})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            _hidden_params={"response_cost": 0.0},
        )

    gw._router.acompletion = fake_acompletion  # type: ignore[method-assign]
    return calls


def _request(*messages: ChatMessage) -> CompletionRequest:
    return CompletionRequest(messages=tuple(messages), task="draft")


# -- complete() path ----------------------------------------------------------


def test_user_then_system_reaches_provider_system_first(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    # The model swap's second measured 400 shape: (user, system).
    gw = _gateway(sink)
    calls = _install_recorder(gw)
    request = _request(
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="system", content="be helpful"),
    )
    asyncio.run(gw.complete(request))

    assert len(calls) == 1
    sent = calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[0]["content"] == "be helpful"
    assert sent[1]["content"] == "hi"


def test_system_user_system_collapses_to_single_leading_system(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    # The SHIPPED agent shape and the model swap's third measured 400:
    # (system, user, system) — instructions, then user, then the trailing D3
    # catalog system message. Must collapse to ONE leading system message.
    gw = _gateway(sink)
    calls = _install_recorder(gw)
    request = _request(
        ChatMessage(role="system", content="INSTRUCTIONS-HEAD"),
        ChatMessage(role="user", content="please search"),
        ChatMessage(role="system", content="Available tools (JSON): [...]"),
    )
    asyncio.run(gw.complete(request))

    assert len(calls) == 1
    sent = calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[0]["content"] == "INSTRUCTIONS-HEAD\n\nAvailable tools (JSON): [...]"
    assert sent[1]["content"] == "please search"


def test_merge_is_order_stable_and_lossless(sink: RecordingCostSink, frozen_config: str) -> None:
    gw = _gateway(sink)
    calls = _install_recorder(gw)
    request = _request(
        ChatMessage(role="system", content="FIRST"),
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="system", content="SECOND"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="system", content="THIRD"),
        ChatMessage(role="user", content="u2"),
    )
    asyncio.run(gw.complete(request))

    sent = calls[0]["messages"]
    system_msgs = [m for m in sent if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert sent[0]["role"] == "system"
    head = sent[0]["content"]
    # Nothing dropped, all three sources present, in encounter order.
    assert "FIRST" in head and "SECOND" in head and "THIRD" in head
    assert head.index("FIRST") < head.index("SECOND") < head.index("THIRD")
    # Non-system messages keep their original relative order.
    assert [m["content"] for m in sent if m["role"] != "system"] == ["u1", "a1", "u2"]


def test_conforming_request_passes_through_byte_identical(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    # A control: a request already shaped correctly must be untouched — the
    # guard discriminates, it does not rewrite everything indiscriminately.
    gw = _gateway(sink)
    calls = _install_recorder(gw)
    original = (
        ChatMessage(role="system", content="be helpful"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
        ChatMessage(role="user", content="thanks"),
    )
    asyncio.run(gw.complete(_request(*original)))

    sent = calls[0]["messages"]
    assert sent == [m.model_dump() for m in original]


def test_no_system_messages_pass_through_unchanged(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    gw = _gateway(sink)
    calls = _install_recorder(gw)
    original = (
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
        ChatMessage(role="user", content="thanks"),
    )
    asyncio.run(gw.complete(_request(*original)))

    sent = calls[0]["messages"]
    assert sent == [m.model_dump() for m in original]
    assert all(m["role"] != "system" for m in sent)


# -- stream() path — the stream lane cannot regress independently -----------


def test_stream_path_normalizes_identically(sink: RecordingCostSink, frozen_config: str) -> None:
    gw = _gateway(sink)
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append({"model": model, "messages": messages, **kwargs})

        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))])

        return gen()

    gw._router.acompletion = fake_acompletion  # type: ignore[method-assign]

    request = _request(
        ChatMessage(role="system", content="INSTRUCTIONS-HEAD"),
        ChatMessage(role="user", content="please search"),
        ChatMessage(role="system", content="Available tools (JSON): [...]"),
    )

    async def _drain() -> None:
        async for _chunk in gw.stream(request):
            pass

    asyncio.run(_drain())

    assert len(calls) == 1
    sent = calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[0]["content"] == "INSTRUCTIONS-HEAD\n\nAvailable tools (JSON): [...]"
    assert sent[1]["content"] == "please search"
