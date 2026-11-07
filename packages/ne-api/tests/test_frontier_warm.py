# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FRONTIER-LIVE: a staged frontier lane must be able to EARN its evidence.

Two halves, proven to fit rather than asserted separately:

* :class:`ne.api.frontier_warm.GatewayFrontierWarm` issues ONE governed
  completion, pinned to the frontier lane AND the exact alias, through the same
  gateway production traffic uses;
* :class:`ne.api.model_selection.ModelSelectionService` accepts exactly what
  that call leaves on the chain — pinned end to end by
  ``test_a_real_gateway_warm_produces_the_evidence_the_catalog_requires``,
  which runs a REAL ``LiteLLMGateway`` into a REAL ``PostgresAuditSink`` and
  then asks the REAL catalog. Nothing in that test hand-writes an audit record.

The bug this closes, pinned first as
``test_without_a_warmer_a_frontier_route_degrades_onto_its_private_fallback``:
frontier availability requires a <60s successful provider observation, and
before this unit nothing on a cold deployment produced the first one. So
``ne demo up --frontier anthropic`` appended a model that could never be
authorized — invisible in the picker, and on a route with private fallbacks
(``agent.meridian-analyst.v2``) SILENTLY demoted onto the open-weight lane.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from ne.api.frontier_warm import (
    WARM_MAX_TOKENS,
    WARM_PROMPT,
    GatewayFrontierWarm,
)
from ne.api.model_selection import (
    FRONTIER_EVIDENCE_CURRENCY_HORIZON_S,
    FRONTIER_EVIDENCE_REFRESH_INTERVAL_S,
    FRONTIER_OBSERVATION_MAX_AGE_S,
    FRONTIER_RECORDS_CACHE_TTL_S,
    MODEL_OBSERVATION_ACTION,
    MODEL_SELECTION_EVIDENCE_ACTION,
    ModelSelectionEvidenceError,
    ModelSelectionPolicyRefused,
    ModelSelectionService,
    ModelSelectionUnavailable,
)
from ne.audit import (
    CHAIN_ANCHOR_MAX_AGE_S,
    AnchoredChainVerifier,
    PostgresAuditSink,
    VerifiedAnchor,
    ensure_audit_schema,
    list_records,
    verify_chain,
)
from ne.audit.verify import ChainVerification
from ne.contracts import (
    RedactionResult,
    freeze_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.config import NeSettings
from ne.contracts.governance import DecisionEffect, PolicyDecision, PolicyRequest
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.types import Lane, Principal
from ne.events import new_audit_event
from ne.gateway._litellm import litellm
from ne.gateway.config import GatewaySettings, ModelDef, TaskRoute
from ne.gateway.egress import EgressSettings, build_egress_guard
from ne.gateway.gateway import LiteLLMGateway
from ne.schema import AuditOutcome

from .conftest import run

NOW = datetime(2026, 8, 26, 23, 30, tzinfo=UTC)
TRACE_ID = "trace-frontier-warm"
PRINCIPAL = Principal(id="user:demo", groups=("users",))
TASK = "agent.meridian-analyst.v2"

PRIVATE = ModelDef(
    name="private-default",
    litellm_model="hosted_vllm/qwen",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=32768,
)
#: Mirrors the CAPPED window ``live._gateway_lane_env`` now emits: equal to the
#: private lane's, never the provider's 200k. See
#: ``test_a_larger_frontier_window_makes_the_private_fallback_unselectable``
#: for what the un-capped value did to the air-gap beat.
FRONTIER = ModelDef(
    name="frontier-default",
    litellm_model="anthropic/claude-sonnet-4-5",
    provider="anthropic",
    lane=Lane.FRONTIER_API,
    context_window=32768,
)
#: The demo's ``--frontier`` route shape verbatim: frontier PRIMARY, private
#: fallbacks. The fallbacks are what make a silent degrade possible at all.
SETTINGS = GatewaySettings(
    models=[PRIVATE, FRONTIER],
    tasks={TASK: TaskRoute(model=FRONTIER.name, fallbacks=(PRIVATE.name,))},
)


class _Settings(NeSettings):
    demo: int = 7


@pytest.fixture(autouse=True)
def _frozen_config() -> Any:
    reset_config_hash_for_testing()
    freeze_config_hash(_Settings())
    yield
    reset_config_hash_for_testing()


@pytest.fixture
def audit_engine(tmp_path: Path) -> Any:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'frontier-warm.db'}")
    ensure_audit_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


class _ServingRow:
    def __init__(self, model: ModelDef, *, checked_at: datetime) -> None:
        self.model_id = model.name
        self.lane = model.lane.value
        self.state = "ok"
        self.checked_at = checked_at
        self.serves_configured_model = True


class _StubServing:
    """Always-healthy private lane. ``checked_at`` is stamped from the SAME
    clock the service reads, so the 5s private-observation window is satisfied
    whether a test pins the clock or uses wall time."""

    def __init__(self, models: Sequence[ModelDef], clock: Callable[[], datetime]) -> None:
        self._models = tuple(models)
        self._clock = clock

    async def observe(
        self, models: Sequence[ModelDef], *, force_refresh: bool = False
    ) -> Sequence[_ServingRow]:
        names = {m.name for m in models}
        now = self._clock()
        return tuple(
            _ServingRow(model, checked_at=now) for model in self._models if model.name in names
        )


class _AllowPdp:
    def __init__(self) -> None:
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        return PolicyDecision(
            effect=DecisionEffect.ALLOW, policy_version="warm-policy-1", reason="test"
        )


class _RecordingWarm:
    """Records the ask, and (optionally) leaves the SAME evidence a real
    gateway warm would leave — so the service half can be exercised without a
    provider, while the real-gateway test proves the record shape is right."""

    def __init__(self, sink: PostgresAuditSink | None, *, occurred_at: datetime = NOW) -> None:
        self._sink = sink
        self._occurred_at = occurred_at
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
        self.calls.append((tuple(m.name for m in models), task))
        if self._sink is None:
            return
        for model in models:
            await self._sink.append(
                new_audit_event(
                    action=MODEL_OBSERVATION_ACTION,
                    resource=model.litellm_model,
                    outcome=AuditOutcome.success,
                    actor="ne-gateway",
                    trace_id="trace-warm",
                    config_hash=get_config_hash(),
                    occurred_at=self._occurred_at,
                    provenance={
                        "task": task,
                        "model_name": model.name,
                        "operation": "complete",
                        "lane": model.lane.value,
                    },
                )
            )


def _service(
    audit_engine: sa.Engine,
    *,
    warm: Any | None = None,
    air_gap: bool = False,
    settings: GatewaySettings = SETTINGS,
    clock: Callable[[], datetime] = lambda: NOW,
    chain_verifier: AnchoredChainVerifier | None = None,
) -> tuple[ModelSelectionService, PostgresAuditSink]:
    sink = PostgresAuditSink(audit_engine)
    service = ModelSelectionService(
        gateway_settings=settings,
        serving_status=_StubServing((PRIVATE,), clock),
        audit_engine=audit_engine,
        pdp=_AllowPdp(),
        sink=sink,
        config_hash=get_config_hash(),
        air_gap_provider=lambda: air_gap,
        frontier_warm=warm,
        chain_verifier=chain_verifier,
        clock=clock,
    )
    return service, sink


def _selection_reasons(engine: sa.Engine) -> list[tuple[str, str, str]]:
    import json

    out: list[tuple[str, str, str]] = []
    for record in list_records(engine, action=MODEL_SELECTION_EVIDENCE_ACTION, limit=1000):
        detail = json.loads(record.payload["detail"])
        out.append((detail["requested_model"], detail["reason"], str(record.outcome)))
    return out


# -- the bug, pinned ---------------------------------------------------------


def test_without_a_warmer_a_frontier_route_degrades_onto_its_private_fallback(
    audit_engine: sa.Engine,
) -> None:
    """Pre-fix behaviour, kept as the control: no warmer ⇒ no frontier evidence
    ⇒ the run is quietly served by the open-weight lane. This is the demo
    failure the unit exists to end, so it is asserted rather than described."""
    service, _sink = _service(audit_engine, warm=None)

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert authorized.model == PRIVATE.name
    assert authorized.lane is Lane.PRIVATE_VLLM


def test_a_cold_frontier_lane_is_warmed_and_then_selected(audit_engine: sa.Engine) -> None:
    """MUTATION: drop ``warm_frontier=True`` from ``authorize_auto`` (or set
    ``frontier_warm=None``) and this returns ``private-default`` -> RED."""
    service, sink = _service(audit_engine, warm=None)
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm)

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert warm.calls == [((FRONTIER.name,), TASK)]
    assert authorized.model == FRONTIER.name
    assert authorized.lane is Lane.FRONTIER_API


def test_an_explicit_frontier_request_also_warms(audit_engine: sa.Engine) -> None:
    service, sink = _service(audit_engine, warm=None)
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm)

    authorized = run(
        service.authorize(
            task=TASK, requested_model=FRONTIER.name, principal=PRINCIPAL, trace_id=TRACE_ID
        )
    )

    assert warm.calls == [((FRONTIER.name,), TASK)]
    assert authorized.model == FRONTIER.name


def test_a_display_read_warms_the_task_it_is_actually_routed_to(
    audit_engine: sa.Engine,
) -> None:
    """The Ask picker only ever submits a model it was OFFERED (it sends an
    explicit ``requested_model``, never "use the route"). So a catalog that
    cannot offer the primary this task is ROUTED to does not merely omit a row
    — the picker silently submits a DIFFERENT model than the operator
    configured, and the frontier lane is unreachable from the product no matter
    how correctly the route is set.

    MUTATION: drop the route-primary-is-frontier arm of ``should_warm`` in
    ``_choices`` and the frontier row never appears for a frontier-routed
    task -> RED.
    """
    service, sink = _service(audit_engine, warm=None)
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm)

    catalog = run(service.choices(TASK))

    assert warm.calls == [((FRONTIER.name,), TASK)]
    assert [c.model for c in catalog.choices] == [PRIVATE.name, FRONTIER.name]
    assert catalog.effective_default == FRONTIER.name


def test_a_display_read_never_warms_a_privately_routed_task(
    audit_engine: sa.Engine,
) -> None:
    """A registered-but-unrouted frontier model costs nothing: a deployment
    that did not route a task at the frontier lane never egresses to list its
    choices, however many frontier aliases sit in the registry."""
    settings = GatewaySettings(
        models=[PRIVATE, FRONTIER],
        tasks={TASK: TaskRoute(model=PRIVATE.name, fallbacks=(FRONTIER.name,))},
    )
    service, sink = _service(audit_engine, warm=None, settings=settings)
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm, settings=settings)

    catalog = run(service.choices(TASK))

    assert warm.calls == []
    assert [c.model for c in catalog.choices] == [PRIVATE.name]


def test_current_evidence_is_not_re_warmed(audit_engine: sa.Engine) -> None:
    """A lane already inside the 60s window costs no extra provider call."""
    service, sink = _service(audit_engine, warm=None)
    warm = _RecordingWarm(sink)
    run(warm((FRONTIER,), task=TASK))
    warm.calls.clear()
    service, _ = _service(audit_engine, warm=warm)

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert warm.calls == []
    assert authorized.model == FRONTIER.name


def test_stale_evidence_is_re_warmed(audit_engine: sa.Engine) -> None:
    """61s old is not evidence about the present — the 60s rule is unchanged."""
    service, sink = _service(audit_engine, warm=None)
    stale = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=61))
    run(stale((FRONTIER,), task=TASK))
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm)

    run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert warm.calls == [((FRONTIER.name,), TASK)]


def test_a_failed_warm_leaves_the_lane_unavailable(audit_engine: sa.Engine) -> None:
    """Absence of evidence, never an invented availability: a warm that wrote
    nothing must leave the catalog exactly as it found it."""
    warm = _RecordingWarm(None)  # records the ask, appends no observation
    service, _sink = _service(audit_engine, warm=warm)

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert warm.calls == [((FRONTIER.name,), TASK)]
    assert authorized.model == PRIVATE.name
    assert (FRONTIER.name, "unavailable", AuditOutcome.denied.value) in _selection_reasons(
        audit_engine
    )


# -- air gap: the kill must be legible, not a silent absence -----------------


def test_air_gap_refuses_the_frontier_lane_without_attempting_a_warm(
    audit_engine: sa.Engine,
) -> None:
    """The flag can never loosen a seal: under AIR_GAP the frontier candidate
    is dropped BEFORE the warm branch, so no outbound call is even attempted,
    and the private fallback serves the run.

    MUTATION: remove the ``_emit_auto_skips`` call from ``authorize_auto`` and
    the air-gap denial disappears from the chain -> RED. That record is the
    difference between 'the air gap killed the frontier lane and the sovereign
    lane took over' and an unexplained change of served model.
    """
    service, sink = _service(audit_engine, warm=None)
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm, air_gap=True)

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert warm.calls == []
    assert authorized.model == PRIVATE.name
    reasons = _selection_reasons(audit_engine)
    assert (FRONTIER.name, "air-gap", AuditOutcome.denied.value) in reasons
    assert (PRIVATE.name, "authorized", AuditOutcome.success.value) in reasons


def test_air_gap_refuses_an_explicit_frontier_selection(audit_engine: sa.Engine) -> None:
    service, sink = _service(audit_engine, warm=None)
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm, air_gap=True)

    with pytest.raises(ModelSelectionPolicyRefused) as excinfo:
        run(
            service.authorize(
                task=TASK, requested_model=FRONTIER.name, principal=PRINCIPAL, trace_id=TRACE_ID
            )
        )

    assert "AIR_GAP" in str(excinfo.value)
    assert warm.calls == []


def test_a_larger_frontier_window_leaves_the_private_fallback_selectable(
    audit_engine: sa.Engine,
) -> None:
    """WHY ``live._gateway_lane_env`` no longer caps the emitted frontier window.

    THE OLD BEHAVIOUR, which this test used to pin: ``refusal_for_selection``
    refused any model whose input budget was smaller than the task PRIMARY's, so
    with a 200k model as the primary of ``agent.meridian-analyst.v2`` the 32k
    ``private-default`` it declares as its own fallback became UNSELECTABLE.
    Engaging AIR_GAP then killed the frontier lane AND the sovereign lane it was
    supposed to fall back to, and the run refused outright. The emitter's
    ``min(...)`` window cap existed to prevent that — at the price of every lane
    declaring the narrowest lane's window.

    NE-CTX-IN-FORCE removed the floor instead. The trim guard re-derives its
    budget from the model in force, so the private lane survives the gap with its
    own window and its own budget — which is the whole point of the air-gap beat.
    """
    uncapped = FRONTIER.model_copy(update={"context_window": 200_000})
    settings = GatewaySettings(
        models=[PRIVATE, uncapped],
        tasks={TASK: TaskRoute(model=uncapped.name, fallbacks=(PRIVATE.name,))},
    )
    service, _sink = _service(audit_engine, warm=None, air_gap=True, settings=settings)

    catalog = run(service.choices(TASK))

    # The frontier primary is still gone — AIR_GAP is enforced, and that arm of
    # `refusal_for_selection` is untouched. Only the budget arm was removed.
    assert [choice.model for choice in catalog.choices] == [PRIVATE.name]
    assert catalog.effective_default == PRIVATE.name


# -- the warm call itself ----------------------------------------------------


