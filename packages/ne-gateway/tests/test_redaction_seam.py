# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-162 — the fail-closed pre-egress redaction seam.

The enumerated ratchet for the frontier-only redaction contract: the real
litellm ``Router`` runs end-to-end via ``mock_response`` where possible;
``Router.acompletion`` is monkeypatched only where a test must capture the
exact per-attempt kwargs forwarded to litellm (mirroring ``test_routing.py``).
Nothing is mocked past the redaction seam — no live Presidio venv is used
anywhere here, only the ``PassthroughRedactor`` / ``SubstringRedactor`` test
doubles from ``conftest.py`` (real ``Redactor`` Protocol implementations).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.contracts.governance import RedactionResult
from ne.gateway import (
    FrontierProviderError,
    LiteLLMGateway,
    ModelCallFailedError,
    RedactionRequiredError,
    TaskRoute,
)
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    SubstringRedactor,
    make_model,
    make_settings,
)

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str, **kw: Any) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task, **kw)


class _RaisingRedactor:
    """A ``Redactor`` whose ``redact()`` always raises — proves the fail-closed
    abort path (design §10.3 rule 2) without needing the real
    ``RedactionStage``/Presidio: any exception from the redactor must abort the
    frontier attempt unwrapped, never caught into the provider-failure list."""

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        raise RuntimeError("redact-fail")


def _install_recorder(monkeypatch: pytest.MonkeyPatch, gw: LiteLLMGateway) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append({"model": model, "messages": messages, **kwargs})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            _hidden_params={"response_cost": 0.0},
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    return calls


# -- fail-closed: None is a REFUSAL, never a no-op ----------------------------


def test_init_permits_frontier_models_without_redactor(sink: RecordingCostSink) -> None:
    # Construction over a FRONTIER_API model without a redactor is permitted: the
    # fail-closed guarantee is the per-attempt RUNTIME refusal (see
    # test_runtime_refusal_when_redactor_absent_on_frontier_attempt), not a
    # construction-time assertion — which would false-positive on routing/egress
    # tests and non-production composition roots that never egress to the frontier
    # lane. The sanctioned production root (_default_gateway) always wires a redactor.
    settings = make_settings(
        [make_model("primary", lane=Lane.FRONTIER_API)],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink)  # no raise
    assert gw is not None


def test_init_allows_private_only_models_without_redactor(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model(
                "vllm",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
                mock_response="ok",
            )
        ],
        {"draft": TaskRoute(model="vllm")},
    )
    gw = LiteLLMGateway(settings, sink)  # redactor=None: legal, no frontier model
    result = asyncio.run(gw.complete(_request("draft")))
    assert result.text == "ok"


def test_runtime_refusal_when_redactor_absent_on_frontier_attempt(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    calls = _install_recorder(monkeypatch, gw)
    gw._redactor = None  # white-box: settings.models is a mutable list too

    with pytest.raises(RedactionRequiredError):
        asyncio.run(gw.complete(_request("draft")))

    assert calls == []
    assert sink.events == []


# -- RedactionError aborts unwrapped, no fallback -----------------------------


def test_redaction_error_aborts_frontier_attempt_no_fallback(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response="ok"),
            make_model("backup", mock_response="ok"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=_RaisingRedactor())

    calls: list[Any] = []

    async def never_called(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(model)
        raise AssertionError("acompletion must never be called on a redaction refusal")

    gw._router.acompletion = never_called  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="redact-fail"):
        asyncio.run(gw.complete(_request("draft")))

    assert calls == []  # neither candidate ever reached the provider
    assert sink.events == []  # a policy refusal is not a provider failure

    assert len(audit_sink.events) == 1
    event = audit_sink.events[0]
    assert isinstance(event, AuditEvent)
    assert event.action == "redact.pre_egress"
    assert event.outcome == AuditOutcome.denied
    assert event.detail is not None
    assert "RuntimeError" in event.detail
    assert "redact-fail" not in event.detail  # class name only, never backend text


# -- frontier body redacted, private byte-identical ---------------------------


def test_frontier_body_redacted_private_untouched(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        [
            make_model("front", lane=Lane.FRONTIER_API, mock_response="ok"),
            make_model(
                "priv",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
                mock_response="ok",
            ),
        ],
        {
            "front-task": TaskRoute(model="front"),
            "priv-task": TaskRoute(model="priv"),
        },
    )
    gw = LiteLLMGateway(settings, sink, redactor=SubstringRedactor())
    calls = _install_recorder(monkeypatch, gw)

    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="my SECRET is here"),), task="front-task"
    )
    priv_request = CompletionRequest(
        messages=(ChatMessage(role="user", content="my SECRET is here"),), task="priv-task"
    )

    asyncio.run(gw.complete(request))
    asyncio.run(gw.complete(priv_request))

    assert len(calls) == 2
    front_call, priv_call = calls
    assert front_call["messages"][0]["content"] == "my [REDACTED] is here"
    assert "SECRET" not in front_call["messages"][0]["content"]
    assert priv_call["messages"][0]["content"] == "my SECRET is here"

    # The shared request objects (frozen ChatMessage tuples) are unchanged.
    assert request.messages[0].content == "my SECRET is here"
    assert priv_request.messages[0].content == "my SECRET is here"


