# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The thinking channel at the ``complete()`` boundary.

Live-isolated 2026-08-26 on the frontier lane (``anthropic/claude-sonnet-5``,
``task=agent.meridian-analyst.v2``): a turn returned ``finish_reason='stop'``
with ``completion_tokens=144``, ``content`` empty, ``reasoning_content`` empty,
and ONE entry on ``message.thinking_blocks``. The model completed normally,
billed 144 tokens of reasoning, and delivered no answer — but the gateway,
reading only ``content`` and ``reasoning_content``, recorded it as a reply that
carried *nothing*.

The cause is not a mapping gap that hides an answer. Read from the pinned
litellm 1.90.3 rather than assumed:

* ``AnthropicConfig.extract_response_content`` collects ``thinking`` and
  ``redacted_thinking`` blocks onto ``message.thinking_blocks`` and derives
  ``message.reasoning_content`` as the concatenation of ONLY each block's
  ``thinking`` field — so ``reasoning_content`` already carries every byte of
  answer-shaped text the channel can hold;
* ``ChatCompletionThinkingBlock`` is ``{type, thinking, signature}`` and
  ``ChatCompletionRedactedThinkingBlock`` is ``{type, data}`` — the remaining
  fields are opaque provider replay tokens, not text.

What the channel DOES tell you that ``reasoning_content`` cannot is *that the
model reasoned*: a redacted block carries no ``thinking`` field, and a provider
that withholds thinking text (the current Anthropic default, ``display`` =
``"omitted"``) returns ``thinking=""`` with a signature. Either leaves
``reasoning_content`` empty while the completion tokens prove reasoning
happened.

Pinned here:

* a reply with a thinking block and no answer ⇒ typed
  :class:`ReasoningWithoutAnswerError`, NOT the exhausted sibling — the budget
  was not the limit, so the terminal must not name raising it as the fix;
* the same for a ``redacted_thinking`` block, whose opaque ``data`` never
  reaches a record;
* reasoning is NEVER promoted into the answer — a declared fallback serves
  instead, and no thinking text reaches ``Completion.text``;
* ``finish_reason='length'`` with reasoning text keeps
  :class:`ReasoningExhaustedError` (unchanged), because there the remediation
  it names is real.

``Router.acompletion`` is monkeypatched (the conftest-sanctioned seam) because
litellm's ``mock_response`` cannot shape a ``thinking_blocks``-bearing message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.contracts import ChatMessage, CompletionRequest
from ne.gateway import (
    LiteLLMGateway,
    ModelCallFailedError,
    ReasoningExhaustedError,
    ReasoningWithoutAnswerError,
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

#: Opaque provider replay tokens. Neither is text, and neither may reach a
#: Completion, an audit record, or a rendered error (C12).
_SIGNATURE_SENTINEL = "NEVER-SURFACED-signature-blob-EncryptedThinkingSignature=="
_REDACTED_SENTINEL = "NEVER-SURFACED-redacted-thinking-payload-blob"
#: Reasoning TEXT. Same rule, and additionally: it must never become the answer.
_THINKING_SENTINEL = "NEVER-SURFACED reasoning scratch about the customer text"


def _request(task: str) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def _thinking_reply(
    blocks: list[dict[str, Any]],
    *,
    content: str | None = None,
    reasoning: str | None = None,
    finish_reason: str = "stop",
    completion_tokens: int = 144,
) -> litellm.ModelResponse:
    """The live shape: an assistant message whose only output is thinking.

    Mirrors what ``AnthropicConfig.transform_response`` builds — ``content`` is
    ``None`` when the reply had no ``text`` block, and ``reasoning_content`` is
    the concatenation of the blocks' ``thinking`` fields (empty string when the
    provider withheld them), which the caller states explicitly here.
    """
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "thinking_blocks": blocks,
    }
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return litellm.ModelResponse(
        choices=[{"message": message, "finish_reason": finish_reason}],
        usage={
            "prompt_tokens": 5792,
            "completion_tokens": completion_tokens,
            "total_tokens": 5792 + completion_tokens,
        },
    )


