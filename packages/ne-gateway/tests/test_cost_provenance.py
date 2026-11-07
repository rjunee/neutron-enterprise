# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-163a — per-model-call cost/tokens in the success-path AuditEvent
provenance, so ``/v1/audit/replay?trace_id`` becomes the queryable per-trace
cost/token substrate (NE-167) without a new cost store or a separate NE-165
route. Every assertion here is against a real ``AuditEvent`` recorded by
``RecordingAuditSink``, driven by the sanctioned litellm boundary mock
(``Router.acompletion`` monkeypatch), following the established patterns in
test_cost_events.py / test_instrumentation.py / test_streaming.py."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from ne.contracts import ChatMessage, CompletionChunk, CompletionRequest
from ne.gateway import LiteLLMGateway, ModelCallFailedError, TaskRoute
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str = "draft") -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def _find_event(events: Sequence[object], action: str) -> AuditEvent:
    """Find the (narrowed) AuditEvent with the given action — mirrors the
    helper in test_instrumentation.py (NE-162 adds a redact.pre_egress event
    ahead of every frontier attempt's own ne.gateway.complete/stream event)."""
    for event in events:
        if isinstance(event, AuditEvent) and event.action == action:
            return event
    raise AssertionError(f"no AuditEvent with action={action!r} in {events!r}")


async def _drain(gen: object) -> list[CompletionChunk]:
    out: list[CompletionChunk] = []
    async for chunk in gen:  # type: ignore[attr-defined]
        out.append(chunk)
    return out


def test_complete_success_audit_provenance_carries_cost_tokens_model(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [make_model("primary")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.001234},
            model="gpt-3.5-turbo-0125",
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request()))

    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.outcome == AuditOutcome.success
    provenance = audit_event.provenance
    assert provenance["cost_usd"] == "0.001234"
    assert provenance["input_tokens"] == "3"
    assert provenance["output_tokens"] == "4"
    assert provenance["response_model"] == "gpt-3.5-turbo-0125"
    assert provenance["lane"] == "frontier-api"
    assert provenance["task"] == "draft"
    assert provenance["model_name"] == "primary"
    assert audit_event.config_hash == frozen_config


def test_complete_audit_provenance_agrees_with_cost_event(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [make_model("primary")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.001234},
            model="gpt-3.5-turbo-0125",
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request()))

    assert len(sink.events) == 1
    cost_event = sink.events[0]
    assert cost_event.cost_usd == 0.001234
    assert cost_event.input_tokens == 3
    assert cost_event.output_tokens == 4
    assert cost_event.lane == "frontier-api"
    assert cost_event.task == "draft"

    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    provenance = audit_event.provenance
    assert provenance["cost_usd"] == str(cost_event.cost_usd)
    assert provenance["input_tokens"] == str(cost_event.input_tokens)
    assert provenance["output_tokens"] == str(cost_event.output_tokens)


def test_stream_success_audit_provenance_carries_cost_tokens_model(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [make_model("primary")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])
            yield SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=7),
                _hidden_params={"response_cost": 0.000321},
                model="gpt-3.5-turbo-0125",
            )

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(_drain(gw.stream(_request())))

    assert len(sink.events) == 1
    cost_event = sink.events[0]
    audit_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert audit_event.outcome == AuditOutcome.success
    provenance = audit_event.provenance
    assert provenance["cost_usd"] == "0.000321"
    assert provenance["input_tokens"] == "3"
    assert provenance["output_tokens"] == "7"
    assert provenance["response_model"] == "gpt-3.5-turbo-0125"
    assert provenance["lane"] == "frontier-api"
    assert provenance["cost_usd"] == str(cost_event.cost_usd)
    assert provenance["input_tokens"] == str(cost_event.input_tokens)
    assert provenance["output_tokens"] == str(cost_event.output_tokens)