# -- every string leaf redacted; roles preserved verbatim ---------------------


def test_redacts_every_string_leaf_roles_preserved(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=SubstringRedactor())
    calls = _install_recorder(monkeypatch, gw)

    messages = (
        ChatMessage(role="system", content="system prompt with SECRET context"),
        ChatMessage(role="user", content="please look up SECRET"),
    )
    params = {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "call SECRET desk",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "contains SECRET too"}
                        },
                    },
                },
            }
        ],
        "stop": "SECRET-stop",
        "temperature": 0.5,
        "max_tokens": 10,
        "stream": False,
    }
    request = CompletionRequest(messages=messages, task="draft", params=params)
    asyncio.run(gw.complete(request))

    assert len(calls) == 1
    call = calls[0]
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1]["role"] == "user"
    assert "SECRET" not in call["messages"][0]["content"]
    assert "SECRET" not in call["messages"][1]["content"]

    tool = call["tools"][0]
    assert "SECRET" not in tool["function"]["description"]
    assert "SECRET" not in tool["function"]["parameters"]["properties"]["query"]["description"]
    # Non-redacted structural strings survive untouched.
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "lookup"

    assert "SECRET" not in call["stop"]
    # Non-string leaves pass through untouched.
    assert call["temperature"] == 0.5
    assert call["max_tokens"] == 10
    assert call["stream"] is False


# -- multi-turn: full accumulated body re-redacted every egress ---------------


def test_multi_turn_full_body_reredacted(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=SubstringRedactor())
    calls = _install_recorder(monkeypatch, gw)

    turn_1 = (ChatMessage(role="user", content="first turn, no PII here"),)
    asyncio.run(gw.complete(CompletionRequest(messages=turn_1, task="draft")))

    turn_2 = (
        ChatMessage(role="user", content="first turn, no PII here"),
        ChatMessage(role="assistant", content="calling a tool with SECRET arg"),
        ChatMessage(role="tool", content="tool result carrying SECRET data"),
        ChatMessage(role="user", content="thanks, and my SECRET follow-up"),
    )
    asyncio.run(gw.complete(CompletionRequest(messages=turn_2, task="draft")))

    assert len(calls) == 2
    second_call_messages = calls[1]["messages"]
    assert len(second_call_messages) == 4
    for msg in second_call_messages:
        assert "SECRET" not in msg["content"]
    assert second_call_messages[1]["role"] == "assistant"
    # NE-GW-TOOL-RESULT: a ``tool``-role ChatMessage carries no tool_call_id (the
    # contract has no such field), so it is emitted on the wire as a ``user``
    # message — see ``_to_wire_messages``. Redaction still covers it, which is
    # what this test is about; the role it lands under is the wire's business.
    assert second_call_messages[2]["role"] == "user"
    assert second_call_messages[2]["content"] == "tool result carrying [REDACTED] data"


# -- fallback chain: only the frontier hop is redacted ------------------------