class _RecordingGateway:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.requests: list[CompletionRequest] = []
        self._failure = failure

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self._failure is not None:
            raise self._failure
        return Completion(
            text="ok",
            model_id=request.requested_model or "?",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    def stream(self, request: CompletionRequest) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def test_the_warm_pins_both_the_lane_and_the_alias() -> None:
    """Both together are what stop a route's PRIVATE fallback from answering a
    frontier warm and minting evidence for the wrong lane
    (``LiteLLMGateway._candidates`` filters by lane once one is pinned).

    MUTATION: drop ``lane=Lane.FRONTIER_API`` -> RED.
    """
    gateway = _RecordingGateway()
    warm = GatewayFrontierWarm(gateway)

    run(warm((FRONTIER,), task=TASK))

    (request,) = gateway.requests
    assert request.lane is Lane.FRONTIER_API
    assert request.requested_model == FRONTIER.name
    assert request.task == TASK
    assert dict(request.params)["max_tokens"] == WARM_MAX_TOKENS
    assert [m.content for m in request.messages] == [WARM_PROMPT]


def test_the_warm_never_egresses_anything_a_user_typed() -> None:
    """The prompt is a module literal, not assembled from any request."""
    gateway = _RecordingGateway()
    run(GatewayFrontierWarm(gateway)((FRONTIER,), task=TASK))
    (request,) = gateway.requests
    assert [m.content for m in request.messages] == ["Reply with exactly one word: ok"]


def test_the_warm_never_raises() -> None:
    """A provider failure is absence of evidence, never an exception the
    selection path has to interpret."""
    gateway = _RecordingGateway(failure=RuntimeError("provider down"))
    run(GatewayFrontierWarm(gateway)((FRONTIER,), task=TASK))
    assert len(gateway.requests) == 1


def test_the_warm_ignores_a_private_alias() -> None:
    gateway = _RecordingGateway()
    run(GatewayFrontierWarm(gateway)((PRIVATE,), task=TASK))
    assert gateway.requests == []


def test_the_warm_debounces_repeats_for_the_same_alias() -> None:
    clock = iter(
        [
            NOW,  # _recently_warmed (first, no entry)
            NOW,  # stamped
            NOW + timedelta(seconds=1),  # _recently_warmed (second)
        ]
    )
    gateway = _RecordingGateway()
    warm = GatewayFrontierWarm(gateway, clock=lambda: next(clock))

    run(warm((FRONTIER,), task=TASK))
    run(warm((FRONTIER,), task=TASK))

    assert len(gateway.requests) == 1


# -- the two halves, proven to fit -------------------------------------------


class _CostSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


class _PassthroughRedactor:
    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult:
        return RedactionResult(text=text)


def _mocked_frontier_gateway(sink: PostgresAuditSink, *, air_gap: bool) -> LiteLLMGateway:
    mocked = FRONTIER.model_copy(
        update={"extra_params": {"api_key": "test", "mock_response": "ok"}}
    )
    settings = GatewaySettings(
        models=[PRIVATE, mocked],
        tasks={TASK: TaskRoute(model=mocked.name, fallbacks=(PRIVATE.name,))},
    )
    return LiteLLMGateway(
        settings,
        _CostSink(),
        egress_guard=build_egress_guard(
            EgressSettings(allowed_frontier_hosts=("api.anthropic.com",), air_gap=air_gap)
        ),
        audit_sink=sink,
        redactor=_PassthroughRedactor(),
    )


def test_a_real_gateway_warm_produces_the_evidence_the_catalog_requires(
    audit_engine: sa.Engine,
) -> None:
    """The anti-vacuous core: a REAL ``LiteLLMGateway`` completion, a REAL audit
    sink, and the REAL catalog rule — no hand-written observation anywhere.

    If the record the gateway writes and the record ``_frontier_choices``
    accepts ever drift (actor, operation, lane, config_hash, action), this goes
    RED where two separately-stubbed halves would both stay green.
    """
    sink = PostgresAuditSink(audit_engine)
    gateway = _mocked_frontier_gateway(sink, air_gap=False)
    service, _ = _service(audit_engine, warm=GatewayFrontierWarm(gateway), clock=_utcnow)

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert authorized.model == FRONTIER.name
    assert authorized.lane is Lane.FRONTIER_API
    observations = list(list_records(audit_engine, action=MODEL_OBSERVATION_ACTION, limit=100))
    assert [record.outcome for record in observations] == [AuditOutcome.success.value]


#: What ``anthropic/claude-fable-5`` GENERATES for :data:`WARM_PROMPT` when it
#: uses the think channel: one thinking block plus the one-word answer, 20
#: completion tokens (largest of the live 2026-09-09 sweep; 18 on the other
#: thinking-carrying sample, 4 when it does not think).
#:
#: This is a size of OUTPUT, not a setting. It does not move with
#: ``max_tokens`` — the sweep measured the same handful of tokens at ceilings
#: from 16 to 2048 — which is exactly why it, and not the constant under test,
#: is what decides whether a given ceiling truncates. Keying the stub off
#: ``WARM_MAX_TOKENS`` (or off any constant derived from it) would make the
#: provider answer iff the ceiling cleared the threshold the assertion is
#: about, and the test would pass at any value.
_REASONED_REPLY_TOKENS = 20


def _install_always_reasoning_provider(
    monkeypatch: pytest.MonkeyPatch, gateway: LiteLLMGateway
) -> None:
    """A provider stub that reasons, and truncates the way a real one does.

    Shaped after ``anthropic/claude-fable-5``, which is in
    ``live._THINKING_DISABLE_REFUSED`` (the provider 400s a thinking disable),
    so the think channel is always available and the model may open any reply
    with a thinking block. The rule the stub implements is the only thing
    ``max_tokens`` actually does: generation stops at the ceiling.

    * ceiling >= ``_REASONED_REPLY_TOKENS`` — the block AND the answer fit;
      ``content='ok'``, ``finish_reason='stop'``, 20 completion tokens.
    * ceiling < that — generation is cut off before any answer token is
      emitted. The reply is the live shape from the error rows: ``content``
      empty, ``reasoning_content`` empty (thinking text withheld, only the
      signature survives), ONE thinking block, ``finish_reason='length'`` at
      exactly ``max_tokens``.

    ``Router.acompletion`` is the sanctioned seam (the one ``ne-gateway``'s
    ``test_thinking_channel`` uses): litellm's ``mock_response`` cannot shape a
    ``thinking_blocks``-bearing message.
    """

    block = {"type": "thinking", "thinking": "", "signature": "opaque-replay-token"}

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        ceiling = int(kwargs["max_tokens"])
        if ceiling < _REASONED_REPLY_TOKENS:
            return litellm.ModelResponse(
                choices=[
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "thinking_blocks": [block],
                            "reasoning_content": "",
                        },
                        "finish_reason": "length",
                    }
                ],
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": ceiling,
                    "total_tokens": 12 + ceiling,
                },
            )
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "thinking_blocks": [block],
                        "reasoning_content": "",
                    },
                    "finish_reason": "stop",
                }
            ],
            usage={
                "prompt_tokens": 12,
                "completion_tokens": _REASONED_REPLY_TOKENS,
                "total_tokens": 12 + _REASONED_REPLY_TOKENS,
            },
        )

    monkeypatch.setattr(gateway._router, "acompletion", fake_acompletion)


