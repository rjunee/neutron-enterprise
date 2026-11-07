# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""An empty completion at the ``complete()`` boundary.

Live-isolated 2026-08-26 on the pilot's frontier lane (``frontier-default`` =
``anthropic/claude-sonnet-5``, task ``agent.meridian-analyst.v2``): two of a
run's eight model hops came back with ``message.content`` empty AND
``message.reasoning_content`` empty, while the provider still BILLED 182 and 65
completion tokens. ``finish_reason`` was not ``length`` on any hop, so nothing
was truncated.

What the gateway did with that reply is the bug this module pins. It coerced it
to ``Completion(text="")`` and audited the call as a plain SUCCESS. Downstream,
``GatewayModel._parse("")`` makes a ``TextPart`` with no content, so pydantic-ai
sees a response that is not empty (it has a part) but carries nothing usable,
and reports it as non-actionable — ``ToolRetryError("Please return text or call
a tool.")``. The runtime turns THAT into "the model's final reply did not match
this Run's required answer format". Three hops from an empty reply to a sentence
about a schema rejection that never happened, and a success row that recorded
none of it.

Pinned here:

* an empty reply with NO reasoning ⇒ typed :class:`EmptyCompletionError`
  appended to the candidate attempts, so a DECLARED fallback still serves
  (visible routing) and a lone candidate surfaces it inside
  ``ModelCallFailedError``;
* the burned call is still metered (the transport was real and billable) and
  audited as an ERROR with counts only;
* the record answers WHERE the billed output went — the ``response_channels``
  census and an unconditional ``finish_reason`` — because the failing run left
  no trace of what arrived;
* ``ModelCallFailedError`` carries a publishable ``safe_reason``, so a run that
  dies this way can say so instead of naming a bare exception class;
* the CONTROL: a reply WITH content is untouched — same success row, no new
  provenance keys, no typed error.

``Router.acompletion`` is monkeypatched (the conftest-sanctioned seam for
shaping the exact provider reply).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.gateway import (
    EmptyCompletionError,
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

#: The live shape: billed output tokens on a reply carrying no content at all.
_BILLED_TOKENS = 182


def _request(task: str) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def _reply(
    content: str | None,
    *,
    finish_reason: str = "stop",
    completion_tokens: int = _BILLED_TOKENS,
    **channels: Any,
) -> litellm.ModelResponse:
    """A provider reply whose message carries ``content`` plus ``channels``.

    ``channels`` exists to reproduce the diagnostic question the live failure
    could not answer: output tokens were billed, so SOMETHING was generated —
    which field did it arrive on?
    """
    message: dict[str, Any] = {"role": "assistant", "content": content, **channels}
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


def _error_rows(audit_sink: RecordingAuditSink) -> list[AuditEvent]:
    """The PER-CANDIDATE error rows.

    The route-level "all model candidates failed" row shares this action and
    outcome but is a summary of the chain, not a record of one provider reply —
    it carries no per-candidate provenance and would otherwise be mistaken for a
    second empty completion.
    """
    return [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent)
        and e.action == "ne.gateway.complete"
        and e.outcome == AuditOutcome.error
        and not (e.detail or "").startswith("all model candidates failed")
    ]


def test_empty_completion_falls_back_to_the_declared_chain(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The primary returns nothing ⇒ the DECLARED fallback serves the request.

    This is the recovery the live run never got: the failing hop had
    ``fallbacks=["private-default", <the 0.6B CPU lane>]`` configured and the empty reply
    was served through as an answer anyway, so the fallback was never reached.
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
        {"primary": _reply(None), "backup": _reply("recovered", completion_tokens=7)},
    )

    result = asyncio.run(gw.complete(_request("draft")))

    assert result.model_id == "backup"
    assert result.text == "recovered"
    # BOTH calls consumed provider tokens; both are metered. The empty one was a
    # real, billable transport and stays on the cost record.
    assert [e.model for e in sink.events] == ["gpt-3.5-turbo", "gpt-3.5-turbo"]
    (event,) = _error_rows(audit_sink)
    assert event.detail is not None
    assert "returned an EMPTY completion" in event.detail
    assert f"completion_tokens={_BILLED_TOKENS}" in event.detail
    assert event.provenance is not None
    assert event.provenance["empty_completion_tokens"] == str(_BILLED_TOKENS)
    # The serving row records that it was reached for second.
    success = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent)
        and e.action == "ne.gateway.complete"
        and e.outcome == AuditOutcome.success
    ]
    assert success[-1].provenance is not None
    assert success[-1].provenance["fallback_from"] == "primary"


