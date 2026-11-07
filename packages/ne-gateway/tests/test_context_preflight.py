# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-2a budget pre-flight (design §3.3) — end-to-end on the real Router.

Proves the pinned per-candidate ordering (egress-guard -> budget pre-flight ->
redaction), the fresh-dict ``max_tokens`` default (shared-``merged`` poisoning
regression), the all-skip vs mixed raise decision, and stream-establishment
parity. Arithmetic (estimator bytes_per_token=1.5, per_call_overhead=8):

* primary window 2048  -> reserve 256,  margin 1024 -> input_budget 768
* fallback window 32768 -> reserve 4096, margin 1639 -> input_budget 27033
* a ~1200-char user message estimates 830 tokens (skips 2048, fits 32768)
* a ~2000-char user message estimates 1364 tokens
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from ne.contracts import (
    ChatMessage,
    CompletionChunk,
    CompletionRequest,
    ContextBudgetExceededError,
)
from ne.contracts.governance import RedactionResult
from ne.gateway import LiteLLMGateway, ModelCallFailedError, TaskRoute
from ne.gateway.gateway import _estimate_messages_tokens
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_MID = "x" * 1200  # estimate 830
_BIG = "x" * 2000  # estimate 1364


def _request(task: str, content: str = _MID, **kw: Any) -> CompletionRequest:
    return CompletionRequest(messages=(ChatMessage(role="user", content=content),), task=task, **kw)


def _estimate(content: str) -> int:
    msgs = [ChatMessage(role="user", content=content).model_dump()]
    return _estimate_messages_tokens_oracle(msgs)


def _estimate_messages_tokens_oracle(msgs: list[Any]) -> int:
    from ne.gateway import GatewaySettings

    return _estimate_messages_tokens(GatewaySettings(models=[make_model("z")]).estimator(), msgs)


class CountingRedactor:
    """A ``Redactor`` that records every invocation so a test can prove the
    redactor was NEVER reached for a budget-skipped candidate (order pin)."""

    def __init__(self) -> None:
        self.calls = 0

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        self.calls += 1
        return RedactionResult(text=text)


def _fake_ok(content: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        _hidden_params={"response_cost": 0.0},
        model="fake",
    )


# -- [1] budget-skip then larger-window fallback succeeds ---------------------


