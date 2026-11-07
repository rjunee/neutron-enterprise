# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CKD1 executable composition proof — the guardrail composition seams closed.

This module is the executable proof for boundary-audit findings #1/#7/#8. It
constructs the REAL ne-identity ``AgentScopeGuardrail`` over a REAL
``AgentCredentialService`` (backed by the contracts ``MemorySecretStore``) and
injects it — through the ``extra_guardrails`` seam ONLY — into all three executor
factories, proving:

* finding #7/#8 (least-privilege scope): an agent whose STORED credential scope is
  ``{read.docs.search}`` is DENIED the out-of-scope tier-1 tool
  ``write.internal.notes.append`` on the session AND dispatch AND cron paths (an
  audited ``guardrail.block`` with rule ``agent.scope``; the handler never runs),
  while the in-scope tool is ALLOWED. A CONTROL test pins that WITHOUT the injected
  rail the same principal executes the out-of-scope tool — the seam, not luck,
  provides enforcement.
* finding #1 (Nx cap multiplication): ONE ``create_shared_guardrail_state()`` pair
  threaded into all three factories makes a department spend cap — and the rate
  limit — platform-wide, not reset per executor.

Cross-package TEST imports are legal here: import-linter analyzes only the ``ne.*``
SOURCE packages (ne-runtime src never imports ne-identity — the seam is typed by
the frozen ``ne.contracts.governance.Guardrail`` Protocol), and
``uv sync --all-packages`` installs ne-identity for the test runtime. The wiring
lives at this composition-root/test boundary, which MAY import both.

DECIDED (NE-CKD1): ``DepartmentAgentSession.tool_specs`` is NOT filtered by scope.
Filtering the model's advertised tool list by scope would require credential
knowledge INSIDE ne-runtime (violating the ne-runtime→ne-identity import ban).
Enforcement lives in the engine instead: an out-of-scope call is blocked+audited
BEFORE execution even though the spec is listed to the model (``runner.py`` turns
the ``GuardrailViolation`` into a ``BLOCKED: …`` tool result and the loop
continues), so on the session/dispatch/cron loop paths these tests assert the
block via the audit sink + the tool log, NOT via ``pytest.raises``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from ne.contracts.governance import DecisionEffect, PolicyDecision, PolicyRequest
from ne.contracts.plugins import PluginRegistry
from ne.contracts.runtime import ScheduledJob, SubAgentSpec
from ne.contracts.testing.memory import MemorySecretStore
from ne.contracts.types import Principal
from ne.govern.config import (
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    SpendCapSettings,
)
from ne.identity.agents.config import AgentCredentialConfig
from ne.identity.agents.guardrail import AgentScopeGuardrail
from ne.identity.agents.model import AgentRef, CredentialScope
from ne.identity.agents.service import AgentCredentialService
from ne.runtime import (
    create_cron_scheduler,
    create_department_session,
    create_subagent_dispatcher,
    create_tool_executor,
)
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.factory import create_shared_guardrail_state
from ne.runtime.principals import StaticAgentPrincipalProvider
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import ToolBinding

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    govern_settings,
    make_principal,
    standard_bindings,
    text_completion,
    tool_call_completion,
)

_CLOCK_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
_T0 = _CLOCK_NOW
_LATER = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)

_IN_SCOPE = "read.docs.search"
_OUT_OF_SCOPE = "write.internal.notes.append"

_RESEARCHER = {
    "researcher": AgentDefinition(
        name="researcher",
        instructions="Research.",
        toolset=(_IN_SCOPE, _OUT_OF_SCOPE),
    )
}


class _StubPdp:
    """An ALLOW-always PDP. Faithful here: C12 guardrails run BEFORE the PDP
    (middleware orders guardrails first), and these tests exercise tier-0/1 tools
    only, so the scope/spend/rate block always precedes any PDP judgment."""

    def __init__(self, sink: Any) -> None:
        self._sink = sink

    @classmethod
    def from_settings(cls, settings: GovernSettings, *, sink: Any) -> _StubPdp:
        return cls(sink)

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        return PolicyDecision(effect=DecisionEffect.ALLOW, policy_version="stub-1")


def _allow_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", _StubPdp)
    return registry


def _make_service() -> tuple[AgentCredentialService, RecordingAuditSink]:
    """A REAL AgentCredentialService over the contracts MemorySecretStore (frozen clock)."""
    svc_sink = RecordingAuditSink()
    service = AgentCredentialService(
        MemorySecretStore(now=lambda: _CLOCK_NOW),
        svc_sink,
        AgentCredentialConfig(grace_seconds=300.0, rotate_after_seconds=86400.0),
        clock=lambda: _CLOCK_NOW,
    )
    return service, svc_sink


