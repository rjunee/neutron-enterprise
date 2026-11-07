# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""T5 — upgrade-in-place: the SAME workflow under durability='off' is a plain call.

``run_turn_workflow`` run with ``durability='off'`` (the default; the decorators are
transparent passthroughs) yields identical frames/output as it would durably — the
owner's "upgrade-in-place, nothing wasted" claim is a tested property, not a slogan.
The error path emits a terminal ``error`` frame AND re-raises; ``seq`` is strictly
monotonic; a department-mismatch principal becomes a safe error frame.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

import ne.worker.workflows as worker_workflows
from ne.api.platform import Platform, build_platform
from ne.api.turn_wire import (
    LegacyTurnRequest,
    ProtectedTerminalPayload,
    ProtectedTurnRef,
    TurnRequest,
)
from ne.audit import list_records
from ne.contracts import reset_config_hash_for_testing
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.types import Chunk, Lane, Principal, ScoredChunk
from ne.runtime.correlation import (
    current_action_scope,
    current_run_requested_model,
    current_run_requested_model_lane,
    current_run_trace_id,
)
from ne.runtime.factory import create_department_session
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.run_checkpoint import RunApprovalResumeRequest
from ne.runtime.run_finalizer import RunTurnTarget
from ne.runtime.run_store import (
    ProtectedTurnUnavailable,
    RunState,
    ne_agent_evidence_locators,
    ne_agent_evidence_records,
    ne_agent_evidence_sets,
    ne_agent_pending_source_tool_results,
)
from ne.runtime.run_workflow import RunWorkflowRequest
from ne.runtime.source_results import current_authenticated_context
from ne.runtime.store import (
    AttachmentRequestMismatchError,
    conversation_request_fingerprint,
    ne_agent_conversation_turns,
)
from ne.worker.relay import RecordingFrameWriter
from ne.worker.runtime_state import set_frame_writer_factory, set_worker_platform
from ne.worker.testing import StubChunkStore
from ne.worker.workflows import run_turn_workflow

from .conftest import ScriptedGateway, build_sqlite_platform, run, sqlite_seams, text_completion

_DEPT = "engineering"
_AGENT = "console-chat"

pytestmark = pytest.mark.usefixtures("protected_transport_env")


def test_agent_run_step_binds_and_resets_the_persisted_authenticated_context(
    monkeypatch,
) -> None:
    principal = Principal(id="alice", groups=("ops",), claims={"department": _DEPT})
    context = _context(principal, _DEPT)
    request = RunWorkflowRequest(
        dispatch_id="dispatch-1",
        run_id="run-1",
        owner_principal_id=principal.id,
        config_hash="a" * 64,
        target=RunTurnTarget(
            conversation_id="conversation-1",
            run_id="run-1",
            turn_id="turn-1",
            turn_sequence=1,
            attachment_id="attachment-1",
            attempt_id="attempt-1",
        ),
        authenticated_context=context,
    )

    class _Service:
        async def execute(self, value: RunWorkflowRequest) -> Any:
            assert value == request
            assert current_authenticated_context() == context
            return SimpleNamespace(model_dump_json=lambda: '{"run_id":"run-1"}')

    monkeypatch.setattr(worker_workflows, "_run_workflow_service", lambda: _Service())
    assert (
        asyncio.run(worker_workflows.execute_agent_run_step(request.model_dump_json()))
        == '{"run_id":"run-1"}'
    )
    assert current_authenticated_context() is None


def test_resume_run_approval_step_uses_exact_join_and_reauthorizes(monkeypatch) -> None:
    calls: list[str] = []

    class _Checkpoints:
        async def get_join(self, approval_id: str):
            calls.append(f"join:{approval_id}")
            return SimpleNamespace(
                run_id="run-1",
                attempt_id="attempt-1",
                action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
            )

        async def authorize(self, approval_id: str, **_kwargs):
            calls.append(f"authorize:{approval_id}")
            return {"opaque": True}

        async def load_checkpoint(self, approval_id: str, **_kwargs):
            calls.append(f"load:{approval_id}")
            return SimpleNamespace(run_id="run-1", status="approved")

        async def resume(self, approval_id: str, **_kwargs):
            calls.append(f"resume:{approval_id}")
            return SimpleNamespace(run_id="run-1")

    class _Lifecycle:
        def __init__(self, _engine: Any, **_kwargs: Any) -> None:
            self.fences = iter((RunState.WAITING_APPROVAL, RunState.RUNNING))

        def reconcile_approval_join(self, **_kwargs: Any) -> None:
            calls.append("reconcile")
            return None

        def approval_resume_fence(self, **_kwargs: Any) -> RunState:
            calls.append("fence")
            return next(self.fences)

    class _Service:
        async def record_approval_audit_receipt(self, **_kwargs: Any) -> None:
            return None  # the drain seam; its behavior is pinned end-to-end elsewhere

    platform = SimpleNamespace(
        gate=SimpleNamespace(_run_approval_checkpoints=_Checkpoints()),
        source_guard=SimpleNamespace(),
        runtime_engine=SimpleNamespace(),
        agent_run_store=SimpleNamespace(),
    )
    monkeypatch.setattr(worker_workflows, "worker_platform", lambda: platform)
    monkeypatch.setattr(worker_workflows, "RunWorkflowStateStore", _Lifecycle)
    monkeypatch.setattr(worker_workflows, "_run_workflow_service", lambda: _Service())
    result = asyncio.run(
        worker_workflows.resume_run_approval_step("approval-1", "a" * 64, "alice", "b" * 64)
    )
    assert result == "run-1"
    assert calls == [
        "join:approval-1",
        "reconcile",
        "load:approval-1",
        "fence",
        "authorize:approval-1",
        "resume:approval-1",
        "reconcile",
        "fence",
    ]


