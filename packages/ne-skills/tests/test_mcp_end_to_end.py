# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""MERGE-BLOCKING (plan §2.9): a real MCP fire of read.skills.view is governed.

Builds a REAL :class:`GovernedToolExecutor` (real guardrail engine over the two
skill arg policies, an allow-PDP, a recording sink) with the skill bindings wired
to ``current_tool_principal`` (the runtime carrier the executor sets), wraps it in a
:class:`GovernedMcpServer`, and drives an in-process MCP client: ``list_tools``
advertises both ``read.skills.*`` specs; ``call_tool('read.skills.view', ...)``
returns the body, appends the invocation row, and the sink holds the
``tool.execute`` success event with a trace id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.contracts import (
    freeze_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.governance import (
    ApprovalOutcome,
    ApprovalRequest,
    AuditSink,
    DecisionEffect,
    PolicyDecision,
    PolicyRequest,
)
from ne.contracts.types import Principal
from ne.govern import create_guardrail_engine
from ne.govern.config import (
    ArgSpec,
    GovernSettings,
    GuardrailSettings,
    RateLimitRule,
    RateLimitSettings,
    ToolArgKind,
    ToolArgPolicy,
)
from ne.mcp._sdk import create_connected_server_and_client_session, types
from ne.mcp.server import GovernedMcpServer
from ne.runtime.correlation import run_trace_scope
from ne.runtime.middleware import GovernedToolExecutor, current_tool_principal
from ne.runtime.tools import ToolBinding
from ne.skills.config import SkillsSettings
from ne.skills.registry import SqlAlchemySkillRegistry, ensure_skills_schema
from ne.skills.tools import (
    SEARCH_TOOL_NAME,
    VIEW_TOOL_NAME,
    build_search_handler,
    build_skill_tool_specs,
    build_view_handler,
)

from .conftest import StaticExpander, make_pack

P = Principal(id="user:ada")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def append(self, event: object) -> None:
        self.events.append(event)

    async def verify(self, *, start: datetime | None = None, end: datetime | None = None):
        from ne.contracts.governance import AuditVerification

        return AuditVerification(valid=True, checked=len(self.events))


class _AllowPdp:
    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        return PolicyDecision(effect=DecisionEffect.ALLOW, policy_version="test-allow")


class _UnusedGate:
    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        raise AssertionError("tier-0 read tools must not reach the approval gate")


@pytest.fixture(autouse=True)
def _frozen_hash() -> Iterator[str]:
    reset_config_hash_for_testing()
    freeze_config_hash(SkillsSettings())
    try:
        yield get_config_hash()
    finally:
        reset_config_hash_for_testing()


def _guardrails(sink: AuditSink):
    settings = GovernSettings(
        guardrails=GuardrailSettings(
            rate=RateLimitSettings(default=RateLimitRule(max_calls=1000, window_s=3600.0)),
            tools={
                SEARCH_TOOL_NAME: ToolArgPolicy(
                    args={"query": ArgSpec(kind=ToolArgKind.text, required=True)}
                ),
                VIEW_TOOL_NAME: ToolArgPolicy(
                    args={"name": ArgSpec(kind=ToolArgKind.identifier, required=True)}
                ),
            },
        )
    )
    return create_guardrail_engine(settings, sink=sink)


def _build(tmp_path: Path) -> tuple[GovernedMcpServer, _RecordingSink, SqlAlchemySkillRegistry]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'skills.db'}")
    ensure_skills_schema(engine)
    registry = SqlAlchemySkillRegistry(engine)
    registry.register_pack(make_pack("global-skill", acl_labels=()), origin="bundle")
    expander = StaticExpander()
    settings = SkillsSettings()
    specs = build_skill_tool_specs()
    search = build_search_handler(registry, expander, settings, current_tool_principal)
    view = build_view_handler(registry, expander, settings, current_tool_principal)
    bindings = (
        ToolBinding(spec=specs[0], handler=search),
        ToolBinding(spec=specs[1], handler=view),
    )
    sink = _RecordingSink()
    executor = GovernedToolExecutor(
        bindings,
        pdp=_AllowPdp(),
        guardrails=_guardrails(sink),
        approval_gate=_UnusedGate(),
        sink=sink,
    )
    server = GovernedMcpServer(executor, principal=P)
    return server, sink, registry


def test_list_tools_advertises_both(tmp_path: Path) -> None:
    server, _sink, _registry = _build(tmp_path)

    async def scenario() -> set[str]:
        async with create_connected_server_and_client_session(server.server) as session:
            listed = await session.list_tools()
        return {t.name for t in listed.tools}

    names = asyncio.run(scenario())
    assert {SEARCH_TOOL_NAME, VIEW_TOOL_NAME} <= names


def test_view_fire_is_governed(tmp_path: Path) -> None:
    server, sink, registry = _build(tmp_path)

    async def scenario() -> str:
        with run_trace_scope("trace-mcp-fire"):
            async with create_connected_server_and_client_session(server.server) as session:
                result = await session.call_tool(VIEW_TOOL_NAME, {"name": "global-skill"})
        assert result.isError is False
        block = result.content[0]
        assert isinstance(block, types.TextContent)
        return block.text

    body = asyncio.run(scenario())
    assert body.startswith("# global-skill")

    # invocation row appended, attributed to the constructor principal.
    page = registry.invocations_page(None)
    assert len(page.items) == 1
    assert page.items[0].principal_id == "user:ada"

    # the sink holds a tool.execute success event with a trace id.
    execs = [e for e in sink.events if getattr(e, "action", "") == "tool.execute"]
    assert execs
    assert any(getattr(e, "trace_id", None) == "trace-mcp-fire" for e in execs)