def test_the_shipped_ceiling_lets_a_reasoning_reply_evidence_the_lane(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, end to end, at the ceiling this module actually ships.

    Live on the demo tip, ``frontier-best`` warms are 26,934 success / 2,507
    error since 2026-08-27 (7.1% over the last 12h): the lane evidences ~93% of
    the time, and the defect is that the remaining ~7% burn a cycle. Every one
    of those is this shape — the model used the think channel and the 16-token
    ceiling stopped generation before an answer token, so the gateway raised
    ``ReasoningWithoutAnswerError``, wrote the observation with outcome
    ``error``, and the lane sat past its 60s window for ~3.4s until the
    expiry-anchored retry landed. The control says the think channel is the
    whole cause: ``claude-sonnet-5``, thinking disabled, is 1,258 / 0.

    The gate is NOT touched: the assertion below is still the REAL catalog rule
    reading a REAL gateway-written observation. What is pinned is that the
    SHIPPED ceiling clears the reply the live model actually generates.

    MUTATION: ``WARM_MAX_TOKENS = 16`` -> RED. 16 < 20, so the stub truncates
    mid-think exactly as the provider did, the observation is written with
    outcome ``error``, no frontier choice survives, and this goes red on
    ``authorized.model == 'private-default'`` — the production symptom itself,
    the silent degrade onto the private fallback. Any ceiling from 20 up is
    green; 256 is where it ships because that is >12x the largest reply ever
    measured, and the sweep showed no latency or billing reason to go higher.
    """
    sink = PostgresAuditSink(audit_engine)
    gateway = _mocked_frontier_gateway(sink, air_gap=False)
    _install_always_reasoning_provider(monkeypatch, gateway)
    service, _ = _service(audit_engine, warm=GatewayFrontierWarm(gateway), clock=_utcnow)

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert authorized.model == FRONTIER.name
    assert authorized.lane is Lane.FRONTIER_API
    observations = list(list_records(audit_engine, action=MODEL_OBSERVATION_ACTION, limit=100))
    assert [record.outcome for record in observations] == [AuditOutcome.success.value]


def test_a_warm_truncated_before_any_answer_still_earns_nothing(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half, and the one that must NOT move with the constant.

    Raising the ceiling is only safe if a truncated warm remains a non-event.
    This pins the ceiling at 16 EXPLICITLY — not via ``WARM_MAX_TOKENS`` — so
    it keeps testing the truncated shape no matter what the constant becomes,
    and it fails if anyone ever "fixes" the 7% by letting a
    ``finish_reason='length'`` thinking-only reply count as reachability: the
    observation stays ``error`` and the lane stays unevidenced, so the run
    degrades VISIBLY onto the private fallback rather than minting a false
    success on the frontier lane.
    """
    sink = PostgresAuditSink(audit_engine)
    gateway = _mocked_frontier_gateway(sink, air_gap=False)
    _install_always_reasoning_provider(monkeypatch, gateway)
    warm = GatewayFrontierWarm(gateway, max_tokens=16)
    service, _ = _service(audit_engine, warm=warm, clock=_utcnow)

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert authorized.model == PRIVATE.name
    assert authorized.lane is Lane.PRIVATE_VLLM
    observations = list(list_records(audit_engine, action=MODEL_OBSERVATION_ACTION, limit=100))
    assert [record.outcome for record in observations] == [AuditOutcome.error.value]


def test_air_gap_kills_a_real_gateway_warm(audit_engine: sa.Engine) -> None:
    """Belt to the selection-layer brace: even called directly, with the
    frontier candidate never filtered out, the C2 guard refuses before
    transport and the lane earns nothing."""
    sink = PostgresAuditSink(audit_engine)
    gateway = _mocked_frontier_gateway(sink, air_gap=True)

    run(GatewayFrontierWarm(gateway)((FRONTIER,), task=TASK))

    service, _ = _service(audit_engine, warm=None, clock=_utcnow)
    catalog = run(service.choices(TASK))
    assert [c.model for c in catalog.choices] == [PRIVATE.name]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# -- NE-SELECT-REPROBE / NE-SELECT-LATENCY -----------------------------------


class _WarmMissingOnce:
    """Warms, but leaves usable evidence only from the SECOND ask onward.

    Models the observed live failure: on a multi-hop turn whose hops are slower
    than the 60s window, the first warm's row is already outside the window (or
    not yet visible) when the catalog is rebuilt, so the lane looks dark even
    though it is demonstrably serving.
    """

    def __init__(self, sink: PostgresAuditSink) -> None:
        self._sink = sink
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
        self.calls.append((tuple(m.name for m in models), task))
        if len(self.calls) < 2:
            return
        for model in models:
            await self._sink.append(
                new_audit_event(
                    action=MODEL_OBSERVATION_ACTION,
                    resource=model.litellm_model,
                    outcome=AuditOutcome.success,
                    actor="ne-gateway",
                    trace_id="trace-warm",
                    config_hash=get_config_hash(),
                    occurred_at=NOW,
                    provenance={
                        "task": task,
                        "model_name": model.name,
                        "operation": "complete",
                        "lane": model.lane.value,
                    },
                )
            )


def test_a_lapsed_window_is_re_probed_once_instead_of_failing_the_run(
    audit_engine: sa.Engine,
) -> None:
    """The live defect: policy ALLOWED the model and the lane was serving, but a
    single missing evidence row failed the whole user run. One inline re-probe
    settles it with a REAL call — no invented evidence, no substituted model."""
    _service_seed, sink = _service(audit_engine, warm=None)
    warm = _WarmMissingOnce(sink)
    service, _ = _service(audit_engine, warm=warm)

    authorized = run(
        service.authorize(
            task=TASK,
            requested_model=FRONTIER.name,
            principal=PRINCIPAL,
            trace_id=TRACE_ID,
        )
    )

    assert authorized.model == FRONTIER.name
    assert len(warm.calls) == 2, "exactly ONE inline re-probe, never a retry loop"


def test_a_genuinely_dark_lane_still_refuses_after_the_re_probe(
    audit_engine: sa.Engine,
) -> None:
    """The re-probe must not become a way to admit a lane that is really down:
    a warm that writes no evidence leaves the refusal exactly as it was."""
    warm = _RecordingWarm(None)  # records the ask, appends nothing
    service, _ = _service(audit_engine, warm=warm)

    with pytest.raises(ModelSelectionUnavailable):
        run(
            service.authorize(
                task=TASK,
                requested_model=FRONTIER.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )

    assert len(warm.calls) == 2, "one warm + one re-probe, then it stops"


def test_current_evidence_is_read_from_the_chain_once_not_per_authorize(
    audit_engine: sa.Engine,
) -> None:
    """NE-SELECT-LATENCY: each read is an O(chain) replay + FULL-chain verify.
    Paying it per model call is what put minutes between a turn's hops."""
    _service_seed, sink = _service(audit_engine, warm=None)
    seed = _RecordingWarm(sink)
    run(seed((FRONTIER,), task=TASK))
    service, _ = _service(audit_engine, warm=_RecordingWarm(sink))

    reads: list[int] = []
    original = service._frontier_records

    def counting(models: Sequence[ModelDef]) -> Any:
        reads.append(1)
        return original(models)

    service._frontier_records = counting  # type: ignore[method-assign]

    for _ in range(2):
        run(
            service.authorize(
                task=TASK,
                requested_model=FRONTIER.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )

    assert reads == [1], f"expected ONE chain read across both authorizes, got {len(reads)}"


class _ExplodingWarm:
    """A warm that RAISES — a docstring-violating implementation.

    Production ``GatewayFrontierWarm`` never raises (it swallows the provider
    exception after the gateway audits the attempt), so this double models a
    BROKEN warm, and is used only to prove the callers survive one.  Failure
    behaviour tests must use the two production shapes below instead — an
    earlier revision used this double for those and its assertions silently
    skipped the darkening the real shape causes.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
        self.calls += 1
        raise RuntimeError("provider unreachable")


class _AuditedFailureWarm:
    """THE production failure shape, end to end.

    ``GatewayFrontierWarm`` issues a real completion; on a dead provider
    ``LiteLLMGateway`` appends an ``ne.gateway.model.observed`` row with
    ``outcome=error`` (transport WAS attempted — see
    ``LiteLLMGateway._record_model_observation``) and the warm then SWALLOWS
    the provider exception (``frontier_warm.py``: "the attempt is audited
    either way, which IS the evidence").  So a failed warm leaves a NEWER
    error observation on the chain and returns normally.
    """

    def __init__(self, sink: PostgresAuditSink, *, clock: Callable[[], datetime]) -> None:
        self._sink = sink
        self._clock = clock
        self.calls = 0

    async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
        self.calls += 1
        for model in models:
            await self._sink.append(
                new_audit_event(
                    action=MODEL_OBSERVATION_ACTION,
                    resource=model.litellm_model,
                    outcome=AuditOutcome.error,
                    actor="ne-gateway",
                    trace_id="trace-warm",
                    config_hash=get_config_hash(),
                    occurred_at=self._clock(),
                    provenance={
                        "task": task,
                        "model_name": model.name,
                        "operation": "complete",
                        "lane": model.lane.value,
                    },
                )
            )
        # ...and returns normally: production swallows, it never raises.


class _RefusedBeforeTransportWarm:
    """Production's OTHER failure shape: refused before transport.

    An egress/policy refusal stops the call before the provider is reached;
    the gateway appends NO observation row (observations are recorded only
    after transport was attempted) and the warm swallows.  The chain is left
    exactly as it was.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
        self.calls += 1


def test_the_refresh_interval_stays_inside_the_currency_window() -> None:
    """The refresher must MEET the 60s rule, never widen it. Deriving the
    interval from the window is what keeps the two from drifting apart."""
    assert FRONTIER_EVIDENCE_REFRESH_INTERVAL_S == FRONTIER_OBSERVATION_MAX_AGE_S / 2
    assert FRONTIER_EVIDENCE_REFRESH_INTERVAL_S < FRONTIER_OBSERVATION_MAX_AGE_S


def test_a_background_refresh_earns_the_evidence_a_later_turn_spends(
    audit_engine: sa.Engine,
) -> None:
    """The point of the whole change: the warm happens OFF the turn, so the
    turn's own authorize finds current evidence and issues no provider call."""
    _seed, sink = _service(audit_engine, warm=None)
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm)

    assert run(service.refresh_frontier_evidence(TASK)) is True
    assert len(warm.calls) == 1

    authorized = run(
        service.authorize(
            task=TASK,
            requested_model=FRONTIER.name,
            principal=PRINCIPAL,
            trace_id=TRACE_ID,
        )
    )

    assert authorized.model == FRONTIER.name
    assert len(warm.calls) == 1, "the user's turn paid for no warm of its own"


def test_a_refresh_against_a_dead_lane_never_raises_into_its_caller(
    audit_engine: sa.Engine,
) -> None:
    """Fail-safe: the refresher is an optimisation, never authority. A provider
    outage must degrade to today's semantics, not take the loop (or a run) down."""
    warm = _ExplodingWarm()
    service, _ = _service(audit_engine, warm=warm)

    assert run(service.refresh_frontier_evidence(TASK)) is False
    assert warm.calls == 1


def test_the_refresher_picks_one_frontier_routed_task_not_every_task(
    audit_engine: sa.Engine,
) -> None:
    """Warming is per-MODEL, so a second task would only double provider calls."""
    service, _ = _service(audit_engine, warm=_RecordingWarm(None))
    assert service._frontier_refresh_task() == TASK


# -- NE-SELECT-LATENCY: the refresher is PROACTIVE, not reactive -------------
#
# The defect these pin (owner-reported, ~45s before any activity on a turn):
# the refresher's cold filter used the full 60s window, so a record aged
# 30-60s passed every 30s tick untouched, expired mid-interval, and the next
# admitted turn paid the provider warm + a forced O(chain) chain verify INLINE.
# ~A third of every cycle was a stale sliver — a pure phase lottery per
# admission.


def test_the_currency_horizon_is_derived_so_it_cannot_drift() -> None:
    """horizon + interval <= window is THE structural guarantee: a record that
    passes one tick still satisfies the full window at the next tick, so an
    admitted turn never finds stale evidence while the refresher runs."""
    assert FRONTIER_EVIDENCE_CURRENCY_HORIZON_S == (
        FRONTIER_OBSERVATION_MAX_AGE_S - FRONTIER_EVIDENCE_REFRESH_INTERVAL_S
    )
    assert (
        FRONTIER_EVIDENCE_CURRENCY_HORIZON_S + FRONTIER_EVIDENCE_REFRESH_INTERVAL_S
        <= FRONTIER_OBSERVATION_MAX_AGE_S
    )
    assert FRONTIER_EVIDENCE_CURRENCY_HORIZON_S > 0
    # The evidence-read cache is refilled ONLY by the forced post-warm
    # re-read, and refills land one observation period (interval + slack +
    # warm cost) apart — so the reuse window must EXCEED the interval or the
    # tail of every cycle pays the inline chain read, and it never usefully
    # exceeds the window (a window-old read holds only expired rows).
    assert FRONTIER_RECORDS_CACHE_TTL_S == FRONTIER_OBSERVATION_MAX_AGE_S
    assert FRONTIER_RECORDS_CACHE_TTL_S > FRONTIER_EVIDENCE_REFRESH_INTERVAL_S


def test_an_admission_in_the_stale_sliver_pays_no_inline_warm(
    audit_engine: sa.Engine,
) -> None:
    """FAILS on the reactive refresher (pre-fix): evidence aged 35s is inside
    the 60s window but will NOT be at the next 30s tick.  The proactive
    refresher re-warms it NOW, so the admission that lands 30s later — dead
    centre of what used to be the stale sliver — issues no provider call and
    pays no forced chain re-read of its own."""
    moment = [NOW]
    _seed, sink = _service(audit_engine, warm=None)
    seed = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=35))
    run(seed((FRONTIER,), task=TASK))
    warm = _RecordingWarm(sink, occurred_at=NOW)
    service, _ = _service(audit_engine, warm=warm, clock=lambda: moment[0])

    # The background tick.  Pre-fix this declined to warm (35s < 60s) and the
    # sliver stayed open.
    assert run(service.refresh_frontier_evidence(TASK)) is True
    assert len(warm.calls) == 1, "the tick must re-warm evidence past the 30s horizon"

    # The user's turn arrives 30s later: the ORIGINAL evidence is now 65s old
    # (stale), but the tick's warm is 30s old (current).  The admission spends
    # evidence the background already earned.
    moment[0] = NOW + timedelta(seconds=30)
    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert authorized.model == FRONTIER.name
    assert len(warm.calls) == 1, "the admitted turn paid for no inline warm"


def test_the_refresher_loop_proactively_warms_on_its_interval(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LOOP itself (not just the method) applies the proactive horizon on
    its own tick.  Also pins that the background refresher still runs on its
    interval at all."""
    import ne.api.model_selection as model_selection_module

    monkeypatch.setattr(model_selection_module, "FRONTIER_EVIDENCE_REFRESH_INTERVAL_S", 0.01)
    _seed, sink = _service(audit_engine, warm=None)
    seed = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=35))
    run(seed((FRONTIER,), task=TASK))
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm)

    async def drive() -> None:
        loop_task = asyncio.create_task(service.run_frontier_evidence_refresher())
        try:
            for _ in range(500):
                if warm.calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

    run(drive())
    assert warm.calls == [((FRONTIER.name,), TASK)], (
        "the loop's tick must warm 35s-old evidence (past the proactive horizon), exactly once"
    )


def test_the_refresher_does_not_re_warm_evidence_inside_its_horizon(
    audit_engine: sa.Engine,
) -> None:
    """Proactive is not paranoid: evidence young enough to survive until the
    next tick costs no extra provider call."""
    _seed, sink = _service(audit_engine, warm=None)
    seed = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=10))
    run(seed((FRONTIER,), task=TASK))
    warm = _RecordingWarm(sink)
    service, _ = _service(audit_engine, warm=warm)

    assert run(service.refresh_frontier_evidence(TASK)) is True
    assert warm.calls == []


def test_a_warm_refused_before_transport_leaves_still_current_evidence_admittable(
    audit_engine: sa.Engine,
) -> None:
    """The horizon tightens WHEN the background warm happens, never the
    admission rule — for the failure shape that leaves the chain UNTOUCHED.
    A proactive warm refused before transport (production's swallowing,
    nothing-appended shape) leaves 35s-old evidence exactly as current as the
    60s window says it is: the refresh still finds the lane evidenced, the
    turn is admitted, and the admission's own cold filter (still the FULL
    window) issues no second warm.

    An earlier revision claimed this with ``_ExplodingWarm`` — a raising
    double no production path matches (``GatewayFrontierWarm`` swallows) —
    and that mis-model also hid the audited-error behaviour pinned by the
    next test."""
    _seed, sink = _service(audit_engine, warm=None)
    seed = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=35))
    run(seed((FRONTIER,), task=TASK))
    warm = _RefusedBeforeTransportWarm()
    service, _ = _service(audit_engine, warm=warm)

    # The refusal appended nothing, so the forced post-warm re-read re-selects
    # the still-current 35s-old success and the lane stays evidenced.
    assert run(service.refresh_frontier_evidence(TASK)) is True
    assert warm.calls == 1

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert authorized.model == FRONTIER.name
    assert warm.calls == 1, "admission must still judge currency by the full window"


def test_a_warm_whose_failed_attempt_is_audited_darkens_the_lane_by_name(
    audit_engine: sa.Engine,
) -> None:
    """The production failure shape the exploding double hid, stated plainly.

    When the proactive warm's provider call is ATTEMPTED and fails, the
    gateway audits the attempt as a newer ``observed[error]`` row and the warm
    swallows.  The forced post-warm re-read then selects that error row —
    newest observation wins, the same rule fix (1) enforces for cross-process
    appends — so the still-current 35s-old success is superseded and the lane
    darkens NOW, not at the 60s boundary.  The next automatic admission pays
    ONE more real inline probe and then falls back BY NAME, with the skipped
    frontier candidate recorded as denied/unavailable on the chain: fail
    closed and audited, never a silent success against a lane whose newest
    real measurement is a failure.

    This is weaker than the earlier revision's claim ("a failed proactive
    warm leaves still-current evidence admittable" — true only for the
    nothing-appended refusal shape above), and the AS-BUILT row says so."""
    moment = [NOW]
    _seed, sink = _service(audit_engine, warm=None)
    seed = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=35))
    run(seed((FRONTIER,), task=TASK))
    warm = _AuditedFailureWarm(sink, clock=lambda: moment[0])
    service, _ = _service(audit_engine, warm=warm, clock=lambda: moment[0])

    # The 35s-old success is past the 30s proactive horizon: the tick warms,
    # the attempt fails and is AUDITED, and the re-read sees the error.
    assert run(service.refresh_frontier_evidence(TASK)) is False
    assert warm.calls == 1

    authorized = run(service.authorize_auto(task=TASK, principal=PRINCIPAL, trace_id=TRACE_ID))

    assert authorized.model == PRIVATE.name, (
        "with the newest observation an audited failure, the automatic policy "
        "must fall back rather than admit against the superseded success"
    )
    assert warm.calls == 2, (
        "the admission's catalog build re-probes the dark lane exactly once "
        "inline (authorize_auto's second pass suppresses its own warm)"
    )
    assert (FRONTIER.name, "unavailable", AuditOutcome.denied.value) in _selection_reasons(
        audit_engine
    ), "the passed-over frontier primary must be denied BY NAME on the chain"


def test_a_cached_success_is_not_reused_after_a_later_observation_supersedes_it(
    audit_engine: sa.Engine,
) -> None:
    """FAILS on 80c54311: the records cache authorized against a SUPERSEDED
    success.  Only THIS process's own warm invalidates the cache
    (``force=True``); an ``observed[error]`` row appended by anyone else — a
    replica's warm, or normal frontier traffic failing — left the cached
    success authoritative for a full TTL, and raising that TTL 5s → 60s
    (NE-SELECT-LATENCY round 2) stretched the exposure to almost the whole
    currency window: a model whose NEWEST on-chain observation is a failure
    stayed authorizable, with no inline re-probe at all, and the admission
    record asserted a liveness the chain itself contradicted.

    The fix bounds every reuse against the read's verified baseline head (an
    indexed, chain-verified ``seq > baseline`` extension proof — never an
    O(chain) replay, so it cannot recreate the 45s defect) and FOLDS the
    newer error row into the snapshot: the admission below must re-measure
    the lane for real (two governed warm attempts: the cold warm and the
    NE-SELECT-REPROBE re-measure) and then refuse BY NAME, instead of
    admitting off the snapshot with zero provider calls."""
    moment = [NOW]
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    warm = _AuditedFailureWarm(sink, clock=lambda: moment[0])
    service, _ = _service(audit_engine, warm=warm, clock=lambda: moment[0])

    reads: list[tuple[str, ...]] = []
    original = service._frontier_records

    def counting(models: Sequence[ModelDef]) -> Any:
        reads.append(tuple(m.name for m in models))
        return original(models)

    service._frontier_records = counting  # type: ignore[method-assign]

    # Fill the cache with a display read while the success is fresh.
    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]
    assert reads == [(FRONTIER.name,)]
    assert warm.calls == 0

    # ANOTHER writer supersedes it: a later error observation for the same
    # alias under the same config hash (what a replica's failed warm, or a
    # user's failed frontier call, leaves on the shared chain).
    run(
        sink.append(
            new_audit_event(
                action=MODEL_OBSERVATION_ACTION,
                resource=FRONTIER.litellm_model,
                outcome=AuditOutcome.error,
                actor="ne-gateway",
                trace_id="trace-other-writer",
                config_hash=get_config_hash(),
                occurred_at=NOW + timedelta(seconds=5),
                provenance={
                    "task": TASK,
                    "model_name": FRONTIER.name,
                    "operation": "complete",
                    "lane": FRONTIER.lane.value,
                },
            )
        )
    )

    # Well inside BOTH the 60s cache TTL and the 60s currency window: only
    # invalidation can save this admission, not expiry.
    moment[0] = NOW + timedelta(seconds=10)

    with pytest.raises(ModelSelectionUnavailable):
        run(
            service.authorize(
                task=TASK,
                requested_model=FRONTIER.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )

    assert warm.calls == 2, (
        "the admission must re-measure the superseded lane for real — the "
        "cold warm plus the NE-SELECT-REPROBE re-measure — not decide from "
        "the snapshot (on 80c54311 this is 0: no probe, no read, a straight "
        "authorization of the dead model)"
    )
    assert len(reads) == 3, (
        "expected the initial fill plus the two post-warm re-reads — the "
        "superseding error row is FOLDED into the snapshot by the verified "
        "extension proof, so no probe-forced full read remains — got "
        f"{reads}"
    )


def test_the_refresher_loop_stays_paced_and_suspended_when_failed_warms_are_audited(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P0 await-invariant, re-proven under the PRODUCTION failure shape.

    ``test_every_pass_through_the_refresher_loop_suspends`` drives the
    dead-lane region with a RAISING warm, where the trailing paced retry
    sleep is the pass's only await.  A production failed warm instead APPENDS
    an error observation and swallows — the forced post-warm re-read then
    moves ``_frontier_observed_at``, the retry-pacing guard sees a MOVED
    target and deliberately does not sleep, and pacing must come from the
    expiry-anchored sleep re-anchoring to the error row's own
    ``occurred_at``.  This test proves that hand-off actually paces: on a
    scripted clock, consecutive failed warm attempts land exactly
    ``interval + anchor slack`` apart — the loop neither hammers the dead
    provider nor spins an await-free pass."""
    import types

    import ne.api.model_selection as model_selection_module

    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    real_sleep = asyncio.sleep
    sleeps = [0]

    async def fake_sleep(delay: float) -> None:
        sleeps[0] += 1
        fake[0] += max(delay, 0.0)
        await real_sleep(0)

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=fake_sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=35))
    run(seed((FRONTIER,), task=TASK))

    class _TimedAuditedFailureWarm(_AuditedFailureWarm):
        def __init__(self) -> None:
            super().__init__(sink, clock=fake_now)
            self.called_at: list[float] = []

        async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
            self.called_at.append(fake[0])
            await super().__call__(models, task=task)

    warm = _TimedAuditedFailureWarm()
    service, _ = _service(audit_engine, warm=warm, clock=fake_now)

    passes = [0]
    limit = 25

    class _LoopBounded(Exception):
        pass

    original_target = service._frontier_refresh_target

    def counting_target() -> datetime | None:
        passes[0] += 1
        if passes[0] > limit:
            raise _LoopBounded
        return original_target()

    service._frontier_refresh_target = counting_target  # type: ignore[method-assign]

    async def drive() -> None:
        with pytest.raises(_LoopBounded):
            await service.run_frontier_evidence_refresher()

    run(drive())

    assert len(warm.called_at) >= 3, "the dead-lane cycle was not driven"
    gaps = [later - earlier for earlier, later in itertools.pairwise(warm.called_at)]
    expected_gap = (
        FRONTIER_EVIDENCE_REFRESH_INTERVAL_S
        + model_selection_module._FRONTIER_REFRESH_ANCHOR_SLACK_S
    )
    assert gaps == [pytest.approx(expected_gap)] * len(gaps), (
        f"audited failed warms must re-anchor the schedule to their own error "
        f"rows and retry {expected_gap}s apart; got gaps {gaps}"
    )
    assert sleeps[0] >= len(warm.called_at), (
        f"{len(warm.called_at)} failed warm attempts saw only {sleeps[0]} "
        "awaits: a pass in the audited-error shape completed without "
        "suspending"
    )


