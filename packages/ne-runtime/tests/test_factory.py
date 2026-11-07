# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""create_tool_executor wires ne.govern.create_pdp + create_guardrail_engine to one sink."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from ne.contracts.governance import (
    DecisionEffect,
    GuardrailVerdict,
    PolicyDecision,
    PolicyRequest,
)
from ne.contracts.plugins import PluginRegistry
from ne.events import new_audit_event
from ne.govern.config import GovernSettings
from ne.govern.errors import GuardrailViolation
from ne.govern.guardrails.rate import SlidingWindowCounter
from ne.govern.guardrails.spend import InMemorySpendLedger, SpendLedger
from ne.runtime import (
    create_cron_scheduler,
    create_subagent_dispatcher,
    create_tool_executor,
)
from ne.runtime.factory import (
    ExistingRunEvidenceProvider,
    create_agent_execution_binding,
    create_shared_guardrail_state,
    require_existing_run_evidence,
)
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.run_store import AgentRun, RunAuditDecisionKind, RunAuditReceipt, RunState
from ne.runtime.runner import BoundedAgenticAdapter
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    ExecutionStrategy,
)
from ne.schema import AuditOutcome

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ToolLog,
    govern_settings,
    make_principal,
    standard_bindings,
)


class _FailingRail:
    """An extra guardrail that always blocks — proves the extra_guardrails seam runs."""

    def __init__(self, rule: str = "stub.block") -> None:
        self._rule = rule

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        return GuardrailVerdict(passed=False, rule=self._rule, detail="injected rail blocked")


def _allow_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", StubPdp)
    return registry


class StubPdp:
    """A stub PDP proving the create_pdp registry path; audits via the shared sink."""

    def __init__(self, sink: Any) -> None:
        self._sink = sink

    @classmethod
    def from_settings(cls, settings: GovernSettings, *, sink: Any) -> StubPdp:
        return cls(sink)

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        await self._sink.append(
            new_audit_event(
                action="pdp.decide",
                resource=request.resource,
                outcome=AuditOutcome.success,
                actor=request.principal.id,
                provenance={"source": "stub-pdp"},
            )
        )
        return PolicyDecision(effect=DecisionEffect.ALLOW, policy_version="stub-1")


def _executor_with_stub_pdp(sink: RecordingAuditSink) -> GovernedToolExecutor:
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", StubPdp)
    return create_tool_executor(
        standard_bindings(ToolLog()),
        sink=sink,
        approval_gate=NamedApproverGate(),
        govern_settings=govern_settings(),
        registry=registry,
    )


def test_factory_threads_sink_into_the_pdp() -> None:
    sink = RecordingAuditSink()
    executor = _executor_with_stub_pdp(sink)

    asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))

    actions = [getattr(e, "action", "") for e in sink.events]
    # The stub PDP (resolved via the registry) audited through the SAME sink.
    assert "pdp.decide" in actions
    assert any(dict(e.provenance).get("source") == "stub-pdp" for e in sink.events)
    assert "tool.execute" in actions


def test_factory_threads_sink_into_the_guardrail_engine() -> None:
    sink = RecordingAuditSink()
    executor = _executor_with_stub_pdp(sink)

    with pytest.raises(GuardrailViolation):
        asyncio.run(
            executor.execute(
                "write.external.email.send",
                {"to": "attacker@evil.example", "body": "x"},
                principal=make_principal(),
            )
        )

    # The real guardrail engine (built by create_guardrail_engine) audited the
    # block through the SAME sink — proving sink threading into both seams.
    assert any(getattr(e, "action", "") == "guardrail.block" for e in sink.events)


def test_create_tool_executor_requires_sink_and_gate() -> None:
    with pytest.raises(TypeError):
        create_tool_executor([])  # type: ignore[call-arg]


def test_create_subagent_dispatcher_requires_sink_and_gate() -> None:
    from ne.runtime import create_subagent_dispatcher

    # sink and approval_gate are required keyword args (C6 non-optional).
    with pytest.raises(TypeError):
        create_subagent_dispatcher(bindings=[], definitions={})  # type: ignore[call-arg]


