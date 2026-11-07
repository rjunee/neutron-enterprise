# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-14 Context Ledger: content-free identity, evidence, and R3 scope."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.pool import StaticPool

from ne.api.context_ledger import _agent_context_rows
from ne.api.deps import api_state, current_principal
from ne.api.routers import context_ledger
from ne.audit.store import ne_audit_log
from ne.contracts import Lane, Principal
from ne.events import new_audit_event
from ne.gateway.config import ModelDef, TaskRoute
from ne.runtime.run_store import (
    ensure_run_schema,
    ne_agent_run_attempt_links,
    ne_agent_run_history_attachments,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
    ne_agent_conversations,
)
from ne.schema import AuditOutcome

from .conftest import AppBuilder, Harness, get_state, run

_PRIVATE_A = ModelDef(
    name="private-a",
    litellm_model="hosted_vllm/qwen-a",
    provider="hosted_vllm",
    lane=Lane.PRIVATE_VLLM,
    api_base="http://vllm:8000/v1",
    context_window=32_768,
)
_PRIVATE_B = _PRIVATE_A.model_copy(
    update={"name": "private-b", "litellm_model": "hosted_vllm/qwen-b"}
)
_TASKS = {"agent": TaskRoute(model="private-a")}
_PROMPT_SENTINEL = "PROMPT-NEVER-ON-CONTEXT-LEDGER-1e55"
_ANSWER_SENTINEL = "ANSWER-NEVER-ON-CONTEXT-LEDGER-58d1"
_AGENT = "agt_context_ledger"
_OWNER = "user:ada"
_DEPARTMENT = "engineering"
_PROTECTED_REF_SENTINEL = "PROTECTED-REF-NEVER-ON-CONTEXT-LEDGER-86bd"


def _ledger_app(make_app: AppBuilder) -> Harness:
    return make_app(gateway_models=[_PRIVATE_A, _PRIVATE_B], gateway_tasks=_TASKS)


def _turn(prompt: str, answer: str) -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[TextPart(content=answer)], model_name="private-a"),
    ]


async def _append_owned(state: Any, session_id: str, messages: list[Any]) -> None:
    attachment_id = f"test-turn-{messages[0]!s}"
    await state.session_store.append_conversation_turn(
        session_id,
        "user:ada",
        "engineering",
        "console-chat",
        messages,
        served_output="test-served-output",
        trace_id="test-trace",
        attachment_id=attachment_id,
        attempt_id=f"attempt-{attachment_id}",
    )
    await state.session_store.publish_conversation_turn(
        session_id, "user:ada", attachment_id, f"attempt-{attachment_id}", []
    )