def test_a_refresh_without_a_warmer_is_a_no_op(audit_engine: sa.Engine) -> None:
    """A composition that staged no frontier lane (the default everywhere) is
    byte-identical in behaviour: the refresh returns immediately.

    Strengthened after review: ``is False`` alone stayed green even with the
    warmer guard deleted (the refresh would then read + verify the whole chain
    every tick and merely find nothing to warm).  The no-op claim is COUNTED:
    no chain read, no full-chain verify, no evidence-read cache entry, and the
    refresher LOOP exits immediately rather than ticking forever.

    MUTATION: delete the ``if self._frontier_warm is None`` guard in
    ``refresh_frontier_evidence`` and the read/verify counts go non-zero ->
    RED.  Delete the loop's guard and the ``wait_for`` times out -> RED."""
    service, _ = _service(audit_engine, warm=None)

    reads: list[tuple[str, ...]] = []
    original = service._frontier_records

    def counting(models: Sequence[ModelDef]) -> Any:
        reads.append(tuple(m.name for m in models))
        return original(models)

    service._frontier_records = counting  # type: ignore[method-assign]

    assert run(service.refresh_frontier_evidence(TASK)) is False
    assert reads == [], "no warmer ⇒ no O(chain) evidence read at all"
    assert not service._frontier_cache, "no warmer ⇒ the records cache is never populated"

    # The loop half of the same guarantee: without a warmer the background
    # refresher returns at once instead of running a tick that could read.
    run(asyncio.wait_for(service.run_frontier_evidence_refresher(), timeout=5.0))
    assert reads == []
    assert not service._frontier_cache


def test_air_gap_refresh_issues_no_warm(audit_engine: sa.Engine) -> None:
    """Under AIR_GAP every frontier model is dropped from eligibility before
    the cold filter runs, so the proactive horizon changes nothing.  Counted,
    not assumed: the sealed posture pays no chain read and allocates no
    evidence-read cache entry either."""
    warm = _RecordingWarm(None)
    service, _ = _service(audit_engine, warm=warm, air_gap=True)

    reads: list[tuple[str, ...]] = []
    original = service._frontier_records

    def counting(models: Sequence[ModelDef]) -> Any:
        reads.append(tuple(m.name for m in models))
        return original(models)

    service._frontier_records = counting  # type: ignore[method-assign]

    assert run(service.refresh_frontier_evidence(TASK)) is False
    assert warm.calls == []
    assert reads == [], "air gap ⇒ frontier is ineligible before any evidence read"
    assert not service._frontier_cache


# -- NE-SELECT-LATENCY round 2: the SCHEDULE, not just the horizon -----------
#
# Review of the proactive horizon found the derived ``interval - horizon``
# schedule still V-dependent: the loop slept a fixed interval from the END of
# an iteration, so the iteration's own cost — one O(chain) evidence read (V)
# before the cold filter, the warm (W), and the forced post-warm re-read (V
# again) — was silently APPENDED to the schedule.  Consecutive observations
# landed ``interval + 2V + W`` apart, past the 60s window as soon as
# ``2V + W > interval`` (V ≳ 12.8s; the live box measures 10-27s and the
# owner's 44.7s turn implies ~20s).  The fix anchors every wake-up to the
# evidence's own ``occurred_at``.


