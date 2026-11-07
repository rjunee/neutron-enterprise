# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Agent loop — prompt -> governed tool call -> final answer, over a STUB gateway."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.models import Completion, CompletionRequest
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    GovernedLocalT3SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundModelSendResolution,
    SourceBoundToolResultRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.contracts.types import ApprovalTier
from ne.runtime.agent import AgentExecutionBinding, DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.factory import create_department_session
from ne.runtime.source_results import (
    SOURCE_WITHHELD_TEXT,
    ProtectedSourceResultGuard,
    authenticated_request_scope,
    encode_protected_reference,
    model_call_scope,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import ToolBinding
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    ExecutionStrategy,
)

from .conftest import (
    AppendCountFailingSink,
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_principal,
    standard_bindings,
    text_completion,
    tool_call_completion,
)


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_runtime_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _session(
    engine: sa.Engine,
    gateway: ScriptedGateway,
    sink: Any,
    log: ToolLog,
    *,
    pdp_overrides: dict[str, dict[str, Any]] | None = None,
    settings: RuntimeSettings | None = None,
) -> DepartmentAgentSession:
    store = SqlAlchemySessionStore(engine)
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=NamedApproverGate("alice"),
        pdp_overrides=pdp_overrides,
    )
    settings = settings or RuntimeSettings()
    return DepartmentAgentSession(
        session_id=asyncio.run(
            store.create_conversation("assistant", "eng", "user:ada")
        ).conversation_id,
        agent_name="assistant",
        department="eng",
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=gateway,
        settings=settings,
    )


def test_agent_drives_governed_tool_call(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "q3"}), text_completion("final answer")]
    )
    session = _session(engine, gw, sink, log)

    result = asyncio.run(session.run("please search", principal=make_principal(department="eng")))

    assert result.output == "final answer"
    assert log.names() == ["read.docs.search"]
    actions = [getattr(e, "action", "") for e in sink.events]
    assert "pdp.decide" in actions
    assert "tool.execute" in actions


def test_deterministic_profile_cannot_be_relabelled_as_agentic() -> None:
    with pytest.raises(ValueError, match="agentic Agent binding"):
        AgentExecutionBinding(
            agent_id="agt_hardship",
            agent_version=1,
            strategy=ExecutionStrategy.AGENTIC,
            workflow_profile=MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
        )


def test_session_past_window_still_runs_and_model_sees_bounded_history(engine: sa.Engine) -> None:
    # P1-3 anti-context_length_exceeded regression: a session with FAR more stored
    # batches than the window still runs, and the model receives at most the window.
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway([text_completion("done")])
    settings = RuntimeSettings(session_history_max_turns=5)
    session = _session(engine, gw, sink, log, settings=settings)

    async def seed() -> None:
        for i in range(12):  # 12 batches, 2 messages each = 24 stored messages
            attachment = f"seed-turn-{i}"
            await session._store.append_conversation_turn(
                session._session_id,
                "user:ada",
                "eng",
                "assistant",
                [
                    ModelRequest(parts=[UserPromptPart(content=f"u{i}")]),
                    ModelResponse(
                        parts=[TextPart(content=f"a{i}")],
                        usage=RequestUsage(input_tokens=1, output_tokens=1),
                        model_name="m",
                    ),
                ],
                served_output=f"a{i}",
                trace_id=f"seed-trace-{i}",
                attachment_id=attachment,
                attempt_id=f"attempt-{attachment}",
            )
            await session._store.publish_conversation_turn(
                session._session_id, "user:ada", attachment, f"attempt-{attachment}", []
            )

    asyncio.run(seed())
    result = asyncio.run(session.run("next", principal=make_principal(department="eng")))

    assert result.output == "done"
    # The model saw at most the last 5 batches (10 messages) + the new prompt — never
    # the full 24 pre-seeded messages, so history can't grow the context unbounded.
    assert gw.requests
    seen = len(gw.requests[0].messages)
    assert seen < 24
    # Bounded to the 5-batch window (10 messages) plus a small per-run framing
    # allowance — decisively the window, not the full 24-message log.
    assert seen <= 5 * 2 + 2


def test_denied_tool_yields_blocked_result_and_terminates(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "q"}), text_completion("all done")]
    )
    session = _session(
        engine,
        gw,
        sink,
        log,
        pdp_overrides={
            "read.docs.search": {
                "effect": "deny",
                "tier": 0,
                "reason": "nope",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )

    result = asyncio.run(session.run("search", principal=make_principal(department="eng")))

    assert result.output == "all done"
    assert log.names() == []  # handler never ran
    denied = [
        e for e in sink.events if getattr(e, "action", "") == "pdp.decide" and e.outcome == "denied"
    ]
    assert len(denied) == 1


def test_guardrail_blocked_call_terminates(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gw = ScriptedGateway(
        [
            tool_call_completion(
                "write.external.email.send", {"to": "attacker@evil.example", "body": "x"}
            ),
            text_completion("handled"),
        ]
    )
    session = _session(engine, gw, sink, log)

    result = asyncio.run(session.run("email", principal=make_principal(department="eng")))

    assert result.output == "handled"
    assert log.names() == []
    assert any(getattr(e, "action", "") == "guardrail.block" for e in sink.events)


def test_request_limit_is_honored(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = RecordingAuditSink()

    class AlwaysToolCallGateway(ScriptedGateway):
        async def complete(self, request: CompletionRequest) -> Completion:
            self.requests.append(request)
            return tool_call_completion("read.docs.search", {"q": "loop"})

    gw = AlwaysToolCallGateway()
    session = _session(engine, gw, sink, log, settings=RuntimeSettings(max_requests=3))

    with pytest.raises(UsageLimitExceeded):
        asyncio.run(session.run("spin", principal=make_principal(department="eng")))


def test_runtime_audit_error_kills_the_run(engine: sa.Engine) -> None:
    log = ToolLog()
    sink = AppendCountFailingSink(fail_on_action="tool.execute")
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "q"}), text_completion("never reached")]
    )
    session = _session(engine, gw, sink, log)

    # The middleware's tool.execute audit append fails => RuntimeAuditError, which
    # is NOT converted to a BLOCKED tool result; it propagates and kills the run.
    with pytest.raises(RuntimeAuditError):
        asyncio.run(session.run("search", principal=make_principal(department="eng")))


