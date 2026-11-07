# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Agent identity binds to the session principal and is what governance SEES (C6).

Real-seam integration (dev deps ne-runtime + ne-govern). Cross-package TEST
imports are forbidden, so the model/pdp/gate doubles are duplicated locally per
the ne-runtime conftest precedent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.governance import (
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
    DecisionEffect,
    PolicyDecision,
    PolicyRequest,
)
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.runtime import ToolSpec
from ne.contracts.testing.memory import MemorySecretStore
from ne.contracts.types import ApprovalTier
from ne.govern import create_guardrail_engine
from ne.govern.config import (
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    ToolArgPolicy,
)
from ne.identity.agents import (
    AgentCredentialConfig,
    AgentCredentialService,
)
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import SessionDepartmentMismatchError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import ToolBinding

from .conftest import ListSink, MutableClock, run, sample_agent, sample_scope

# --- local doubles (duplicated per the cross-package no-test-import rule) ----


class RecordingPdp:
    """A PolicyDecisionPoint double that ALLOWs and records every request."""

    def __init__(self) -> None:
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        return PolicyDecision(effect=DecisionEffect.ALLOW, policy_version="test-1.0")


class AutoApproveGate:
    """Never consulted on the tier-0 allow path; APPROVES with a named approver."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome(status=ApprovalStatus.APPROVED, approver="ops")


class ScriptedGateway:
    """A ``ModelGateway`` returning scripted completions (final answer by default)."""

    def __init__(self, completions: Sequence[Completion] | None = None) -> None:
        self._queue = list(completions or [])
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        return Completion(
            text="done", model_id="stub", usage=Usage(prompt_tokens=1, completion_tokens=1)
        )

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


def _read_notes_binding() -> ToolBinding:
    async def _handler(args: Any) -> str:
        return "notes: ok"

    return ToolBinding(
        spec=ToolSpec(
            name="read.notes",
            description="Read internal notes.",
            tier=ApprovalTier.TIER_0,
            input_schema={"type": "object", "properties": {}},
        ),
        handler=_handler,
    )


def _govern_settings() -> GovernSettings:
    return GovernSettings(
        guardrails=GuardrailSettings(
            rate=RateLimitSettings(default=RateLimitRule(max_calls=1000, window_s=3600.0)),
            tools={"read.notes": ToolArgPolicy(args={})},
        )
    )


def _executor(sink: ListSink, pdp: RecordingPdp) -> GovernedToolExecutor:
    return GovernedToolExecutor(
        [_read_notes_binding()],
        pdp=pdp,
        guardrails=create_guardrail_engine(_govern_settings(), sink=sink),
        approval_gate=AutoApproveGate(),
        sink=sink,
    )


# --- tests -----------------------------------------------------------------


def test_governed_executor_sees_agent_principal(
    agent_service: Any,
) -> None:
    # Issue a scoped credential, bind its principal, and run a real governed
    # tool call: the PDP request principal + the audit actor must BOTH be the
    # agent identity (C6 attribution).
    cred = run(agent_service.issue(sample_agent(), sample_scope("read.notes"), actor="op"))
    principal = agent_service.bind_principal(cred.credential_id)

    sink = ListSink()
    pdp = RecordingPdp()
    executor = _executor(sink, pdp)

    result = run(executor.execute("read.notes", {}, principal=principal))
    assert result == "notes: ok"

    assert pdp.requests
    assert pdp.requests[0].principal.id == "agent:sales/assistant"
    execute_events = sink.with_action("tool.execute")
    assert execute_events
    assert all(e.actor == "agent:sales/assistant" for e in execute_events)


def test_department_session_accepts_bound_principal() -> None:
    # Standalone service (this test does not use the agent_service fixture so it
    # can freeze the config hash itself for the real runtime audit path).
    reset_config_hash_for_testing()
    freeze_config_hash(RuntimeSettings())
    try:
        clock = MutableClock()
        svc = AgentCredentialService(
            MemorySecretStore(now=clock), ListSink(), AgentCredentialConfig(), clock=clock
        )
        cred = run(
            svc.issue(sample_agent("sales", "assistant"), sample_scope("read.notes"), actor="op")
        )
        principal = svc.bind_principal(cred.credential_id)

        engine = sa.create_engine("sqlite://")
        ensure_runtime_schema(engine)
        try:
            store = SqlAlchemySessionStore(engine)
            sink = ListSink()
            executor = _executor(sink, RecordingPdp())
            session = DepartmentAgentSession(
                session_id=run(
                    store.create_conversation("assistant", "sales", principal.id)
                ).conversation_id,
                agent_name="assistant",
                department="sales",
                owner_principal_id=principal.id,
                store=store,
                executor=executor,
                gateway=ScriptedGateway(),
                settings=RuntimeSettings(),
            )
            # department claim matches -> run completes.
            result = run(session.run("hello", principal=principal))
            assert result.output == "done"
        finally:
            engine.dispose()
    finally:
        reset_config_hash_for_testing()


def test_cross_department_principal_rejected() -> None:
    reset_config_hash_for_testing()
    freeze_config_hash(RuntimeSettings())
    try:
        clock = MutableClock()
        svc = AgentCredentialService(
            MemorySecretStore(now=clock), ListSink(), AgentCredentialConfig(), clock=clock
        )
        # A credential (and principal) bound for the ENG department.
        cred = run(
            svc.issue(sample_agent("eng", "assistant"), sample_scope("read.notes"), actor="op")
        )
        eng_principal = svc.bind_principal(cred.credential_id)

        engine = sa.create_engine("sqlite://")
        ensure_runtime_schema(engine)
        try:
            store = SqlAlchemySessionStore(engine)
            session = DepartmentAgentSession(
                session_id=run(
                    store.create_conversation("assistant", "sales", eng_principal.id)
                ).conversation_id,
                agent_name="assistant",
                department="sales",
                owner_principal_id=eng_principal.id,
                store=store,
                executor=_executor(ListSink(), RecordingPdp()),
                gateway=ScriptedGateway(),
                settings=RuntimeSettings(),
            )
            # Bound for eng, run against a sales session -> rejected before any model.
            with pytest.raises(SessionDepartmentMismatchError):
                run(session.run("hello", principal=eng_principal))
        finally:
            engine.dispose()
    finally:
        reset_config_hash_for_testing()
