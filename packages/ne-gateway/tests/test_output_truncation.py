# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FRONTIER-OUTPUT: a cut-off answer must never be served as a whole one.

``ReasoningExhaustedError`` (test_reasoning_exhausted.py) covers the case where
the output budget is burned inside the reasoning channel and the content comes
back EMPTY. The sibling case had no coverage and no signal at all: a completion
that is truncated but NOT empty — ``finish_reason='length'`` with real content —
was returned through a ``Completion`` indistinguishable from a complete one, and
its success audit row said nothing either. A partial answer presented as a whole
one is a silent degrade, which is the failure class this product exists to make
impossible.

Pinned here, in both directions:

* the truncated completion is still SERVED (the partial content is real and the
  caller keeps it — this is deliberately NOT turned into a failure), but the
  fact is carried on ``Completion.finish_reason``/``truncated`` and stated in
  the success audit row's detail AND provenance;
* the CONTROL: a complete answer records NOTHING new — same detail string, same
  provenance keys as before the unit. The presence of the key IS the signal, so
  it has to be absent when there is nothing to signal;
* the same pair on the STREAM path, where truncation is hardest to notice
  because every token has already been handed to the consumer;
* C12: ``finish_reason`` is a provider status token, never text.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from ne.contracts import ChatMessage, CompletionChunk, CompletionRequest
from ne.gateway import LiteLLMGateway, TaskRoute
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

#: A long answer that stops mid-sentence — the shape a grounded multi-hop
#: synthesis turn takes when it runs out of output budget.
_PARTIAL = "Machine A's defect rate is 3.1%, machine B's is 4.7%, and machine C"


def _request(task: str = "draft") -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def _reply(content: str, finish_reason: str | None) -> litellm.ModelResponse:
    return litellm.ModelResponse(
        choices=[
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106},
    )


def _install(
    monkeypatch: pytest.MonkeyPatch, gw: LiteLLMGateway, reply: litellm.ModelResponse
) -> None:
    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return reply

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)


def _gateway(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink | None = None
) -> LiteLLMGateway:
    settings = make_settings(
        [make_model("only")], {"draft": TaskRoute(model="only")}, num_retries=0
    )
    return LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)


def _success_row(audit_sink: RecordingAuditSink, action: str) -> AuditEvent:
    rows = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent) and e.action == action and e.outcome == AuditOutcome.success
    ]
    assert len(rows) == 1, rows
    return rows[0]


# -- complete() -------------------------------------------------------------