async def _open_session(harness: Harness, headers: dict[str, str]) -> str:
    async with harness.client() as client:
        response = await client.post(
            "/api/v1/chat/sessions",
            json={"agent_name": "console-chat"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    return str(response.json()["session_id"])


async def _append_gateway_event(
    harness: Harness,
    *,
    trace_id: str,
    model: str,
    input_tokens: str | None,
    outcome: AuditOutcome = AuditOutcome.success,
) -> None:
    state = get_state(harness.app)
    provenance = {"task": "agent", "model_name": model}
    if input_tokens is not None:
        provenance["input_tokens"] = input_tokens
    await state.sink.append(
        new_audit_event(
            action="ne.gateway.complete",
            resource=f"model:{model}",
            outcome=outcome,
            actor="ne-gateway",
            detail="provider call completed",
            trace_id=trace_id,
            provenance=provenance,
        )
    )


async def _append_terminal(
    harness: Harness,
    *,
    session_id: str,
    trace_id: str,
    outcome: AuditOutcome,
) -> None:
    state = get_state(harness.app)
    provenance: dict[str, str] = {}
    if outcome == AuditOutcome.success:
        snapshot = await state.session_store.context_snapshot(
            session_id,
            max_components=max(1, state.runtime_settings.session_history_max_turns),
        )
        assert snapshot is not None
        provenance = {
            "context_id": snapshot.context_id,
            "context_active_components": str(len(snapshot.components)),
            "context_total_components": str(snapshot.total_components),
        }
    await state.sink.append(
        new_audit_event(
            action="session.run",
            resource=f"session:{session_id}",
            outcome=outcome,
            actor="user:ada",
            detail=json.dumps(
                {
                    "session_id": session_id,
                    "succeeded": outcome == AuditOutcome.success,
                }
            ),
            trace_id=trace_id,
            provenance=provenance,
        )
    )


async def _append_route_event(
    harness: Harness,
    *,
    old_model: str,
    new_model: str,
    detail: str | None = None,
    config_hash: str | None = None,
) -> None:
    state = get_state(harness.app)
    event = new_audit_event(
        action="model.routing.changed",
        resource="model:routing:agent",
        outcome=AuditOutcome.success,
        actor="user:ada",
        detail=(
            detail
            if detail is not None
            else json.dumps(
                {
                    "task": "agent",
                    "model": {"old": old_model, "new": new_model},
                }
            )
        ),
    )
    if config_hash is not None:
        event = event.model_copy(update={"config_hash": config_hash})
    await state.sink.append(event)


def _assert_content_free(payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload)
    assert _PROMPT_SENTINEL not in serialized
    assert _ANSWER_SENTINEL not in serialized
    assert "payload" not in serialized
    assert "prompt" not in serialized
    assert "answer" not in serialized


class _VisibleAgentCatalog:
    def get_visible(self, agent_id: str, _principal: Principal) -> object | None:
        return object() if agent_id == _AGENT else None


def _agent_ledger_environment() -> tuple[
    sa.Engine, SqlAlchemySessionStore, TestClient, SimpleNamespace
]:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    state = SimpleNamespace(
        session_store=sessions,
        agent_catalog=_VisibleAgentCatalog(),
        engines=[engine],
        mutable_route_generation="route-a",
    )
    app = FastAPI()
    app.include_router(context_ledger.router)
    app.dependency_overrides[api_state] = lambda: state
    app.dependency_overrides[current_principal] = lambda: Principal(
        id=_OWNER,
        claims={"department": _DEPARTMENT},
    )
    return engine, sessions, TestClient(app), state


def _insert_agent_context_row(
    connection: sa.Connection,
    *,
    conversation_id: str,
    sequence: int,
    agent_id: str = _AGENT,
) -> None:
    run_id = f"run-{agent_id}-{sequence}"
    attempt_id = f"attempt-{agent_id}-{sequence}"
    turn_id = f"turn-{agent_id}-{sequence}"
    attachment_id = f"attachment-{agent_id}-{sequence}"
    # Deliberately reverse timestamps relative to canonical sequence.
    created_at = datetime(2026, 8, 23, 12, tzinfo=UTC) + timedelta(minutes=100 - sequence)
    connection.execute(
        ne_agent_runs.insert().values(
            run_id=run_id,
            conversation_id=conversation_id,
            owner_principal_id=_OWNER,
            department=_DEPARTMENT,
            conversation_agent_name=_AGENT,
            agent_id=agent_id,
            agent_version=1,
            strategy="agentic",
            config_hash="c" * 64,
            capability_snapshot_hash="d" * 64,
            capability_snapshot_receipt_id=f"receipt-{sequence}",
            requested_model="private-a",
            requested_model_lane="private-vllm",
            sequence=sequence,
            display_label=f"Run {sequence}",
            status="completed",
            output_verdict="grounded",
            version=2,
            created_at=created_at,
            updated_at=created_at,
            terminal_at=created_at,
        )
    )
    connection.execute(
        ne_agent_run_attempt_links.insert().values(
            attempt_id=attempt_id,
            run_id=run_id,
            conversation_id=conversation_id,
            predecessor_attempt_id=None,
            attempt_number=1,
            created_at=created_at,
        )
    )
    connection.execute(
        ne_agent_run_turn_targets.insert().values(
            run_id=run_id,
            attempt_id=attempt_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=sequence,
            attachment_id=attachment_id,
            created_at=created_at,
        )
    )
    connection.execute(
        ne_agent_run_history_attachments.insert().values(
            run_id=run_id,
            conversation_id=conversation_id,
            owner_principal_id=_OWNER,
            turn_id=turn_id,
            turn_sequence=sequence,
            attachment_id=attachment_id,
            attempt_id=attempt_id,
            protected_domain="protected_turn_content",
            protected_ref=f"{_PROTECTED_REF_SENTINEL}-{sequence}",
            protected_hash=f"{sequence:x}" * 64,
            content_status="source_free",
            created_at=created_at,
        )
    )


def _seed_agent_context(
    engine: sa.Engine,
    sessions: SqlAlchemySessionStore,
    *,
    sequences: tuple[int, ...] = (1, 2, 3),
) -> str:
    conversation = run(sessions.create_conversation(_AGENT, _DEPARTMENT, _OWNER))
    with engine.begin() as connection:
        connection.execute(
            ne_agent_conversations.update()
            .where(ne_agent_conversations.c.conversation_id == conversation.conversation_id)
            .values(last_sequence=max(sequences, default=0))
        )
        for sequence in reversed(sequences):
            _insert_agent_context_row(
                connection,
                conversation_id=conversation.conversation_id,
                sequence=sequence,
            )
    return conversation.conversation_id


def test_agent_ledger_is_owner_scoped_bounded_ordered_and_model_independent() -> None:
    engine, sessions, client, state = _agent_ledger_environment()
    conversation_id = _seed_agent_context(engine, sessions)
    with engine.begin() as connection:
        _insert_agent_context_row(
            connection,
            conversation_id=conversation_id,
            sequence=4,
            agent_id="agt_other",
        )
    path = f"/v1/agents/{_AGENT}/conversations/{conversation_id}/context-ledger"

    first = client.get(path, params={"limit": 2})
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "private, no-store"
    body = first.json()
    assert set(body) == {
        "conversation_id",
        "agent_id",
        "context_id",
        "resides",
        "components",
        "total_components",
        "active_components",
        "truncated",
    }
    assert body["conversation_id"] == conversation_id
    assert body["agent_id"] == _AGENT
    assert body["resides"] == "SQLite"
    assert body["total_components"] == 3
    assert body["active_components"] == 2
    assert body["truncated"] is True
    assert [row["turn_sequence"] for row in body["components"]] == [2, 3]
    assert all(
        set(row)
        == {
            "turn_sequence",
            "run_id",
            "turn_id",
            "input_target_id",
            "terminal_hash",
            "component_id",
        }
        for row in body["components"]
    )
    serialized = json.dumps(body)
    assert _PROTECTED_REF_SENTINEL not in serialized
    assert "private-a" not in serialized

    for owner, department, agent in (
        ("user:mallory", _DEPARTMENT, _AGENT),
        (_OWNER, "sales", _AGENT),
        (_OWNER, _DEPARTMENT, "agt_other"),
    ):
        with pytest.raises(LookupError):
            _agent_context_rows(
                engine,
                owner_principal_id=owner,
                department=department,
                agent_id=agent,
                conversation_id=conversation_id,
                limit=25,
            )

    full_window = client.get(path, params={"limit": 25})
    assert full_window.status_code == 200
    assert full_window.json()["context_id"] == body["context_id"]

    state.mutable_route_generation = "route-b"
    after_route_change = client.get(path, params={"limit": 2})
    assert after_route_change.status_code == 200
    assert after_route_change.json() == body

    with engine.begin() as connection:
        # Red-team fixture only: bypass terminal immutability so this test can
        # prove that model-selection fields are not context-hash inputs.
        connection.exec_driver_sql("DROP TRIGGER trg_agent_run_terminal_immutable")
        connection.execute(
            ne_agent_runs.update().values(
                requested_model="private-b",
                requested_model_lane="private-vllm",
            )
        )
    after_model_selection = client.get(path, params={"limit": 2})
    assert after_model_selection.status_code == 200
    assert after_model_selection.json() == body

    with engine.begin() as connection:
        connection.execute(
            ne_agent_conversations.update()
            .where(ne_agent_conversations.c.conversation_id == conversation_id)
            .values(last_sequence=5)
        )
        _insert_agent_context_row(
            connection,
            conversation_id=conversation_id,
            sequence=5,
        )
    after_new_turn = client.get(path, params={"limit": 2})
    assert after_new_turn.status_code == 200
    assert after_new_turn.json()["context_id"] != body["context_id"]
    assert [row["turn_sequence"] for row in after_new_turn.json()["components"]] == [3, 5]
    engine.dispose()


def test_agent_ledger_foreign_department_hidden_and_random_are_indistinguishable() -> None:
    engine, sessions, client, _state = _agent_ledger_environment()
    conversation_id = _seed_agent_context(engine, sessions, sequences=(1,))
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[current_principal] = lambda: Principal(
        id="user:mallory",
        claims={"department": "sales"},
    )
    foreign = client.get(f"/v1/agents/{_AGENT}/conversations/{conversation_id}/context-ledger")
    random = client.get(f"/v1/agents/{_AGENT}/conversations/conversation-random/context-ledger")
    hidden = client.get(f"/v1/agents/agt_hidden/conversations/{conversation_id}/context-ledger")
    assert (foreign.status_code, foreign.content) == (random.status_code, random.content)
    assert (hidden.status_code, hidden.content) == (random.status_code, random.content)
    assert foreign.status_code == 404
    assert foreign.headers["cache-control"] == "private, no-store"
    engine.dispose()


def test_ledger_is_stable_content_free_and_department_scoped(
    make_app: AppBuilder,
) -> None:
    harness = _ledger_app(make_app)
    state = get_state(harness.app)

    async def scenario() -> None:
        headers = harness.bearer("user:ada", department="engineering")
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn(_PROMPT_SENTINEL, _ANSWER_SENTINEL))

        async with harness.client() as client:
            first = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
            again = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
            foreign = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger",
                headers=harness.bearer("user:mallory", department="sales"),
            )
            unknown = await client.get(
                "/api/v1/chat/sessions/not-a-session/context-ledger", headers=headers
            )

        assert first.status_code == 200, first.text
        assert again.status_code == 200, again.text
        assert first.headers["cache-control"] == "private, no-store"
        assert again.headers["cache-control"] == "private, no-store"
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json() == unknown.json() == {"detail": "unknown session"}

        body = first.json()
        assert body == again.json()
        assert set(body) == {
            "session_id",
            "context_id",
            "resides",
            "components",
            "total_components",
            "active_components",
            "truncated",
            "budget",
            "current_model",
            "served_model",
            "flip",
        }
        assert body["session_id"] == session_id
        assert body["resides"] == "SQLite"
        assert body["total_components"] == body["active_components"] == 1
        assert body["truncated"] is False
        assert len(body["context_id"]) == 64
        assert len(body["components"]) == 1
        assert set(body["components"][0]) == {"seq", "component_id", "created_at"}
        assert body["components"][0]["seq"] == 1
        assert len(body["components"][0]["component_id"]) == 64
        assert body["budget"]["input_budget"] > 0
        assert body["budget"]["input_tokens"] is None
        assert body["budget"]["observed_at"] is None
        assert body["current_model"] == "private-a"
        assert body["served_model"] is None
        assert body["flip"] is None
        _assert_content_free(body)

    run(scenario())


