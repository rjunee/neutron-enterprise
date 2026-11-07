# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ConversationService — a Slack message drives a FULLY-GOVERNED agent turn (C6).

No mocking past the runtime seam: a real GovernedToolExecutor + Rego-faithful PDP
+ real GuardrailEngine + real DepartmentAgentSession + ScriptedGateway. The proof
is in the audit trail (pdp.decide + tool.execute) and the recorded Slack transport
(placeholder chat.postMessage then chat.update with the answer).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import sqlalchemy as sa

from ne.chat.conversation import ConversationService
from ne.chat.slack.streaming import SlackStreamer
from ne.contracts.runtime import InboundMessage
from ne.contracts.types import Principal
from ne.govern.errors import AuditEmissionError
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .conftest import (
    FailingAuditSink,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    chat_settings,
    conversation_bindings,
    make_executor,
    make_gate,
    make_linker,
    principal,
    recording_client,
    text_completion,
    tool_call_completion,
)

DEPARTMENT = "ops"
SLACK_USER = "U0ADA00000"
SUBJECT = "user:ada"


def _session_factory(
    sink: Any,
    gateway: ScriptedGateway,
    log: ToolLog,
    *,
    pdp_overrides: dict[str, dict[str, Any]] | None = None,
):
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)
    _s, gate, _e = make_gate(sink)
    executor = make_executor(
        conversation_bindings(log), sink, approval_gate=gate, pdp_overrides=pdp_overrides
    )

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


def _methods(recorder: list[httpx.Request]) -> list[str]:
    return [r.url.path.rsplit("/", 1)[-1] for r in recorder]


def test_slack_message_runs_fully_governed_agent_turn() -> None:
    sink = RecordingAuditSink()
    log = ToolLog()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "status"}),
            text_completion("All systems nominal."),
        ]
    )
    streamer = SlackStreamer(recording_client(recorder), min_update_interval_s=0.0)

    async def scenario() -> None:
        linker = await make_linker(
            sink, links={SLACK_USER: SUBJECT}, principals=[principal(SUBJECT)]
        )
        # A fresh sink for the governed turn so identity + turn audit are isolated.
        svc = ConversationService(linker, _session_factory(sink, gateway, log), streamer, sink)
        await svc.handle(
            InboundMessage(channel="C1", sender=SLACK_USER, text="what is the status?")
        )

    asyncio.run(scenario())

    # C6: the tool call went THROUGH the governed executor (pdp + execute audited).
    actions = sink.actions()
    assert "pdp.decide" in actions
    assert "tool.execute" in actions
    assert log.names() == ["read.docs.search"]

    # Streaming shape: placeholder postMessage, then chat.update(s) with the answer.
    methods = _methods(recorder)
    assert methods[0] == "chat.postMessage"
    assert "chat.update" in methods
    rendered = json.dumps([json.loads(r.content) for r in recorder])
    assert "All systems nominal." in rendered


def test_unmapped_user_gets_no_agent_turn() -> None:
    sink = RecordingAuditSink()
    log = ToolLog()
    recorder: list[httpx.Request] = []
    invoked = {"n": 0}
    streamer = SlackStreamer(recording_client(recorder), min_update_interval_s=0.0)

    async def factory(p: Principal) -> DepartmentAgentSession:  # pragma: no cover
        invoked["n"] += 1
        raise AssertionError("session_factory must not be called for an unmapped user")

    async def scenario() -> None:
        linker = await make_linker(sink, links={})  # nobody linked
        svc = ConversationService(linker, factory, streamer, sink)
        await svc.handle(InboundMessage(channel="C1", sender="U0GHOST", text="hi"))

    asyncio.run(scenario())

    assert invoked["n"] == 0
    assert log.names() == []
    assert sink.with_action("chat.identity.denied")  # denial audited by the linker
    # An ephemeral denial was sent; no PDP/tool events occurred.
    assert "pdp.decide" not in sink.actions()
    assert _methods(recorder) == ["chat.postEphemeral"]


def test_denied_action_blocked_and_audited() -> None:
    sink = RecordingAuditSink()
    log = ToolLog()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [
            tool_call_completion("read.docs.search", {"q": "status"}),
            text_completion("Could not retrieve that."),
        ]
    )
    streamer = SlackStreamer(recording_client(recorder), min_update_interval_s=0.0)
    overrides = {
        "read.docs.search": {
            "effect": "deny",
            "tier": 0,
            "reason": "policy denies",
            "obligations": {},
            "policy_version": "1.0.0",
        }
    }

    async def scenario() -> None:
        linker = await make_linker(
            sink, links={SLACK_USER: SUBJECT}, principals=[principal(SUBJECT)]
        )
        svc = ConversationService(
            linker, _session_factory(sink, gateway, log, pdp_overrides=overrides), streamer, sink
        )
        await svc.handle(InboundMessage(channel="C1", sender=SLACK_USER, text="search"))

    asyncio.run(scenario())

    assert log.names() == []  # the denied tool never executed
    denied = [
        e for e in sink.events if getattr(e, "action", "") == "pdp.decide" and e.outcome == "denied"
    ]
    assert len(denied) == 1
    # The turn still completed with a final streamed answer.
    rendered = json.dumps([json.loads(r.content) for r in recorder])
    assert "Could not retrieve that." in rendered


def test_audit_sink_failure_kills_turn_with_generic_slack_message() -> None:
    turn_sink = FailingAuditSink()  # every append fails => the turn is unauditable
    identity_sink = RecordingAuditSink()
    log = ToolLog()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway(
        [tool_call_completion("read.docs.search", {"q": "status"}), text_completion("unreached")]
    )
    streamer = SlackStreamer(recording_client(recorder), min_update_interval_s=0.0)

    async def scenario() -> None:
        linker = await make_linker(
            identity_sink, links={SLACK_USER: SUBJECT}, principals=[principal(SUBJECT)]
        )
        svc = ConversationService(
            linker, _session_factory(turn_sink, gateway, log), streamer, turn_sink
        )
        try:
            await svc.handle(InboundMessage(channel="C1", sender=SLACK_USER, text="search"))
            raise AssertionError("expected the unauditable turn to propagate")
        except AuditEmissionError:
            pass

    asyncio.run(scenario())

    # The Slack message shows a generic failure — no internals / exception text.
    rendered = json.dumps([json.loads(r.content) for r in recorder])
    assert "went wrong" in rendered
    assert "AuditEmissionError" not in rendered
    assert chat_settings().bot_token.get_secret_value() not in rendered