# --- PRE-015A0-ENFORCEMENT: the composed session wires the delivery guard ------


def _stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="governed_local_t3",
        connector_instance_id="connector-a",
        point=GovernedLocalT3SourcePoint(dataset_name="meridian", contributor_key="docs"),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="governed_local_t3", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="rev-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


class _SessionResolver:
    """Materializes one Tool result for the model send, then flips to withheld."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.decision = SourceAuthorityDecision.AUTHORIZED

    async def for_model_send(
        self,
        reference: Any,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        model_call_attempt_id: str,
    ) -> SourceBoundModelSendResolution:
        authorized = self.decision is SourceAuthorityDecision.AUTHORIZED
        return SourceBoundModelSendResolution(
            decision=self.decision,
            materialized_text=self._text if authorized else None,
            stamps=(_stamp(),) if authorized else (),
            safe_reason=None if authorized else "safe",
            run_id=run_id,
            model_call_attempt_id=model_call_attempt_id,
        )

    async def for_direct_result(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_approval(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_dispatch(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_workflow_projection(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


_SESSION_SECRET = "Q3 revenue was 41.2M, per the board deck."
_SESSION_EXPIRES_AT = 1_800_000_000


def _protected_session(
    engine: sa.Engine, gateway: ScriptedGateway, resolver: _SessionResolver
) -> Any:
    """A session composed exactly as production composes it — through the factory."""
    encoded = encode_protected_reference(
        SourceBoundToolResultRef(reference_id="t-1", stamps=(_stamp(),), result_hash="e" * 64)
    )
    binding = ToolBinding(
        spec=ToolSpec(
            name="read.docs.search",
            description="Search internal docs.",
            tier=ApprovalTier.TIER_0,
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        ),
        handler=ToolLog().handler("read.docs.search", encoded),
    )
    sink = RecordingAuditSink()
    store = SqlAlchemySessionStore(engine)
    return asyncio.run(
        create_department_session(
            "assistant",
            "eng",
            owner_principal_id="user:ada",
            store=store,
            gateway=gateway,
            executor=make_executor([binding], sink, approval_gate=NamedApproverGate("alice")),
            source_guard=ProtectedSourceResultGuard(
                resolver,
                clock=lambda: datetime.fromtimestamp(_SESSION_EXPIRES_AT - 1, tz=UTC),
            ),
        )
    )


def test_factory_composed_session_delivers_protected_tool_text_only_to_the_gateway(
    engine: sa.Engine,
) -> None:
    resolver = _SessionResolver(_SESSION_SECRET)
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "q3"}), text_completion("final answer")]
    )
    session = _protected_session(engine, gw, resolver)
    context = AuthenticatedRequestContext(
        principal_id="user:ada",
        authorization_groups=("group:staff",),
        issuer="https://oidc.test/realms/ne",
        expires_at=_SESSION_EXPIRES_AT,
    )
    with (
        authenticated_request_scope(context),
        model_call_scope(run_id="run-1", model_call_attempt_id="attempt-1"),
    ):
        result = asyncio.run(
            session.run("please search", principal=make_principal(department="eng"))
        )
    assert result.output == "final answer"
    # The second model call is the one carrying the tool return: the model sees the
    # materialized text, and the opaque reference never reaches it.
    #
    # NE-REF-VOCAB: the authorized delivery now also carries the reference's own
    # `reference_id`. The guard emits it wherever it delivers authorized Tool
    # material, INCLUDING this session path which has no claim contract — a seam
    # that decides what reaches a model is easier to trust with one behaviour than
    # with two, and the id is a random surrogate that discloses nothing.
    tool_messages = [m.content for m in gw.requests[1].messages if m.role == "tool"]
    # The tool NAME prefix is added by request assembly, so the id lands at the
    # head of the returned content itself.
    assert tool_messages == [f"read.docs.search returned: [source-id: t-1]\n{_SESSION_SECRET}"]
    # The reference itself still never crosses.
    assert "ne-source-ref:v1:" not in tool_messages[0]


def test_factory_composed_session_withholds_the_tool_text_when_authority_drifts(
    engine: sa.Engine,
) -> None:
    resolver = _SessionResolver(_SESSION_SECRET)
    resolver.decision = SourceAuthorityDecision.PERMANENT_WITHHELD
    gw = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "q3"}), text_completion("final answer")]
    )
    session = _protected_session(engine, gw, resolver)
    context = AuthenticatedRequestContext(
        principal_id="user:ada",
        authorization_groups=("group:staff",),
        issuer="https://oidc.test/realms/ne",
        expires_at=_SESSION_EXPIRES_AT,
    )
    with (
        authenticated_request_scope(context),
        model_call_scope(run_id="run-1", model_call_attempt_id="attempt-1"),
    ):
        asyncio.run(session.run("please search", principal=make_principal(department="eng")))
    tool_messages = [m.content for m in gw.requests[1].messages if m.role == "tool"]
    assert tool_messages == [f"read.docs.search returned: {SOURCE_WITHHELD_TEXT}"]
    assert _SESSION_SECRET not in json.dumps([m.content for m in gw.requests[1].messages])