def test_approval_resume_binds_persisted_context_for_reauthorization_and_execution(
    monkeypatch,
) -> None:
    principal = Principal(id="alice", groups=("ops",), claims={"department": _DEPT})
    context = _context(principal, _DEPT)
    resume = RunApprovalResumeRequest(
        resume_id="resume-1",
        approval_id="approval-1",
        decision="approved",
        run_id="run-1",
        attempt_id="attempt-1",
        dispatch_id="dispatch-1",
        owner_principal_id="alice",
        config_hash="c" * 64,
        action_hash="a" * 64,
        policy_hash="b" * 64,
    )
    workflow_request = RunWorkflowRequest(
        dispatch_id="dispatch-1",
        run_id="run-1",
        owner_principal_id="alice",
        config_hash="c" * 64,
        target=RunTurnTarget(
            conversation_id="conversation-1",
            run_id="run-1",
            turn_id="turn-1",
            turn_sequence=1,
            attachment_id="attachment-1",
            attempt_id="attempt-1",
        ),
        authenticated_context=context,
    )

    class _Checkpoints:
        async def get_join(self, _approval_id: str):
            return SimpleNamespace(
                run_id="run-1",
                attempt_id="attempt-1",
                action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
            )

        async def require_resume_request(self, value: RunApprovalResumeRequest):
            assert value == resume
            return value

        async def load_checkpoint(self, *_args: Any, **_kwargs: Any):
            return SimpleNamespace(run_id="run-1", status="approved")

        async def authorize(self, *_args: Any, **_kwargs: Any):
            assert current_authenticated_context() == context
            return {"protected": True}

        async def resume(self, *_args: Any, **_kwargs: Any):
            return SimpleNamespace(run_id="run-1", status="resumed")

    class _Store:
        async def load_workflow_request(self, **kwargs: Any):
            assert kwargs == {
                "enqueue_id": "dispatch-1",
                "run_id": "run-1",
                "attempt_id": "attempt-1",
                "owner_principal_id": "alice",
            }
            return workflow_request

    class _Lifecycle:
        def __init__(self, _engine: Any, **_kwargs: Any) -> None:
            self.fences = iter((RunState.WAITING_APPROVAL, RunState.RUNNING))

        def reconcile_approval_join(self, **_kwargs: Any) -> None:
            return None

        def approval_resume_fence(self, **_kwargs: Any) -> RunState:
            return next(self.fences)

    class _Service:
        async def execute(self, request: RunWorkflowRequest):
            assert request == workflow_request
            assert current_authenticated_context() == context
            return SimpleNamespace(model_dump_json=lambda: '{"run_id":"run-1"}')

        async def record_approval_audit_receipt(self, **_kwargs: Any) -> None:
            return None  # the drain seam; its behavior is pinned end-to-end elsewhere

    platform = SimpleNamespace(
        gate=SimpleNamespace(_run_approval_checkpoints=_Checkpoints()),
        source_guard=SimpleNamespace(),
        runtime_engine=SimpleNamespace(),
        agent_run_store=_Store(),
    )
    monkeypatch.setattr(worker_workflows, "worker_platform", lambda: platform)
    monkeypatch.setattr(worker_workflows, "RunWorkflowStateStore", _Lifecycle)
    monkeypatch.setattr(worker_workflows, "_run_workflow_service", lambda: _Service())
    result = asyncio.run(
        worker_workflows.resume_run_approval_step(
            "approval-1",
            "a" * 64,
            "alice",
            "b" * 64,
            resume.model_dump_json(),
        )
    )
    assert result == '{"run_id":"run-1"}'
    assert current_authenticated_context() is None