def _issue_agent_principal(
    service: AgentCredentialService, *, tools: tuple[str, ...] = (_IN_SCOPE,)
) -> Principal:
    cred = asyncio.run(
        service.issue(
            AgentRef(department="sales", agent_name="assistant"),
            CredentialScope(tools=tools),
            actor="op",
        )
    )
    # bind_principal carries claims['department']=='sales' so the session dept gate
    # and the department spend cap both key on it.
    return service.bind_principal(cred.credential_id)


def _settings(
    *,
    spend: SpendCapSettings | None = None,
    rate: RateLimitSettings | None = None,
) -> GovernSettings:
    """Governance settings reusing the conftest tool/recipient policies, with an
    optional per-test spend/rate override."""
    base = govern_settings().guardrails
    return GovernSettings(
        guardrails=GuardrailSettings(
            recipients=base.recipients,
            rate=rate if rate is not None else base.rate,
            tools=base.tools,
            spend=spend if spend is not None else SpendCapSettings(),
        )
    )


def _bindings_with_estimate(log: ToolLog) -> list[ToolBinding]:
    """standard_bindings with a fixed $0.02 estimate on the in-scope tool."""
    out: list[ToolBinding] = []
    for binding in standard_bindings(log):
        if binding.spec.name == _IN_SCOPE:
            binding = dataclasses.replace(binding, estimate_cost_usd=lambda a: "0.02")
        out.append(binding)
    return out


def _rule_blocks(sink: RecordingAuditSink, rule: str) -> list[Any]:
    """guardrail.block events whose detail rules include ``rule``."""
    hits: list[Any] = []
    for event in sink.events:
        if getattr(event, "action", "") != "guardrail.block":
            continue
        detail = getattr(event, "detail", None)
        if detail and rule in json.loads(detail).get("rules", []):
            hits.append(event)
    return hits


# --- finding #7/#8: scope enforcement on all three paths ------------------


def test_scope_restricted_agent_denied_out_of_scope_tool_on_session_path() -> None:
    service, _svc_sink = _make_service()
    agent = _issue_agent_principal(service)
    scope_rail = AgentScopeGuardrail(service)
    ledger, counter = create_shared_guardrail_state()
    log = ToolLog()
    sink = RecordingAuditSink()
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    try:
        store = SqlAlchemySessionStore(engine)
        executor = create_tool_executor(
            standard_bindings(log),
            sink=sink,
            approval_gate=NamedApproverGate("alice"),
            govern_settings=_settings(),
            registry=_allow_registry(),
            ledger=ledger,
            counter=counter,
            extra_guardrails=(scope_rail,),
        )
        session = asyncio.run(
            create_department_session(
                "assistant",
                "sales",
                owner_principal_id=agent.id,
                store=store,
                gateway=ScriptedGateway(
                    [tool_call_completion(_OUT_OF_SCOPE, {"note": "x"}), text_completion("done")]
                ),
                executor=executor,
            )
        )
        result = asyncio.run(session.run("go", principal=agent))

        assert result.output == "done"
        assert _OUT_OF_SCOPE not in log.names()
        assert _rule_blocks(sink, "agent.scope")
    finally:
        engine.dispose()


def test_scope_restricted_agent_allowed_in_scope_tool_on_session_path() -> None:
    service, _svc_sink = _make_service()
    agent = _issue_agent_principal(service)
    scope_rail = AgentScopeGuardrail(service)
    ledger, counter = create_shared_guardrail_state()
    log = ToolLog()
    sink = RecordingAuditSink()
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    try:
        store = SqlAlchemySessionStore(engine)
        executor = create_tool_executor(
            standard_bindings(log),
            sink=sink,
            approval_gate=NamedApproverGate("alice"),
            govern_settings=_settings(),
            registry=_allow_registry(),
            ledger=ledger,
            counter=counter,
            extra_guardrails=(scope_rail,),
        )
        session = asyncio.run(
            create_department_session(
                "assistant",
                "sales",
                owner_principal_id=agent.id,
                store=store,
                gateway=ScriptedGateway(
                    [tool_call_completion(_IN_SCOPE, {"q": "x"}), text_completion("done")]
                ),
                executor=executor,
            )
        )
        result = asyncio.run(session.run("go", principal=agent))

        assert result.output == "done"
        assert log.names() == [_IN_SCOPE]  # the in-scope tool ran; the rail did not over-block
        assert any(getattr(e, "action", "") == "tool.execute" for e in sink.events)
        assert not _rule_blocks(sink, "agent.scope")
    finally:
        engine.dispose()


