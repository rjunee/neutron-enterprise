# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governed conversation disclosure: identity, trace, audit, and hard bounds."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.api.conversation_ledger import (
    ConversationTurnTooLargeError,
    build_conversation_ledger,
    encoded_ledger,
)
from ne.api.turn_wire import ProtectedTerminalPayload
from ne.audit import list_records
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.run_store import ne_agent_protected_turn_contents
from ne.runtime.store import ConversationTurn, ConversationTurnPage, ne_agent_conversation_turns

from .conftest import AppBuilder, get_state, run

QUESTION = "QUESTION-BODY-SENTINEL"
ANSWER = "ANSWER-BODY-SENTINEL"


def _messages(question: str = QUESTION, answer: str = ANSWER):
    return [
        ModelRequest(parts=[UserPromptPart(content=question)]),
        ModelResponse(parts=[TextPart(content=answer)], model_name="private-model"),
    ]


def _seed(h, *, attributed: bool = True, question: str = QUESTION) -> str:
    state = get_state(h.app)

    async def scenario() -> str:
        session_id = await state.session_store.open_session("console-chat", "finance")
        kwargs = {"principal_id": "user:subject", "trace_id": "trace-exact"} if attributed else {}
        await state.session_store.append_turn(session_id, _messages(question), **kwargs)
        return session_id

    return run(scenario())


async def _seed_protected(
    state: object,
    *,
    owner: str,
    department: str = "finance",
    answer: str = ANSWER,
) -> tuple[str, str]:
    sessions = state.session_store  # type: ignore[attr-defined]
    protected = state.agent_run_store  # type: ignore[attr-defined]
    assert protected is not None
    conversation = await sessions.create_conversation("console-chat", department, owner)
    attachment_id = "attachment-" + owner
    attempt_id = "attempt-" + owner
    await sessions.append_conversation_turn(
        conversation.conversation_id,
        owner,
        department,
        "console-chat",
        _messages("PRIVATE-" + owner, "PRIVATE-DRAFT"),
        served_output=answer,
        trace_id="trace-" + owner,
        attachment_id=attachment_id,
        attempt_id=attempt_id,
    )
    await sessions.publish_conversation_turn(
        conversation.conversation_id,
        owner,
        attachment_id,
        attempt_id,
        [],
    )

    async def protect(slot: str, plaintext: bytes):
        return await protected.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=None,
            owner_principal_id=owner,
            target_kind=CONVERSATION_TURN_TARGET_KIND,
            target_binding=conversation_turn_content_binding(
                conversation_id=conversation.conversation_id,
                attachment_id=attachment_id,
                attempt_id=attempt_id,
                slot=slot,
            ),
            content_status=ProtectedContentStatus.SOURCE_FREE,
            plaintext=plaintext,
        )

    input_ref = await protect("input", ("QUESTION-" + owner).encode())
    terminal_ref = await protect(
        "terminal", ProtectedTerminalPayload(output=answer).model_dump_json().encode()
    )
    with sessions.engine.begin() as conn:
        conn.execute(
            ne_agent_conversation_turns.update()
            .where(ne_agent_conversation_turns.c.conversation_id == conversation.conversation_id)
            .values(
                private_context="",
                served_output="",
                input_status="source_free",
                protected_input_ref=input_ref.row_id,
                protected_input_hash=input_ref.plaintext_hash,
                terminal_status="source_free",
                protected_terminal_ref=terminal_ref.row_id,
                protected_terminal_hash=terminal_ref.plaintext_hash,
            )
        )
    return conversation.conversation_id, terminal_ref.row_id


def test_ops_read_returns_complete_body_exact_identity_and_pre_return_receipt(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    session_id = _seed(h)

    async def scenario() -> dict[str, object]:
        async with h.client() as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:reader", groups=["ops"]),
            )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        return response.json()

    body = run(scenario())
    assert body["retention"] == "until_governed_reset"
    assert body["total_turns"] == 1
    assert body["returned_turns"] == 1
    assert body["truncated"] is False
    turn = body["turns"][0]  # type: ignore[index]
    assert turn["session_id"] == session_id
    assert turn["principal_id"] == "user:subject"
    assert turn["trace_id"] == "trace-exact"
    assert turn["attribution"] == "exact"
    assert QUESTION in json.dumps(turn["messages"])
    assert ANSWER in json.dumps(turn["messages"])

    engine = sa.create_engine(h.audit_url)
    try:
        receipts = list_records(engine, action="api.chat.conversation_turn_read")
    finally:
        engine.dispose()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.actor == "user:reader"
    detail = json.loads(receipt.payload["detail"])
    assert detail == {
        "session_id": session_id,
        "seq": "1",
        "attribution": "exact",
        "subject_principal_id": "user:subject",
        "trace_id": "trace-exact",
    }
    assert QUESTION not in str(receipt.payload)
    assert ANSWER not in str(receipt.payload)