def test_a_pinned_lane_reports_the_enforced_candidates_budget_not_the_primarys(
    make_app: AppBuilder,
) -> None:
    """NE-CTX-IN-FORCE round 4, finding C — reported == enforced under a lane pin.

    The trim guard budgets the FIRST candidate the gateway's candidate rule
    yields under the composed ``RuntimeSettings.lane`` pin
    (``GatewayModel._turn_budget`` → ``TurnContextBudget``). This projection
    used to re-derive "current model" from the route register's PRIMARY —
    which, with the private lane pinned over a wide frontier primary (the
    air-gap beat's exact route shape), is a model that is NOT A CANDIDATE at
    all: the endpoint reported the 1M frontier window while the 32k private
    one was being enforced. It now asks the SAME resolver the guard asks, so
    both wire fields must name the enforced private candidate.

    The CONTROL leg clears the pin on the same state and re-reads: the same
    resolver then names the frontier primary, so the pin — not the harness —
    is what moved the number.
    """
    from ne.contracts.governance import RedactionResult
    from ne.gateway import GatewaySettings, LiteLLMGateway

    narrow = _PRIVATE_A.model_copy(update={"max_output_tokens": 12_288})
    wide = ModelDef(
        name="frontier-wide",
        litellm_model="anthropic/claude-sonnet-5",
        provider="anthropic",
        lane=Lane.FRONTIER_API,
        context_window=1_000_000,
        max_output_tokens=12_288,
    )
    tasks = {"agent": TaskRoute(model=wide.name, fallbacks=(narrow.name,))}
    settings = GatewaySettings(models=[narrow, wide], tasks=tasks)

    class _PassRedactor:
        def redact(self, text: str, *, categories: Any = None) -> RedactionResult:
            return RedactionResult(text=text)

    class _CostSink:
        def emit(self, event: Any) -> None:
            return None

    gateway = LiteLLMGateway(settings, _CostSink(), redactor=_PassRedactor())
    harness = make_app(gateway=gateway, gateway_models=[narrow, wide], gateway_tasks=tasks)
    state = get_state(harness.app)
    # The air-gap beat's composed pin, applied exactly where build_platform
    # composes it (RuntimeSettings.lane; ApiState is a plain dataclass).
    state.runtime_settings = state.runtime_settings.model_copy(
        update={"lane": Lane.PRIVATE_VLLM.value}
    )

    async def scenario() -> None:
        headers = harness.bearer(_OWNER, department=_DEPARTMENT)
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn(_PROMPT_SENTINEL, _ANSWER_SENTINEL))

        async with harness.client() as client:
            pinned = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
            assert pinned.status_code == 200, pinned.text
            body = pinned.json()
            # The pin filters the frontier primary OUT of the candidate list:
            # the enforced (and therefore reported) model is the private
            # fallback, at ITS window's input budget — 32768 - 12288 output
            # reserve - overhead margin = 18841, not the primary's 937712.
            assert body["current_model"] == narrow.name
            assert body["budget"]["input_budget"] == 18_841

            # CONTROL — same state, pin cleared: the same resolver now names
            # the frontier primary and its own window.
            state.runtime_settings = state.runtime_settings.model_copy(update={"lane": None})
            unpinned = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
            assert unpinned.status_code == 200, unpinned.text
            control = unpinned.json()
            assert control["current_model"] == wide.name
            assert control["budget"]["input_budget"] == 937_712

    run(scenario())


