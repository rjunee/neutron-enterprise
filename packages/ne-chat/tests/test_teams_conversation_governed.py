# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE C6 proof (NE-080): a Teams activity drives a FULLY-GOVERNED agent turn.

No mocking past the runtime seam: a signed Bot Framework activity POSTed to the
Teams ingress -> ConversationService -> real GovernedToolExecutor + Rego-faithful
PDP + real GuardrailEngine + real DepartmentAgentSession + ScriptedGateway. The
proof is in the audit trail (pdp.decide + tool.execute) and the recorded Bot
Connector transport (placeholder activity POST then answer PUT). An unmapped
sender is denied + audited and never opens a session.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import sqlalchemy as sa
from fastapi.testclient import TestClient

from ne.chat.conversation import ConversationService
from ne.chat.identity import ChatIdentityLinker
from ne.chat.teams.adapter import TeamsChatAdapter
from ne.chat.teams.auth import StaticJwksKeySource, TeamsActivityAuthenticator
from ne.chat.teams.ingress import create_teams_ingress
from ne.chat.teams.refs import ConversationRefs
from ne.chat.teams.streaming import TeamsStreamer
from ne.contracts.types import Principal
from ne.identity.provisioning import InMemoryPrincipalStore, JitPrincipalResolver
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import (
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    conversation_bindings,
    make_executor,
    make_gate,
    principal,
    text_completion,
    tool_call_completion,
)
from .teams_support import (
    JWKS,
    bearer,
    load_fixture,
    mint_activity_token,
    recording_teams_client,
    teams_chat_settings,
)

DEPARTMENT = "ops"
SUBJECT = "user:ada"


def _session_factory(sink: Any, gateway: ScriptedGateway, log: ToolLog):
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)
    _s, gate, _e = make_gate(sink)
    executor = make_executor(conversation_bindings(log), sink, approval_gate=gate)

    async def factory(p: Principal) -> DepartmentAgentSession:
        session_id = (
            await store.create_conversation("assistant", DEPARTMENT, p.id)
        ).conversation_id
        return DepartmentAgentSession(
            session_id=session_id,
            agent_name="assistant",
            department=DEPARTMENT,
            owner_principal_id=p.id,
            store=store,
            executor=executor,
            gateway=gateway,
            settings=RuntimeSettings(),
        )

    return factory


async def _teams_linker(
    sink: Any, links: dict[str, str], principals: list[Principal]
) -> ChatIdentityLinker:
    store = InMemoryPrincipalStore()
    for p in principals:
        await store.put(p)
    return ChatIdentityLinker(links, JitPrincipalResolver(store), sink, surface="teams")


def _methods(recorder: list[httpx.Request]) -> list[str]:
    out: list[str] = []
    for r in recorder:
        url = str(r.url)
        if url.endswith("/activities"):
            out.append("POST")
        elif "/activities/" in url:
            out.append("PUT")
    return out


def _build(links: dict[str, str], principals: list[Principal]) -> SimpleNamespace:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    refs = ConversationRefs(max_size=100)
    client, _ = recording_teams_client(recorder, refs, seed_conversation=False)
    streamer = TeamsStreamer(client, min_update_interval_s=0.0)
    adapter = TeamsChatAdapter(client, streamer)
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "status"}),
            text_completion("All systems nominal."),
        ]
    )
    log = ToolLog()
    linker = asyncio.run(_teams_linker(sink, links, principals))
    service = ConversationService(linker, _session_factory(sink, gateway, log), streamer, sink)
    adapter.on_message(service.handle)
    authenticator = TeamsActivityAuthenticator(teams_chat_settings(), StaticJwksKeySource(JWKS))
    captured: list[Any] = []

    def spawn(coro: Any) -> Any:
        captured.append(coro)
        return coro

    app = create_teams_ingress(
        settings=teams_chat_settings(),
        adapter=adapter,
        authenticator=authenticator,
        refs=refs,
        spawn=spawn,
    )
    return SimpleNamespace(
        client=TestClient(app), captured=captured, sink=sink, recorder=recorder, log=log
    )


def _post(ctx: SimpleNamespace, body: dict[str, Any]) -> httpx.Response:
    token = mint_activity_token(serviceurl=str(body.get("serviceUrl", "")))
    return ctx.client.post(
        "/teams/messages",
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json", **bearer(token)},
    )


def test_signed_activity_runs_fully_governed_turn() -> None:
    ctx = _build({"aad-ada": SUBJECT}, [principal(SUBJECT)])
    body = load_fixture("message_activity.json")
    assert _post(ctx, body).status_code == 200
    assert len(ctx.captured) == 1
    asyncio.run(ctx.captured[0])

    # C6: the tool call went THROUGH the governed executor.
    actions = ctx.sink.actions()
    assert "pdp.decide" in actions
    assert "tool.execute" in actions
    assert ctx.log.names() == ["read.docs.search"]

    # Streaming shape: placeholder POST then a final PUT with the answer.
    methods = _methods(ctx.recorder)
    assert methods[0] == "POST"
    assert "PUT" in methods
    rendered = json.dumps(
        [json.loads(r.content) for r in ctx.recorder if "/activities" in str(r.url)]
    )
    assert "All systems nominal." in rendered


def test_unmapped_sender_denied_and_audited_no_session() -> None:
    ctx = _build({"aad-ada": SUBJECT}, [principal(SUBJECT)])
    body = load_fixture("message_activity.json")
    # A stranger with no AAD-object-id link.
    body["from"] = {"id": "29:stranger"}
    assert _post(ctx, body).status_code == 200
    assert len(ctx.captured) == 1
    asyncio.run(ctx.captured[0])

    assert ctx.log.names() == []  # no tool ran
    assert "pdp.decide" not in ctx.sink.actions()  # no governed turn
    assert ctx.sink.with_action("chat.identity.denied")  # denial audited by the linker
    # An ephemeral (plain-message) denial was posted, nothing else.
    assert _methods(ctx.recorder) == ["POST"]