def test_legacy_row_is_disclosed_as_explicitly_unattributed(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, attributed=False)

    async def scenario() -> dict[str, object]:
        async with h.client() as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:reader", groups=["ops"]),
            )
        assert response.status_code == 200
        return response.json()

    turn = run(scenario())["turns"][0]  # type: ignore[index]
    assert turn["principal_id"] is None
    assert turn["trace_id"] is None
    assert turn["attribution"] == "legacy-unattributed"


def test_protected_ledger_withholds_source_bound_content_without_private_query(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",), protected_transport=True)
    state = get_state(h.app)

    async def seed_and_read() -> dict[str, object]:
        conversation = await state.session_store.create_conversation(
            "console-chat", "finance", "user:reader"
        )
        await state.session_store.append_conversation_turn(
            conversation.conversation_id,
            "user:reader",
            "finance",
            "console-chat",
            _messages("PRIVATE-SOURCE-BOUND", "PRIVATE-DRAFT"),
            served_output="PRIVATE-SOURCE-BOUND-ANSWER",
            trace_id="trace-protected",
            attachment_id="attachment-protected",
            attempt_id="attempt-protected",
        )
        await state.session_store.publish_conversation_turn(
            conversation.conversation_id,
            "user:reader",
            "attachment-protected",
            "attempt-protected",
            [],
        )
        with state.session_store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation.conversation_id
                )
                .values(
                    private_context="",
                    served_output="",
                    input_status="source_bound_container",
                    protected_input_ref="protected-input",
                    protected_input_hash="a" * 64,
                    terminal_status="source_bound_container",
                    protected_terminal_ref="protected-terminal",
                    protected_terminal_hash="b" * 64,
                )
            )
        async with h.client() as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:reader", groups=["ops"], department="finance"),
            )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        return response.json()

    body = run(seed_and_read())
    turn = body["turns"][0]  # type: ignore[index]
    assert turn["content_state"] == "content_withheld"
    assert "messages" not in turn
    assert turn["transport_generation"] == "agent-transport-v1"
    assert turn["input"] == {
        "status": "source_bound_container",
        "protected_ref": "protected-input",
        "protected_hash": "a" * 64,
    }
    assert turn["terminal"] == {
        "status": "source_bound_container",
        "protected_ref": "protected-terminal",
        "protected_hash": "b" * 64,
    }
    assert "PRIVATE-SOURCE-BOUND" not in json.dumps(body)


def test_protected_ops_ledger_keeps_cross_principal_department_scope(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",), protected_transport=True)
    state = get_state(h.app)

    async def scenario() -> dict[str, object]:
        await _seed_protected(state, owner="user:ada", answer="ANSWER-ADA")
        await _seed_protected(state, owner="user:grace", answer="ANSWER-GRACE")
        await _seed_protected(
            state,
            owner="user:foreign-department",
            department="engineering",
            answer="CROSS-DEPARTMENT-MUST-NOT-APPEAR",
        )
        async with h.client() as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:ops", groups=["ops"], department="finance"),
            )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        return response.json()

    body = run(scenario())
    assert {turn["principal_id"] for turn in body["turns"]} == {  # type: ignore[index]
        "user:ada",
        "user:grace",
    }
    assert {turn["messages"][1]["content"] for turn in body["turns"]} == {  # type: ignore[index]
        "ANSWER-ADA",
        "ANSWER-GRACE",
    }
    assert "CROSS-DEPARTMENT-MUST-NOT-APPEAR" not in json.dumps(body)


@pytest.mark.parametrize("damage", ["missing", "tampered", "wrong-owner"])
def test_unreadable_protected_turn_projects_one_neutral_shell(
    make_app: AppBuilder, damage: str
) -> None:
    h = make_app(ops_roles=("ops",), protected_transport=True)
    state = get_state(h.app)

    async def scenario() -> tuple[dict[str, object], str]:
        conversation_id, terminal_ref = await _seed_protected(
            state, owner="user:subject", answer="PRIVATE-TERMINAL"
        )
        foreign_ref = None
        if damage == "wrong-owner":
            foreign_ref = await state.agent_run_store.claim_protected_payload(  # type: ignore[union-attr]
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                run_id=None,
                owner_principal_id="user:foreign",
                target_kind=CONVERSATION_TURN_TARGET_KIND,
                target_binding=conversation_turn_content_binding(
                    conversation_id=conversation_id,
                    attachment_id="attachment-user:subject",
                    attempt_id="attempt-user:subject",
                    slot="terminal",
                ),
                content_status=ProtectedContentStatus.SOURCE_FREE,
                plaintext=ProtectedTerminalPayload(output="FOREIGN-PRIVATE")
                .model_dump_json()
                .encode(),
            )
        with state.agent_run_store._engine.begin() as conn:  # type: ignore[union-attr]
            row = ne_agent_protected_turn_contents
            if damage == "missing":
                conn.execute(sa.delete(row).where(row.c.turn_content_id == terminal_ref))
            elif damage == "tampered":
                conn.execute(
                    row.update()
                    .where(row.c.turn_content_id == terminal_ref)
                    .values(encrypted_dek="A" * 64)
                )
            else:
                assert foreign_ref is not None
                conn.execute(
                    ne_agent_conversation_turns.update()
                    .where(ne_agent_conversation_turns.c.conversation_id == conversation_id)
                    .values(
                        protected_terminal_ref=foreign_ref.row_id,
                        protected_terminal_hash=foreign_ref.plaintext_hash,
                    )
                )
        async with h.client() as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:ops", groups=["ops"], department="finance"),
            )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        return response.json(), response.text

    body, raw = run(scenario())
    turn = body["turns"][0]  # type: ignore[index]
    assert turn["content_state"] == "legacy_unavailable"
    assert turn["input"]["status"] == "legacy_unavailable"
    assert turn["terminal"]["status"] == "legacy_unavailable"
    assert "messages" not in turn
    assert set(turn) == {
        "session_id",
        "agent_name",
        "department",
        "seq",
        "principal_id",
        "trace_id",
        "attribution",
        "transport_generation",
        "input",
        "terminal",
        "created_at",
        "content_state",
    }
    assert damage not in raw
    assert "PRIVATE-TERMINAL" not in raw