def test_the_refresher_schedule_absorbs_the_evidence_reads_own_cost(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILS on the fixed-interval refresher (db58cac9): with a 20s evidence
    read and a 2s warm, its observations land 30 + 2·20 + 2 = 72s apart, so
    12s of every cycle admitted turns against expired evidence.  The
    expiry-anchored schedule keeps consecutive observations ``interval + W``
    apart — independent of V — so no instant exists at which the newest
    frontier evidence is older than the window.

    Everything is measured on a scripted clock (the loop's sleeps, the cache's
    monotonic reads, the chain read's cost, the warm's cost): the assertion is
    on the fake-time spacing of the warm's appended observations, never on
    wall-clock.
    """
    import types

    import ne.api.model_selection as model_selection_module

    read_cost_s = 20.0  # V: one O(chain) replay + full-chain verify
    warm_cost_s = 2.0  # W: the governed provider warm

    fake = [0.0]  # seconds since NOW, advanced by sleeps/reads/warms

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        fake[0] += max(delay, 0.0)
        await real_sleep(0)

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=fake_sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)

    class _SlowWarm:
        """Appends the same evidence a real warm would, W fake-seconds later."""

        def __init__(self) -> None:
            self.observed_at_s: list[float] = []

        async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
            fake[0] += warm_cost_s
            self.observed_at_s.append(fake[0])
            for model in models:
                await sink.append(
                    new_audit_event(
                        action=MODEL_OBSERVATION_ACTION,
                        resource=model.litellm_model,
                        outcome=AuditOutcome.success,
                        actor="ne-gateway",
                        trace_id="trace-warm",
                        config_hash=get_config_hash(),
                        occurred_at=fake_now(),
                        provenance={
                            "task": task,
                            "model_name": model.name,
                            "operation": "complete",
                            "lane": model.lane.value,
                        },
                    )
                )

    warm = _SlowWarm()
    service, _ = _service(audit_engine, warm=warm, clock=fake_now)

    original = service._frontier_records

    def slow_read(models: Sequence[ModelDef]) -> Any:
        fake[0] += read_cost_s
        return original(models)

    service._frontier_records = slow_read  # type: ignore[method-assign]

    async def drive() -> None:
        loop_task = asyncio.create_task(service.run_frontier_evidence_refresher())
        try:
            for _ in range(5000):
                if len(warm.observed_at_s) >= 4:
                    break
                await real_sleep(0.002)
            else:
                pytest.fail("the refresher never produced four observations")
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

    run(drive())

    times = warm.observed_at_s[:4]
    gaps = [later - earlier for earlier, later in itertools.pairwise(times)]
    assert max(gaps) <= FRONTIER_OBSERVATION_MAX_AGE_S, (
        "consecutive background observations must land inside the currency "
        f"window however slow the evidence read is; got gaps {gaps} "
        f"(fixed-interval scheduling gives interval + 2V + W = "
        f"{FRONTIER_EVIDENCE_REFRESH_INTERVAL_S + 2 * read_cost_s + warm_cost_s}s)"
    )


# -- the evidence-read cache is keyed by WHAT was read -----------------------


FRONTIER_BEST = ModelDef(
    name="frontier-best",
    litellm_model="anthropic/claude-opus-4",
    provider="anthropic",
    lane=Lane.FRONTIER_API,
    context_window=32768,
)


def test_a_cached_read_for_one_model_set_is_not_reused_for_another(
    audit_engine: sa.Engine,
) -> None:
    """FAILS on the unkeyed cache (db58cac9): a read taken while only
    ``frontier-default`` was eligible was returned verbatim to a later caller
    asking about ``frontier-default`` AND ``frontier-best``, so the second
    model read as dark for the whole TTL even though its evidence sat on the
    chain the entire time.  (Fail-closed — a model can only ever look darker —
    but dark-for-no-reason is still a silent degrade, and with a warmer
    composed it bought a pointless real provider call to "re-earn" evidence
    that already existed.)

    The cache must miss when the model set changes — and still be reused when
    it does not (the second assertion keeps the NE-SELECT-LATENCY win)."""
    two_frontier = GatewaySettings(
        models=[PRIVATE, FRONTIER, FRONTIER_BEST],
        tasks={TASK: TaskRoute(model=FRONTIER.name, fallbacks=(FRONTIER_BEST.name, PRIVATE.name))},
    )
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink)
    run(seed((FRONTIER, FRONTIER_BEST), task=TASK))

    lifecycle_gate = {FRONTIER_BEST.name: False}
    service = ModelSelectionService(
        gateway_settings=two_frontier,
        serving_status=_StubServing((PRIVATE,), lambda: NOW),
        audit_engine=audit_engine,
        pdp=_AllowPdp(),
        sink=sink,
        config_hash=get_config_hash(),
        air_gap_provider=lambda: False,
        lifecycle_availability=lambda name: (lifecycle_gate.get(name, True), None),
        frontier_warm=None,  # the read must SEE the evidence, not re-earn it
        clock=lambda: NOW,
    )

    reads: list[tuple[str, ...]] = []
    original = service._frontier_records

    def counting(models: Sequence[ModelDef]) -> Any:
        reads.append(tuple(m.name for m in models))
        return original(models)

    service._frontier_records = counting  # type: ignore[method-assign]

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]
    assert FRONTIER_BEST.name not in [c.model for c in first.choices]

    lifecycle_gate[FRONTIER_BEST.name] = True
    second = run(service.choices(TASK))
    assert FRONTIER_BEST.name in [c.model for c in second.choices], (
        "current on-chain evidence for a newly-eligible model must be visible "
        "immediately, not hidden behind a read taken for a different model set"
    )
    assert reads == [
        (FRONTIER.name,),
        (FRONTIER.name, FRONTIER_BEST.name),
    ], "a changed model set is a cache MISS (one fresh read), never a reuse"

    third = run(service.choices(TASK))
    assert FRONTIER_BEST.name in [c.model for c in third.choices]
    assert len(reads) == 2, "the SAME model set still reuses the read"


def test_a_fresh_superset_read_serves_a_subset_caller_without_a_new_chain_read(
    audit_engine: sa.Engine,
) -> None:
    """FAILS on 80c54311: the exact-key cache made subset admissions pay the
    O(chain) replay+verify INLINE, recurringly.  After a read for
    ``[frontier-default, frontier-best]``, an admission whose lifecycle/
    snapshot filter leaves only ``[frontier-default]`` missed the exact-key
    lookup — and because every forced post-warm re-read CLEARS smaller
    entries and refills only its own set, the miss recurred every cycle.  A
    fresh superset read already contains every row a subset needs (each
    model's newest row is computed independently), so the subset caller must
    reuse it.

    Reuse stays bounded by fix (1)'s conditions — the TTL AND the verified
    extension proof — which the tail of this test pins: a later error row
    for the subset's model is folded into the still-TTL-fresh superset entry
    and darkens the lane WITHOUT a fresh O(chain) read."""
    moment = [NOW]
    two_frontier = GatewaySettings(
        models=[PRIVATE, FRONTIER, FRONTIER_BEST],
        tasks={TASK: TaskRoute(model=FRONTIER.name, fallbacks=(FRONTIER_BEST.name, PRIVATE.name))},
    )
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink)
    run(seed((FRONTIER, FRONTIER_BEST), task=TASK))

    lifecycle_gate = {FRONTIER_BEST.name: True}
    service = ModelSelectionService(
        gateway_settings=two_frontier,
        serving_status=_StubServing((PRIVATE,), lambda: moment[0]),
        audit_engine=audit_engine,
        pdp=_AllowPdp(),
        sink=sink,
        config_hash=get_config_hash(),
        air_gap_provider=lambda: False,
        lifecycle_availability=lambda name: (lifecycle_gate.get(name, True), None),
        frontier_warm=None,  # reads only: reuse must come from the cache, not a warm
        clock=lambda: moment[0],
    )

    reads: list[tuple[str, ...]] = []
    original = service._frontier_records

    def counting(models: Sequence[ModelDef]) -> Any:
        reads.append(tuple(m.name for m in models))
        return original(models)

    service._frontier_records = counting  # type: ignore[method-assign]

    first = run(service.choices(TASK))
    assert {FRONTIER.name, FRONTIER_BEST.name} <= {c.model for c in first.choices}
    assert reads == [(FRONTIER.name, FRONTIER_BEST.name)], "one superset read fills the cache"

    # The lifecycle filter narrows the eligible set to [frontier-default]:
    # the superset entry is fresh and unsuperseded, so NO new chain read.
    lifecycle_gate[FRONTIER_BEST.name] = False
    second = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in second.choices]
    assert FRONTIER_BEST.name not in [c.model for c in second.choices]
    assert len(reads) == 1, (
        "a subset caller must reuse the fresh superset read instead of paying "
        f"the inline O(chain) replay+verify (got {reads})"
    )

    # ...but superset reuse must not weaken the supersession rule: a later
    # error observation for the subset's own model is folded into the
    # still-TTL-fresh superset entry — the verdict follows the newest
    # observation, and no fresh O(chain) read is paid for it.
    run(
        sink.append(
            new_audit_event(
                action=MODEL_OBSERVATION_ACTION,
                resource=FRONTIER.litellm_model,
                outcome=AuditOutcome.error,
                actor="ne-gateway",
                trace_id="trace-other-writer",
                config_hash=get_config_hash(),
                occurred_at=NOW + timedelta(seconds=5),
                provenance={
                    "task": TASK,
                    "model_name": FRONTIER.name,
                    "operation": "complete",
                    "lane": FRONTIER.lane.value,
                },
            )
        )
    )
    moment[0] = NOW + timedelta(seconds=10)

    third = run(service.choices(TASK))
    assert FRONTIER.name not in [c.model for c in third.choices], (
        "a superseded success must not survive behind a fresh superset entry"
    )
    assert len(reads) == 1, (
        "the superseding error must be folded in by the verified extension "
        "proof, never paid for with a fresh O(chain) read"
    )


def test_an_absurd_refresher_horizon_cannot_weaken_admission_currency(
    audit_engine: sa.Engine,
) -> None:
    """Re-proof (post-schedule-change) of the non-negotiable: however the
    refresher is scheduled or parameterised, no turn is admitted against
    evidence older than ``FRONTIER_OBSERVATION_MAX_AGE_S``.  A 10,000s horizon
    makes 61s-old evidence look current to the refresher's COLD FILTER (so it
    never even warms) — yet the catalog and admission verdicts, hard-wired to
    the full window, still refuse the lane by name."""
    _seed, sink = _service(audit_engine, warm=None)
    stale = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=61))
    run(stale((FRONTIER,), task=TASK))
    warm = _RecordingWarm(None)  # appends nothing: the lane cannot re-earn evidence
    service, _ = _service(audit_engine, warm=warm)

    assert run(service.refresh_frontier_evidence(TASK, currency_horizon_s=10_000.0)) is False
    assert warm.calls == [], "the absurd horizon suppressed even the background warm"

    with pytest.raises(ModelSelectionUnavailable):
        run(
            service.authorize(
                task=TASK,
                requested_model=FRONTIER.name,
                principal=PRINCIPAL,
                trace_id=TRACE_ID,
            )
        )


def test_a_private_only_registry_never_reads_the_chain_for_evidence(
    audit_engine: sa.Engine,
) -> None:
    """The sealed private-only composition, counted: with no frontier model
    registered there is no frontier evidence question to answer — no O(chain)
    read (and therefore no replay / full-chain verify from this module), no
    records-cache entry, no warm — even with a warmer injected and the
    refresher's method invoked directly."""
    private_only = GatewaySettings(
        models=[PRIVATE],
        tasks={TASK: TaskRoute(model=PRIVATE.name, fallbacks=())},
    )
    warm = _RecordingWarm(None)
    service, _ = _service(audit_engine, warm=warm, settings=private_only)

    reads: list[tuple[str, ...]] = []
    original = service._frontier_records

    def counting(models: Sequence[ModelDef]) -> Any:
        reads.append(tuple(m.name for m in models))
        return original(models)

    service._frontier_records = counting  # type: ignore[method-assign]

    assert run(service.refresh_frontier_evidence(TASK)) is False
    catalog = run(service.choices(TASK))

    assert [c.model for c in catalog.choices] == [PRIVATE.name]
    assert warm.calls == []
    assert reads == [], "no frontier models ⇒ the audit chain is never read"
    assert not service._frontier_cache


# -- P0: every pass through the refresher loop must SUSPEND ------------------


def test_every_pass_through_the_refresher_loop_suspends(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILS on b71b751d: with the anchor already in the past and every
    frontier route repointed off its frontier primary — the demo's air-gap
    prerequisite (docs/demo/README.md: every frontier-routed task is repointed
    before the Tier-1 engage), which is exactly what makes
    ``_frontier_refresh_task()`` return ``None`` while catalog reads keep the
    anchor armed — the loop body reached ``continue`` without a single await.

    That is not a busy task burning a core: a coroutine that never suspends
    never hands control back to the event loop, so the API stops serving
    EVERYTHING until the evidence ages out (measured: ``60 - evidence_age``
    seconds of total stall, 28.9s at age 31, re-armed by every read that
    refreshes a frontier row).

    The invariant is structural, not shape-specific: the loop is driven
    through every branch under a scripted clock —

    * a future anchor (the capped sleep-and-re-anchor branch);
    * a past anchor with no frontier task (the paced ``continue`` above);
    * no anchor at all (the base-interval pacing branch);
    * a past anchor WITH a frontier task, where the whole refresh completes
      without suspending: the pre-warm read is a records-cache hit (an
      uncontended ``asyncio.Lock`` acquire never yields) and the injected
      warm raises before its first await — the ``FrontierWarm`` docstring
      forbids raising but nothing enforces it, which is exactly why
      ``refresh_frontier_evidence`` catches — so the trailing paced retry
      sleep is that pass's ONLY await, and deleting it is a genuine
      await-free spin

    — and the assertion is that the number of loop passes never exceeds the
    number of times the loop awaited (the anchor may be consulted twice per
    pass — once at the top, once by the post-refresh retry check — so N
    consultations bound at least ``N // 2`` completed passes).  Any future
    edit that re-opens ANY await-free path through those branches re-fails
    it.
    """
    import types

    import ne.api.model_selection as model_selection_module

    fake = [0.0]  # seconds since NOW, advanced ONLY by the loop's own sleeps

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    real_sleep = asyncio.sleep
    sleeps = [0]

    async def fake_sleep(delay: float) -> None:
        sleeps[0] += 1
        fake[0] += max(delay, 0.0)
        await real_sleep(0)

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=fake_sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)
    warm = _RecordingWarm(sink, occurred_at=NOW)
    routes: list[dict[str, TaskRoute]] = [dict(SETTINGS.tasks)]
    service = ModelSelectionService(
        gateway_settings=SETTINGS,
        serving_status=_StubServing((PRIVATE,), fake_now),
        audit_engine=audit_engine,
        pdp=_AllowPdp(),
        sink=sink,
        config_hash=get_config_hash(),
        air_gap_provider=lambda: False,
        frontier_warm=warm,
        route_provider=lambda: routes[0],
        clock=fake_now,
    )

    # Earn real evidence at fake t=0, anchoring the refresh target at
    # occurred_at + interval + slack = t+30.05 — then repoint the only
    # frontier-routed task onto its private lane (the air-gap beat's
    # prerequisite state).  The frontier model stays registered and eligible,
    # so the anchor stays armed while ``_frontier_refresh_task()`` is None.
    assert run(service.refresh_frontier_evidence(TASK)) is True
    routes[0] = {TASK: TaskRoute(model=PRIVATE.name, fallbacks=(FRONTIER.name,))}

    # Start with the anchor in the FUTURE so the first pass takes the sleeping
    # branch; the second pass then finds it in the past with no frontier task
    # (the b71b751d spin), and once the evidence ages past the window the
    # remaining passes run with no anchor at all.
    fake[0] = 10.0

    passes = [0]
    limit = 25

    class _LoopBounded(Exception):
        pass

    original_target = service._frontier_refresh_target

    def counting_target() -> datetime | None:
        passes[0] += 1
        if passes[0] > limit:
            raise _LoopBounded
        return original_target()

    service._frontier_refresh_target = counting_target  # type: ignore[method-assign]

    async def drive() -> None:
        with pytest.raises(_LoopBounded):
            await service.run_frontier_evidence_refresher()

    run(drive())

    assert sleeps[0] >= limit, (
        f"{limit} passes through the refresher loop performed only "
        f"{sleeps[0]} awaits: at least one path through the loop body "
        "completes without suspending, so the coroutine monopolises the "
        "event loop and the API stops serving until the evidence ages out"
    )
    assert len(warm.calls) == 1, (
        "with every frontier route repointed, pacing must come from sleeping, "
        "never from hammering the provider warm"
    )

    # -- The task-resolved region: a refresh that never suspends -------------
    #
    # Re-arm the loop with the routes restored, evidence still current but
    # past its anchor, the records cache warm, and a warmer that fails BEFORE
    # its first await.  ``refresh_frontier_evidence`` then completes
    # synchronously — cache-hit read, warm raises, no post-warm re-read — and
    # the trailing paced retry sleep is the only thing standing between this
    # state and an event-loop monopoly.
    routes[0] = dict(SETTINGS.tasks)
    service._frontier_warm = _RecordingWarm(sink, occurred_at=fake_now())
    assert run(service.refresh_frontier_evidence(TASK)) is True

    chain_reads: list[tuple[str, ...]] = []
    original_records = service._frontier_records

    def counting_records(models: Sequence[ModelDef]) -> Any:
        chain_reads.append(tuple(m.name for m in models))
        return original_records(models)

    service._frontier_records = counting_records  # type: ignore[method-assign]

    class _SyncRaisingWarm:
        def __init__(self) -> None:
            self.calls = 0
            self.chain_reads_at_first_call: int | None = None

        async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
            if self.calls == 0:
                self.chain_reads_at_first_call = len(chain_reads)
            self.calls += 1
            raise RuntimeError("provider client failed before its first await")

    raising = _SyncRaisingWarm()
    service._frontier_warm = raising

    # Anchor (occurred_at + 30.05s) in the past; the evidence (60s window) and
    # the cache (60s TTL, refilled by the post-warm re-read just above) are
    # both still current, so the pre-warm read on the next pass is a hit.
    fake[0] += 35.0

    passes[0] = 0
    sleeps[0] = 0
    run(drive())

    assert raising.calls >= 1, "the task-resolved branch was never driven"
    assert raising.chain_reads_at_first_call == 0, (
        "the pre-warm read was meant to be a records-cache hit, so that the "
        "refresh reaches the failing warm without a single suspension point"
    )
    assert sleeps[0] >= limit // 2, (
        f"{passes[0]} anchor consultations (at most two per pass) saw only "
        f"{sleeps[0]} awaits: a pass whose refresh completes without "
        "suspending — cache-hit read, warm failing before its first await — "
        "must still be paced by the trailing retry sleep, or the coroutine "
        "monopolises the event loop"
    )


# -- P2: the read-cache TTL must cover the whole observation period ----------


def test_no_admission_phase_pays_an_inline_chain_read_while_the_refresher_runs(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILS on b71b751d: the records-cache TTL was derived from the refresh
    interval (30s), but the cache is refilled ONLY by the forced post-warm
    re-read — a cache hit never renews ``read_at`` — so refills land one full
    OBSERVATION PERIOD apart: interval + anchor slack + warm cost W (34.45s
    at the live box's measured W≈4.4s).  The last ``W + slack`` seconds of
    every cycle therefore missed the cache, and ~13% of admissions still paid
    the inline O(chain) replay + full-chain verify this cache exists to keep
    off the user's turn.

    Everything runs on a scripted clock: the REAL refresher loop is driven
    for several cycles (V=2s reads, W=4.4s warms), the refill spacing is
    measured off the recorded read completions, and then admissions are
    sampled at every 0.05s phase of one steady-state cycle with the cache
    restored between samples.  The uncovered fraction — samples that issue a
    chain read — must be ZERO."""
    import types

    import ne.api.model_selection as model_selection_module

    # V: coverage is total only for V + W <= window - interval - slack
    # (29.95s; see FRONTIER_RECORDS_CACHE_TTL_S) — 2.0 sits well inside it.
    read_cost_s = 2.0
    warm_cost_s = 4.4  # W: the governed provider warm, as measured live

    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    real_sleep = asyncio.sleep
    parked = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        if warm.calls >= 5:
            # Freeze the world at the first sleep AFTER the fifth warm — i.e.
            # after its forced re-read refilled the cache, since the loop body
            # never sleeps between the two.  Merely polling ``warm.calls`` from
            # the drive loop raced: fake sleeps cost no real time, so the
            # refresher could rip through another cycle and append an
            # observation row POSTDATING the snapshot taken below — a row the
            # sampled (rewound) clock then places in the future.  The reuse
            # probe rightly refuses to reuse a snapshot the chain has moved
            # past, which turned that harness race into a spurious warm.
            parked.set()
            await asyncio.Event().wait()
        fake[0] += max(delay, 0.0)
        await real_sleep(0)

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=fake_sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)

    class _SlowWarm:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
            fake[0] += warm_cost_s
            self.calls += 1
            for model in models:
                await sink.append(
                    new_audit_event(
                        action=MODEL_OBSERVATION_ACTION,
                        resource=model.litellm_model,
                        outcome=AuditOutcome.success,
                        actor="ne-gateway",
                        trace_id="trace-warm",
                        config_hash=get_config_hash(),
                        occurred_at=fake_now(),
                        provenance={
                            "task": task,
                            "model_name": model.name,
                            "operation": "complete",
                            "lane": model.lane.value,
                        },
                    )
                )

    warm = _SlowWarm()
    service, _ = _service(audit_engine, warm=warm, clock=fake_now)

    read_done_at: list[float] = []
    original = service._frontier_records

    def slow_read(models: Sequence[ModelDef]) -> Any:
        fake[0] += read_cost_s
        records = original(models)
        read_done_at.append(fake[0])
        return records

    service._frontier_records = slow_read  # type: ignore[method-assign]

    async def drive() -> None:
        loop_task = asyncio.create_task(service.run_frontier_evidence_refresher())
        try:
            for _ in range(5000):
                if parked.is_set():
                    break
                await real_sleep(0.002)
            else:
                pytest.fail("the refresher never produced five observations")
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

    run(drive())
    assert warm.calls == 5, "the parked loop must stop exactly after the fifth warm's refill"

    # Pin the premise: once steady, cache refills (each a forced post-warm
    # read) land exactly one observation period apart — interval + slack + W —
    # NOT one interval apart.  This spacing is what the TTL must cover.
    cycle_s = (
        FRONTIER_EVIDENCE_REFRESH_INTERVAL_S
        + model_selection_module._FRONTIER_REFRESH_ANCHOR_SLACK_S
        + warm_cost_s
    )
    steady_gaps = [later - earlier for earlier, later in itertools.pairwise(read_done_at[-3:])]
    assert steady_gaps == [pytest.approx(cycle_s), pytest.approx(cycle_s)], (
        f"steady-state refills must land interval + slack + W = {cycle_s}s apart; got {steady_gaps}"
    )

    # Sample an admission at every 0.05s phase of the next full cycle, with
    # the post-refill cache state restored before each sample.  A sample that
    # issues a chain read is an admission paying the inline O(chain)
    # replay+verify — the exact cost this cache exists to keep off the turn.
    refill_at = read_done_at[-1]
    snapshot = dict(service._frontier_cache)
    phases = [round(step * 0.05, 2) for step in range(int(cycle_s / 0.05))]
    uncovered: list[float] = []

    async def sample_all() -> None:
        for phase in phases:
            fake[0] = refill_at + phase
            service._frontier_cache = dict(snapshot)
            reads_before = len(read_done_at)
            await service.choices(TASK)
            if len(read_done_at) > reads_before:
                uncovered.append(phase)

    warms_before = warm.calls
    run(sample_all())

    assert warm.calls == warms_before, (
        "sampled admissions inside the window must never pay a provider warm"
    )
    assert uncovered == [], (
        f"{len(uncovered)} of {len(phases)} admission phases "
        f"({100 * len(uncovered) / len(phases):.1f}%) still pay an inline "
        f"O(chain) read (phases {uncovered[:5]}...): the read-cache TTL does "
        f"not cover the {cycle_s}s observation period"
    )


# -- foreign traffic is FOLDED into the snapshot, never re-paid --------------
#
# The live-demo shape: every agent turn's own frontier calls append
# ``ne.gateway.model.observed`` SUCCESS rows (``gateway.py``
# ``_record_model_observation`` runs after every attempted frontier
# transport).  A reuse bound that forced the full O(chain) replay+verify on
# ANY newer observation row therefore re-paid the multi-second inline read on
# most admissions of an actively used deployment — the very defect the cache
# exists to remove, back through a different door.  The fix folds the
# chain-verified extension rows into the snapshot instead.


def _observed(
    sink: PostgresAuditSink,
    *,
    model: ModelDef = FRONTIER,
    outcome: AuditOutcome,
    occurred_at: datetime,
    config_hash: str | None = None,
    trace_id: str = "trace-other-writer",
) -> None:
    """What ANY other writer's frontier transport leaves on the shared chain."""
    run(
        sink.append(
            new_audit_event(
                action=MODEL_OBSERVATION_ACTION,
                resource=model.litellm_model,
                outcome=outcome,
                actor="ne-gateway",
                trace_id=trace_id,
                config_hash=config_hash if config_hash is not None else get_config_hash(),
                occurred_at=occurred_at,
                provenance={
                    "task": TASK,
                    "model_name": model.name,
                    "operation": "complete",
                    "lane": model.lane.value,
                },
            )
        )
    )


def _counting_reads(service: ModelSelectionService) -> list[tuple[str, ...]]:
    reads: list[tuple[str, ...]] = []
    original = service._frontier_records

    def counting(models: Sequence[ModelDef]) -> Any:
        reads.append(tuple(m.name for m in models))
        return original(models)

    service._frontier_records = counting  # type: ignore[method-assign]
    return reads


def test_a_foreign_success_observation_never_forces_a_full_chain_read(
    audit_engine: sa.Engine,
) -> None:
    """FAILS on 702d0173: ordinary use invalidated the cache every turn.

    The reuse probe treated ANY newer observation row — error OR success —
    as supersession and re-paid the full O(chain) replay+verify, so in a
    live demo each turn's own successful frontier calls forced the next
    admission back onto the 10-27s inline read.  The verified extension
    proof folds the success in instead: ZERO additional chain reads, and the
    verdict still reflects the newer row — pinned here by advancing the
    clock past the ORIGINAL observation's currency window, so only the
    folded row can be keeping the lane lit."""
    moment = [NOW]
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    service, _ = _service(audit_engine, warm=None, clock=lambda: moment[0])
    reads = _counting_reads(service)

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]
    assert reads == [(FRONTIER.name,)]

    # Foreign SUCCESS traffic — another turn's frontier call succeeding.
    _observed(sink, outcome=AuditOutcome.success, occurred_at=NOW + timedelta(seconds=5))

    # Past the seed's 60s currency window, inside the folded row's: the
    # admission below is lit BY the folded row or not at all.
    moment[0] = NOW + timedelta(seconds=63)

    authorized = run(
        service.authorize(
            task=TASK,
            requested_model=FRONTIER.name,
            principal=PRINCIPAL,
            trace_id=TRACE_ID,
        )
    )
    assert authorized.model == FRONTIER.name
    assert authorized.observed_at == NOW + timedelta(seconds=5), (
        "the folded success must be the evidence the admission carries"
    )
    assert reads == [(FRONTIER.name,)], (
        "a foreign SUCCESS observation must be folded into the snapshot, "
        f"never re-paid as a full O(chain) replay+verify (got {reads})"
    )


def test_a_foreign_error_still_darkens_the_lane_without_a_chain_read(
    audit_engine: sa.Engine,
) -> None:
    """The fold must not weaken 702d0173's correctness win: a newer
    ``observed[error]`` still supersedes the cached success — now via the
    fold, with no full chain read either."""
    moment = [NOW]
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    service, _ = _service(audit_engine, warm=None, clock=lambda: moment[0])
    reads = _counting_reads(service)

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]

    _observed(sink, outcome=AuditOutcome.error, occurred_at=NOW + timedelta(seconds=5))
    moment[0] = NOW + timedelta(seconds=10)

    second = run(service.choices(TASK))
    assert FRONTIER.name not in [c.model for c in second.choices], (
        "a superseded success must never outlive the fold"
    )
    assert reads == [(FRONTIER.name,)], "the error folds in; no full read is paid"