def test_create_subagent_dispatcher_couples_pdp_and_returns_registry() -> None:
    from ne.runtime import ProcessRegistry, create_subagent_dispatcher
    from ne.runtime.dispatch import GovernedSubAgentDispatcher

    sink = RecordingAuditSink()
    dispatcher, registry = create_subagent_dispatcher(
        bindings=standard_bindings(ToolLog()),
        definitions={},
        sink=sink,
        approval_gate=NamedApproverGate("alice"),
        gateway=object(),  # type: ignore[arg-type]  # not called on this path
        principal=make_principal(),
    )
    assert isinstance(dispatcher, GovernedSubAgentDispatcher)
    assert isinstance(registry, ProcessRegistry)


def test_create_cron_scheduler_ensures_schema_and_requires_sink() -> None:
    import sqlalchemy as sa

    from ne.runtime import create_cron_scheduler
    from ne.runtime.principals import StaticAgentPrincipalProvider
    from ne.runtime.scheduler import CronScheduler, ne_scheduled_jobs

    engine = sa.create_engine("sqlite://")
    try:
        # sink + approval_gate required (C6).
        with pytest.raises(TypeError):
            create_cron_scheduler(engine=engine, bindings=[], definitions={})  # type: ignore[call-arg]

        scheduler = create_cron_scheduler(
            engine=engine,
            bindings=standard_bindings(ToolLog()),
            definitions={},
            sink=RecordingAuditSink(),
            approval_gate=NamedApproverGate("alice"),
            gateway=object(),  # type: ignore[arg-type]
            principals=StaticAgentPrincipalProvider({}),
        )
        assert isinstance(scheduler, CronScheduler)
        # The schema (job table) was ensured on the engine by the factory.
        with engine.connect() as conn:
            conn.execute(sa.select(ne_scheduled_jobs)).all()
    finally:
        engine.dispose()


def test_tool_executor_factory_threads_extra_guardrails() -> None:
    sink = RecordingAuditSink()
    rail = _FailingRail("stub.executor")
    executor = create_tool_executor(
        standard_bindings(ToolLog()),
        sink=sink,
        approval_gate=NamedApproverGate(),
        govern_settings=govern_settings(),
        registry=_allow_registry(),
        extra_guardrails=(rail,),
    )

    with pytest.raises(GuardrailViolation):
        asyncio.run(executor.execute("read.docs.search", {"q": "x"}, principal=make_principal()))

    blocks = [e for e in sink.events if getattr(e, "action", "") == "guardrail.block"]
    assert blocks and "stub.executor" in (blocks[0].detail or "")


def test_dispatcher_factory_threads_shared_ledger_counter_and_extra_guardrails() -> None:
    ledger = InMemorySpendLedger()
    counter = SlidingWindowCounter()
    rail = _FailingRail("stub.dispatch")
    dispatcher, _registry = create_subagent_dispatcher(
        bindings=standard_bindings(ToolLog()),
        definitions={},
        sink=RecordingAuditSink(),
        approval_gate=NamedApproverGate("alice"),
        gateway=object(),  # type: ignore[arg-type]  # not called on this path
        principal=make_principal(),
        govern_settings=govern_settings(),
        ledger=ledger,
        counter=counter,
        extra_guardrails=(rail,),
        plugin_registry=_allow_registry(),
    )
    engine = dispatcher._executor._guardrails
    assert engine._spend_ledger is ledger
    assert engine._rate_counter is counter
    assert rail in engine._guardrails


def test_cron_factory_threads_shared_ledger_counter_and_extra_guardrails() -> None:
    import sqlalchemy as sa

    from ne.runtime.principals import StaticAgentPrincipalProvider

    engine_db = sa.create_engine("sqlite://")
    try:
        ledger = InMemorySpendLedger()
        counter = SlidingWindowCounter()
        rail = _FailingRail("stub.cron")
        scheduler = create_cron_scheduler(
            engine=engine_db,
            bindings=standard_bindings(ToolLog()),
            definitions={},
            sink=RecordingAuditSink(),
            approval_gate=NamedApproverGate("alice"),
            gateway=object(),  # type: ignore[arg-type]
            principals=StaticAgentPrincipalProvider({}),
            govern_settings=govern_settings(),
            ledger=ledger,
            counter=counter,
            extra_guardrails=(rail,),
            plugin_registry=_allow_registry(),
        )
        engine = scheduler._executor._guardrails
        assert engine._spend_ledger is ledger
        assert engine._rate_counter is counter
        assert rail in engine._guardrails
    finally:
        engine_db.dispose()


