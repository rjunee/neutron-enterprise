# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reasoning-channel policy at the ``complete()`` boundary.

Live-isolated 2026-08-04 on the GPU demo lane (Qwen3.6-35B-A3B-FP8, vLLM
``--reasoning-parser qwen3``, max_tokens=4096): a grounded synthesis turn
reasons for thousands of tokens, ``finish_reason='length'`` lands inside the
think channel, and the provider reply carries a populated
``message.reasoning_content`` with ``content=None``. The pre-fix gateway
coerced that to ``Completion(text="")`` — a silent empty answer the runtime
retried into ``UnexpectedModelBehavior``.

Pinned here:

* empty content + non-empty reasoning ⇒ typed :class:`ReasoningExhaustedError`
  appended to the candidate attempts (declared fallbacks still serve; a lone
  candidate surfaces it inside ``ModelCallFailedError``), with the burned
  tokens still metered by a CostEvent and a counts-only error AuditEvent;
* the CONTROL: empty content with NO reasoning is a DIFFERENT typed failure
  (:class:`EmptyCompletionError`, see ``test_empty_completion.py``) and must
  never be reported as this one — the two say different things about what the
  provider did, and only one of them names a budget to raise;
* reasoning TEXT is discarded at this boundary — never in ``Completion.text``,
  never in any audit detail/provenance (C12: counts only; the success-audit
  provenance carries ``reasoning_chars`` IFF the model reasoned).

``Router.acompletion`` is monkeypatched (the conftest-sanctioned seam for
shaping the exact provider reply) because litellm's ``mock_response`` cannot
produce a ``reasoning_content``-bearing message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.contracts import ChatMessage, CompletionRequest
from ne.gateway import (
    EmptyCompletionError,
    LiteLLMGateway,
    ModelCallFailedError,
    ReasoningExhaustedError,
    TaskRoute,
)
from ne.gateway._litellm import litellm
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_MSGS = (ChatMessage(role="user", content="hi"),)

#: Sentinel reasoning text — must NEVER appear in a Completion, an audit
#: record, or a rendered error (C12 counts-only discipline).
_REASONING_SENTINEL = "NEVER-SURFACED reasoning scratch about the customer text"


def _request(task: str) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def _provider_reply(
    content: str | None,
    reasoning: str | None,
    *,
    finish_reason: str = "length",
    completion_tokens: int = 4096,
) -> litellm.ModelResponse:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return litellm.ModelResponse(
        choices=[{"message": message, "finish_reason": finish_reason}],
        usage={
            "prompt_tokens": 10,
            "completion_tokens": completion_tokens,
            "total_tokens": 10 + completion_tokens,
        },
    )


def _install_replies(
    monkeypatch: pytest.MonkeyPatch,
    gw: LiteLLMGateway,
    replies: dict[str, litellm.ModelResponse],
) -> None:
    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return replies[model]

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)


def test_reasoning_exhausted_falls_back_to_declared_chain(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary burns its budget reasoning ⇒ the DECLARED fallback serves; the
    burned attempt is still metered (CostEvent) and audited (counts-only)."""
    settings = make_settings(
        [make_model("primary"), make_model("backup")],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(
        monkeypatch,
        gw,
        {
            "primary": _provider_reply(None, _REASONING_SENTINEL),
            "backup": _provider_reply("recovered", None, finish_reason="stop"),
        },
    )

    result = asyncio.run(gw.complete(_request("draft")))

    assert result.model_id == "backup"
    assert result.text == "recovered"
    # BOTH calls consumed provider tokens; both are metered.
    assert [e.model for e in sink.events] == ["gpt-3.5-turbo", "gpt-3.5-turbo"]
    # The burned attempt produced a counts-only ERROR audit event.
    errors = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent)
        and e.action == "ne.gateway.complete"
        and e.outcome == AuditOutcome.error
    ]
    assert len(errors) == 1
    (event,) = errors
    assert event.detail is not None
    assert "reasoning exhausted the output budget" in event.detail
    assert f"reasoning_chars={len(_REASONING_SENTINEL)}" in event.detail
    assert event.provenance is not None
    assert event.provenance["reasoning_chars"] == str(len(_REASONING_SENTINEL))
    # C12 sentinel scan: the reasoning TEXT reaches no audit record.
    for e in audit_sink.events:
        if isinstance(e, AuditEvent):
            assert _REASONING_SENTINEL not in (e.detail or "")
            assert _REASONING_SENTINEL not in str(e.provenance)


def test_reasoning_exhausted_without_fallback_raises_typed(
    sink: RecordingCostSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(monkeypatch, gw, {"only": _provider_reply(None, _REASONING_SENTINEL)})

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    attempts = excinfo.value.attempts
    assert [name for name, _ in attempts] == ["only"]
    exc = attempts[0][1]
    assert isinstance(exc, ReasoningExhaustedError)
    assert exc.completion_tokens == 4096
    assert exc.reasoning_chars == len(_REASONING_SENTINEL)
    assert exc.finish_reason == "length"
    # The counts are in the rendered message; the reasoning text is NOT (C12).
    assert "completion_tokens=4096" in str(exc)
    assert _REASONING_SENTINEL not in str(excinfo.value)
    # The burned call is still metered.
    assert len(sink.events) == 1


def test_empty_content_without_reasoning_is_not_reported_as_reasoning_exhausted(
    sink: RecordingCostSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTROL (bidirectional guard): an empty completion WITHOUT reasoning also
    fails typed — but as :class:`EmptyCompletionError`, never as this one.

    The distinction is not taxonomy for its own sake. ``ReasoningExhaustedError``
    asserts a specific, actionable cause (the output budget was spent in the think
    channel; raise ``max_output_tokens``) that this reply gives no evidence for:
    nothing was generated in either channel. Reporting it here would hand the
    reader a fix for a problem they do not have.
    """
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(monkeypatch, gw, {"only": _provider_reply(None, None)})

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    exc = excinfo.value.attempts[0][1]
    assert isinstance(exc, EmptyCompletionError)
    assert not isinstance(exc, ReasoningExhaustedError)


def test_reasoning_text_discarded_count_recorded_on_success(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful reasoning completion returns the CONTENT only; the
    success-audit provenance carries the char count, never the text."""
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(
        monkeypatch,
        gw,
        {"only": _provider_reply("the answer", _REASONING_SENTINEL, finish_reason="stop")},
    )

    result = asyncio.run(gw.complete(_request("draft")))

    assert result.text == "the answer"
    assert _REASONING_SENTINEL not in result.text
    successes = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent)
        and e.action == "ne.gateway.complete"
        and e.outcome == AuditOutcome.success
    ]
    assert len(successes) == 1
    (event,) = successes
    assert event.provenance is not None
    assert event.provenance["reasoning_chars"] == str(len(_REASONING_SENTINEL))
    assert _REASONING_SENTINEL not in (event.detail or "")
    assert _REASONING_SENTINEL not in str(event.provenance)


def test_no_reasoning_success_omits_reasoning_chars_key(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTROL: a reasoning-free completion carries NO reasoning_chars key —
    absent means the model did not reason, never the string '0'."""
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(
        monkeypatch, gw, {"only": _provider_reply("plain", None, finish_reason="stop")}
    )

    result = asyncio.run(gw.complete(_request("draft")))

    assert result.text == "plain"
    successes = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent)
        and e.action == "ne.gateway.complete"
        and e.outcome == AuditOutcome.success
    ]
    assert len(successes) == 1
    assert "reasoning_chars" not in (successes[0].provenance or {})