def test_private_to_frontier_fallback_redacts_only_frontier_hop(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        [
            make_model(
                "priv",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
            ),
            make_model("front", lane=Lane.FRONTIER_API),
        ],
        {"draft": TaskRoute(model="priv", fallbacks=("front",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=SubstringRedactor())

    calls: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append({"model": model, "messages": messages})
        if model == "priv":
            raise RuntimeError("private candidate unavailable")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            _hidden_params={"response_cost": 0.0},
        )

    gw._router.acompletion = fake_acompletion  # type: ignore[method-assign]

    request = CompletionRequest(
        messages=(ChatMessage(role="user", content="carrying SECRET data"),), task="draft"
    )
    result = asyncio.run(gw.complete(request))
    assert result.model_id == "front"

    assert len(calls) == 2
    priv_call, front_call = calls
    assert priv_call["model"] == "priv"
    assert priv_call["messages"][0]["content"] == "carrying SECRET data"  # untouched
    assert front_call["model"] == "front"
    assert "SECRET" not in front_call["messages"][0]["content"]  # redacted


# -- frontier provider errors sanitized to class name only --------------------


def test_frontier_provider_error_reduced_to_class_name(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    leak = "SSN 123-45-6789 echoed by provider"
    settings = make_settings(
        [
            make_model("front", lane=Lane.FRONTIER_API, mock_response=Exception(leak)),
            make_model(
                "priv",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
                mock_response=Exception(leak),
            ),
        ],
        {
            "front-task": TaskRoute(model="front"),
            "priv-task": TaskRoute(model="priv"),
        },
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    with pytest.raises(ModelCallFailedError) as front_excinfo:
        asyncio.run(gw.complete(_request("front-task")))
    front_err = front_excinfo.value
    assert len(front_err.attempts) == 1
    front_name, front_exc = front_err.attempts[0]
    assert front_name == "front"
    assert isinstance(front_exc, FrontierProviderError)
    # str(front_exc) is the ORIGINAL exception's CLASS NAME only (litellm wraps
    # the injected mock_response Exception in its own error type) — never the
    # leaked message text, and always a bare identifier (no spaces/digits).
    assert str(front_exc).isidentifier()
    assert "123-45-6789" not in str(front_exc)
    assert front_exc.__cause__ is None
    assert "123-45-6789" not in str(front_err)
    assert str(front_exc) in str(front_err)

    audit_sink.events.clear()
    with pytest.raises(ModelCallFailedError) as priv_excinfo:
        asyncio.run(gw.complete(_request("priv-task")))
    priv_err = priv_excinfo.value
    assert len(priv_err.attempts) == 1
    priv_name, priv_exc = priv_err.attempts[0]
    assert priv_name == "priv"
    assert not isinstance(priv_exc, FrontierProviderError)
    assert "123-45-6789" in str(priv_exc)  # private lane: original text preserved


# -- redact.pre_egress carries counts only, never text ------------------------


def test_redact_pre_egress_audit_counts_only(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=SubstringRedactor())

    request = CompletionRequest(
        messages=(
            ChatMessage(role="system", content="SECRET one"),
            ChatMessage(role="user", content="SECRET two SECRET three"),
        ),
        task="draft",
        trace_id="t-162",
    )
    asyncio.run(gw.complete(request))

    assert len(audit_sink.events) == 3
    redact_event, complete_event, observation_event = audit_sink.events
    assert isinstance(redact_event, AuditEvent)
    assert isinstance(complete_event, AuditEvent)
    assert isinstance(observation_event, AuditEvent)
    assert redact_event.action == "redact.pre_egress"
    assert complete_event.action == "ne.gateway.complete"  # emitted AFTER redaction
    assert observation_event.action == "ne.gateway.model.observed"

    assert redact_event.outcome == AuditOutcome.success
    assert redact_event.trace_id == "t-162"
    assert redact_event.provenance["redaction_hits"] == "3"
    assert redact_event.provenance["redaction_categories"] == "TEST_SECRET=3"
    assert redact_event.provenance["task"] == "draft"
    assert redact_event.provenance["lane"] == Lane.FRONTIER_API.value
    assert redact_event.provenance["model_name"] == "primary"

    # The category LABEL ("TEST_SECRET") legitimately appears (it's metadata,
    # not the redacted VALUE) — what must never appear is the raw body text or
    # its redacted replacement.
    dump = repr(redact_event.detail) + repr(redact_event.provenance)
    assert "SECRET one" not in dump
    assert "SECRET two SECRET three" not in dump
    assert "[REDACTED]" not in dump


# -- streaming: identical treatment to complete() -----------------------------


def test_stream_frontier_redacted_private_untouched(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("front", lane=Lane.FRONTIER_API, mock_response="ok"),
            make_model(
                "priv",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/x",
                provider="vllm",
                api_base="http://vllm:8000/v1",
                mock_response="ok",
            ),
        ],
        {
            "front-task": TaskRoute(model="front"),
            "priv-task": TaskRoute(model="priv"),
        },
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=SubstringRedactor())

    calls: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append({"model": model, "messages": messages})

        async def gen() -> Any:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])

        return gen()

    gw._router.acompletion = fake_acompletion  # type: ignore[method-assign]

    async def _drain(task: str, content: str) -> None:
        msgs = (ChatMessage(role="user", content=content),)
        request = CompletionRequest(messages=msgs, task=task)
        async for _ in gw.stream(request):
            pass

    asyncio.run(_drain("front-task", "carrying SECRET data"))
    asyncio.run(_drain("priv-task", "carrying SECRET data"))

    assert len(calls) == 2
    front_call, priv_call = calls
    assert "SECRET" not in front_call["messages"][0]["content"]
    assert priv_call["messages"][0]["content"] == "carrying SECRET data"

    redact_events = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent) and e.action == "redact.pre_egress"
    ]
    assert len(redact_events) == 1  # only the frontier attempt gets a redact event