def test_factory_shared_state_signature_guardian() -> None:
    for factory in (create_tool_executor, create_subagent_dispatcher, create_cron_scheduler):
        params = inspect.signature(factory).parameters
        assert params["ledger"].default is None
        assert params["counter"].default is None
        assert params["extra_guardrails"].default == ()

    for factory in (create_subagent_dispatcher, create_cron_scheduler):
        assert inspect.signature(factory).parameters["plugin_registry"].default is None

    a, b = create_shared_guardrail_state()
    assert isinstance(a, SpendLedger)
    assert isinstance(b, SlidingWindowCounter)
    # A distinct fresh pair every call (never a shared module-level singleton).
    c, d = create_shared_guardrail_state()
    assert a is not c and b is not d


def test_existing_run_evidence_context_uses_only_canonical_records() -> None:
    now = datetime.now(UTC)
    run = AgentRun(
        "run-1",
        "conversation-1",
        "owner-1",
        "ops",
        "agent",
        "agent-id",
        1,
        "bounded",
        "a" * 64,
        "b" * 64,
        "receipt-cap",
        1,
        "Run",
        RunState.RUNNING,
        now,
        now,
        None,
    )
    receipt = RunAuditReceipt(
        "receipt-1",
        "run-1",
        "attempt-1",
        "event-1",
        "step-1",
        "tool-1",
        None,
        RunAuditDecisionKind.EVIDENCE,
        "audit-1",
        1,
        "c" * 64,
        "a" * 64,
        now,
    )
    provider = ExistingRunEvidenceProvider.from_existing(run=run, receipt=receipt)
    first = require_existing_run_evidence(provider)
    second = require_existing_run_evidence(provider)
    assert (first.run_id, first.step_id, first.tool_call_id, first.read_ordinal) == (
        "run-1",
        "step-1",
        "tool-1",
        0,
    )
    assert second.read_ordinal == 1


def test_missing_existing_run_context_fails_without_store_side_effect() -> None:
    with pytest.raises(LookupError, match="existing canonical Run"):
        require_existing_run_evidence(None)
    with pytest.raises(TypeError, match="from_existing"):
        ExistingRunEvidenceProvider()


def test_factory_binds_agent_version_to_exact_deterministic_profile_pin() -> None:
    profile = MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE
    binding = create_agent_execution_binding(
        agent_id="agt_01k3hardshipreview0000000000",
        agent_version=1,
        strategy="deterministic",
        workflow_profile=profile,
        declared_profile_id=profile.workflow_id,
        declared_profile_version=profile.version,
        declared_profile_config_hash=profile.config_hash,
    )

    assert binding.strategy is ExecutionStrategy.DETERMINISTIC
    assert binding.profile_identity == (profile.workflow_id, profile.version, profile.config_hash)

    with pytest.raises(ValueError, match="does not match"):
        create_agent_execution_binding(
            agent_id="agt_01k3hardshipreview0000000000",
            agent_version=1,
            strategy="deterministic",
            workflow_profile=profile,
            declared_profile_id=profile.workflow_id,
            declared_profile_version=profile.version,
            declared_profile_config_hash="0" * 64,
        )


def test_factory_refuses_hidden_hybrid_admission() -> None:
    with pytest.raises(ValueError, match="AgenticStep/child-Run"):
        create_agent_execution_binding(
            agent_id="agt_future_hybrid",
            agent_version=1,
            strategy="hybrid",
            agentic_adapter=BoundedAgenticAdapter(
                instructions="Attempt a hidden hybrid sub-run.",
                max_tool_calls=2,
                total_tokens_limit=1024,
                wall_clock_seconds=10,
                max_parked_writes=0,
            ),
        )


def test_factory_agentic_binding_requires_bounded_adapter_and_no_profile_pin() -> None:
    adapter = BoundedAgenticAdapter(
        instructions="Use only bound governed capabilities.",
        max_tool_calls=4,
        total_tokens_limit=4096,
        wall_clock_seconds=30,
        max_parked_writes=1,
    )
    binding = create_agent_execution_binding(
        agent_id="agt_analyst",
        agent_version=2,
        strategy="agentic",
        agentic_adapter=adapter,
    )

    assert binding.strategy is ExecutionStrategy.AGENTIC
    assert binding.agentic_adapter is adapter
    assert binding.profile_identity is None