def test_a_capability_less_composition_reports_the_budget_the_guard_actually_enforces(
    make_app: AppBuilder,
) -> None:
    """NE-CTX-IN-FORCE round 4 — reported == enforced in the capability-less fallback.

    The conftest gateway double does NOT implement ``TurnContextBudget``, so
    ``GatewayModel._turn_budget`` enforces the composed boot budget (the boot
    primary ``private-a``'s window) on EVERY turn — including every turn after a
    governed repoint, which the guard cannot follow without the capability. The
    fallback used to re-derive the reported budget from the route register's
    CURRENT model: after repointing to a 1M-window target it reported 937712
    input tokens while the guard stayed armed at private-a's 27033 — a
    governance surface misreporting the number it exists to state. The BUDGET
    must be the composed one the guard enforces; the model NAME beside it stays
    the register's honest routing fact.
    """
    from ne.contracts import TurnContextBudget

    wide = ModelDef(
        name="wide-b",
        litellm_model="hosted_vllm/qwen-wide",
        provider="hosted_vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://vllm:8000/v1",
        context_window=1_000_000,
        max_output_tokens=12_288,
    )
    harness = make_app(
        gateway_models=[_PRIVATE_A, wide],
        gateway_tasks={"agent": TaskRoute(model=_PRIVATE_A.name)},
    )
    state = get_state(harness.app)
    route_store = state.route_store
    assert route_store is not None
    # The premise: this composition's gateway carries no TurnContextBudget, so
    # the guard is armed at the composed boot budget for the whole process life.
    assert not isinstance(state.gateway, TurnContextBudget)
    composed = state.runtime_settings.context_budget
    assert composed is not None

    async def scenario() -> None:
        headers = harness.bearer(_OWNER, department=_DEPARTMENT)
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn(_PROMPT_SENTINEL, _ANSWER_SENTINEL))

        route_store.apply(
            task="agent",
            model=wide.name,
            actor="user:ada",
            approver="user:bob",
            approval_id="approval-capless-repoint",
            via_action="write.internal.model.routing.repoint",
        )

        async with harness.client() as client:
            response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert response.status_code == 200, response.text
        body = response.json()
        # The register's routing fact: the repoint IS in force for routing.
        assert body["current_model"] == wide.name
        # But nothing enforces wide-b's window here — the guard is still armed
        # at the composed boot budget, and that is what must be reported.
        assert body["budget"]["input_budget"] == composed.input_budget
        assert body["budget"]["input_budget"] != 937_712

    run(scenario())