def test_stream_without_usage_omits_cost_keys_never_stringifies_none(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    asyncio.run(_drain(gw.stream(_request())))

    audit_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert audit_event.outcome == AuditOutcome.success
    provenance = audit_event.provenance
    assert "cost_usd" not in provenance
    assert "input_tokens" not in provenance
    assert "output_tokens" not in provenance
    assert all(value != "None" for value in provenance.values())


def test_failed_candidates_audit_has_no_cost_keys(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom-1")),
            make_model("backup", mock_response=Exception("boom-2")),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )

    gw_complete = LiteLLMGateway(
        settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR
    )
    with pytest.raises(ModelCallFailedError):
        asyncio.run(gw_complete.complete(_request()))
    assert sink.events == []
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.outcome == AuditOutcome.error
    provenance = audit_event.provenance
    assert "cost_usd" not in provenance
    assert "input_tokens" not in provenance
    assert "output_tokens" not in provenance
    assert "response_model" not in provenance
    # B12: the all-candidates-failed terminal event aggregates N attempts — no
    # single per-call measurement or principal attribution exists to stamp.
    assert "latency_ms" not in provenance
    assert "ttft_ms" not in provenance
    assert "principal" not in provenance

    stream_sink = RecordingCostSink()
    stream_audit_sink = RecordingAuditSink()
    gw_stream = LiteLLMGateway(
        settings, stream_sink, audit_sink=stream_audit_sink, redactor=PASSTHROUGH_REDACTOR
    )
    with pytest.raises(ModelCallFailedError):
        asyncio.run(_drain(gw_stream.stream(_request())))
    assert stream_sink.events == []
    stream_audit_event = _find_event(stream_audit_sink.events, "ne.gateway.stream")
    assert stream_audit_event.outcome == AuditOutcome.error
    stream_provenance = stream_audit_event.provenance
    assert "cost_usd" not in stream_provenance
    assert "input_tokens" not in stream_provenance
    assert "output_tokens" not in stream_provenance
    assert "response_model" not in stream_provenance
    assert "latency_ms" not in stream_provenance
    assert "ttft_ms" not in stream_provenance
    assert "principal" not in stream_provenance


_SENTINEL = "SENTINEL-PII-123-45-6789"


def _sentinel_request() -> CompletionRequest:
    return CompletionRequest(messages=(ChatMessage(role="user", content=_SENTINEL),), task="draft")


def test_no_message_content_in_success_audit_provenance(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [make_model("primary")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_SENTINEL))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.001234},
            model="gpt-3.5-turbo-0125",
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_sentinel_request()))

    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert _SENTINEL not in repr(audit_event.detail)
    assert _SENTINEL not in repr(audit_event.provenance)

    stream_sink = RecordingCostSink()
    stream_audit_sink = RecordingAuditSink()
    gw_stream = LiteLLMGateway(
        settings, stream_sink, audit_sink=stream_audit_sink, redactor=PASSTHROUGH_REDACTOR
    )

    async def fake_stream_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=_SENTINEL))]
            )
            yield SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=7),
                _hidden_params={"response_cost": 0.000321},
                model="gpt-3.5-turbo-0125",
            )

        return gen()

    monkeypatch.setattr(gw_stream._router, "acompletion", fake_stream_acompletion)
    asyncio.run(_drain(gw_stream.stream(_sentinel_request())))

    stream_audit_event = _find_event(stream_audit_sink.events, "ne.gateway.stream")
    assert _SENTINEL not in repr(stream_audit_event.detail)
    assert _SENTINEL not in repr(stream_audit_event.provenance)