def test_empty_completion_without_fallback_raises_typed(
    sink: RecordingCostSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(monkeypatch, gw, {"only": _reply(None, finish_reason="stop")})

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    attempts = excinfo.value.attempts
    assert [name for name, _ in attempts] == ["only"]
    exc = attempts[0][1]
    assert isinstance(exc, EmptyCompletionError)
    # NOT the reasoning-exhausted sibling: that one asserts a cause (the budget
    # was spent in the think channel) this reply gives no evidence for.
    assert not isinstance(exc, ReasoningExhaustedError)
    assert exc.completion_tokens == _BILLED_TOKENS
    # litellm normalizes a provider stop reason onto the OpenAI vocabulary, so
    # this is the provider's word as the gateway can actually observe it.
    assert exc.finish_reason == "stop"
    assert "returned an empty completion" in str(exc)
    # The burned call is still metered.
    assert len(sink.events) == 1


def test_whitespace_only_content_is_not_empty(
    sink: RecordingCostSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary is CONTENT PRESENT / ABSENT, not "looks useful".

    Deliberate: deciding that whitespace, or a lone fence, is "empty enough"
    would put this gateway in the business of judging an answer's worth, and the
    parse/validation layers downstream already own that. Only a reply that
    carries nothing at all is a reply the provider did not give.
    """
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(monkeypatch, gw, {"only": _reply("   \n")})

    result = asyncio.run(gw.complete(_request("draft")))

    assert result.text == "   \n"


def test_empty_record_names_where_the_billed_output_went(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The census answers the question the live failure could not.

    182 completion tokens were billed on a reply with no content. Without this,
    the record says only that the reply was empty — never which channel the
    output actually arrived on, which is the whole difference between "the model
    said nothing" and "we are not reading the field it said it on".
    """
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(
        monkeypatch,
        gw,
        {
            "only": _reply(
                None,
                finish_reason="stop",
                thinking_blocks=[{"type": "thinking", "thinking": "SECRET-SCRATCH"}],
            )
        },
    )

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    # A thinking block IS output: this reply reasoned and did not answer, which
    # is its own terminal (ReasoningWithoutAnswerError) and NOT the empty case —
    # "no content and no reasoning" would be false of it. The census is what
    # says so, and it rides that row for exactly this reason.
    assert isinstance(excinfo.value.attempts[0][1], ReasoningWithoutAnswerError)
    (event,) = _error_rows(audit_sink)
    assert event.provenance is not None
    assert event.provenance["response_channels"] == "thinking_blocks=1"
    # finish_reason rides this row UNCONDITIONALLY — on a reply with no content
    # it is the most diagnostic field there is.
    assert event.provenance["finish_reason"] == "stop"
    assert event.detail is not None
    assert "channels=thinking_blocks=1" in event.detail
    # C12: the census reports SIZES. No channel's content reaches the chain.
    for e in audit_sink.events:
        if isinstance(e, AuditEvent):
            assert "SECRET-SCRATCH" not in (e.detail or "")
            assert "SECRET-SCRATCH" not in str(e.provenance)


def test_all_channels_empty_records_none(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply with nothing on ANY channel says so — that is itself the finding."""
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(monkeypatch, gw, {"only": _reply(None, completion_tokens=0)})

    with pytest.raises(ModelCallFailedError):
        asyncio.run(gw.complete(_request("draft")))

    (event,) = _error_rows(audit_sink)
    assert event.provenance is not None
    assert event.provenance["response_channels"] == "none"
    assert event.provenance["empty_completion_tokens"] == "0"


def test_model_call_failed_carries_a_publishable_safe_reason(
    sink: RecordingCostSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run record can say WHY instead of naming a bare exception class.

    ``run_workflow`` publishes a failed run's ``safe_reason`` if the exception
    opts in by carrying one; without it the reader gets ``error_class`` alone.
    The projection drops anything over 256 characters, so the sentence is built
    to fit.
    """
    settings = make_settings(
        [make_model("primary"), make_model("backup")],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(monkeypatch, gw, {"primary": _reply(None), "backup": _reply(None)})

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    reason = excinfo.value.safe_reason
    # NE-DEMO-NO-CPU-FALLBACK: the sentence names the routed TASK and each
    # candidate's LANE, so a reader can tell an unreachable private rig from a
    # frontier refusal from an air-gapped egress block — see
    # ``_safe_attempts_reason``. All four identifiers are server-owned
    # (registry keys, a Lane enum, exception class names); no provider text.
    assert reason == (
        "no authorized model lane could serve this request for task 'draft': "
        "primary [frontier-api] (EmptyCompletionError), "
        "backup [frontier-api] (EmptyCompletionError)"
    )
    assert len(reason) <= 256
    assert reason.isprintable()


def test_safe_reason_is_trimmed_to_the_publication_ceiling(
    sink: RecordingCostSink,
    frozen_config: str,
) -> None:
    """A long chain is TRIMMED here, not composed and silently dropped there.

    The run projection discards an ``error_reason`` over 256 characters outright,
    so a sentence built past the ceiling would publish NOTHING. Naming the
    candidates that fit beats naming all of them to a reader who sees none.
    """
    attempts = tuple(
        (
            f"candidate-{index:03d}",
            EmptyCompletionError(
                f"candidate-{index:03d}", completion_tokens=0, max_tokens=1, finish_reason=None
            ),
        )
        for index in range(40)
    )

    reason = ModelCallFailedError(attempts).safe_reason

    assert len(reason) <= 256
    assert reason.startswith("no authorized model lane could serve this request: candidate-000")
    # Trimmed, not truncated mid-token: the last thing named is a whole entry.
    assert reason.endswith("(EmptyCompletionError)")


def test_content_bearing_reply_is_unchanged(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTROL: a normal reply keeps the byte-identical success row it always had.

    The presence of a ``response_channels`` or ``empty_completion_tokens`` key is
    the SIGNAL; a completion that carried an answer must record neither.
    """
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(monkeypatch, gw, {"only": _reply("a real answer")})

    result = asyncio.run(gw.complete(_request("draft")))

    assert result.text == "a real answer"
    assert not _error_rows(audit_sink)
    (success,) = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent)
        and e.action == "ne.gateway.complete"
        and e.outcome == AuditOutcome.success
    ]
    assert success.provenance is not None
    assert "response_channels" not in success.provenance
    assert "empty_completion_tokens" not in success.provenance


def test_exhausted_chain_names_the_lane_that_could_not_be_reached(
    sink: RecordingCostSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published sentence distinguishes WHICH lane failed, not just which model.

    NE-DEMO-NO-CPU-FALLBACK removed the 0.6B CPU tier from the demo's task
    routes, so a private-primary task now degrades to the frontier lane and, if
    that is unreachable too, the chain EXHAUSTS. That is an expected, showable
    state under air-gap posture (the frontier egress is blocked on purpose), so
    the reason has to read as a governed refusal naming the lanes rather than as
    an anonymous crash. Model NAMES alone cannot carry that: ``private-default``
    and ``frontier-default`` are deployment labels, while ``private-vllm`` and
    ``frontier-api`` are the governed lanes an operator reasons about.
    """
    settings = make_settings(
        [
            make_model(
                "private-default",
                lane=Lane.PRIVATE_VLLM,
                litellm_model="hosted_vllm/qwen",
                provider="hosted_vllm",
                api_base="http://vllm:8000/v1",
                mock_response=_reply(None),
            ),
            make_model("frontier-default", lane=Lane.FRONTIER_API),
        ],
        {"reason": TaskRoute(model="private-default", fallbacks=("frontier-default",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    _install_replies(
        monkeypatch,
        gw,
        {"private-default": _reply(None), "frontier-default": _reply(None)},
    )

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("reason")))

    reason = excinfo.value.safe_reason
    assert reason == (
        "no authorized model lane could serve this request for task 'reason': "
        "private-default [private-vllm] (EmptyCompletionError), "
        "frontier-default [frontier-api] (EmptyCompletionError)"
    )
    # Publishable: the projection drops a reason over the ceiling outright.
    assert len(reason) <= 256
    assert reason.isprintable()
    # BOTH governed lanes are named, so "no lane could serve this" is a claim
    # the reader can actually check rather than take on faith.
    assert "private-vllm" in reason and "frontier-api" in reason
    # C12 CONTROL: the model's own reply and the api_base never reach the
    # published sentence — only registry identifiers and exception classes.
    assert "vllm:8000" not in reason and "hosted_vllm/qwen" not in reason


def test_safe_reason_still_publishes_when_no_lane_map_is_supplied(
    frozen_config: str,
) -> None:
    """CONTROL: ``task``/``lanes`` are enrichment, never a precondition.

    ``ModelCallFailedError`` is constructed directly in places that have no
    resolved ``ModelDef`` to hand. Those must still publish a sentence — the
    degradation is losing the lane detail, never losing the reason.
    """
    attempts = (
        (
            "solo",
            EmptyCompletionError("solo", completion_tokens=0, max_tokens=1, finish_reason=None),
        ),
    )

    reason = ModelCallFailedError(attempts).safe_reason

    assert reason == (
        "no authorized model lane could serve this request: solo (EmptyCompletionError)"
    )
    assert reason.isprintable()
