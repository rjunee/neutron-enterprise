# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-009B principal-owned Conversation HTTP projection controls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from ne.api.deps import api_state, current_principal
from ne.api.routers import conversations
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import Principal
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_store import (
    AgentRunStore,
    ensure_run_schema,
)
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_conversations,
)

AGENT = "agt_projection"
OWNER = "user:ada"
DEPARTMENT = "operations"


class _Catalog:
    def get_visible(self, agent_id: str, _principal: Principal):
        return object() if agent_id == AGENT else None


def _environment(principal: Principal):
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(
                current_key_id="projection-k1", keys={"projection-k1": b"k" * 32}
            )
        ),
    )
    state = SimpleNamespace(
        session_store=sessions,
        agent_run_store=runs,
        agent_catalog=_Catalog(),
        engines=[engine],
    )
    app = FastAPI()
    app.include_router(conversations.router)
    app.dependency_overrides[api_state] = lambda: state
    app.dependency_overrides[current_principal] = lambda: principal
    return engine, sessions, runs, TestClient(app)


def test_list_and_get_use_safe_fallback_titles_and_total_order_cursor() -> None:
    engine, sessions, _runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )

    async def seed():
        one = await sessions.create_conversation(AGENT, DEPARTMENT, OWNER)
        two = await sessions.create_conversation(AGENT, DEPARTMENT, OWNER)
        await sessions.create_conversation("agt_other", DEPARTMENT, OWNER)
        return one, two

    one, two = asyncio.run(seed())
    tie = datetime(2026, 8, 21, 10, 42, tzinfo=UTC)
    first_id = "conversation-z"
    second_id = "conversation-a"
    with engine.begin() as conn:
        conn.execute(
            ne_agent_conversations.update()
            .where(ne_agent_conversations.c.conversation_id == one.conversation_id)
            .values(conversation_id=second_id)
        )
        conn.execute(
            ne_agent_conversations.update()
            .where(ne_agent_conversations.c.conversation_id == two.conversation_id)
            .values(conversation_id=first_id)
        )
        conn.execute(
            ne_agent_conversations.update()
            .where(ne_agent_conversations.c.conversation_id.in_((first_id, second_id)))
            .values(created_at=tie, updated_at=tie)
        )

    first = client.get(f"/v1/agents/{AGENT}/conversations", params={"limit": 1})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-store"
    first_body = first.json()
    assert first_body["items"][0]["conversation_id"] == first_id
    assert first_body["items"][0]["title"] == "Conversation · 2026-08-21 10:42"
    assert first_body["next_cursor"]

    second = client.get(
        f"/v1/agents/{AGENT}/conversations",
        params={"limit": 1, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["conversation_id"] == second_id
    assert second.json()["next_cursor"] is None

    fetched = client.get(f"/v1/agents/{AGENT}/conversations/{second_id}")
    assert fetched.status_code == 200
    assert set(fetched.json()) == {
        "conversation_id",
        "agent_id",
        "title",
        "created_at",
        "updated_at",
        "last_sequence",
    }
    engine.dispose()


def test_create_mints_distinct_blank_principal_owned_conversations() -> None:
    engine, sessions, _runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )

    first = client.post(f"/v1/agents/{AGENT}/conversations")
    second = client.post(
        f"/v1/agents/{AGENT}/conversations",
        json={
            "agent_id": "agt_spoofed",
            "department": "finance",
            "owner_principal_id": "user:mallory",
        },
    )

    for response in (first, second):
        assert response.status_code == 201
        assert response.headers["cache-control"] == "private, no-store"
        assert set(response.json()) == {
            "conversation_id",
            "agent_id",
            "title",
            "created_at",
            "updated_at",
            "last_sequence",
        }
        assert response.json()["agent_id"] == AGENT
        assert response.json()["last_sequence"] == 0
    assert first.json()["conversation_id"] != second.json()["conversation_id"]

    async def load_created():
        first_row = await sessions.get_conversation(
            first.json()["conversation_id"], OWNER, DEPARTMENT
        )
        second_row = await sessions.get_conversation(
            second.json()["conversation_id"], OWNER, DEPARTMENT
        )
        return first_row, second_row

    created = asyncio.run(load_created())
    assert all(row is not None for row in created)
    assert {
        (row.owner_principal_id, row.department, row.agent_name, row.last_sequence)
        for row in created
        if row is not None
    } == {(OWNER, DEPARTMENT, AGENT, 0)}
    engine.dispose()


def test_create_hidden_random_and_missing_department_write_no_rows() -> None:
    engine, _sessions, _runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )

    hidden = client.post("/v1/agents/agt_hidden/conversations")
    random = client.post("/v1/agents/agt_random/conversations")
    assert (hidden.status_code, hidden.content, hidden.headers["cache-control"]) == (
        random.status_code,
        random.content,
        random.headers["cache-control"],
    )
    assert hidden.status_code == 404

    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[current_principal] = lambda: Principal(id=OWNER)
    missing_department = client.post(f"/v1/agents/{AGENT}/conversations")
    assert missing_department.status_code == 422
    assert missing_department.headers["cache-control"] == "private, no-store"

    with engine.connect() as conn:
        row_count = conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_conversations))
    assert row_count == 0
    engine.dispose()