def test_scope_restricted_agent_denied_out_of_scope_tool_on_dispatch_path() -> None:
    service, _svc_sink = _make_service()
    agent = _issue_agent_principal(service)
    scope_rail = AgentScopeGuardrail(service)
    ledger, counter = create_shared_guardrail_state()
    log = ToolLog()
    sink = RecordingAuditSink()
    dispatcher, _registry = create_subagent_dispatcher(
        bindings=standard_bindings(log),
        definitions=_RESEARCHER,
        sink=sink,
        approval_gate=NamedApproverGate("alice"),
        gateway=ScriptedGateway(
            [tool_call_completion(_OUT_OF_SCOPE, {"note": "x"}), text_completion("done")]
        ),
        principal=agent,
        govern_settings=_settings(),
        ledger=ledger,
        counter=counter,
        extra_guardrails=(scope_rail,),
        plugin_registry=_allow_registry(),
    )

    result = asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))

    assert result.succeeded is True  # loop completes; the block is not fatal
    assert _OUT_OF_SCOPE not in log.names()
    assert _rule_blocks(sink, "agent.scope")


def test_scope_restricted_agent_denied_out_of_scope_tool_on_cron_path() -> None:
    service, _svc_sink = _make_service()
    agent = _issue_agent_principal(service)
    scope_rail = AgentScopeGuardrail(service)
    ledger, counter = create_shared_guardrail_state()
    log = ToolLog()
    sink = RecordingAuditSink()
    engine = sa.create_engine("sqlite://")
    try:
        scheduler = create_cron_scheduler(
            engine=engine,
            bindings=standard_bindings(log),
            definitions=_RESEARCHER,
            sink=sink,
            approval_gate=NamedApproverGate("alice"),
            gateway=ScriptedGateway(
                [tool_call_completion(_OUT_OF_SCOPE, {"note": "x"}), text_completion("done")]
            ),
            principals=StaticAgentPrincipalProvider({"researcher": agent}),
            govern_settings=_settings(),
            clock=lambda: _T0,
            ledger=ledger,
            counter=counter,
            extra_guardrails=(scope_rail,),
            plugin_registry=_allow_registry(),
        )
        scheduler.schedule(
            ScheduledJob(
                name="cron-scope",
                cron="* * * * *",
                agent="researcher",
                toolset=(_IN_SCOPE, _OUT_OF_SCOPE),
            )
        )
        outcomes = asyncio.run(scheduler.run_due_jobs(now=_LATER))

        assert len(outcomes) == 1 and outcomes[0].succeeded is True
        assert _OUT_OF_SCOPE not in log.names()
        assert _rule_blocks(sink, "agent.scope")
    finally:
        engine.dispose()


def test_without_scope_rail_out_of_scope_tool_executes_documenting_the_closed_gap() -> None:
    # CONTROL: the SAME agent principal, but the executor is built WITHOUT the
    # injected scope rail — the pre-fix hazard (finding #7/#8). The out-of-scope
    # tier-1 tool EXECUTES: the rail (not luck) is what closes the gap.
    service, _svc_sink = _make_service()
    agent = _issue_agent_principal(service)
    ledger, counter = create_shared_guardrail_state()
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = create_tool_executor(
        standard_bindings(log),
        sink=sink,
        approval_gate=NamedApproverGate("alice"),
        govern_settings=_settings(),
        registry=_allow_registry(),
        ledger=ledger,
        counter=counter,
    )

    result = asyncio.run(executor.execute(_OUT_OF_SCOPE, {"note": "x"}, principal=agent))

    assert result == "noted"
    assert _OUT_OF_SCOPE in log.names()
    assert not _rule_blocks(sink, "agent.scope")


# --- finding #1: shared spend ledger + rate counter across executors ------