def _plain_reply(text: str) -> litellm.ModelResponse:
    return litellm.ModelResponse(
        choices=[{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        usage={"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
    )


def _install_replies(
    monkeypatch: pytest.MonkeyPatch,
    gw: LiteLLMGateway,
    replies: dict[str, litellm.ModelResponse],
) -> None:
    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return replies[model]

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)


def _candidate_error_rows(audit_sink: RecordingAuditSink) -> list[AuditEvent]:
    """The per-CANDIDATE error rows only.

    A run with no surviving candidate also emits a final "all model candidates
    failed" row, which is keyed on the TASK rather than a model
    (``resource=request.task``). Filtering on ``model_name`` keeps this
    assertion about the candidate's own record either way.
    """
    return [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent)
        and e.action == "ne.gateway.complete"
        and e.outcome == AuditOutcome.error
        and e.provenance is not None
        and "model_name" in e.provenance
    ]


def _assert_no_sentinels(audit_sink: RecordingAuditSink) -> None:
    """C12: no reasoning text and no opaque provider token reaches a record."""
    for event in audit_sink.events:
        if isinstance(event, AuditEvent):
            rendered = (event.detail or "") + str(event.provenance)
            assert _SIGNATURE_SENTINEL not in rendered
            assert _REDACTED_SENTINEL not in rendered
            assert _THINKING_SENTINEL not in rendered


def test_withheld_thinking_text_is_reported_as_reasoned_without_answer(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE live shape, reproduced exactly: ``finish_reason='stop'``, 144 billed
    completion tokens, no content, no reasoning CHARS, one thinking block.

    The terminal must say the model reasoned and produced no answer — never
    that it produced nothing, and never that the output budget was the limit
    (144 of 24576 is not a budget). The reasoning is not served as the answer.
    """
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(
        monkeypatch,
        gw,
        {
            "only": _thinking_reply(
                # display="omitted": the text is withheld, the signature is not.
                [{"type": "thinking", "thinking": "", "signature": _SIGNATURE_SENTINEL}],
                reasoning="",
            )
        },
    )

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    exc = excinfo.value.attempts[0][1]
    assert isinstance(exc, ReasoningWithoutAnswerError)
    # The distinct terminal: NOT the exhausted sibling, whose message names a
    # remediation ("raise max_output_tokens") that would change nothing here.
    assert not isinstance(exc, ReasoningExhaustedError)
    assert exc.thinking_blocks == 1
    assert exc.reasoning_chars == 0
    assert exc.completion_tokens == 144
    assert exc.finish_reason == "stop"
    assert "reasoned and returned NO answer" in str(exc)
    # The class name is the terminal a reader is shown: it is rendered into
    # the attempt chain verbatim.
    assert "ReasoningWithoutAnswerError" in str(excinfo.value)

    errors = _candidate_error_rows(audit_sink)
    assert len(errors) == 1
    (event,) = errors
    assert event.detail is not None
    assert "reasoned and returned NO answer" in event.detail
    assert "thinking_blocks=1" in event.detail
    assert event.provenance is not None
    # The PAIR is the finding: zero reasoning chars beside a thinking block is
    # the withheld/redacted shape, and neither number alone says so.
    assert event.provenance["reasoning_chars"] == "0"
    assert event.provenance["thinking_blocks"] == "1"
    assert event.provenance["finish_reason"] == "stop"
    # The burned tokens are still metered — a real, billable transport.
    assert len(sink.events) == 1
    _assert_no_sentinels(audit_sink)


def test_redacted_thinking_block_counts_as_reasoning(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``redacted_thinking`` block has NO ``thinking`` field at all, so
    litellm leaves ``reasoning_content`` empty. It is still reasoning, and its
    opaque ``data`` payload is never read."""
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(
        monkeypatch,
        gw,
        {
            "only": _thinking_reply(
                [{"type": "redacted_thinking", "data": _REDACTED_SENTINEL}],
                reasoning="",
            )
        },
    )

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    exc = excinfo.value.attempts[0][1]
    assert isinstance(exc, ReasoningWithoutAnswerError)
    assert exc.thinking_blocks == 1
    assert exc.reasoning_chars == 0
    assert _REDACTED_SENTINEL not in str(excinfo.value)
    _assert_no_sentinels(audit_sink)


def test_thinking_text_is_never_served_as_the_answer(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutation that matters most: a reply carrying ONLY reasoning must not
    have that reasoning promoted into ``Completion.text``.

    A thinking block is the model's working scratch, not its answer. Here the
    provider returns a fully populated thinking channel and no content; the
    declared fallback serves the request (visible routing) and the reasoning
    text appears nowhere.
    """
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
            "primary": _thinking_reply(
                [
                    {
                        "type": "thinking",
                        "thinking": _THINKING_SENTINEL,
                        "signature": _SIGNATURE_SENTINEL,
                    }
                ],
                reasoning=_THINKING_SENTINEL,
            ),
            "backup": _plain_reply("the real answer"),
        },
    )

    result = asyncio.run(gw.complete(_request("draft")))

    assert result.model_id == "backup"
    assert result.text == "the real answer"
    assert _THINKING_SENTINEL not in result.text
    # finish_reason 'stop' with reasoning present is still NOT budget
    # exhaustion — the accurate terminal is the new one.
    errors = _candidate_error_rows(audit_sink)
    assert len(errors) == 1
    assert errors[0].detail is not None
    assert "reasoned and returned NO answer" in errors[0].detail
    assert f"reasoning_chars={len(_THINKING_SENTINEL)}" in errors[0].detail
    _assert_no_sentinels(audit_sink)


def test_truncated_reasoning_still_reports_exhausted_budget(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CONTROL for the other direction: when the reply really was cut off
    inside the think channel (``finish_reason='length'`` with reasoning text),
    the exhausted terminal is unchanged — that terminal names a remediation
    which is true there and only there."""
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(
        monkeypatch,
        gw,
        {
            "only": _thinking_reply(
                [{"type": "thinking", "thinking": _THINKING_SENTINEL, "signature": "sig"}],
                reasoning=_THINKING_SENTINEL,
                finish_reason="length",
                completion_tokens=4096,
            )
        },
    )

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    exc = excinfo.value.attempts[0][1]
    assert isinstance(exc, ReasoningExhaustedError)
    assert not isinstance(exc, ReasoningWithoutAnswerError)
    assert "exhausted inside the reasoning channel" in str(exc)
    _assert_no_sentinels(audit_sink)