def test_stream_redaction_error_refuses_before_stream_established(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=_RaisingRedactor())

    calls: list[Any] = []

    async def never_called(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(model)
        raise AssertionError("acompletion must never be called on a redaction refusal")

    gw._router.acompletion = never_called  # type: ignore[method-assign]

    async def _consume() -> None:
        async for _ in gw.stream(_request("draft")):
            pass

    with pytest.raises(RuntimeError, match="redact-fail"):
        asyncio.run(_consume())

    assert calls == []
    assert sink.events == []


# -- redaction is offloaded to a worker thread (Codex P1) ---------------------


def test_redaction_runs_off_the_event_loop(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production ``PresidioRedactor`` backend synchronously launches and
    waits for a subprocess for EVERY string leaf. Calling ``redact_payload``
    directly on the coroutine would block the event loop for the whole
    redaction walk, stalling every other concurrent request — so it must run
    via ``asyncio.to_thread``, never on the calling (event-loop) thread."""
    settings = make_settings(
        [make_model("front", lane=Lane.FRONTIER_API, mock_response="ok")],
        {"front-task": TaskRoute(model="front")},
    )

    main_thread_ident = threading.get_ident()
    seen_idents: list[int] = []

    class _ThreadRecordingRedactor:
        def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
            seen_idents.append(threading.get_ident())
            return RedactionResult(text=text)

    gw = LiteLLMGateway(settings, sink, redactor=_ThreadRecordingRedactor())
    _install_recorder(monkeypatch, gw)

    asyncio.run(gw.complete(_request("front-task")))

    assert seen_idents  # redact() was actually invoked
    assert all(ident != main_thread_ident for ident in seen_idents)


# -- streaming mid-drain frontier failures: class-name only (Codex P1) -------


def test_stream_mid_drain_frontier_failure_class_name_only(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """A FRONTIER_API stream that raises DURING ``async for`` drain (not just
    while establishing the stream) must get the same class-name-only
    sanitization as an establish-phase provider failure: provider exception
    text raised mid-drain can itself echo prompt/response substrings that
    ``redact_url_for_logs`` alone does not strip."""
    leak = "SSN 123-45-6789 echoed mid-stream by provider"
    settings = make_settings(
        [make_model("front", lane=Lane.FRONTIER_API, mock_response="ok")],
        {"front-task": TaskRoute(model="front")},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> Any:
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="partial"))]
            )
            raise RuntimeError(leak)

        return gen()

    gw._router.acompletion = fake_acompletion  # type: ignore[method-assign]

    async def _consume() -> None:
        async for _ in gw.stream(_request("front-task")):
            pass

    with pytest.raises(RuntimeError, match="echoed mid-stream"):
        asyncio.run(_consume())

    stream_events = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent) and e.action == "ne.gateway.stream"
    ]
    assert len(stream_events) == 1
    event = stream_events[0]
    assert event.outcome == AuditOutcome.error
    assert event.detail is not None
    assert "123-45-6789" not in event.detail
    assert leak not in event.detail
    assert "RuntimeError" in event.detail


def test_stream_mid_drain_private_failure_keeps_original_text(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """Regression guard: the class-name-only treatment above must stay
    FRONTIER_API-only — a PRIVATE_VLLM mid-drain failure keeps its
    (URL-redacted) original exception text, unchanged from before this fix."""
    leak = "private-lane diagnostic detail, no PII policy applies"
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
        {"priv-task": TaskRoute(model="priv")},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> Any:
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="partial"))]
            )
            raise RuntimeError(leak)

        return gen()

    gw._router.acompletion = fake_acompletion  # type: ignore[method-assign]

    async def _consume() -> None:
        async for _ in gw.stream(_request("priv-task")):
            pass

    with pytest.raises(RuntimeError, match="private-lane diagnostic detail"):
        asyncio.run(_consume())

    stream_events = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent) and e.action == "ne.gateway.stream"
    ]
    assert len(stream_events) == 1
    event = stream_events[0]
    assert event.detail is not None
    assert leak in event.detail  # private lane: original text preserved