def test_skip_primary_then_fallback_succeeds_with_own_max_tokens(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", context_window=2048),
            make_model("backup", context_window=32768),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    captured: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.append({"model": model, **kwargs})
        return _fake_ok("from-backup")

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    result = asyncio.run(gw.complete(_request("draft")))

    assert result.model_id == "backup"
    # Only the fallback ever reached the provider -> exactly one call, one cost.
    assert len(captured) == 1
    assert captured[0]["model"] == "backup"
    # Fallback carries its OWN default reserve (32768 -> 4096), never the
    # skipped primary's 256 — proves the fresh-dict fix (no shared-merged poison).
    assert captured[0]["max_tokens"] == 4096
    assert len(sink.events) == 1


def test_fresh_dict_no_max_tokens_poisoning_on_provider_error(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    """Even when the primary is ATTEMPTED (provider error, not a budget skip) its
    ``setdefault`` must not leak into the fallback's params: the fallback carries
    its own max_output_tokens because each candidate builds a fresh dict."""
    settings = make_settings(
        [
            make_model("primary", context_window=8192),  # reserve 1024
            make_model("backup", context_window=32768),  # reserve 4096
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    captured: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.append({"model": model, **kwargs})
        if model == "primary":
            raise RuntimeError("primary boom")
        return _fake_ok()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request("draft")))

    by_model = {c["model"]: c for c in captured}
    assert by_model["primary"]["max_tokens"] == 1024
    assert by_model["backup"]["max_tokens"] == 4096


# -- [2] all candidates budget-skip -> ContextBudgetExceededError -------------


def test_all_candidates_skip_raises_typed_error_with_primary_counts(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", context_window=2048),
            make_model("backup", context_window=2048),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    with pytest.raises(ContextBudgetExceededError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    err = excinfo.value
    assert err.stage == "gateway-preflight"
    assert err.estimated_tokens == _estimate(_MID)
    assert err.context_window == 2048
    assert err.input_budget == 768
    assert err.dropped_turns == 0
    # No provider call happened -> no CostEvent.
    assert sink.events == []
    # The all-candidates-failed AuditEvent still fired before the raise.
    audit = next(
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent) and e.action == "ne.gateway.complete"
    )
    assert audit.outcome == AuditOutcome.error


# -- [3] mixed (budget-skip + provider error) -> ModelCallFailedError ---------


def test_mixed_skip_and_provider_error_raises_model_call_failed(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", context_window=2048),  # budget-skip
            make_model(
                "backup", context_window=32768, mock_response=Exception("provider down")
            ),  # provider error
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    attempts = excinfo.value.attempts
    kinds = {name: type(exc) for name, exc in attempts}
    assert kinds["primary"] is ContextBudgetExceededError
    # backup is FRONTIER_API -> provider error sanitized to FrontierProviderError.
    from ne.gateway import FrontierProviderError

    assert kinds["backup"] is FrontierProviderError


# -- [4] frontier budget-skip: typed error, redactor never invoked ------------


def test_frontier_skip_is_typed_error_and_redactor_untouched(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    redactor = CountingRedactor()
    settings = make_settings(
        [make_model("frontier", context_window=2048)],
        {"draft": TaskRoute(model="frontier")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=redactor)

    with pytest.raises(ContextBudgetExceededError):
        asyncio.run(gw.complete(_request("draft")))

    # Pre-flight ran BEFORE redaction -> the redactor was never reached for the
    # skipped frontier candidate, and no unredacted body ever egressed.
    assert redactor.calls == 0
    assert sink.events == []


# -- [5] egress-guard denial precedes pre-flight (order pin) ------------------


def test_egress_guard_denial_precedes_preflight(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", context_window=2048)],  # also over budget
        {"draft": TaskRoute(model="primary")},
    )

    def guard(_model: object) -> None:
        raise RuntimeError("egress denied")

    gw = LiteLLMGateway(settings, sink, egress_guard=guard, redactor=PASSTHROUGH_REDACTOR)

    # Guard runs first -> its RuntimeError propagates UNWRAPPED, not the typed
    # budget error and not a ModelCallFailedError.
    with pytest.raises(RuntimeError, match="egress denied"):
        asyncio.run(gw.complete(_request("draft")))
    assert sink.events == []


# -- [6] explicit caller max_tokens honored + threaded into arithmetic --------


def test_explicit_max_tokens_forwarded_unchanged(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", context_window=32768)],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    captured: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.append({"model": model, **kwargs})
        return _fake_ok()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request("draft", params={"max_tokens": 42})))
    # Explicit caller max_tokens is NEVER overridden by the model default.
    assert captured[0]["max_tokens"] == 42


def test_explicit_oversized_max_tokens_shrinks_budget_into_skip(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", context_window=32768)],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    # _BIG (estimate 1364) fits under max_tokens=42 (budget 31087) but NOT under
    # an explicit max_tokens=30000 (budget 32768-30000-1639 = 1129).
    asyncio.run(gw.complete(_request("draft", content=_BIG, params={"max_tokens": 42})))  # fits
    with pytest.raises(ContextBudgetExceededError):
        asyncio.run(gw.complete(_request("draft", content=_BIG, params={"max_tokens": 30000})))


# -- [6b] tool schemas are counted in the pre-flight estimate (Codex C1) -------


def test_tool_schemas_counted_in_preflight(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    """C1: a ``tools`` catalog egresses to the provider and consumes the serving
    window, so the pre-flight must estimate it alongside the messages. A short
    message that fits the small primary window on its own must SKIP to the larger
    fallback once a big tool schema is attached — and the tools are forwarded to
    the provider unchanged."""
    settings = make_settings(
        [
            make_model("primary", context_window=2048),  # input_budget 768
            make_model("backup", context_window=32768),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    captured: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.append({"model": model, **kwargs})
        return _fake_ok()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)

    # Control: the bare "hi" message fits the primary window on its own.
    asyncio.run(gw.complete(_request("draft", content="hi")))
    assert captured[-1]["model"] == "primary"

    # A large tool catalog (~1500-char description -> ~1000+ tokens) overflows the
    # primary's 768-token input budget; a messages-only estimate would have let it
    # slip through. Now it skips to the 32768-window fallback.
    big_tool = {
        "type": "function",
        "function": {"name": "f", "description": "d" * 1500, "parameters": {}},
    }
    asyncio.run(gw.complete(_request("draft", content="hi", params={"tools": [big_tool]})))
    assert captured[-1]["model"] == "backup"
    # tools forwarded to the provider unchanged.
    assert captured[-1]["tools"] == [big_tool]


# -- [7] every acompletion call carries max_tokens ----------------------------


def test_every_completion_call_carries_max_tokens(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", context_window=32768)],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    captured: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.append({"model": model, **kwargs})
        return _fake_ok()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(gw.complete(_request("draft", content="hi")))
    assert "max_tokens" in captured[0]
    assert captured[0]["max_tokens"] == 4096


# -- [8] streaming parity -----------------------------------------------------


async def _drain(gen: object) -> list[CompletionChunk]:
    out: list[CompletionChunk] = []
    async for chunk in gen:  # type: ignore[attr-defined]
        out.append(chunk)
    return out


def test_stream_skip_primary_then_fallback(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [
            make_model("primary", context_window=2048),
            make_model("backup", context_window=32768, mock_response="fallback-stream"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    chunks = asyncio.run(_drain(gw.stream(_request("draft"))))
    assert "".join(c.delta for c in chunks) == "fallback-stream"
    assert all(c.model_id == "backup" for c in chunks)


def test_stream_all_skip_raises_typed_error(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [make_model("primary", context_window=2048)],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    with pytest.raises(ContextBudgetExceededError) as excinfo:
        asyncio.run(_drain(gw.stream(_request("draft"))))
    assert excinfo.value.stage == "gateway-preflight"
    assert sink.events == []


def test_stream_establishment_carries_max_tokens(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", context_window=32768)],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    captured: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.append({"model": model, **kwargs})

        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    asyncio.run(_drain(gw.stream(_request("draft", content="hi"))))
    assert captured[0]["max_tokens"] == 4096
    assert captured[0]["stream"] is True