def test_truncated_completion_is_served_but_marked(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partial answer is KEPT — refusing it would throw away real work — but
    it arrives flagged, and the audit row says why. Dropping the
    ``finish_reason=`` argument from the returned ``Completion`` -> RED."""
    gw = _gateway(sink, audit_sink)
    _install(monkeypatch, gw, _reply(_PARTIAL, "length"))

    result = asyncio.run(gw.complete(_request()))

    # SERVED, not failed: the caller still has the partial content.
    assert result.text == _PARTIAL
    assert result.model_id == "only"
    # ...and can tell it is partial without inspecting the audit chain.
    assert result.finish_reason == "length"
    assert result.truncated is True

    row = _success_row(audit_sink, "ne.gateway.complete")
    assert row.detail is not None
    assert "TRUNCATED" in row.detail
    assert "the served answer is PARTIAL" in row.detail
    assert row.provenance is not None
    assert row.provenance["finish_reason"] == "length"


def test_complete_answer_records_nothing_new(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE PAIR. A whole answer's success row is byte-identical to the pre-unit
    row: no added detail text, no added provenance key. Making the marking
    unconditional -> RED here, which is what keeps the key's PRESENCE meaningful
    rather than something a reader has to compare values on."""
    gw = _gateway(sink, audit_sink)
    _install(monkeypatch, gw, _reply("the whole answer", "stop"))

    result = asyncio.run(gw.complete(_request()))

    assert result.text == "the whole answer"
    # The fact is still CARRIED (it is what the provider said) ...
    assert result.finish_reason == "stop"
    # ... it just does not mean the answer was cut off.
    assert result.truncated is False

    row = _success_row(audit_sink, "ne.gateway.complete")
    assert row.detail == "task=draft lane=frontier-api model=only"
    assert row.provenance is not None
    assert "finish_reason" not in row.provenance


def test_provider_that_reports_nothing_is_not_read_as_truncated(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTROL: absence of evidence is not evidence of a cut. A ``None``
    finish_reason must not flag an answer as partial — reading it the other way
    would mark every fixture/mock completion in the suite as truncated."""
    gw = _gateway(sink, audit_sink)
    # litellm's own ModelResponse SUBSTITUTES "stop" for a None finish_reason at
    # construction, so the silent-provider case has to be built by clearing the
    # field afterwards — constructing it with None would test litellm's default,
    # not this gateway's reading.
    silent = _reply("complete", "stop")
    silent.choices[0].finish_reason = None  # type: ignore[union-attr]

    _install(monkeypatch, gw, silent)

    result = asyncio.run(gw.complete(_request()))

    assert result.finish_reason is None
    assert result.truncated is False
    row = _success_row(audit_sink, "ne.gateway.complete")
    assert row.provenance is not None
    assert "finish_reason" not in row.provenance


def test_truncation_marking_carries_no_model_text(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C12: ``finish_reason`` is a provider STATUS token. The truncated row
    gains counts and a status word — never a substring of the answer."""
    gw = _gateway(sink, audit_sink)
    _install(monkeypatch, gw, _reply(_PARTIAL, "length"))

    asyncio.run(gw.complete(_request()))

    for event in audit_sink.events:
        if isinstance(event, AuditEvent):
            assert _PARTIAL not in (event.detail or "")
            assert _PARTIAL not in str(event.provenance)


def test_truncation_does_not_consume_the_declared_fallback(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated-but-useful completion is NOT routed past. Treating it like
    ``ReasoningExhaustedError`` would burn a second full generation to redo work
    the caller already has, and on the demo's ``agent`` route — which declares NO
    fallback — it would convert a partial answer into a hard failure."""
    settings = make_settings(
        [make_model("primary"), make_model("backup")],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install(monkeypatch, gw, _reply(_PARTIAL, "length"))

    result = asyncio.run(gw.complete(_request()))

    assert result.model_id == "primary"
    # Exactly ONE provider call was metered — the fallback was never dialled.
    assert len(sink.events) == 1


# -- stream() ---------------------------------------------------------------


def _chunks(finish_reason: str | None) -> list[Any]:
    """An OpenAI-compatible stream: content deltas, then the chunk that carries
    the finish_reason, then a usage-only terminal chunk whose ``choices`` is
    EMPTY — which is why the reason cannot be read off ``last_chunk``."""
    return [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="par"), finish_reason=None)],
            usage=None,
            model="gpt-3.5-turbo",
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(delta=SimpleNamespace(content="tial"), finish_reason=finish_reason)
            ],
            usage=None,
            model="gpt-3.5-turbo",
        ),
        SimpleNamespace(choices=[], usage=None, model="gpt-3.5-turbo"),
    ]


def _install_stream(
    monkeypatch: pytest.MonkeyPatch, gw: LiteLLMGateway, finish_reason: str | None
) -> None:
    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> AsyncIterator[Any]:
            for chunk in _chunks(finish_reason):
                yield chunk

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)


async def _drain(gen: object) -> list[CompletionChunk]:
    out: list[CompletionChunk] = []
    async for chunk in gen:  # type: ignore[attr-defined]
        out.append(chunk)
    return out


def test_truncated_stream_says_so_in_its_terminal_row(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream hands the consumer every token BEFORE anything knows how it
    ended, so the terminal row is the only place the cut can be recorded.
    Reading the reason off ``last_chunk`` instead of the drain loop -> RED (the
    terminal usage-only chunk has no ``choices`` and reports nothing)."""
    gw = _gateway(sink, audit_sink)
    _install_stream(monkeypatch, gw, "length")

    chunks = asyncio.run(_drain(gw.stream(_request())))
    assert "".join(c.delta for c in chunks) == "partial"

    row = _success_row(audit_sink, "ne.gateway.stream")
    assert row.detail is not None
    assert "TRUNCATED" in row.detail
    assert "the streamed answer is PARTIAL" in row.detail
    assert row.provenance is not None
    assert row.provenance["finish_reason"] == "length"


def test_complete_stream_records_nothing_new(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE PAIR, stream side: a stream that ended normally produces the row it
    produced before this unit existed."""
    gw = _gateway(sink, audit_sink)
    _install_stream(monkeypatch, gw, "stop")

    asyncio.run(_drain(gw.stream(_request())))

    row = _success_row(audit_sink, "ne.gateway.stream")
    assert row.detail == "task=draft lane=frontier-api model=only"
    assert row.provenance is not None
    assert "finish_reason" not in row.provenance