def test_foreign_known_random_and_role_substitution_are_byte_equivalent() -> None:
    engine, sessions, _runs, client = _environment(
        Principal(
            id="user:bob",
            groups=("role:ops",),
            claims={"department": DEPARTMENT},
        )
    )
    foreign = asyncio.run(sessions.create_conversation(AGENT, DEPARTMENT, OWNER))

    hidden = client.get(f"/v1/agents/{AGENT}/conversations/{foreign.conversation_id}")
    random = client.get(f"/v1/agents/{AGENT}/conversations/conversation-random")
    assert (hidden.status_code, hidden.content, hidden.headers["cache-control"]) == (
        random.status_code,
        random.content,
        random.headers["cache-control"],
    )
    assert hidden.status_code == 404
    engine.dispose()


def test_hidden_agent_and_malformed_or_cross_resource_cursor_fail_closed() -> None:
    engine, sessions, _runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )
    asyncio.run(sessions.create_conversation(AGENT, DEPARTMENT, OWNER))
    malformed = client.get(f"/v1/agents/{AGENT}/conversations", params={"cursor": "not-a-cursor"})
    hidden = client.get("/v1/agents/agt_hidden/conversations")
    random = client.get("/v1/agents/agt_random/conversations")
    assert malformed.status_code == 404
    assert (hidden.status_code, hidden.content) == (random.status_code, random.content)
    assert hidden.headers["cache-control"] == "private, no-store"
    engine.dispose()


def test_turns_are_target_authored_ordered_and_materialize_exact_run_inputs() -> None:
    engine, sessions, runs, client = _environment(
        Principal(id=OWNER, claims={"department": DEPARTMENT})
    )
    conversation = asyncio.run(sessions.create_conversation(AGENT, DEPARTMENT, OWNER))

    async def admit(key: str, text: bytes, *, expected_version: int):
        return await runs.admit_start(
            SimpleNamespace(
                idempotency_key=key,
                request_hash=("a" if key == "one" else "b") * 64,
                conversation_id=conversation.conversation_id,
                expected_version=expected_version,
                owner_principal_id=OWNER,
                department=DEPARTMENT,
                conversation_agent_name=AGENT,
                agent_id=AGENT,
                agent_version=1,
                strategy="agentic",
                config_hash="c" * 64,
                capability_snapshot_hash="d" * 64,
                capability_snapshot_receipt_id="receipt",
                input_plaintext=text,
                authenticated_context=AuthenticatedRequestContext(
                    principal_id=OWNER,
                    authorization_groups=(),
                    department=DEPARTMENT,
                    issuer="https://issuer.example.test",
                    expires_at=4_102_444_800,
                ),
            )
        )

    asyncio.run(admit("one", b"first admitted", expected_version=0))
    asyncio.run(admit("two", b"second admitted", expected_version=1))
    page = client.get(
        f"/v1/agents/{AGENT}/conversations/{conversation.conversation_id}/turns",
        params={"limit": 1},
    )
    assert page.status_code == 200
    assert page.headers["cache-control"] == "private, no-store"
    assert page.json()["items"][0]["turn_sequence"] == 1
    assert page.json()["items"][0]["user_content"] == "first admitted"
    next_page = client.get(
        f"/v1/agents/{AGENT}/conversations/{conversation.conversation_id}/turns",
        params={"limit": 1, "cursor": page.json()["next_cursor"]},
    )
    assert next_page.json()["items"][0]["turn_sequence"] == 2
    assert next_page.json()["items"][0]["user_content"] == "second admitted"
    engine.dispose()


def test_conversation_router_has_only_the_bounded_create_write_method() -> None:
    methods: dict[str, set[str]] = {}
    routes = [route for route in conversations.router.routes if isinstance(route, APIRoute)]
    for route in routes:
        methods.setdefault(route.path, set()).update(route.methods or ())
    assert {path: frozenset(values) for path, values in methods.items()} == {
        "/v1/agents/{agent_id}/conversations": frozenset({"GET", "POST"}),
        "/v1/agents/{agent_id}/conversations/{conversation_id}": frozenset({"GET"}),
        "/v1/agents/{agent_id}/conversations/{conversation_id}/runs": frozenset({"GET"}),
        "/v1/agents/{agent_id}/conversations/{conversation_id}/turns": frozenset({"GET"}),
    }
    create = next(
        route
        for route in routes
        if route.path == "/v1/agents/{agent_id}/conversations" and route.methods == {"POST"}
    )
    assert create.dependant.body_params == []