def test_route_flip_preserves_context_identity_and_reports_only_audited_usage(
    make_app: AppBuilder,
) -> None:
    harness = _ledger_app(make_app)
    state = get_state(harness.app)
    route_store = state.route_store
    assert route_store is not None

    async def scenario() -> None:
        headers = harness.bearer("user:ada", department="engineering")
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn(_PROMPT_SENTINEL, _ANSWER_SENTINEL))

        trace_id = "trace-context-ledger-model-usage"
        await _append_gateway_event(
            harness,
            trace_id=trace_id,
            model="private-a",
            input_tokens="321",
        )
        await _append_terminal(
            harness,
            session_id=session_id,
            trace_id=trace_id,
            outcome=AuditOutcome.success,
        )

        async with harness.client() as client:
            before_response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert before_response.status_code == 200, before_response.text
        before = before_response.json()

        applied = json.loads(
            route_store.apply(
                task="agent",
                model="private-b",
                actor="user:ada",
                approver="user:bob",
                approval_id="approval-context-ledger",
                via_action="write.internal.model.routing.repoint",
            )
        )

        async with harness.client() as client:
            after_response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert after_response.status_code == 200, after_response.text
        after = after_response.json()

        assert after["context_id"] == before["context_id"]
        assert after["components"] == before["components"]
        assert after["current_model"] == "private-b"
        assert after["served_model"] == "private-a"
        assert after["budget"]["input_tokens"] == 321
        assert after["budget"]["observed_at"] is not None
        assert after["flip"] == {
            "old_model": "private-a",
            "new_model": "private-b",
            "audit_seq": applied["audit_seq"],
            "changed_at": after["flip"]["changed_at"],
            "context_id_at_flip": before["context_id"],
        }
        _assert_content_free(after)

        # The terminal stamp is immutable: mutating current persisted payload
        # changes its recomputed identity and removes the placard.
        with state.session_store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(ne_agent_conversation_turns.c.conversation_id == session_id)
                .values(private_context='[{"kind":"mutated-after-terminal-stamp"}]')
            )
        async with harness.client() as client:
            mutated_response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert mutated_response.status_code == 200, mutated_response.text
        mutated = mutated_response.json()
        assert mutated["context_id"] != before["context_id"]
        assert mutated["flip"] is None

        # A later append likewise cannot inherit the old "object unchanged"
        # placard.
        await _append_owned(state, session_id, _turn("after flip", "new context"))
        async with harness.client() as client:
            moved_response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert moved_response.status_code == 200, moved_response.text
        moved = moved_response.json()
        assert moved["context_id"] != before["context_id"]
        assert moved["flip"] is None

    run(scenario())