def test_resume_run_approval_step_publishes_cancel_before_authorize(monkeypatch) -> None:
    calls: list[str] = []
    terminal = SimpleNamespace(run_id="run-1", status=RunState.CANCELLED)

    class _Checkpoints:
        async def get_join(self, _approval_id: str):
            calls.append("join")
            return SimpleNamespace(
                run_id="run-1",
                attempt_id="attempt-1",
                action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
            )

        async def authorize(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("cancelled approval must not authorize")

    class _Lifecycle:
        def __init__(self, _engine: Any, **_kwargs: Any) -> None:
            pass

        def reconcile_approval_join(self, **_kwargs: Any) -> Any:
            calls.append("reconcile")
            return terminal

    class _Service:
        async def publish_terminal(self, value: Any) -> None:
            assert value is terminal
            calls.append("publish")

        async def record_approval_audit_receipt(self, **_kwargs: Any) -> None:
            return None  # the drain seam; its behavior is pinned end-to-end elsewhere

    platform = SimpleNamespace(
        gate=SimpleNamespace(_run_approval_checkpoints=_Checkpoints()),
        source_guard=SimpleNamespace(),
        runtime_engine=SimpleNamespace(),
        agent_run_store=SimpleNamespace(),
    )
    monkeypatch.setattr(worker_workflows, "worker_platform", lambda: platform)
    monkeypatch.setattr(worker_workflows, "RunWorkflowStateStore", _Lifecycle)
    monkeypatch.setattr(worker_workflows, "_run_workflow_service", lambda: _Service())
    result = asyncio.run(
        worker_workflows.resume_run_approval_step("approval-1", "a" * 64, "alice", "b" * 64)
    )
    assert result == terminal.run_id
    assert calls == ["join", "reconcile", "publish"]


def test_resume_run_approval_step_replays_exact_already_resumed_join(monkeypatch) -> None:
    calls: list[str] = []

    class _Checkpoints:
        async def get_join(self, _approval_id: str):
            calls.append("join")
            return SimpleNamespace(
                run_id="run-1",
                attempt_id="attempt-1",
                action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
            )

        async def load_checkpoint(self, _approval_id: str, **_kwargs: Any):
            calls.append("load")
            return SimpleNamespace(run_id="run-1", status="resumed")

        async def authorize(self, _approval_id: str, **_kwargs: Any):
            calls.append("authorize")
            return {"opaque": True}

        async def resume(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("an exact resumed replay must not resume twice")

    class _Lifecycle:
        def __init__(self, _engine: Any, **_kwargs: Any) -> None:
            pass

        def reconcile_approval_join(self, **_kwargs: Any) -> None:
            calls.append("reconcile")
            return None

        def approval_resume_fence(self, **_kwargs: Any) -> RunState:
            calls.append("fence")
            return RunState.RUNNING

    class _Service:
        async def record_approval_audit_receipt(self, **_kwargs: Any) -> None:
            return None  # the drain seam; its behavior is pinned end-to-end elsewhere

    platform = SimpleNamespace(
        gate=SimpleNamespace(_run_approval_checkpoints=_Checkpoints()),
        source_guard=SimpleNamespace(),
        runtime_engine=SimpleNamespace(),
        agent_run_store=SimpleNamespace(),
    )
    monkeypatch.setattr(worker_workflows, "worker_platform", lambda: platform)
    monkeypatch.setattr(worker_workflows, "RunWorkflowStateStore", _Lifecycle)
    monkeypatch.setattr(worker_workflows, "_run_workflow_service", lambda: _Service())
    result = asyncio.run(
        worker_workflows.resume_run_approval_step("approval-1", "a" * 64, "alice", "b" * 64)
    )
    assert result == "run-1"
    assert calls == [
        "join",
        "reconcile",
        "load",
        "fence",
        "authorize",
        "reconcile",
        "fence",
    ]


def _assert_no_source_evidence(platform: Platform) -> None:
    with platform.runtime_engine.connect() as conn:
        for table in (
            ne_agent_evidence_sets,
            ne_agent_evidence_records,
            ne_agent_evidence_locators,
            ne_agent_pending_source_tool_results,
        ):
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0


def _context(principal: Principal, department: str) -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id=principal.id,
        authorization_groups=tuple(sorted(set(principal.groups))),
        department=department,
        issuer="https://issuer.test",
        expires_at=4_102_444_800,
    )


async def _protect_input(
    platform: Platform,
    *,
    principal: Principal,
    session_id: str,
    turn_id: str,
    attempt_id: str,
    prompt: str = "hello",
) -> ProtectedTurnRef:
    assert platform.agent_run_store is not None
    ref = await platform.agent_run_store.claim_protected_payload(
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=None,
        owner_principal_id=principal.id,
        target_kind=CONVERSATION_TURN_TARGET_KIND,
        target_binding=conversation_turn_content_binding(
            conversation_id=session_id,
            attachment_id=turn_id,
            attempt_id=attempt_id,
            slot="input",
        ),
        content_status=ProtectedContentStatus.SOURCE_FREE,
        plaintext=prompt.encode(),
    )
    return ProtectedTurnRef(
        protected_ref=ref.row_id,
        protected_hash=ref.plaintext_hash,
        content_status="source_free",
    )


async def _materialize_terminal(
    platform: Platform,
    *,
    request: TurnRequest,
    protected: ProtectedTurnRef,
) -> ProtectedTerminalPayload:
    assert platform.agent_run_store is not None
    payload = await platform.agent_run_store.materialize_legacy_conversation_turn(
        owner_principal_id=request.principal_id,
        conversation_id=request.session_id,
        attachment_id=request.turn_id,
        attempt_id=request.claimed_attempt_id,
        slot="terminal",
        protected_ref=protected.protected_ref,
        plaintext_hash=protected.protected_hash,
    )
    return ProtectedTerminalPayload.model_validate_json(payload)


def _install(tmp_path: Path, gateway: object) -> tuple[Platform, RecordingFrameWriter]:
    platform = build_sqlite_platform(tmp_path, gateway=gateway)
    set_worker_platform(platform)
    writer = RecordingFrameWriter("shared")
    set_frame_writer_factory(lambda _turn_id: writer)
    return platform, writer


async def _session_id(platform: object) -> str:
    return await _session_id_for_agent(platform, _AGENT)


async def _session_id_for_agent(platform: object, agent_name: str) -> str:
    session = await create_department_session(
        agent_name,
        _DEPT,
        owner_principal_id="user:ada",
        store=platform.session_store,  # type: ignore[attr-defined]
        gateway=platform.gateway,  # type: ignore[attr-defined]
        executor=platform.executor,  # type: ignore[attr-defined]
        settings=platform.runtime_settings,  # type: ignore[attr-defined]
    )
    return session.session_id


async def _request(
    platform: Platform,
    session_id: str,
    *,
    agent_name: str = _AGENT,
    department: str = _DEPT,
    turn_id: str = "turn-t5",
    trace_id: str | None = None,
    requested_model: str | None = None,
    requested_model_lane: Lane | None = None,
) -> str:
    principal = Principal(id="user:ada", groups=("eng",), claims={"department": department})
    effective_trace = trace_id or hashlib.sha256(f"test-turn:{turn_id}".encode()).hexdigest()[:32]
    idempotency = conversation_request_fingerprint(
        session_id,
        principal.id,
        _DEPT,
        agent_name,
        "hello",
        requested_model,
        None,
        effective_trace,
        principal.groups,
        dict(principal.claims),
    )
    execution = conversation_request_fingerprint(
        session_id,
        principal.id,
        _DEPT,
        agent_name,
        "hello",
        requested_model,
        None if requested_model_lane is None else requested_model_lane.value,
        effective_trace,
        principal.groups,
        dict(principal.claims),
    )
    claim = await platform.session_store.claim_conversation_attachment(
        session_id,
        principal.id,
        _DEPT,
        agent_name,
        turn_id,
        idempotency,
        effective_trace,
    )
    assert claim.disposition == "owned" and claim.attempt_id is not None
    await platform.session_store.bind_conversation_execution(
        session_id, principal.id, turn_id, claim.attempt_id, execution
    )
    protected_input = await _protect_input(
        platform,
        principal=principal,
        session_id=session_id,
        turn_id=turn_id,
        attempt_id=claim.attempt_id,
    )
    return TurnRequest.from_principal(
        turn_id=turn_id,
        session_id=session_id,
        agent_name=agent_name,
        department=_DEPT,
        protected_input=protected_input,
        authenticated_context=_context(principal, department),
        principal=principal,
        trace_id=effective_trace,
        requested_model=requested_model,
        requested_model_lane=requested_model_lane,
        request_fingerprint=execution,
        idempotency_fingerprint=idempotency,
        claimed_attempt_id=claim.attempt_id,
    ).model_dump_json()


@pytest.mark.parametrize(
    "field",
    ["trace_id", "request_fingerprint", "idempotency_fingerprint", "claimed_attempt_id"],
)
def test_worker_turn_request_rejects_missing_attempt_binding_before_any_effect(
    tmp_path: Path, field: str
) -> None:
    gateway = ScriptedGateway([text_completion("MUST-NOT-EXECUTE")])
    platform, writer = _install(tmp_path, gateway)

    async def prepare() -> tuple[str, dict[str, Any]]:
        session_id = await _session_id(platform)
        request = await _request(platform, session_id, turn_id=f"turn-missing-{field}")
        return session_id, json.loads(request)

    session_id, payload = run(prepare())
    payload.pop(field)
    with pytest.raises(ValidationError):
        run(run_turn_workflow(json.dumps(payload)))
    assert gateway.requests == []
    assert writer.frames == []
    assert not list_records(platform.audit_engine, action="session.run", limit=10)
    assert (
        run(platform.session_store.load_conversation_history(session_id, "user:ada", _DEPT, _AGENT))
        == []
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trace_id", "not-a-server-trace"),
        ("request_fingerprint", "a" * 63),
        ("idempotency_fingerprint", "z" * 64),
        ("claimed_attempt_id", ""),
    ],
)
def test_worker_turn_request_rejects_malformed_attempt_binding(
    tmp_path: Path, field: str, value: str
) -> None:
    gateway = ScriptedGateway([text_completion("MUST-NOT-EXECUTE")])
    platform, writer = _install(tmp_path, gateway)

    async def prepare() -> tuple[str, dict[str, Any]]:
        session_id = await _session_id(platform)
        request = await _request(platform, session_id, turn_id=f"turn-malformed-{field}")
        return session_id, json.loads(request)

    session_id, payload = run(prepare())
    payload[field] = value
    with pytest.raises(ValidationError):
        run(run_turn_workflow(json.dumps(payload)))
    assert gateway.requests == []
    assert writer.frames == []
    assert not list_records(platform.audit_engine, action="session.run", limit=10)
    assert (
        run(platform.session_store.load_conversation_history(session_id, "user:ada", _DEPT, _AGENT))
        == []
    )