def test_department_spend_cap_exhausted_across_session_dispatch_and_cron_executors() -> None:
    ledger, counter = create_shared_guardrail_state()
    settings = _settings(spend=SpendCapSettings(department_caps_usd={"sales": "0.03"}))
    principal = make_principal("agent:sales/assistant", department="sales")

    # (1) SESSION executor — 1st $0.02 call PASSES and pre-charges department:sales.
    session_log = ToolLog()
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    try:
        store = SqlAlchemySessionStore(engine)
        session_exec = create_tool_executor(
            _bindings_with_estimate(session_log),
            sink=RecordingAuditSink(),
            approval_gate=NamedApproverGate("alice"),
            govern_settings=settings,
            registry=_allow_registry(),
            ledger=ledger,
            counter=counter,
        )
        session = asyncio.run(
            create_department_session(
                "assistant",
                "sales",
                owner_principal_id=principal.id,
                store=store,
                gateway=ScriptedGateway(
                    [tool_call_completion(_IN_SCOPE, {"q": "x"}), text_completion("done")]
                ),
                executor=session_exec,
            )
        )
        asyncio.run(session.run("go", principal=principal))
        assert session_log.names() == [_IN_SCOPE]
    finally:
        engine.dispose()

    # (2) DISPATCH path — 2nd call over the SAME ledger is blocked (0.02+0.02 > 0.03).
    dispatch_log = ToolLog()
    dispatch_sink = RecordingAuditSink()
    dispatcher, _registry = create_subagent_dispatcher(
        bindings=_bindings_with_estimate(dispatch_log),
        definitions=_RESEARCHER,
        sink=dispatch_sink,
        approval_gate=NamedApproverGate("alice"),
        gateway=ScriptedGateway(
            [tool_call_completion(_IN_SCOPE, {"q": "x"}), text_completion("done")]
        ),
        principal=principal,
        govern_settings=settings,
        ledger=ledger,
        counter=counter,
        plugin_registry=_allow_registry(),
    )
    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))
    assert dispatch_log.names() == []
    assert _rule_blocks(dispatch_sink, "spend.cap-exceeded")

    # (3) CRON path — 3rd call over the SAME ledger is blocked too.
    cron_log = ToolLog()
    cron_sink = RecordingAuditSink()
    cron_engine = sa.create_engine("sqlite://")
    try:
        scheduler = create_cron_scheduler(
            engine=cron_engine,
            bindings=_bindings_with_estimate(cron_log),
            definitions=_RESEARCHER,
            sink=cron_sink,
            approval_gate=NamedApproverGate("alice"),
            gateway=ScriptedGateway(
                [tool_call_completion(_IN_SCOPE, {"q": "x"}), text_completion("done")]
            ),
            principals=StaticAgentPrincipalProvider({"researcher": principal}),
            govern_settings=settings,
            clock=lambda: _T0,
            ledger=ledger,
            counter=counter,
            plugin_registry=_allow_registry(),
        )
        scheduler.schedule(
            ScheduledJob(
                name="cron-spend", cron="* * * * *", agent="researcher", toolset=(_IN_SCOPE,)
            )
        )
        asyncio.run(scheduler.run_due_jobs(now=_LATER))
        assert cron_log.names() == []
        assert _rule_blocks(cron_sink, "spend.cap-exceeded")
    finally:
        cron_engine.dispose()

    # ONE ledger: exactly the single admitted call is recorded — caps not reset per executor.
    assert ledger.recorded_spend("department:sales") == Decimal("0.02")


def test_rate_limit_shared_across_executors() -> None:
    ledger, counter = create_shared_guardrail_state()
    settings = _settings(
        rate=RateLimitSettings(default=RateLimitRule(max_calls=1, window_s=3600.0))
    )
    principal = make_principal("agent:sales/assistant", department="sales")

    # (1) SESSION executor — 1st call PASSES and records the shared counter.
    session_log = ToolLog()
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    try:
        store = SqlAlchemySessionStore(engine)
        session_exec = create_tool_executor(
            standard_bindings(session_log),
            sink=RecordingAuditSink(),
            approval_gate=NamedApproverGate("alice"),
            govern_settings=settings,
            registry=_allow_registry(),
            ledger=ledger,
            counter=counter,
        )
        session = asyncio.run(
            create_department_session(
                "assistant",
                "sales",
                owner_principal_id=principal.id,
                store=store,
                gateway=ScriptedGateway(
                    [tool_call_completion(_IN_SCOPE, {"q": "x"}), text_completion("done")]
                ),
                executor=session_exec,
            )
        )
        asyncio.run(session.run("go", principal=principal))
        assert session_log.names() == [_IN_SCOPE]
    finally:
        engine.dispose()

    # (2) DISPATCH path — the SAME principal+tool over the SHARED counter is blocked.
    dispatch_log = ToolLog()
    dispatch_sink = RecordingAuditSink()
    dispatcher, _registry = create_subagent_dispatcher(
        bindings=standard_bindings(dispatch_log),
        definitions=_RESEARCHER,
        sink=dispatch_sink,
        approval_gate=NamedApproverGate("alice"),
        gateway=ScriptedGateway(
            [tool_call_completion(_IN_SCOPE, {"q": "x"}), text_completion("done")]
        ),
        principal=principal,
        govern_settings=settings,
        ledger=ledger,
        counter=counter,
        plugin_registry=_allow_registry(),
    )
    asyncio.run(dispatcher.dispatch(SubAgentSpec(agent="researcher", task="t")))
    assert dispatch_log.names() == []
    assert _rule_blocks(dispatch_sink, "rate.limit-exceeded")