def test_pre_session_flip_and_malformed_usage_are_not_presented_as_live_evidence(
    make_app: AppBuilder,
) -> None:
    harness = _ledger_app(make_app)
    state = get_state(harness.app)
    route_store = state.route_store
    assert route_store is not None
    route_store.apply(
        task="agent",
        model="private-b",
        actor="user:ada",
        approver="user:bob",
        approval_id="approval-before-session",
        via_action="write.internal.model.routing.repoint",
    )

    async def scenario() -> None:
        headers = harness.bearer("user:ada", department="engineering")
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn("prompt", "answer"))
        trace_id = "trace-context-ledger-malformed-usage"
        await _append_gateway_event(
            harness,
            trace_id=trace_id,
            model="private-b",
            input_tokens="not-a-count",
        )
        await _append_terminal(
            harness,
            session_id=session_id,
            trace_id=trace_id,
            outcome=AuditOutcome.success,
        )

        async with harness.client() as client:
            response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["current_model"] == body["served_model"] == "private-b"
        assert body["budget"]["input_tokens"] is None
        assert body["budget"]["observed_at"] is None
        assert body["flip"] is None

    run(scenario())


def test_broken_audit_chain_withholds_usage_and_flip_evidence(
    make_app: AppBuilder,
) -> None:
    harness = _ledger_app(make_app)
    state = get_state(harness.app)
    route_store = state.route_store
    assert route_store is not None

    async def scenario() -> None:
        headers = harness.bearer("user:ada", department="engineering")
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn("prompt", "answer"))
        route_store.apply(
            task="agent",
            model="private-b",
            actor="user:ada",
            approver="user:bob",
            approval_id="approval-tamper-control",
            via_action="write.internal.model.routing.repoint",
        )
        trace_id = "trace-context-ledger-tampered"
        await _append_gateway_event(
            harness,
            trace_id=trace_id,
            model="private-b",
            input_tokens="777",
        )
        await _append_terminal(
            harness,
            session_id=session_id,
            trace_id=trace_id,
            outcome=AuditOutcome.success,
        )

        with state.audit_engine.begin() as conn:
            # Red-team fixture only: defeat SQLite's first append-only layer to
            # prove the independently verified hash chain gates this evidence.
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
            conn.execute(
                sa.update(ne_audit_log)
                .where(ne_audit_log.c.action == "ne.gateway.complete")
                .values(record_hash="0" * 64)
            )

        async with harness.client() as client:
            response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["current_model"] == "private-b"
        assert body["served_model"] is None
        assert body["budget"]["input_tokens"] is None
        assert body["budget"]["observed_at"] is None
        assert body["flip"] is None

    run(scenario())