def test_foreign_rows_for_another_model_or_config_leave_the_verdict_alone(
    audit_engine: sa.Engine,
) -> None:
    """Fold boundaries: rows outside :meth:`_observation_of`'s filter — a
    different model, or the same model under a DIFFERENT config hash — are
    proven, scanned past, and change nothing: no full read, no darkening,
    no false refresh of the cached verdict."""
    moment = [NOW]
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    service, _ = _service(audit_engine, warm=None, clock=lambda: moment[0])
    reads = _counting_reads(service)

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]

    # A different model's traffic, and this model's traffic under another
    # boot's config hash (an ERROR, so folding it by mistake would darken).
    _observed(
        sink,
        model=FRONTIER_BEST,
        outcome=AuditOutcome.error,
        occurred_at=NOW + timedelta(seconds=4),
    )
    _observed(
        sink,
        outcome=AuditOutcome.error,
        occurred_at=NOW + timedelta(seconds=5),
        config_hash="beef" * 16,  # a valid but DIFFERENT boot's hash
    )
    moment[0] = NOW + timedelta(seconds=10)

    second = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in second.choices], (
        "rows outside the observation filter must not darken the lane"
    )
    choice = next(c for c in second.choices if c.model == FRONTIER.name)
    assert choice.observed_at == NOW, "the cached row must stand untouched"
    assert reads == [(FRONTIER.name,)], "non-matching rows never force a full read"


def test_a_later_appended_but_older_stamped_error_still_wins_by_seq(
    audit_engine: sa.Engine,
) -> None:
    """Append order (``seq``) decides supersession in the fold exactly as in
    a full read — never ``occurred_at``, which cross-process clock skew can
    stamp backwards."""
    moment = [NOW]
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    service, _ = _service(audit_engine, warm=None, clock=lambda: moment[0])
    reads = _counting_reads(service)

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]

    _observed(sink, outcome=AuditOutcome.error, occurred_at=NOW - timedelta(seconds=1))
    moment[0] = NOW + timedelta(seconds=10)

    second = run(service.choices(TASK))
    assert FRONTIER.name not in [c.model for c in second.choices], (
        "the later-APPENDED error must supersede by seq despite its older stamp"
    )
    assert reads == [(FRONTIER.name,)]


def test_an_unprovable_extension_falls_back_to_the_full_verified_read(
    audit_engine: sa.Engine,
) -> None:
    """Chain integrity outranks latency: a forged row in the extension window
    fails the incremental proof (hash recomputation over the hash-covered
    payload), the fold is abandoned, and the FULL verified read runs — whose
    ``verify_chain`` refuses the whole chain by name.  The fold can
    therefore never authorize off content the chain does not prove."""
    from ne.audit.store import ne_audit_head, ne_audit_log, read_head

    moment = [NOW]
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    service, _ = _service(audit_engine, warm=None, clock=lambda: moment[0])
    reads = _counting_reads(service)

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]
    assert reads == [(FRONTIER.name,)]

    # Forge a "success" row for the model with a record_hash the payload does
    # not prove, and advance the head to it — what a writer bypassing
    # append_record would have to leave behind.
    with audit_engine.begin() as conn:
        head_seq, head_hash = read_head(conn)
        forged_seq = head_seq + 1
        conn.execute(
            ne_audit_log.insert().values(
                seq=forged_seq,
                event_id="forged-success",
                event_type="audit",
                config_hash=get_config_hash(),
                actor="ne-gateway",
                occurred_at=NOW + timedelta(seconds=5),
                action=MODEL_OBSERVATION_ACTION,
                resource=FRONTIER.litellm_model,
                outcome="success",
                payload={"event_id": "forged-success", "forged": True},
                prev_hash=head_hash,
                record_hash="f" * 64,
            )
        )
        conn.execute(
            ne_audit_head.update()
            .where(ne_audit_head.c.id == 1)
            .values(seq=forged_seq, record_hash="f" * 64)
        )

    moment[0] = NOW + timedelta(seconds=10)

    with pytest.raises(ModelSelectionEvidenceError):
        run(service.choices(TASK))
    assert reads == [(FRONTIER.name,), (FRONTIER.name,)], (
        "the unprovable extension must force the full verified read, whose "
        "verify_chain names the break — never a fold off forged content"
    )


def test_a_warm_refused_before_transport_retries_at_the_evidence_expiry(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILS on 5c43ed84: the retry landed at t≈90s instead of t≈60s.

    A proactive warm REFUSED before transport (egress guard, provider client
    constructor, credential fault) appends no observation row, so the anchor
    never moves.  On 5c43ed84 the anchor-unchanged branch then slept one blind
    base interval — after which the anchor row was ~60.05s old, so
    ``_frontier_refresh_target()`` returned ``None`` and the no-anchor branch
    slept ANOTHER interval before warming.  The retry landed at t≈90s,
    reopening ~30s in which admissions pay the inline warm and the full-chain
    read this refresher exists to remove.  The retry must instead be paced by
    the interval the evidence's own expiry implies: refused at t≈30s ⇒
    retried at t≈60s, when currency is actually lost.  Consecutive failures
    thereafter stay interval-paced (no provider spin)."""
    import types

    import ne.api.model_selection as model_selection_module

    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    real_sleep = asyncio.sleep
    sleeps = [0]

    async def fake_sleep(delay: float) -> None:
        sleeps[0] += 1
        fake[0] += max(delay, 0.0)
        await real_sleep(0)

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=fake_sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)
    service, _ = _service(audit_engine, warm=_RecordingWarm(sink, occurred_at=NOW), clock=fake_now)

    # Earn real evidence at fake t=0 (observed_at == NOW), anchoring the
    # schedule — then swap in a warm the guard refuses BEFORE transport:
    # raises synchronously, appends nothing.
    assert run(service.refresh_frontier_evidence(TASK)) is True

    class _RefusedBeforeTransportWarm:
        def __init__(self) -> None:
            self.called_at: list[float] = []

        async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
            self.called_at.append(fake[0])
            raise RuntimeError("egress guard refused the warm before transport")

    warm = _RefusedBeforeTransportWarm()
    service._frontier_warm = warm

    passes = [0]
    limit = 25

    class _LoopBounded(Exception):
        pass

    original_target = service._frontier_refresh_target

    def counting_target() -> datetime | None:
        passes[0] += 1
        if passes[0] > limit:
            raise _LoopBounded
        return original_target()

    service._frontier_refresh_target = counting_target  # type: ignore[method-assign]

    async def drive() -> None:
        with pytest.raises(_LoopBounded):
            await service.run_frontier_evidence_refresher()

    run(drive())

    assert len(warm.called_at) >= 3, "the refused-warm cycle was not driven"
    assert warm.called_at[0] == pytest.approx(FRONTIER_EVIDENCE_REFRESH_INTERVAL_S, abs=0.5), (
        f"the first proactive warm should land at the anchor; got {warm.called_at}"
    )
    assert warm.called_at[1] == pytest.approx(FRONTIER_OBSERVATION_MAX_AGE_S, abs=1.0), (
        "a warm refused before transport must be retried when the evidence "
        f"itself expires (t≈{FRONTIER_OBSERVATION_MAX_AGE_S:.0f}s), not one "
        "blind base interval after the refusal plus another after expiry "
        f"(t≈90s); got {warm.called_at}"
    )
    assert warm.called_at[2] - warm.called_at[1] == pytest.approx(
        FRONTIER_EVIDENCE_REFRESH_INTERVAL_S, abs=1.0
    ), (
        "after the expiry-timed retry also fails, retries must fall back to "
        f"base-interval pacing (no provider spin); got {warm.called_at}"
    )
    assert sleeps[0] >= len(warm.called_at) - 1, (
        "pacing must come from sleeping, not from hammering the provider"
    )


def test_a_folded_success_advances_the_refreshers_scheduling_anchor(
    audit_engine: sa.Engine,
) -> None:
    """FAILS on 5c43ed84: folding advanced the baseline but not the anchor.

    When another replica (or a normal frontier turn) appends a newer success,
    the extension proof folds it into ``cached_records`` and the entry's
    baseline advances — but ``_frontier_observed_at`` stayed at the OLD row's
    timestamp.  The cold filter then sees a young folded row and skips
    warming, while the scheduler stays anchored to the old timestamp: once
    the old anchor expires, ``_frontier_refresh_target()`` returns ``None``
    and the refresher can wait until after the folded success is itself stale
    — sending admissions back through the inline warm plus the full-chain
    read.  The scheduling bookkeeping must advance from the authoritative
    folded records exactly as it does from a full read."""
    import ne.api.model_selection as model_selection_module

    moment = [NOW]
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    service, _ = _service(audit_engine, warm=None, clock=lambda: moment[0])
    reads = _counting_reads(service)

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]
    assert service._frontier_observed_at[FRONTIER.name] == NOW
    assert reads == [(FRONTIER.name,)]

    # Foreign SUCCESS traffic lands; the next catalog read folds it in
    # via the verified extension proof — no full read.
    _observed(sink, outcome=AuditOutcome.success, occurred_at=NOW + timedelta(seconds=20))
    moment[0] = NOW + timedelta(seconds=25)
    second = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in second.choices]
    assert reads == [(FRONTIER.name,)], "precondition: the row folded; no full read was paid"

    assert service._frontier_observed_at[FRONTIER.name] == NOW + timedelta(seconds=20), (
        "the folded success is the authoritative newest observation; the "
        "refresher's anchor must advance with it, or the scheduler stays "
        "pinned to the superseded row and wakes only after the folded "
        "evidence is itself stale"
    )
    assert service._frontier_refresh_target() == NOW + timedelta(
        seconds=20
        + FRONTIER_EVIDENCE_REFRESH_INTERVAL_S
        + model_selection_module._FRONTIER_REFRESH_ANCHOR_SLACK_S
    ), "the scheduler must plan the next warm off the folded row's expiry"


# -- the cache lock covers the dictionary, never a chain read ----------------


def test_a_slow_full_read_does_not_block_a_concurrent_cache_hit(
    audit_engine: sa.Engine,
) -> None:
    """FAILS on 3b4366ea: ``_frontier_cache_lock`` was held across the miss
    path's full ``replay + verify_chain`` — V = 10-27s on the demo box — so
    EVERY concurrent admission queued behind one caller's miss for that
    whole window instead of taking its ~1-4ms cache hit.

    Staged for real, no sleeps: one caller misses (a changed model set) and
    its full chain read is parked on a thread-side gate; while it is
    parked, a second caller whose model set the cache already covers must
    COMPLETE.  Under the held-across-read lock the hit queues on the gate
    and the bounded wait below expires instead."""
    import threading

    two_frontier = GatewaySettings(
        models=[PRIVATE, FRONTIER, FRONTIER_BEST],
        tasks={TASK: TaskRoute(model=FRONTIER.name, fallbacks=(FRONTIER_BEST.name, PRIVATE.name))},
    )
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink)
    run(seed((FRONTIER, FRONTIER_BEST), task=TASK))

    lifecycle_gate = {FRONTIER_BEST.name: False}
    service = ModelSelectionService(
        gateway_settings=two_frontier,
        serving_status=_StubServing((PRIVATE,), lambda: NOW),
        audit_engine=audit_engine,
        pdp=_AllowPdp(),
        sink=sink,
        config_hash=get_config_hash(),
        air_gap_provider=lambda: False,
        lifecycle_availability=lambda name: (lifecycle_gate.get(name, True), None),
        frontier_warm=None,
        clock=lambda: NOW,
    )

    parked = threading.Event()
    release = threading.Event()
    reads: list[tuple[str, ...]] = []
    original = service._frontier_records

    def gated(models: Sequence[ModelDef]) -> Any:
        names = tuple(m.name for m in models)
        reads.append(names)
        if len(names) > 1:
            parked.set()
            assert release.wait(timeout=30), "the harness never released the gated read"
        return original(models)

    service._frontier_records = gated  # type: ignore[method-assign]

    async def scenario() -> None:
        first = await service.choices(TASK)
        assert FRONTIER.name in [c.model for c in first.choices]
        assert reads == [(FRONTIER.name,)], "the fill for the covered key must land first"

        # A changed model set is a cache MISS; park its full read in flight.
        lifecycle_gate[FRONTIER_BEST.name] = True
        miss = asyncio.create_task(service.choices(TASK))
        assert await asyncio.to_thread(parked.wait, 10), (
            "the miss never reached its full chain read"
        )

        # While the miss's read is parked, an admission the cache covers
        # must hit and complete: the miss may cost V, but V must never be
        # charged to every concurrent admission.
        lifecycle_gate[FRONTIER_BEST.name] = False
        hit = asyncio.create_task(service.choices(TASK))
        done, _pending = await asyncio.wait({hit}, timeout=5.0)
        release.set()
        missed = await miss
        second = await hit
        assert hit in done, (
            "a cache hit must complete while the miss's full read is still "
            "in flight — the cache lock is being held across the O(chain) "
            "replay+verify instead of just the cache mutation"
        )
        assert FRONTIER.name in [c.model for c in second.choices]
        assert reads == [(FRONTIER.name,), (FRONTIER.name, FRONTIER_BEST.name)], (
            "the hit must be served from the cache (fold at most), never a read"
        )
        assert {FRONTIER.name, FRONTIER_BEST.name} <= {c.model for c in missed.choices}, (
            "the parked miss must still complete with its own verified read"
        )

    run(scenario())


def test_folds_cannot_defer_the_full_verified_read_past_the_cache_ttl(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE load-bearing bound behind the extension proof's prefix blindness.

    ``verified_extension`` deliberately never re-checks the prefix at or
    before its baseline, so a prefix tamper is invisible to a fold.  The
    property that keeps that sound is that folds cannot postpone the full
    ``replay + verify_chain``: a successful fold advances the entry's
    baseline but NEVER its ``read_at``, so the entry dies at
    ``FRONTIER_RECORDS_CACHE_TTL_S`` and the next admission pays the full
    verified read — no matter how many provable folds occurred meanwhile.

    Driven on a scripted clock: fresh foreign SUCCESS rows land every 7s
    (each admission therefore has a provable non-empty extension to fold)
    for more than two TTL windows.  The full read must recur at exactly the
    first admission past each window boundary."""
    import types

    import ne.api.model_selection as model_selection_module

    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=asyncio.sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    service, _ = _service(audit_engine, warm=None, clock=fake_now)
    reads = _counting_reads(service)

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]
    assert reads == [(FRONTIER.name,)], "the fill (t=0) is the only read so far"

    step_s = 7.0
    full_reads_at: list[float] = [0.0]
    for step in range(1, 20):  # t = 7 .. 133: two TTL boundaries crossed
        fake[0] = step * step_s
        _observed(sink, outcome=AuditOutcome.success, occurred_at=fake_now())
        before = len(reads)
        admitted = run(service.choices(TASK))
        assert FRONTIER.name in [c.model for c in admitted.choices], (
            f"t={fake[0]}: the freshly folded success must keep the lane lit"
        )
        if len(reads) > before:
            full_reads_at.append(fake[0])

    assert full_reads_at == [0.0, 63.0, 126.0], (
        "a full replay+verify must recur at the FIRST admission past each "
        "TTL window — a fold advances the baseline, never read_at, so no "
        f"number of folds can defer it (full reads landed at {full_reads_at})"
    )
    for earlier, later in itertools.pairwise(full_reads_at):
        assert later - earlier <= FRONTIER_RECORDS_CACHE_TTL_S + step_s, (
            "the gap between full verified reads must stay within one TTL "
            "window (+ one admission step): unbounded deferral would mean "
            "unbounded prefix-tamper blindness"
        )