class _ScopeRecordingGateway(ScriptedGateway):
    """A :class:`ScriptedGateway` that records the ambient action_scope at each
    ``complete`` call (NE-ACTION-DETERMINISTIC-REQID) — proves the worker step
    establishes the scope around the whole session run, not just part of it."""

    def __init__(self, completions: object) -> None:
        super().__init__(completions)  # type: ignore[arg-type]
        self.recorded_scopes: list[str | None] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.recorded_scopes.append(current_action_scope())
        return await super().complete(request)


class _FailingTerminalWriter(RecordingFrameWriter):
    async def emit(self, frame) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError(f"blocked {frame.type} frame")


class _FailOnDoneWriter(RecordingFrameWriter):
    async def emit(self, frame) -> None:  # type: ignore[no-untyped-def]
        if frame.type == "done":
            raise RuntimeError("blocked done frame")
        await super().emit(frame)


class _CitationChunkStore(StubChunkStore):
    async def query(self, **_kwargs: Any) -> list[ScoredChunk]:
        return [
            ScoredChunk(
                chunk=Chunk(
                    chunk_id="served-c1",
                    document_id="served-d1",
                    text="served text",
                    metadata={
                        "connector": "wiki",
                        "source_system": "wiki",
                        "source_ref": "ref-served-c1",
                        "source_url": "https://wiki.example.com/records/served-d1",
                        # This store is the test's intentionally source-free network
                        # edge. Source-bearing rows require an exact authority stamp.
                        "source_status": "no_source",
                    },
                    acl=("eng",),
                ),
                score=1.0,
            )
        ]


def _knowledge_tool_call() -> Completion:
    return Completion(
        text=json.dumps(
            {"tool_call": {"name": "read.knowledge.search", "arguments": {"query": "served"}}}
        ),
        model_id="stub",
        usage=Usage(prompt_tokens=3, completion_tokens=5),
    )


def test_turn_step_establishes_action_scope(tmp_path: Path) -> None:
    gw = _ScopeRecordingGateway([text_completion("the answer")])
    platform, _writer = _install(tmp_path, gw)

    async def _drive() -> str:
        sid = await _session_id(platform)
        return await run_turn_workflow(await _request(platform, sid))

    assert current_action_scope() is None
    run(_drive())

    assert gw.recorded_scopes  # the gateway was actually called
    assert all(scope == "turn-t5" for scope in gw.recorded_scopes)
    assert current_action_scope() is None  # reset across the step boundary


def test_passthrough_yields_token_then_done(tmp_path: Path) -> None:
    gw = ScriptedGateway([text_completion("the answer")])
    platform, writer = _install(tmp_path, gw)

    async def _drive() -> tuple[TurnRequest, ProtectedTurnRef]:
        sid = await _session_id(platform)
        request = TurnRequest.model_validate_json(await _request(platform, sid))
        output = ProtectedTurnRef.model_validate_json(
            await run_turn_workflow(request.model_dump_json())
        )
        return request, output

    request, output = run(_drive())
    terminal = run(_materialize_terminal(platform, request=request, protected=output))
    with platform.session_store.engine.connect() as conn:
        persisted = conn.execute(
            sa.select(ne_agent_conversation_turns).where(
                ne_agent_conversation_turns.c.conversation_id == request.session_id,
                ne_agent_conversation_turns.c.attachment_id == request.turn_id,
            )
        ).one()
    assert terminal.output == "the answer"
    assert persisted.private_context == ""
    assert persisted.served_output == ""
    assert persisted.input_status == "source_free"
    assert persisted.protected_input_ref == request.protected_input.protected_ref
    assert persisted.protected_input_hash == request.protected_input.protected_hash
    assert persisted.terminal_status == "source_free"
    assert persisted.protected_terminal_ref == output.protected_ref
    assert persisted.protected_terminal_hash == output.protected_hash
    assert [f.type for f in writer.frames] == ["done"]
    assert writer.frames[0].output is None
    assert writer.frames[0].protected_terminal == output
    assert writer.frames[0].session_id
    assert writer.closed
    assert [f.seq for f in writer.frames] == [0]


