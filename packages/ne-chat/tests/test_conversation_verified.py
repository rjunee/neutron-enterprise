# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-VAF-U4 — the channel-adapter surface (3 of 3: inline SSE, DBOS-enqueue, THIS).

ne-chat ships NO source change for this unit: :class:`ConversationService` drives
whatever ``AgentSession`` its injected ``SessionFactory`` returns, so a session
built with a composition-root ``VerifiedRouter`` (``ne.runtime.agent.
DepartmentAgentSession(..., router=...)``) inherits the verified-answer guarantee
automatically (design §arch-1.1) — there is no per-channel wiring to add. This
module proves that inheritance with a FAKE router (ne-chat cannot import
``ne.brain_ontology`` and does not need to — the fakes mirror the U3 pattern in
``packages/ne-runtime/tests/test_verified_router.py``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import sqlalchemy as sa

from ne.chat.conversation import ConversationService
from ne.chat.slack.streaming import SlackStreamer
from ne.contracts.runtime import InboundMessage, ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.govern.guardrails.engine import GuardrailEngine
from ne.govern.guardrails.verified import VerifiedDomainGuardrail
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.grounding import current_answer_recorder
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.verified_router import VerifiedRouter

from .conftest import RecordingAuditSink, ScriptedGateway, make_linker, principal, recording_client

DEPARTMENT = "ops"
SLACK_USER = "U0VERIFIED0"
SUBJECT = "user:verified"
_TRIGGER_PROMPT = "list every account i am cleared to see"
_TOOL_NAME = "read.kg.accounts_directory"


# --- fakes mirroring ne-runtime/tests/test_verified_router.py ---------------


@dataclass(frozen=True, slots=True)
class FakeRow:
    """Mirrors ``ne.brain_ontology.verified.routing.RoutingRow`` structurally."""

    domain: str
    template_id: str
    tool_name: str
    description: str
    triggers: tuple[str, ...]
    guard_terms: tuple[str, ...]
    clarify_max: int


@dataclass(frozen=True, slots=True)
class FakeDecision:
    """Mirrors ``ne.brain_ontology.verified.routing.RouteDecision`` structurally."""

    outcome: str
    template_id: str | None = None
    domain: str | None = None
    domains: tuple[str, ...] = ()


def _match_template(prompt: str, rows: Sequence[FakeRow]) -> FakeDecision:
    if prompt == _TRIGGER_PROMPT:
        (row,) = rows
        return FakeDecision(outcome="route", template_id=row.template_id, domain=row.domain)
    return FakeDecision(outcome="pass")


def _match_selection(_reply: str, _options: Sequence[FakeRow]) -> str | None:
    return None


@dataclass
class FakeExecutor:
    """A ``ToolExecutorLike`` fake whose ``execute`` returns a VerifiedAnswer-shaped
    JSON string with a matching ``template_id`` (mirrors ``answer_json`` in the
    ne-runtime U3 fakes)."""

    tool_specs: tuple[ToolSpec, ...] = ()
    calls: list[tuple[str, dict[str, Any], Principal]] = field(default_factory=list)

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        self.calls.append((tool, dict(args), principal))
        mapping = {
            "template_id": "accounts-directory",
            "template_version": 1,
            "config_hash": "hhhhhhhh",
            "args": {},
            "columns": ["name"],
            "rows": [["acme"]],
            "citations": ["urn:ne:entity:acme"],
            "truncated": False,
            "row_limit": 50,
            "ontology_version": "v1",
            "executed_at": "2026-07-16T00:00:00Z",
        }
        # NE-CTXB-5: record the STRUCTURED answer to the active per-run recorder
        # (§4.2) — the router now consumes recorder.answers, not the return string.
        record = current_answer_recorder()
        if record is not None:
            record(mapping)
        return json.dumps(mapping)


def _render_answer(_raw: str) -> str:
    """The deterministic, zero-model render seam (real production plugs
    ``render_answer_text`` here — see ``ne.api.platform``)."""
    return "VERIFIED RENDER"


def _make_router() -> tuple[VerifiedRouter[FakeRow], FakeExecutor]:
    row = FakeRow(
        domain="accounts-directory",
        template_id="accounts-directory",
        tool_name=_TOOL_NAME,
        description="List every account you are cleared to see",
        triggers=(_TRIGGER_PROMPT,),
        guard_terms=("accounts directory",),
        clarify_max=2,
    )
    executor = FakeExecutor(
        tool_specs=(
            ToolSpec(
                name=_TOOL_NAME,
                description="List accounts",
                tier=ApprovalTier.TIER_0,
                input_schema={"type": "object", "properties": {}},
            ),
        )
    )
    guardrail_engine = GuardrailEngine([VerifiedDomainGuardrail()], RecordingAuditSink())
    router: VerifiedRouter[FakeRow] = VerifiedRouter(
        rows=(row,),
        match=_match_template,
        match_selection=_match_selection,
        render_answer=_render_answer,
        executor=executor,
        guardrail_engine=guardrail_engine,
    )
    return router, executor


def _session_factory(gateway: ScriptedGateway):  # type: ignore[no-untyped-def]
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)
    router, executor = _make_router()

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
            executor=executor,  # type: ignore[arg-type]
            gateway=gateway,
            settings=RuntimeSettings(),
            router=router,
        )

    return factory, executor


def test_slack_routed_turn_streams_verified_render_without_model() -> None:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    gateway = ScriptedGateway()  # any consulted request is a hard failure below
    streamer = SlackStreamer(recording_client(recorder), min_update_interval_s=0.0)
    session_factory, executor = _session_factory(gateway)

    async def scenario() -> None:
        linker = await make_linker(
            sink, links={SLACK_USER: SUBJECT}, principals=[principal(SUBJECT)]
        )
        svc = ConversationService(linker, session_factory, streamer, sink)
        await svc.handle(InboundMessage(channel="C1", sender=SLACK_USER, text=_TRIGGER_PROMPT))

    asyncio.run(scenario())

    rendered = json.dumps([json.loads(r.content) for r in recorder])
    assert "VERIFIED RENDER" in rendered
    assert gateway.requests == []  # the model was NEVER consulted
    assert [c[0] for c in executor.calls] == [_TOOL_NAME]  # the ONE governed call