def test_a_prefix_tamper_is_detected_when_the_chain_ANCHOR_ages_out(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prefix blindness is BOUNDED, and this pins that a STALE ANCHOR
    IS NOT REUSED.  ⚠️ It does NOT pin the bound's VALUE — see below.

    It used to be one ``FRONTIER_RECORDS_CACHE_TTL_S`` (60s), because a cache
    MISS paid an unbounded ``verify_chain``.  It no longer does: the miss now
    verifies through :class:`ne.audit.AnchoredChainVerifier` too
    (:meth:`ModelSelectionService._frontier_records`), which is what made the
    miss sub-second — and the honest consequence is that the bound moved off
    the cache TTL.  ⚠️ It did NOT move to :data:`ne.audit.CHAIN_ANCHOR_MAX_AGE_S`
    (900s) alone: the two limits COMPOSE.  An entry cached just before the
    anchor ages out keeps being reused through ``verified_extension``, which
    does not consult the anchor's age, for up to a further
    ``FRONTIER_RECORDS_CACHE_TTL_S`` (60s) — so the effective ceiling is
    ~960s, and 900s is only the anchor's half.  This test asserts the anchor
    age rather than the old cache TTL because a test asserting 60s while the
    code delivered the new bound would be the dangerous kind of green.

    ⚠️ What this test does NOT prove.  It advances the clock to
    ``CHAIN_ANCHOR_MAX_AGE_S + 100`` and THEN calls through to ``verify``,
    landing past BOTH limits — so it pins that a stale anchor is not reused,
    and it pins neither the ~960s boundary itself nor any time-to-detection.
    The age test runs INSIDE ``AnchoredChainVerifier.verify``: elapsed time
    with no caller detects nothing, so "refused the moment the blessing ages
    out" would be the wrong reading.  It is refused on the first verification
    after that age.

    Both halves are proved: an unpropagated prefix tamper survives a cache
    expiry AND a fresh full read while the anchor is young, and is refused on
    the next verification once the blessing has aged past
    ``max_anchor_age_s``.  ⚠️ ``run_chain_anchor_refresher`` catches it sooner
    (one ``CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S``, 300s) ONLY in a process that
    runs the loop — the API, where it is wired.  The worker composes no
    refresher, so there this ceiling is all there is.
    """
    import types

    import ne.api.model_selection as model_selection_module
    from ne.audit.store import ne_audit_log

    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=asyncio.sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    # The verifier shares the test's clock, so the anchor ages exactly with it.
    verifier = AnchoredChainVerifier(audit_engine, clock=lambda: fake[0])
    service, _ = _service(audit_engine, warm=None, clock=fake_now, chain_verifier=verifier)
    reads = _counting_reads(service)

    first = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in first.choices]
    assert reads == [(FRONTIER.name,)]
    assert verifier.anchor is not None, "the first read installs the blessing"

    # Tamper the PREFIX: rewrite the first row's record_hash after the full
    # verify blessed the head — what a privileged writer editing history
    # would leave.  The append-only triggers block UPDATE, so the attacker
    # model (as in test_redteam_tamper) drops them first.
    with audit_engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")
        conn.execute(
            ne_audit_log.update().where(ne_audit_log.c.seq == 1).values(record_hash="a" * 64)
        )

    # Inside the cache window: the fold cannot see the prefix.
    fake[0] = 10.0
    _observed(sink, outcome=AuditOutcome.success, occurred_at=fake_now())
    inside = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in inside.choices]
    assert reads == [(FRONTIER.name,)]

    # Past the cache TTL but INSIDE the anchor's max age: the entry is dead so
    # a genuinely fresh read runs — and it still does not see the prefix,
    # because its chain proof is anchored.  This is the honest cost of the
    # change, asserted rather than described.
    fake[0] = 63.0
    _observed(sink, outcome=AuditOutcome.success, occurred_at=fake_now())
    past_ttl = run(service.choices(TASK))
    assert FRONTIER.name in [c.model for c in past_ttl.choices]
    assert reads == [(FRONTIER.name,), (FRONTIER.name,)], (
        "the TTL expiry does force a fresh read — it is the PROOF, not the "
        "read, that no longer covers the prefix"
    )

    # Past the anchor's max age: the anchor is refused, the inline full verify
    # runs, and the tampered chain is refused by name.
    fake[0] = CHAIN_ANCHOR_MAX_AGE_S + 100.0
    _observed(sink, outcome=AuditOutcome.success, occurred_at=fake_now())
    with pytest.raises(ModelSelectionEvidenceError):
        run(service.choices(TASK))
    assert not service._frontier_cache, (
        "a refused read must clear the cache: nothing folds off a chain the verify would not bless"
    )


# -- the cold-start residual that failed every deploy ------------------------


def test_a_cold_boot_admission_after_the_first_refresh_pays_no_inline_work(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILS before the eager first pass (#958's documented residual — the
    exact shape that auto-rolled-back every deploy): on a cold process
    ``_frontier_observed_at`` is empty, so ``_frontier_refresh_target()``
    returns ``None`` and the loop slept ONE FULL base interval before its
    first warm.  The deploy's acceptance (and the owner's first request after
    any restart) lands squarely inside that window, finds a cold cache, and
    pays the inline warm plus the full O(chain) replay+verify — 26.1s and
    44.7s measured against the acceptance's 20s budget.

    The chain is seeded with a STALE observation (the restart shape: rows
    exist, none current).  Everything runs on a scripted clock — fake time
    advances only through the loop's own sleeps — and every assertion is on
    call counts and scripted-clock spacing, never wall-clock.
    """
    import types

    import ne.api.model_selection as model_selection_module

    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        fake[0] += max(delay, 0.0)
        await real_sleep(0)

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=fake_sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)
    # The restart shape: the chain carries evidence, all of it stale.
    seed = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=120))
    run(seed((FRONTIER,), task=TASK))

    class _TimedWarm(_RecordingWarm):
        def __init__(self) -> None:
            super().__init__(sink, occurred_at=NOW)
            self.called_at: list[float] = []

        async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
            self.called_at.append(fake[0])
            await super().__call__(models, task=task)

    warm = _TimedWarm()
    service, _ = _service(audit_engine, warm=warm, clock=fake_now)

    passes = [0]
    limit = 3

    class _LoopBounded(Exception):
        pass

    original_target = service._frontier_refresh_target

    def counting_target() -> datetime | None:
        passes[0] += 1
        if passes[0] > limit:
            raise _LoopBounded
        return original_target()

    service._frontier_refresh_target = counting_target  # type: ignore[method-assign]

    async def drive() -> None:
        with pytest.raises(_LoopBounded):
            await service.run_frontier_evidence_refresher()

    run(drive())

    assert warm.called_at, "the refresher's first passes never warmed at all"
    assert warm.called_at[0] == pytest.approx(0.0, abs=1.0), (
        "the first proactive warm must begin AT BOOT on a cold process, not "
        f"one base interval ({FRONTIER_EVIDENCE_REFRESH_INTERVAL_S:.0f}s) "
        f"later; it landed at scripted t={warm.called_at[0]:.2f}s — exactly "
        "the cold window the deploy's acceptance times out in"
    )

    # The acceptance's admission arrives at scripted t=2s — after the eager
    # first refresh has landed (its scripted cost here is 0s; on the live box
    # its real cost is the residual the deploy readiness gate covers).  It
    # must find current evidence and a primed cache: no inline warm, no full
    # O(chain) replay+verify.
    fake[0] = 2.0
    reads = _counting_reads(service)
    warm.calls.clear()
    authorized = run(
        service.authorize(
            task=TASK, requested_model=FRONTIER.name, principal=PRINCIPAL, trace_id=TRACE_ID
        )
    )
    assert authorized.model == FRONTIER.name
    assert warm.calls == [], (
        "an admission 2s after boot paid an inline warm — the eager first "
        "refresh did not earn the evidence"
    )
    assert reads == [], (
        "an admission 2s after boot paid a full O(chain) replay+verify — the "
        "eager first refresh did not prime the records cache"
    )