def test_pre_cutover_worker_executes_legacy_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NE_AGENT_TRANSPORT_GENERATION", raising=False)
    platform, writer = _install(tmp_path, ScriptedGateway([text_completion("legacy answer")]))

    async def _drive() -> str:
        session_id = await _session_id(platform)
        principal = Principal(id="user:ada", groups=("eng",), claims={"department": _DEPT})
        trace_id = "a" * 32
        fingerprint = conversation_request_fingerprint(
            session_id,
            principal.id,
            _DEPT,
            _AGENT,
            "hello",
            None,
            None,
            trace_id,
            principal.groups,
            dict(principal.claims),
        )
        claim = await platform.session_store.claim_conversation_attachment(
            session_id,
            principal.id,
            _DEPT,
            _AGENT,
            "turn-legacy",
            fingerprint,
            trace_id,
        )
        assert claim.attempt_id is not None
        await platform.session_store.bind_conversation_execution(
            session_id,
            principal.id,
            "turn-legacy",
            claim.attempt_id,
            fingerprint,
        )
        request = LegacyTurnRequest.from_principal(
            turn_id="turn-legacy",
            session_id=session_id,
            agent_name=_AGENT,
            department=_DEPT,
            prompt="hello",
            principal=principal,
            trace_id=trace_id,
            request_fingerprint=fingerprint,
            idempotency_fingerprint=fingerprint,
            claimed_attempt_id=claim.attempt_id,
        )
        return await run_turn_workflow(request.model_dump_json())

    assert run(_drive()) == "legacy answer"
    assert [frame.type for frame in writer.frames] == ["token", "done"]
    assert writer.frames[-1].output == "legacy answer"


@pytest.mark.parametrize(
    "pole", ["platform_generation", "groups", "content_status", "expired_context"]
)
def test_worker_refuses_foreign_protected_transport_before_model(tmp_path: Path, pole: str) -> None:
    gateway = ScriptedGateway([text_completion("MUST-NOT-EXECUTE")])
    platform, writer = _install(tmp_path, gateway)

    async def _drive() -> None:
        sid = await _session_id(platform)
        request = TurnRequest.model_validate_json(await _request(platform, sid))
        if pole == "platform_generation":
            set_worker_platform(replace(platform, agent_transport_generation="legacy"))
        elif pole == "groups":
            request = request.model_copy(
                update={
                    "authenticated_context": AuthenticatedRequestContext(
                        principal_id=request.principal_id,
                        authorization_groups=("foreign",),
                        department=request.department,
                        issuer="https://issuer.test",
                        expires_at=4_102_444_800,
                    )
                }
            )
        elif pole == "content_status":
            request = request.model_copy(
                update={
                    "protected_input": request.protected_input.model_copy(
                        update={"content_status": "source_bound_container"}
                    )
                }
            )
        else:
            request = request.model_copy(
                update={
                    "authenticated_context": AuthenticatedRequestContext(
                        principal_id=request.principal_id,
                        authorization_groups=tuple(request.principal_groups),
                        department=request.department,
                        issuer=request.authenticated_context.issuer,
                        expires_at=1,
                    )
                }
            )
        with pytest.raises(ProtectedTurnUnavailable):
            await run_turn_workflow(request.model_dump_json())

    run(_drive())
    assert gateway.requests == []
    assert [frame.type for frame in writer.frames] == ["error"]


def test_terminal_frame_failure_replays_protected_pointer_without_second_execution(
    tmp_path: Path,
) -> None:
    gateway = ScriptedGateway([text_completion("must remain hidden")])
    platform = build_sqlite_platform(tmp_path, gateway=gateway)
    set_worker_platform(platform)
    set_frame_writer_factory(lambda turn_id: _FailingTerminalWriter(turn_id))

    async def _drive() -> None:
        session_id = await _session_id(platform)
        request = await _request(platform, session_id, turn_id="turn-frame-failure")
        with pytest.raises(RuntimeError, match="blocked"):
            await run_turn_workflow(request)
        with platform.session_store.engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_conversation_turns).where(
                    ne_agent_conversation_turns.c.conversation_id == session_id
                )
            ).one()
        assert row.private_context == "" and row.served_output == ""
        assert row.protected_terminal_ref is not None
        replay_writer = RecordingFrameWriter("turn-frame-failure")
        set_frame_writer_factory(lambda _turn_id: replay_writer)
        pointer = ProtectedTurnRef.model_validate_json(await run_turn_workflow(request))
        assert replay_writer.frames[-1].type == "done"
        assert replay_writer.frames[-1].output is None
        assert replay_writer.frames[-1].protected_terminal == pointer
        assert len(gateway.requests) == 1

    run(_drive())


def test_failure_after_publication_replays_exact_terminal_without_second_execution(
    tmp_path: Path,
) -> None:
    gateway = ScriptedGateway([text_completion("RECOVERABLE-SERVED")])
    platform = build_sqlite_platform(tmp_path, gateway=gateway)
    set_worker_platform(platform)
    failed = _FailOnDoneWriter("shared")
    set_frame_writer_factory(lambda _turn_id: failed)

    async def _drive() -> None:
        session_id = await _session_id(platform)
        request = await _request(platform, session_id, turn_id="turn-done-failure")
        with pytest.raises(RuntimeError, match="blocked done"):
            await run_turn_workflow(request)
        with platform.session_store.engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_conversation_turns).where(
                    ne_agent_conversation_turns.c.conversation_id == session_id
                )
            ).one()
        assert row.private_context == "" and row.served_output == ""
        assert row.protected_terminal_ref is not None
        replay = RecordingFrameWriter("replay")
        set_frame_writer_factory(lambda _turn_id: replay)
        pointer = ProtectedTurnRef.model_validate_json(await run_turn_workflow(request))
        assert [frame.type for frame in replay.frames] == ["done"]
        assert replay.frames[-1].output is None
        assert replay.frames[-1].protected_terminal == pointer
        assert len(gateway.requests) == 1

    run(_drive())