def test_sub_microdollar_cost_is_not_zeroed_in_provenance(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Codex P2 regression: a fixed ``.6f`` format zeroed sub-1e-6 USD costs
    # ("0.000000") and broke the per-trace cost-aggregation equality NE-167 relies
    # on. ``str(cost)`` must round-trip to the exact CostEvent value.
    tiny = 0.0000004  # 4e-7 USD — a real per-call cost for a cheap local model
    settings = make_settings(
        [make_model("primary")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            _hidden_params={"response_cost": tiny},
            model="local-8b",
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request()))

    cost_event = sink.events[0]
    provenance = _find_event(audit_sink.events, "ne.gateway.complete").provenance
    assert provenance["cost_usd"] != "0.000000"  # would have been zeroed by .6f
    assert provenance["cost_usd"] == str(cost_event.cost_usd)
    assert float(str(provenance["cost_usd"])) == tiny  # round-trips exactly


# ---------------------------------------------------------------------------
# B12 — per-call latency_ms / ttft_ms / principal, same NE-163a disciplines:
# stringified, OMITTED when unknown, byte-equal to the sibling CostEvent.
# Timing assertions are LOWER bounds only (asyncio.sleep guarantees at-least
# semantics); upper bounds would be phantom-failure bait under parallel load.
# ---------------------------------------------------------------------------


def _stringified_int(value: str) -> int:
    """Assert the NE-163a stringified-int convention and return the int."""
    assert value == str(int(value)), f"not a canonical stringified int: {value!r}"
    return int(value)


def test_complete_provenance_latency_present_ttft_absent(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed (non-stream) call records its measured latency on the chain
    and the CostEvent — the SAME value on both — and carries NO ttft_ms: a
    non-stream call has no first-token fact, and the key is a stream fact."""
    settings = make_settings([make_model("primary")], {"draft": TaskRoute(model="primary")})
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.03)  # a real, bounded-below duration to measure
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            _hidden_params={"response_cost": 0.001234},
            model="gpt-3.5-turbo-0125",
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request()))

    cost_event = sink.events[0]
    provenance = _find_event(audit_sink.events, "ne.gateway.complete").provenance

    latency = _stringified_int(str(provenance["latency_ms"]))
    assert latency >= 25  # the 30 ms sleep was actually measured, not hardcoded 0
    assert cost_event.latency_ms is not None
    assert provenance["latency_ms"] == str(cost_event.latency_ms)  # byte-equal, like cost_usd

    assert "ttft_ms" not in provenance
    assert cost_event.ttft_ms is None


def test_stream_clean_provenance_latency_and_ttft(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanly-drained stream records BOTH measurements: ttft_ms (first chunk
    received) and latency_ms (establish→termination), ttft ≤ latency, each
    byte-equal to its CostEvent slot."""
    settings = make_settings([make_model("primary")], {"draft": TaskRoute(model="primary")})
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> AsyncIterator[object]:
            await asyncio.sleep(0.03)  # time-to-first-chunk
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])
            await asyncio.sleep(0.02)  # further drain time after the first chunk
            yield SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=7),
                _hidden_params={"response_cost": 0.000321},
                model="gpt-3.5-turbo-0125",
            )

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(_drain(gw.stream(_request())))

    cost_event = sink.events[0]
    event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert event.outcome == AuditOutcome.success
    provenance = event.provenance

    ttft = _stringified_int(str(provenance["ttft_ms"]))
    latency = _stringified_int(str(provenance["latency_ms"]))
    assert ttft >= 25  # the pre-first-chunk sleep was measured
    assert latency >= 45  # ...and the full drain, not just the establish phase
    assert ttft <= latency  # one clock, one origin: first chunk precedes termination
    assert provenance["ttft_ms"] == str(cost_event.ttft_ms)
    assert provenance["latency_ms"] == str(cost_event.latency_ms)