def test_protected_route_preserves_authoritative_response_byte_ceiling(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",), protected_transport=True)
    state = get_state(h.app)

    async def scenario() -> None:
        await _seed_protected(state, owner="user:subject", answer="X" * 1_100_000)
        async with h.client() as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:ops", groups=["ops"], department="finance"),
            )
        assert response.status_code == 413
        assert response.json()["detail"].startswith("newest retained conversation turn")
        assert "X" * 100 not in response.text

    run(scenario())


def test_unauthorized_caller_is_audited_before_store_read(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_app(ops_roles=("ops",))
    state = get_state(h.app)

    async def forbidden_read(*, limit: int):
        raise AssertionError(f"store reached with limit={limit}")

    monkeypatch.setattr(state.session_store, "list_conversation_turns", forbidden_read)

    async def scenario() -> None:
        async with h.client() as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:outsider", groups=["reader"]),
            )
        assert response.status_code == 403
        assert QUESTION not in response.text
        assert ANSWER not in response.text

    run(scenario())
    engine = sa.create_engine(h.audit_url)
    try:
        denied = list_records(engine, action="api.chat.conversation_unauthorized")
    finally:
        engine.dispose()
    assert [row.actor for row in denied] == ["user:outsider"]


def test_audit_append_failure_withholds_every_body(make_app: AppBuilder) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h)
    state = get_state(h.app)

    class FailingSink:
        async def append(self, event: object) -> None:
            raise RuntimeError("audit unavailable")

        async def verify(self, *, start: object = None, end: object = None) -> object:
            raise NotImplementedError

    state.sink = FailingSink()  # type: ignore[assignment]

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=h.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://ne.test") as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:reader", groups=["ops"]),
            )
        assert response.status_code == 500
        assert QUESTION not in response.text
        assert ANSWER not in response.text

    run(scenario())


def _turn(label: str, body: str) -> ConversationTurn:
    return ConversationTurn(
        session_id=f"session-{label}",
        agent_name="assistant",
        department="finance",
        seq=1,
        principal_id=f"user:{label}",
        trace_id=f"trace-{label}",
        messages=({"kind": "request", "parts": [{"content": body}]},),
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


def test_byte_ceiling_keeps_whole_newest_turn_and_marks_truncation() -> None:
    newest = _turn("newest", "small")
    older = _turn("older", "X" * 2_000)
    page = ConversationTurnPage(turns=(newest, older), total_turns=2)

    view = build_conversation_ledger(page, max_response_bytes=600)

    assert view.returned_turns == 1
    assert view.truncated is True
    assert view.turns[0].session_id == "session-newest"
    assert view.turns[0].messages[0]["parts"][0]["content"] == "small"
    assert len(encoded_ledger(view)) <= 600


def test_byte_ceiling_fails_when_newest_whole_turn_cannot_fit() -> None:
    page = ConversationTurnPage(turns=(_turn("newest", "X" * 2_000),), total_turns=1)
    with pytest.raises(ConversationTurnTooLargeError, match="newest retained"):
        build_conversation_ledger(page, max_response_bytes=300)


def test_route_returns_413_instead_of_slicing_an_oversized_newest_turn(
    make_app: AppBuilder,
) -> None:
    h = make_app(ops_roles=("ops",))
    _seed(h, question="X" * 1_100_000)

    async def scenario() -> None:
        async with h.client() as client:
            response = await client.get(
                "/api/v1/chat/conversations",
                headers=h.bearer("user:reader", groups=["ops"]),
            )
        assert response.status_code == 413
        assert response.json()["detail"].startswith("newest retained conversation turn")
        assert "X" * 100 not in response.text

    run(scenario())