def test_protected_worker_preserves_verified_routing_and_citation_provenance(
    tmp_path: Path,
) -> None:
    gateway = ScriptedGateway([_knowledge_tool_call(), text_completion("the answer")])
    settings, seams = sqlite_seams(tmp_path, gateway=gateway)
    # Keep only the retrieval store edge fake: an empty rerank URL selects the
    # production bare HybridRetriever rather than making a network TEI call.
    settings = settings.model_copy(update={"tei_rerank_url": ""})
    platform = build_platform(settings, replace(seams, chunk_store=_CitationChunkStore()))
    set_worker_platform(platform)
    writer = RecordingFrameWriter("shared")
    set_frame_writer_factory(lambda _turn_id: writer)

    async def _drive() -> tuple[TurnRequest, ProtectedTurnRef]:
        sid = await _session_id(platform)
        request = TurnRequest.model_validate_json(await _request(platform, sid))
        terminal = ProtectedTurnRef.model_validate_json(
            await run_turn_workflow(request.model_dump_json())
        )
        return request, terminal

    request, protected = run(_drive())
    terminal = run(_materialize_terminal(platform, request=request, protected=protected))
    assert terminal.output == "the answer"
    assert terminal.citations == ()
    assert terminal.verified is None
    assert terminal.grounded is None
    assert len(gateway.requests) == 2
    assert [frame.type for frame in writer.frames][-1] == "done"
    assert writer.closed
    _assert_no_source_evidence(platform)


def test_worker_reenters_trace_and_passes_model_lane_without_bleed(tmp_path: Path) -> None:
    gw = ScriptedGateway([text_completion("selected"), text_completion("legacy")])
    platform, writer = _install(tmp_path, gw)

    async def _drive() -> None:
        sid = await _session_id(platform)
        await run_turn_workflow(
            await _request(
                platform,
                sid,
                turn_id="turn-selected",
                trace_id="11223344556677889900aabbccddeeff",
                requested_model="frontier-anthropic",
                requested_model_lane=Lane.FRONTIER_API,
            )
        )
        await run_turn_workflow(await _request(platform, sid, turn_id="turn-legacy"))

    assert current_run_trace_id() is None
    assert current_run_requested_model() is None
    assert current_run_requested_model_lane() is None
    run(_drive())

    assert len(gw.requests) == 2
    selected, legacy = gw.requests
    assert selected.trace_id == "11223344556677889900aabbccddeeff"
    assert selected.requested_model == "frontier-anthropic"
    assert selected.lane is Lane.FRONTIER_API
    assert legacy.trace_id is not None and legacy.trace_id != "11223344556677889900aabbccddeeff"
    assert legacy.requested_model is None
    assert legacy.lane is None
    assert current_run_trace_id() is None
    assert current_run_requested_model() is None
    assert current_run_requested_model_lane() is None
    done = [frame for frame in writer.frames if frame.type == "done"]
    assert done[0].trace_id == "11223344556677889900aabbccddeeff"
    assert done[1].trace_id == legacy.trace_id
    assert all(frame.output is None and frame.protected_terminal is not None for frame in done)


def test_file_db_worker_restart_rehydrates_exact_conversation_and_rejects_wrong_scope(
    tmp_path: Path,
) -> None:
    first_gateway = ScriptedGateway([text_completion("FIRST-RESTART-SENTINEL")])
    settings, seams = sqlite_seams(tmp_path, gateway=first_gateway)
    first = build_platform(settings, seams)
    set_worker_platform(first)
    set_frame_writer_factory(lambda turn_id: RecordingFrameWriter(turn_id))

    async def first_turn() -> str:
        sid = await _session_id(first)
        await run_turn_workflow(await _request(first, sid, turn_id="turn-before-restart"))
        return sid

    session_id = run(first_turn())
    for engine in first.engines:
        engine.dispose()

    second_gateway = ScriptedGateway([text_completion("SECOND-RESTART-SENTINEL")])
    restarted = build_platform(settings, replace(seams, gateway=second_gateway))
    set_worker_platform(restarted)
    set_frame_writer_factory(lambda turn_id: RecordingFrameWriter(turn_id))
    run(run_turn_workflow(run(_request(restarted, session_id, turn_id="turn-after-restart"))))
    assert len(second_gateway.requests) == 1
    assert "FIRST-RESTART-SENTINEL" in str(second_gateway.requests[0].messages)

    async def legacy_id() -> str:
        return await restarted.session_store.open_session(_AGENT, _DEPT)

    legacy = run(legacy_id())
    valid = TurnRequest.model_validate_json(
        run(_request(restarted, session_id, turn_id="turn-invalid"))
    )
    invalid = (
        valid.model_copy(update={"principal_id": "user:bob", "turn_id": "turn-wrong-owner"}),
        valid.model_copy(update={"agent_name": "not-configured", "turn_id": "turn-wrong-agent"}),
        valid.model_copy(update={"session_id": legacy, "turn_id": "turn-legacy"}),
    )
    for request in invalid:
        with pytest.raises((ProtectedTurnUnavailable, AttachmentRequestMismatchError, ValueError)):
            run(run_turn_workflow(request.model_dump_json()))
    assert len(second_gateway.requests) == 1
    for engine in restarted.engines:
        engine.dispose()