def test_stream_abandoned_provenance_latency_and_ttft(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """THE path that gets forgotten: a consumer that abandons mid-stream still
    consumed tokens and time — the error-outcome terminal event and its
    CostEvent BOTH carry latency_ms and ttft_ms (a chunk was received)."""
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def consume_one_then_abandon() -> None:
        agen = gw.stream(_request())
        await agen.__anext__()  # type: ignore[attr-defined]
        await agen.aclose()  # type: ignore[attr-defined]

    asyncio.run(consume_one_then_abandon())

    assert len(sink.events) == 1
    cost_event = sink.events[0]
    event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert event.outcome == AuditOutcome.error  # abandoned, yet still measured

    ttft = _stringified_int(str(event.provenance["ttft_ms"]))
    latency = _stringified_int(str(event.provenance["latency_ms"]))
    assert 0 <= ttft <= latency
    assert event.provenance["ttft_ms"] == str(cost_event.ttft_ms)
    assert event.provenance["latency_ms"] == str(cost_event.latency_ms)


def test_stream_midstream_failure_provenance_latency_and_ttft(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third abnormal termination: a provider failure mid-drain finalizes
    with both measurements too — same clock, same keys, error outcome."""
    settings = make_settings([make_model("primary")], {"draft": TaskRoute(model="primary")})
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])
            raise RuntimeError("provider exploded mid-stream")

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    with pytest.raises(RuntimeError):
        asyncio.run(_drain(gw.stream(_request())))

    cost_event = sink.events[0]
    event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert event.outcome == AuditOutcome.error
    ttft = _stringified_int(str(event.provenance["ttft_ms"]))
    latency = _stringified_int(str(event.provenance["latency_ms"]))
    assert 0 <= ttft <= latency
    assert event.provenance["latency_ms"] == str(cost_event.latency_ms)


def test_stream_failing_before_first_chunk_omits_ttft(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that establishes but dies before ANY chunk has no first-token
    fact: ttft_ms is OMITTED (absent means unknown), while latency_ms — the
    time the failed call still took — is present."""
    settings = make_settings([make_model("primary")], {"draft": TaskRoute(model="primary")})
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        async def gen() -> AsyncIterator[object]:
            raise RuntimeError("dead before the first chunk")
            yield  # pragma: no cover — makes this an async generator

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    with pytest.raises(RuntimeError):
        asyncio.run(_drain(gw.stream(_request())))

    cost_event = sink.events[0]
    event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert "ttft_ms" not in event.provenance
    assert cost_event.ttft_ms is None
    assert "latency_ms" in event.provenance
    assert provenance_has_no_stringified_none(event.provenance)


def provenance_has_no_stringified_none(provenance: Any) -> bool:
    """The NE-163a 'never the string None' convention, checked over every value."""
    return all(value != "None" for value in dict(provenance).values())


def test_principal_provenance_byte_equals_cost_actor_both_transports(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    """When the request carries a principal, the chain's `principal` provenance
    key is BYTE-EQUAL to the sibling CostEvent.actor (the cost_usd equality
    discipline applied to identity) — on both transports. The chained event's
    own actor field stays the service actor (C10-conservative, unchanged)."""
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    asyncio.run(
        gw.complete(
            CompletionRequest(messages=_MSGS, task="draft", principal_id="user:ada@acme.example")
        )
    )
    complete_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert complete_event.provenance["principal"] == "user:ada@acme.example"
    assert complete_event.provenance["principal"] == sink.events[0].actor
    assert complete_event.actor == "ne-gateway"  # the chained actor field is unwidened

    stream_request = CompletionRequest(
        messages=_MSGS, task="draft", principal_id="user:bob@acme.example"
    )
    asyncio.run(_drain(gw.stream(stream_request)))
    stream_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert stream_event.provenance["principal"] == "user:bob@acme.example"
    assert stream_event.provenance["principal"] == sink.events[1].actor
    assert stream_event.actor == "ne-gateway"


def test_service_fallback_omits_principal_key_both_transports(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    """The other direction of the attribution guard: when _cost_actor falls back
    to the service actor (no principal / ADR-0018 empty string), the provenance
    key is OMITTED — stamping 'ne-gateway' as a principal would fabricate
    attribution the meter never recorded (§0.4 recorded-attribution-only)."""
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    asyncio.run(gw.complete(CompletionRequest(messages=_MSGS, task="draft", principal_id=None)))
    asyncio.run(gw.complete(CompletionRequest(messages=_MSGS, task="draft", principal_id="")))
    asyncio.run(
        _drain(gw.stream(CompletionRequest(messages=_MSGS, task="draft", principal_id=None)))
    )

    events = [
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent) and e.action in ("ne.gateway.complete", "ne.gateway.stream")
    ]
    assert len(events) == 3
    for event in events:
        assert "principal" not in event.provenance, event.action
        assert provenance_has_no_stringified_none(event.provenance)
    # ...while every CostEvent honestly says the service actor did it.
    assert [c.actor for c in sink.events] == ["ne-gateway", "ne-gateway", "ne-gateway"]


def test_usage_provenance_zero_is_emitted_and_none_is_omitted() -> None:
    """The absent-vs-zero contract, pinned on the pure function: a measured 0
    IS the string "0" (a sub-millisecond call happened and took no whole
    millisecond), and None omits the key entirely — never the string 'None',
    never a fabricated '0' for an unknown."""
    from ne.gateway.gateway import _usage_provenance

    measured_zero = _usage_provenance(
        None, None, None, None, latency_ms=0, ttft_ms=0, principal=None
    )
    assert measured_zero["latency_ms"] == "0"
    assert measured_zero["ttft_ms"] == "0"

    unknown = _usage_provenance(
        None, None, None, None, latency_ms=None, ttft_ms=None, principal=None
    )
    assert "latency_ms" not in unknown
    assert "ttft_ms" not in unknown
    assert "principal" not in unknown
    assert unknown == {}

    with_principal = _usage_provenance(
        1, 2, 0.5, "m", latency_ms=7, ttft_ms=3, principal="user:ada"
    )
    assert with_principal["principal"] == "user:ada"
    assert with_principal["latency_ms"] == "7"
    assert with_principal["ttft_ms"] == "3"
    assert provenance_has_no_stringified_none(with_principal)