def test_later_failed_terminal_does_not_inherit_earlier_success(
    make_app: AppBuilder,
) -> None:
    harness = _ledger_app(make_app)
    state = get_state(harness.app)

    async def scenario() -> None:
        headers = harness.bearer("user:ada", department="engineering")
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn("first", "answer"))
        await _append_gateway_event(
            harness,
            trace_id="trace-earlier-success",
            model="private-a",
            input_tokens="111",
        )
        await _append_terminal(
            harness,
            session_id=session_id,
            trace_id="trace-earlier-success",
            outcome=AuditOutcome.success,
        )
        await _append_terminal(
            harness,
            session_id=session_id,
            trace_id="trace-later-failure",
            outcome=AuditOutcome.error,
        )

        async with harness.client() as client:
            response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["served_model"] is None
        assert body["budget"]["input_tokens"] is None
        assert body["budget"]["observed_at"] is None

    run(scenario())


def test_gateway_event_after_terminal_is_not_attributed_to_completed_run(
    make_app: AppBuilder,
) -> None:
    harness = _ledger_app(make_app)
    state = get_state(harness.app)

    async def scenario() -> None:
        headers = harness.bearer("user:ada", department="engineering")
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn("verified", "answer"))
        trace_id = "trace-post-terminal-call"
        await _append_terminal(
            harness,
            session_id=session_id,
            trace_id=trace_id,
            outcome=AuditOutcome.success,
        )
        await _append_gateway_event(
            harness,
            trace_id=trace_id,
            model="private-b",
            input_tokens="999",
        )

        async with harness.client() as client:
            response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["served_model"] is None
        assert body["budget"]["input_tokens"] is None
        assert body["budget"]["observed_at"] is None

    run(scenario())


@pytest.mark.parametrize("newer_kind", ["malformed", "config-mismatch"])
def test_newer_unusable_route_event_cannot_resurrect_older_flip(
    make_app: AppBuilder,
    newer_kind: str,
) -> None:
    harness = _ledger_app(make_app)
    state = get_state(harness.app)
    route_store = state.route_store
    assert route_store is not None

    async def scenario() -> None:
        headers = harness.bearer("user:ada", department="engineering")
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn("first", "answer"))
        await _append_terminal(
            harness,
            session_id=session_id,
            trace_id="trace-before-route-events",
            outcome=AuditOutcome.success,
        )
        route_store.apply(
            task="agent",
            model="private-b",
            actor="user:ada",
            approver="user:bob",
            approval_id="approval-older-valid-flip",
            via_action="write.internal.model.routing.repoint",
        )

        if newer_kind == "malformed":
            await _append_route_event(
                harness,
                old_model="private-b",
                new_model="private-b",
                detail="not-json",
            )
        else:
            mismatched = "0" * 64 if state.config_hash != "0" * 64 else "f" * 64
            await _append_route_event(
                harness,
                old_model="private-b",
                new_model="private-b",
                config_hash=mismatched,
            )

        async with harness.client() as client:
            response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["current_model"] == "private-b"
        assert body["flip"] is None

    run(scenario())


def test_route_event_that_does_not_match_enforced_model_has_no_placard(
    make_app: AppBuilder,
) -> None:
    harness = _ledger_app(make_app)
    state = get_state(harness.app)

    async def scenario() -> None:
        headers = harness.bearer("user:ada", department="engineering")
        session_id = await _open_session(harness, headers)
        await _append_owned(state, session_id, _turn("first", "answer"))
        await _append_terminal(
            harness,
            session_id=session_id,
            trace_id="trace-before-mismatched-route",
            outcome=AuditOutcome.success,
        )
        # Valid audit shape but no register write: the enforced route stays A.
        await _append_route_event(
            harness,
            old_model="private-a",
            new_model="private-b",
        )

        async with harness.client() as client:
            response = await client.get(
                f"/api/v1/chat/sessions/{session_id}/context-ledger", headers=headers
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["current_model"] == "private-a"
        assert body["flip"] is None

    run(scenario())