def test_worker_restart_recovers_prepared_envelope_without_model_rerun(tmp_path: Path) -> None:
    first_gateway = ScriptedGateway([text_completion("must not execute before restart")])
    settings, seams = sqlite_seams(tmp_path, gateway=first_gateway)
    first = build_platform(settings, seams)
    attachment_id = "turn-prepared-restart"
    attempt_id = "attempt-prepared-restart"
    prompt = "hello"

    async def stage_prepared() -> tuple[str, str, str]:
        session_id = await _session_id(first)
        idempotency = conversation_request_fingerprint(
            session_id, "user:ada", _DEPT, _AGENT, prompt, None
        )
        execution = conversation_request_fingerprint(
            session_id,
            "user:ada",
            _DEPT,
            _AGENT,
            prompt,
            None,
            None,
            "11223344556677889900aabbccddeeff",
            ("eng",),
            {"department": _DEPT},
        )
        await first.session_store.append_conversation_turn(
            session_id,
            "user:ada",
            _DEPT,
            _AGENT,
            [
                ModelRequest(parts=[UserPromptPart(content=prompt)]),
                ModelResponse(
                    parts=[TextPart(content="PREPARED-RESTART-ANSWER")],
                    model_name="prepared-model",
                ),
            ],
            served_output="PREPARED-RESTART-ANSWER",
            trace_id="11223344556677889900aabbccddeeff",
            attachment_id=attachment_id,
            attempt_id=attempt_id,
            request_fingerprint=execution,
            idempotency_fingerprint=idempotency,
            citation_payloads=[],
        )
        return session_id, idempotency, execution

    session_id, fingerprint, execution = run(stage_prepared())
    assert first_gateway.requests == []
    for engine in first.engines:
        engine.dispose()

    restarted_gateway = ScriptedGateway([text_completion("MUST-NOT-RERUN")])
    restarted = build_platform(settings, replace(seams, gateway=restarted_gateway))
    set_worker_platform(restarted)
    writer = RecordingFrameWriter(attachment_id)
    set_frame_writer_factory(lambda _turn_id: writer)
    restarted_principal = Principal(id="user:ada", groups=("eng",), claims={"department": _DEPT})
    protected_input = run(
        _protect_input(
            restarted,
            principal=restarted_principal,
            session_id=session_id,
            turn_id=attachment_id,
            attempt_id=attempt_id,
            prompt=prompt,
        )
    )
    request = TurnRequest.from_principal(
        turn_id=attachment_id,
        session_id=session_id,
        agent_name=_AGENT,
        department=_DEPT,
        protected_input=protected_input,
        authenticated_context=_context(restarted_principal, _DEPT),
        principal=restarted_principal,
        trace_id="11223344556677889900aabbccddeeff",
        request_fingerprint=execution,
        idempotency_fingerprint=fingerprint,
        claimed_attempt_id=attempt_id,
    )
    pointer = ProtectedTurnRef.model_validate_json(
        run(run_turn_workflow(request.model_dump_json()))
    )
    assert restarted_gateway.requests == []
    assert [frame.type for frame in writer.frames] == ["done"]
    assert writer.frames[-1].output is None
    assert writer.frames[-1].protected_terminal == pointer
    assert (
        run(_materialize_terminal(restarted, request=request, protected=pointer)).output
        == "PREPARED-RESTART-ANSWER"
    )
    replay = run(
        restarted.session_store.protected_conversation_turn(
            session_id,
            "user:ada",
            _DEPT,
            _AGENT,
            attachment_id,
            attempt_id,
        )
    )
    assert replay is not None
    assert replay.terminal.protected_ref == pointer.protected_ref
    for engine in restarted.engines:
        engine.dispose()


@pytest.mark.parametrize(
    "field", ["protected_input", "requested_model", "requested_model_lane", "trace_id"]
)
def test_worker_rejects_tampered_claim_payload_before_execution(tmp_path: Path, field: str) -> None:
    gateway = ScriptedGateway([text_completion("MUST-NOT-EXECUTE")])
    platform, _writer = _install(tmp_path, gateway)

    async def prepare() -> TurnRequest:
        session_id = await _session_id(platform)
        principal = Principal(id="user:ada", groups=("eng",), claims={"department": _DEPT})
        turn_id = f"turn-tamper-{field}"
        trace_id = "0123456789abcdef0123456789abcdef"
        idempotency = conversation_request_fingerprint(
            session_id, principal.id, _DEPT, _AGENT, "hello", "model-a"
        )
        execution = conversation_request_fingerprint(
            session_id,
            principal.id,
            _DEPT,
            _AGENT,
            "hello",
            "model-a",
            Lane.PRIVATE_VLLM.value,
            trace_id,
            principal.groups,
            dict(principal.claims),
        )
        claim = await platform.session_store.claim_conversation_attachment(
            session_id,
            principal.id,
            _DEPT,
            _AGENT,
            turn_id,
            idempotency,
            trace_id,
        )
        assert claim.attempt_id is not None
        await platform.session_store.bind_conversation_execution(
            session_id, principal.id, turn_id, claim.attempt_id, execution
        )
        protected_input = await _protect_input(
            platform,
            principal=principal,
            session_id=session_id,
            turn_id=turn_id,
            attempt_id=claim.attempt_id,
        )
        return TurnRequest.from_principal(
            turn_id=turn_id,
            session_id=session_id,
            agent_name=_AGENT,
            department=_DEPT,
            protected_input=protected_input,
            authenticated_context=_context(principal, _DEPT),
            principal=principal,
            trace_id=trace_id,
            requested_model="model-a",
            requested_model_lane=Lane.PRIVATE_VLLM,
            request_fingerprint=execution,
            idempotency_fingerprint=idempotency,
            claimed_attempt_id=claim.attempt_id,
        )

    request = run(prepare())
    replacements: dict[str, object] = {
        "protected_input": request.protected_input.model_copy(update={"protected_hash": "f" * 64}),
        "requested_model": "model-b",
        "requested_model_lane": Lane.FRONTIER_API,
        "trace_id": "fedcba9876543210fedcba9876543210",
    }
    tampered = request.model_copy(update={field: replacements[field]})
    with pytest.raises((ProtectedTurnUnavailable, AttachmentRequestMismatchError, ValueError)):
        run(run_turn_workflow(tampered.model_dump_json()))
    assert gateway.requests == []
    with platform.session_store.engine.connect() as conn:
        row = conn.execute(
            sa.select(
                ne_agent_conversation_turns.c.attempt_id,
                ne_agent_conversation_turns.c.publication_state,
            ).where(ne_agent_conversation_turns.c.attachment_id == request.turn_id)
        ).one()
    assert row.attempt_id == request.claimed_attempt_id
    assert row.publication_state == ("aborted" if field == "protected_input" else "claimed")