def test_a_cold_boot_under_air_gap_issues_no_warm_and_reads_no_chain(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eager first pass must not fire where the gap is engaged: driving
    the loop from a cold boot with AIR_GAP on issues zero warms, zero chain
    reads, and leaves the evidence cache empty — eligibility already dropped
    every frontier model, exactly as on the interval path."""
    import types

    import ne.api.model_selection as model_selection_module

    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        fake[0] += max(delay, 0.0)
        await real_sleep(0)

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=fake_sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)
    warm = _RecordingWarm(sink, occurred_at=NOW)
    service, _ = _service(audit_engine, warm=warm, air_gap=True, clock=fake_now)
    reads = _counting_reads(service)

    passes = [0]
    limit = 5

    class _LoopBounded(Exception):
        pass

    original_target = service._frontier_refresh_target

    def counting_target() -> datetime | None:
        passes[0] += 1
        if passes[0] > limit:
            raise _LoopBounded
        return original_target()

    service._frontier_refresh_target = counting_target  # type: ignore[method-assign]

    async def drive() -> None:
        with pytest.raises(_LoopBounded):
            await service.run_frontier_evidence_refresher()

    run(drive())

    assert warm.calls == [], "the eager cold-boot pass warmed through an engaged air gap"
    assert reads == [], "the eager cold-boot pass read the chain under air gap"
    assert not service._frontier_cache, "air gap must leave the evidence cache empty"


# -- the readiness signal the deploy's acceptance gate polls -----------------


def test_readiness_is_not_applicable_where_no_warm_can_ever_happen(
    audit_engine: sa.Engine,
) -> None:
    """Every composition in which waiting cannot change the answer says so:
    no warmer composed, air gap engaged, or no task routed at a frontier
    primary."""
    no_warmer, _ = _service(audit_engine, warm=None)
    assert no_warmer.frontier_evidence_readiness() == "not-applicable"

    sink = PostgresAuditSink(audit_engine)
    gapped, _ = _service(audit_engine, warm=_RecordingWarm(sink), air_gap=True)
    assert gapped.frontier_evidence_readiness() == "not-applicable"

    private_routed = GatewaySettings(
        models=[PRIVATE, FRONTIER],
        tasks={TASK: TaskRoute(model=PRIVATE.name, fallbacks=())},
    )
    repointed, _ = _service(audit_engine, warm=_RecordingWarm(sink), settings=private_routed)
    assert repointed.frontier_evidence_readiness() == "not-applicable"


def test_readiness_moves_from_warming_to_ready_when_the_refresh_lands(
    audit_engine: sa.Engine,
) -> None:
    """Cold boot reads ``warming`` (an admission now would pay the inline
    path); the first successful refresh flips it to ``ready``; ageing past
    the currency window honestly returns it to ``warming``.  The boundary is
    the admission window's own: current at 59.9s and at exactly 60.0s, gone
    at 60.1s."""
    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    sink = PostgresAuditSink(audit_engine)
    warm = _RecordingWarm(sink, occurred_at=NOW)
    service, _ = _service(audit_engine, warm=warm, clock=fake_now)

    assert service.frontier_evidence_readiness() == "warming", (
        "a cold process must never report ready before its first chain read"
    )

    assert run(service.refresh_frontier_evidence(TASK)) is True
    assert service.frontier_evidence_readiness() == "ready"

    fake[0] = 59.9
    assert service.frontier_evidence_readiness() == "ready"
    fake[0] = 60.0
    assert service.frontier_evidence_readiness() == "ready", (
        "the boundary is the admission window's: age == max_age is current"
    )
    fake[0] = 60.1
    assert service.frontier_evidence_readiness() == "warming", (
        "evidence past the window must never keep reporting ready"
    )


def test_readiness_reads_no_chain_and_issues_no_warm(audit_engine: sa.Engine) -> None:
    """The signal is O(1) bookkeeping, never an evidence source: polling it
    costs no provider call and no chain read, in either verdict."""
    sink = PostgresAuditSink(audit_engine)
    warm = _RecordingWarm(sink, occurred_at=NOW)
    service, _ = _service(audit_engine, warm=warm)
    reads = _counting_reads(service)

    assert service.frontier_evidence_readiness() == "warming"
    assert run(service.refresh_frontier_evidence(TASK)) is True
    baseline_reads = list(reads)
    baseline_warms = list(warm.calls)

    for _ in range(50):
        service.frontier_evidence_readiness()

    assert reads == baseline_reads, "readiness polling read the chain"
    assert warm.calls == baseline_warms, "readiness polling issued a warm"


def test_the_eager_cold_boot_pass_runs_once_never_a_hot_spin(
    audit_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills the surviving mutation: an eager branch that never clears its
    ``cold_boot`` flag re-arms itself behind zero-length sleeps, and a warm
    refused before transport (nothing appended, anchor never moves) is then
    retried BACK-TO-BACK — a hot provider spin that sleep-COUNTING guards
    cannot see, because ``sleep(0)`` counts as a sleep
    (``test_every_pass_through_the_refresher_loop_suspends`` stays green
    under it).  Pinned on scripted SPACING and call counts instead: the first
    attempt lands at boot, and every later attempt lands a full base interval
    after its predecessor."""
    import types

    import ne.api.model_selection as model_selection_module

    fake = [0.0]

    def fake_now() -> datetime:
        return NOW + timedelta(seconds=fake[0])

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        fake[0] += max(delay, 0.0)
        await real_sleep(0)

    class _FakeLoop:
        @staticmethod
        def time() -> float:
            return fake[0]

    shim = types.SimpleNamespace(
        sleep=fake_sleep,
        get_running_loop=lambda: _FakeLoop(),
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
    )
    monkeypatch.setattr(model_selection_module, "asyncio", shim)

    sink = PostgresAuditSink(audit_engine)
    # The restart shape: stale evidence on the chain, none current.
    seed = _RecordingWarm(sink, occurred_at=NOW - timedelta(seconds=120))
    run(seed((FRONTIER,), task=TASK))

    class _RefusedWarm:
        def __init__(self) -> None:
            self.called_at: list[float] = []

        async def __call__(self, models: Sequence[ModelDef], *, task: str) -> None:
            self.called_at.append(fake[0])
            raise RuntimeError("egress guard refused the warm before transport")

    warm = _RefusedWarm()
    service, _ = _service(audit_engine, warm=warm, clock=fake_now)

    passes = [0]
    limit = 8

    class _LoopBounded(Exception):
        pass

    original_target = service._frontier_refresh_target

    def counting_target() -> datetime | None:
        passes[0] += 1
        if passes[0] > limit:
            raise _LoopBounded
        return original_target()

    service._frontier_refresh_target = counting_target  # type: ignore[method-assign]

    async def drive() -> None:
        with pytest.raises(_LoopBounded):
            await service.run_frontier_evidence_refresher()

    run(drive())

    assert len(warm.called_at) >= 3, "the refused-warm cold boot was not driven"
    assert warm.called_at[0] == pytest.approx(0.0, abs=0.5), (
        f"the eager pass must still fire at boot; got {warm.called_at}"
    )
    gaps = [later - earlier for earlier, later in itertools.pairwise(warm.called_at)]
    assert all(
        gap == pytest.approx(FRONTIER_EVIDENCE_REFRESH_INTERVAL_S, abs=1.0) for gap in gaps
    ), (
        "after the ONE eager pass, a warm that earned nothing must be retried "
        f"a full base interval apart — never back-to-back behind sleep(0); "
        f"got attempts at {warm.called_at}"
    )
    assert len(warm.called_at) <= passes[0], (
        "more provider calls than loop passes: the eager branch re-armed"
    )


def test_readiness_reports_unknown_when_a_register_cannot_be_read(
    audit_engine: sa.Engine,
) -> None:
    """ "Could not determine" must never render as "nothing to wait for": a
    raising or garbage-returning air-gap provider, or a raising route
    provider, reads ``unknown`` — a caller treats it as not-ready — and the
    same service flips back to an honest verdict once the register reads
    again (the bidirectional control)."""
    sink = PostgresAuditSink(audit_engine)
    service, _ = _service(audit_engine, warm=_RecordingWarm(sink))

    def gap_boom() -> bool:
        raise RuntimeError("posture register unreadable")

    service._air_gap_provider = gap_boom
    assert service.frontier_evidence_readiness() == "unknown"

    service._air_gap_provider = lambda: "engaged"  # type: ignore[assignment,return-value]
    assert service.frontier_evidence_readiness() == "unknown", (
        "a non-bool posture is not a measurement and must not read as one"
    )

    service._air_gap_provider = lambda: False

    def routes_boom() -> dict[str, TaskRoute]:
        raise RuntimeError("route register unreadable")

    service._route_provider = routes_boom
    assert service.frontier_evidence_readiness() == "unknown", (
        "an unreadable route register must not collapse into not-applicable"
    )

    service._route_provider = None
    assert service.frontier_evidence_readiness() == "warming", (
        "with the registers readable the same service answers honestly again"
    )


def test_the_eager_cold_boot_pass_never_blocks_the_event_loop(
    audit_engine: sa.Engine,
) -> None:
    """FAILS while any register/lifecycle read inside the eager pass runs
    inline on the event-loop thread — the guard the suspends-style tests
    cannot provide, because a coroutine that suspends between blocking reads
    still freezes the loop DURING each read.

    The route provider and the lifecycle availability read are made
    deliberately slow (a contended pool / slow Postgres, 1.5s per read) while
    a heartbeat coroutine ticks on the same event loop for the whole eager
    refresh.  Off-thread reads leave the heartbeat ticking (~10ms gaps);
    a single inline read opens a >=1.5s gap and trips the assertion.  This
    one is deliberately wall-clock: a loop stall IS a wall-clock fact, and
    the injected 1.5s dwarfs scheduler noise."""
    block_s = 1.5

    def slow_routes() -> dict[str, TaskRoute]:
        import time as _time

        _time.sleep(block_s)
        return dict(SETTINGS.tasks)

    def slow_lifecycle(name: str) -> tuple[bool, str | None]:
        import time as _time

        _time.sleep(block_s)
        return True, None

    sink = PostgresAuditSink(audit_engine)
    warm = _RecordingWarm(sink, occurred_at=datetime.now(UTC))
    service = ModelSelectionService(
        gateway_settings=SETTINGS,
        serving_status=_StubServing((PRIVATE,), lambda: datetime.now(UTC)),
        audit_engine=audit_engine,
        pdp=_AllowPdp(),
        sink=sink,
        config_hash=get_config_hash(),
        air_gap_provider=lambda: False,
        lifecycle_availability=slow_lifecycle,
        route_provider=slow_routes,
        frontier_warm=warm,
        clock=lambda: datetime.now(UTC),
    )

    done = asyncio.Event()
    original_refresh = service.refresh_frontier_evidence

    async def spying_refresh(task: str, **kwargs: Any) -> bool:
        try:
            return await original_refresh(task, **kwargs)
        finally:
            done.set()

    service.refresh_frontier_evidence = spying_refresh  # type: ignore[method-assign]

    gaps: list[float] = []

    async def drive() -> None:
        loop = asyncio.get_running_loop()
        loop_task = asyncio.create_task(service.run_frontier_evidence_refresher())
        try:
            last = loop.time()
            for _ in range(6000):
                await asyncio.sleep(0.01)
                now = loop.time()
                gaps.append(now - last)
                last = now
                if done.is_set():
                    break
            else:
                pytest.fail("the eager cold-boot refresh never completed")
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

    run(drive())

    assert warm.calls, "the eager pass never warmed"
    # Threshold 1.2s, NOT 1.0s, and the margin is deliberate: this box
    # manufactures failures at exactly the 1.0s timescale under load (findBy*
    # timeouts were recorded at 1000ms at load 30-42), and a wall-clock assert
    # with 500ms of headroom is the first thing to go on a saturated runner.
    # Discriminating power is untouched: an off-thread read measures ~10ms gaps
    # and an inline one is >= block_s (1.5s), so 1.2s sits in a 300ms-wide gap
    # between the two populations. Measured, not assumed: forcing this assert at
    # load ~15 puts the off-thread gap population at 0.02-0.05s, and an inline
    # read cannot produce a gap below block_s (1.5s). So 1.2s sits ~24-60x above
    # the observed noise floor and 0.3s below the trip floor.
    assert max(gaps) < 1.2, (
        f"the event loop stalled for {max(gaps):.2f}s during the eager "
        f"cold-boot refresh — a register or lifecycle read ran inline on the "
        f"loop thread instead of off it (injected read latency {block_s}s; "
        "at cold boot this stall takes /healthz and every request with it)"
    )


def test_the_background_refresher_clears_the_anchor_when_the_full_verify_RAISES(
    audit_engine: sa.Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refresher's ERROR log for a RAISED tick must be true.

    A tick that raises (dropped connection, statement timeout, failover mid-walk)
    is not the ``not result.valid`` branch: it lands in ``except Exception``,
    whose log tells the operator that evidence reads "fall back to an inline
    full verify".  That is only true if the anchor is gone, and ONLY the
    verifier can make it so — which is why this drives the REAL
    ``AnchoredChainVerifier.full_verify`` and faults the walk underneath it
    (stubbing ``full_verify`` itself would prove nothing about the anchor).

    Two ticks, so the second half of the claim is pinned too: the loop does not
    exit on a raise, and the first full walk that COMPLETES re-blesses the
    anchor.  (In a live process that walk is usually an inline request-path one,
    since ``verify`` falls back to the same ``_full_verify``; this test uses the
    loop's own second tick because it drives the loop.)

    Completion is signalled from the WORKER THREAD, per
    ``test_the_background_anchor_refresher_catches_a_prefix_tamper`` — the loop
    dispatches through ``anyio.to_thread.run_sync``, so no number of event-loop
    turns bounds a pass.

    MUTATION-CHECK: delete the unconditional ``self._clear()`` from
    ``AnchoredChainVerifier._full_verify``'s ``except Exception`` handler → THIS
    TEST GOES RED at ``assert anchor_after_the_raise == [None]`` with the
    surviving ``VerifiedAnchor(...)`` in the list, which is precisely the state
    the log denies.  Weakening it to the identity CAS ``self._clear(renewing)``
    reds it the same way whenever a concurrent extension has moved the anchor —
    see ``test_anchored_verify.py``'s own CAS mutation case.
    """
    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    # A short interval so the RECOVERY tick lands inside the timeout below; the
    # max age is only ever read by verify(), which this test does not use.
    verifier = AnchoredChainVerifier(
        audit_engine, full_verify_interval_s=0.05, max_anchor_age_s=0.15
    )
    service, _ = _service(audit_engine, warm=None, chain_verifier=verifier)

    run(service.choices(TASK))
    assert verifier.anchor is not None, "the evidence read must have blessed an anchor"

    anchor_after_the_raise: list[VerifiedAnchor | None] = []

    async def _two_passes() -> None:
        loop = asyncio.get_running_loop()
        raised = asyncio.Event()
        recovered = asyncio.Event()
        full_verify = verifier.full_verify
        ticks = 0

        def _fault_once_then_recover(conn: sa.Connection) -> ChainVerification:
            """Fault the FIRST walk only, so tick 2 is a genuine recovery.

            Faulting inside ``verify_chain`` rather than stubbing ``full_verify``
            is the point: the anchor bookkeeping under test lives in
            ``_full_verify``, and a stub would never run it.
            """
            if ticks == 1:
                raise OperationalError("SELECT 1", {}, Exception("statement timeout"))
            return verify_chain(conn)

        def _tick_then_signal() -> Any:
            nonlocal ticks
            ticks += 1
            first = ticks == 1
            try:
                return full_verify()
            finally:
                if first:
                    # Read the anchor on THIS thread, before the loop can act on
                    # the raise, so what is asserted is the state the verifier
                    # left behind rather than anything the loop did after.
                    anchor_after_the_raise.append(verifier.anchor)
                    loop.call_soon_threadsafe(raised.set)
                else:
                    loop.call_soon_threadsafe(recovered.set)

        monkeypatch.setattr("ne.audit.anchor.verify_chain", _fault_once_then_recover)
        verifier.full_verify = _tick_then_signal  # type: ignore[method-assign]
        task = asyncio.create_task(service.run_chain_anchor_refresher())
        try:
            async with asyncio.timeout(30):
                await raised.wait()
                await recovered.wait()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            del verifier.full_verify  # type: ignore[misc]

    run(_two_passes())

    assert anchor_after_the_raise == [None], (
        "the RAISED full verify left the blessing standing, so evidence reads "
        "kept being served anchored verdicts off it — the refresher's ERROR log "
        "promises the opposite"
    )
    # The loop reached a second tick at all, and that tick re-blessed: a raise
    # costs latency until the fault clears, never a wedged process.
    assert verifier.anchor is not None, "a later successful tick must re-bless the anchor"


def test_the_background_anchor_refresher_catches_a_prefix_tamper(
    audit_engine: sa.Engine,
) -> None:
    """The bound above is the WORST case; this is the normal one.

    ``run_chain_anchor_refresher`` is the only thing that walks the full ledger
    once evidence reads are anchored, so it is what actually detects an
    unpropagated prefix tamper on a live deployment.  When it does, it clears
    the anchor, and the next evidence read refuses — without waiting for
    ``max_anchor_age_s``.

    MUTATION-CHECK: make ``ModelSelectionService.run_chain_anchor_refresher``
    call ``self._chain_verifier.verify()`` instead of ``full_verify()`` and
    THIS TEST GOES RED — the wait below never fires and times out, because the
    anchored proof cannot see a prefix tamper, which is the whole reason the
    background loop must run the FULL one.
    """
    from ne.audit.store import ne_audit_log

    sink = PostgresAuditSink(audit_engine)
    seed = _RecordingWarm(sink, occurred_at=NOW)
    run(seed((FRONTIER,), task=TASK))
    verifier = AnchoredChainVerifier(audit_engine)
    service, _ = _service(audit_engine, warm=None, chain_verifier=verifier)

    run(service.choices(TASK))
    assert verifier.anchor is not None

    with audit_engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")
        conn.execute(
            ne_audit_log.update().where(ne_audit_log.c.seq == 1).values(record_hash="a" * 64)
        )

    async def _one_pass() -> None:
        """Run the refresher until its first FULL verify has actually LANDED.

        The pass finishes on a WORKER THREAD (the loop dispatches
        ``full_verify`` through ``anyio.to_thread.run_sync``), so its completion
        is bounded by wall clock and thread scheduling, NOT by event-loop turns.
        An earlier version of this waited by counting ``await asyncio.sleep(0)``
        turns; measured, 200 of those turns are worth ~1.5 ms, the pass needs
        14-190 of them at load ~10, and it FAILED 5 times in 150 serial trials
        here and once on CI (run 33996279266). Injecting a 2 ms delay into
        ``full_verify`` failed it 20/20 — proof the budget was wall-clock, not
        turns. So: signal the real completion from the worker thread and await
        that. The ``finally`` fires after ``full_verify`` has
        completed BY EITHER EXIT — a return or a raise — and the anchor is
        settled inside the call on both (an install or a clear on the return
        paths, a clear on the raise path), so reading ``verifier.anchor`` after
        this event is reading the pass's finished state, not a race.
        """
        loop = asyncio.get_running_loop()
        pass_landed = asyncio.Event()
        full_verify = verifier.full_verify

        def _full_verify_then_signal() -> Any:
            try:
                return full_verify()
            finally:
                loop.call_soon_threadsafe(pass_landed.set)

        verifier.full_verify = _full_verify_then_signal  # type: ignore[method-assign]
        task = asyncio.create_task(service.run_chain_anchor_refresher())
        try:
            async with asyncio.timeout(30):
                await pass_landed.wait()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            del verifier.full_verify  # type: ignore[misc]

    run(_one_pass())
    assert verifier.anchor is None, "the background FULL verify must clear the blessing"

    service._frontier_cache.clear()
    with pytest.raises(ModelSelectionEvidenceError):
        run(service.choices(TASK))