def test_stale_queued_worker_cannot_revive_an_expired_then_aborted_attempt(
    tmp_path: Path,
) -> None:
    gateway = ScriptedGateway([text_completion("MUST-NOT-EXECUTE")])
    platform, writer = _install(tmp_path, gateway)

    async def prepare() -> TurnRequest:
        session_id = await _session_id(platform)
        principal = Principal(id="user:ada", groups=("eng",), claims={"department": _DEPT})
        turn_id = "turn-stale-queued-attempt"
        trace_id = "0123456789abcdef0123456789abcdef"
        idempotency = conversation_request_fingerprint(
            session_id, principal.id, _DEPT, _AGENT, "hello", None
        )
        execution = conversation_request_fingerprint(
            session_id,
            principal.id,
            _DEPT,
            _AGENT,
            "hello",
            None,
            None,
            trace_id,
            principal.groups,
            dict(principal.claims),
        )
        old = await platform.session_store.claim_conversation_attachment(
            session_id,
            principal.id,
            _DEPT,
            _AGENT,
            turn_id,
            idempotency,
            trace_id,
            lease_seconds=0.01,
        )
        assert old.attempt_id is not None
        await platform.session_store.bind_conversation_execution(
            session_id, principal.id, turn_id, old.attempt_id, execution
        )
        await asyncio.sleep(0.02)
        replacement = await platform.session_store.claim_conversation_attachment(
            session_id,
            principal.id,
            _DEPT,
            _AGENT,
            turn_id,
            idempotency,
            trace_id,
            lease_seconds=60,
        )
        assert replacement.disposition == "owned"
        assert replacement.attempt_id is not None
        assert replacement.attempt_id != old.attempt_id
        await platform.session_store.abort_conversation_turn(
            session_id, principal.id, turn_id, replacement.attempt_id
        )
        protected_input = await _protect_input(
            platform,
            principal=principal,
            session_id=session_id,
            turn_id=turn_id,
            attempt_id=old.attempt_id,
        )
        return TurnRequest.from_principal(
            turn_id=turn_id,
            session_id=session_id,
            agent_name=_AGENT,
            department=_DEPT,
            protected_input=protected_input,
            authenticated_context=_context(principal, _DEPT),
            principal=principal,
            trace_id=trace_id,
            request_fingerprint=execution,
            idempotency_fingerprint=idempotency,
            claimed_attempt_id=old.attempt_id,
        )

    request = run(prepare())
    with pytest.raises(LookupError, match="stale"):
        run(run_turn_workflow(request.model_dump_json()))
    assert gateway.requests == []
    assert [frame.type for frame in writer.frames] == ["error"]
    with platform.session_store.engine.connect() as conn:
        row = conn.execute(
            sa.select(
                ne_agent_conversation_turns.c.attempt_id,
                ne_agent_conversation_turns.c.publication_state,
            ).where(ne_agent_conversation_turns.c.attachment_id == request.turn_id)
        ).one()
    assert row.attempt_id != request.claimed_attempt_id
    assert row.publication_state == "aborted"


def test_worker_restart_rejects_grounded_conversation_removed_from_chat_allowlist(
    tmp_path: Path,
) -> None:
    grounded = "grounded-analyst"
    first_gateway = ScriptedGateway()
    settings, seams = sqlite_seams(tmp_path, gateway=first_gateway)
    old_settings = settings.model_copy(update={"chat_agents": (_AGENT, grounded)})
    first = replace(build_platform(old_settings, seams), grounded_agent_name=grounded)

    async def create_old_conversation() -> tuple[str, str]:
        conversation_id = await _session_id_for_agent(first, grounded)
        request = await _request(
            first,
            conversation_id,
            agent_name=grounded,
            turn_id="turn-removed-grounded",
        )
        return conversation_id, request

    conversation_id, request = run(create_old_conversation())
    for engine in first.engines:
        engine.dispose()

    second_gateway = ScriptedGateway([text_completion("must not run")])
    new_settings = settings.model_copy(update={"chat_agents": (_AGENT,)})
    reset_config_hash_for_testing()
    restarted = replace(
        build_platform(new_settings, replace(seams, gateway=second_gateway)),
        grounded_agent_name=grounded,
    )
    set_worker_platform(restarted)
    writer = RecordingFrameWriter("removed-grounded")
    set_frame_writer_factory(lambda _turn_id: writer)
    with pytest.raises(ValueError, match="unknown configured chat agent"):
        run(run_turn_workflow(request))
    assert second_gateway.requests == []
    assert (
        run(
            restarted.session_store.load_conversation_history(
                conversation_id, "user:ada", _DEPT, grounded
            )
        )
        == []
    )
    assert [frame.type for frame in writer.frames] == ["error"]
    for engine in restarted.engines:
        engine.dispose()


def test_department_mismatch_emits_terminal_error_and_reraises(tmp_path: Path) -> None:
    gw = ScriptedGateway([text_completion("never reached")])
    platform, writer = _install(tmp_path, gw)

    async def _drive() -> None:
        sid = await _session_id(platform)
        request = TurnRequest.model_validate_json(await _request(platform, sid))
        await run_turn_workflow(
            request.model_copy(update={"department": "marketing"}).model_dump_json()
        )

    with pytest.raises(Exception):  # noqa: B017 - the mismatch propagates
        run(_drive())
    assert writer.frames == []
    assert gw.requests == []
