# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance (a): every MCP-arriving tool call routes through the REAL C6 executor.

Each test drives a REAL in-process MCP client session against a
:class:`GovernedMcpServer` wrapping the REAL
:class:`~ne.runtime.middleware.GovernedToolExecutor` (real guardrail engine, real
:class:`~ne.govern.pdp.GovernedPdp` over a Rego-faithful MockTransport OPA wire): PDP
consulted + audit emitted on every call; Tier-3 blocks on HITL; unknown tools get the
NE-033 tool.unknown discipline; guardrail violations block pre-handler; malformed args
still reach governance (``validate_input=False``, the NE-033-bounce class closed).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping

import pytest

from ne.contracts.governance import ApprovalOutcome, ApprovalStatus
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.mcp._sdk import create_connected_server_and_client_session
from ne.mcp.server import TURN_META_KEY, GovernedMcpServer, _thaw
from ne.runtime.correlation import current_action_scope
from ne.runtime.errors import ActionValidationError
from ne.runtime.tools import ToolBinding

from .conftest import (
    ALLOWED_RECIPIENT,
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedApprovalGate,
    ToolLog,
    UnusedGate,
    make_executor,
    make_principal,
    result_text,
    run,
    standard_bindings,
)


def _adapter(executor, principal=None) -> GovernedMcpServer:
    return GovernedMcpServer(executor, principal=principal or make_principal())


def _scope_observing_binding(scopes: list[str | None]) -> ToolBinding:
    """A tier-0 ``read.docs.search`` binding (policy-allowlisted in ``govern_settings``)
    whose handler records the ambient :func:`current_action_scope` on every call."""

    async def handler(args: Mapping[str, object]) -> str:
        scopes.append(current_action_scope())
        return "ok"

    return ToolBinding(
        spec=ToolSpec(
            name="read.docs.search",
            description="Search internal docs.",
            tier=ApprovalTier.TIER_0,
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        ),
        handler=handler,
    )


def test_list_tools_renders_specs_only() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=NamedApproverGate())
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            listed = await session.list_tools()
        by_name = {t.name: t for t in listed.tools}
        specs = {s.name: s for s in executor.tool_specs}
        assert set(by_name) == set(specs)
        for name, tool in by_name.items():
            assert tool.description == specs[name].description
            # read.docs.search declares a schema; all render a JSON-object schema.
            assert tool.inputSchema.get("type") == "object"

    run(scenario())


def test_list_tools_catalog_is_stable_and_matches_specs_exactly() -> None:
    """The construction-time-eager catalog (Finding D hardening) renders EXACTLY what
    the pre-hardening lazy list comprehension rendered — byte-for-byte, same order —
    and is stable across repeated ``list_tools()`` calls on the same session."""
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=NamedApproverGate())
    adapter = _adapter(executor)

    async def scenario():
        async with create_connected_server_and_client_session(adapter.server) as session:
            first = await session.list_tools()
            second = await session.list_tools()
        return first, second

    first, second = run(scenario())

    # Exact parity with what the pre-hardening live comprehension would have rendered,
    # in spec order.
    assert [
        {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
        for t in first.tools
    ] == [
        {
            "name": s.name,
            "description": s.description,
            "inputSchema": _thaw(s.input_schema) or {"type": "object"},
        }
        for s in executor.tool_specs
    ]
    # Stable across calls: the same frozen snapshot is served every time.
    assert [t.model_dump() for t in first.tools] == [t.model_dump() for t in second.tools]


def test_tier0_tool_executes_through_governance() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    principal = make_principal("user:ada")
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor, principal)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("read.docs.search", {"q": "hello"})
        assert result.isError is False
        assert result_text(result) == "docs: 3 results"

    run(scenario())

    assert "pdp.decide" in sink.actions()
    assert "tool.execute" in sink.actions()
    assert log.names() == ["read.docs.search"]
    # No anonymous execution: every audited event carries the constructor principal.
    for event in sink.events:
        assert getattr(event, "actor", None) == principal.id


def test_tier3_call_blocks_on_hitl_named_approval() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = NamedApproverGate("alice")
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("finance.payment.initiate", {"amount": 10})
        assert result.isError is False
        assert result_text(result) == "paid"

    run(scenario())

    # The gate WAS consulted at Tier-3 before the handler ran.
    assert gate.requests and gate.requests[0].tier == ApprovalTier.TIER_3
    approvals = sink.with_action("tool.approval")
    assert approvals and approvals[0].provenance.get("approver") == "alice"
    assert log.names() == ["finance.payment.initiate"]


def test_tier3_rejected_approval_yields_error_and_no_execution() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = ScriptedApprovalGate([ApprovalOutcome(status=ApprovalStatus.REJECTED)])
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("finance.payment.initiate", {"amount": 10})
        assert result.isError is True
        assert result_text(result).startswith("ApprovalNotGrantedError")

    run(scenario())

    assert log.calls == []
    denied = [e for e in sink.with_action("tool.approval") if getattr(e, "outcome", "") == "denied"]
    assert denied


def test_unknown_tool_rejected_and_audited() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("write.internal.nope.x", {})
        assert result.isError is True
        assert "ToolNotRegisteredError" in result_text(result)

    run(scenario())

    assert log.calls == []
    assert sink.with_action("tool.unknown")
    # The NE-033 unknown-tool discipline reached over MCP: a pdp.decide still follows.
    assert sink.with_action("pdp.decide")
    assert sink.with_action("tool.execute") == []


def test_guardrail_violation_blocks_before_handler() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    # UnusedGate: a guardrail block must never reach the approval gate.
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool(
                "write.external.email.send",
                {"to": "attacker@evil.example", "body": "x"},
            )
        assert result.isError is True
        assert "GuardrailViolation" in result_text(result)

    run(scenario())

    assert log.calls == []
    assert sink.with_action("guardrail.block")
    assert sink.with_action("tool.execute") == []


def test_malformed_args_still_reach_governance() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            # q must be text; a number is malformed. With validate_input=False the SDK
            # forwards it to governance instead of silently pre-rejecting it.
            result = await session.call_tool("read.docs.search", {"q": 42})
        assert result.isError is True
        assert "GuardrailViolation" in result_text(result)

    run(scenario())

    assert sink.with_action("guardrail.block")
    assert log.calls == []


def test_validation_refusal_renders_as_governed_error() -> None:
    """NE-ACTION-VALIDATE-BEFORE-GATE (STEP 6/10): a binding whose ``validate_args``
    refuses the call renders as an ordinary governed ``isError`` — redaction-safe
    ``"ActionValidationError: ..."`` — NEVER the sanitized
    ``NE_MCP_EXECUTION_UNAVAILABLE`` infra code (pins ``_GOVERNED_ERRORS``
    membership). The gate is never reached (``UnusedGate``) and the handler never
    runs."""
    log = ToolLog()
    sink = RecordingAuditSink()

    def _refuse(args: Mapping[str, object]) -> None:
        raise ActionValidationError("invalid input for 'write.external.email.send': fields ['to']")

    binding = ToolBinding(
        spec=ToolSpec(
            name="write.external.email.send",
            description="Send an external email.",
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
            },
        ),
        handler=log.handler("write.external.email.send", "sent"),
        validate_args=_refuse,
    )
    executor = make_executor([binding], sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool(
                "write.external.email.send", {"to": ALLOWED_RECIPIENT, "body": "hi"}
            )
        assert result.isError is True
        text = result_text(result)
        assert "ActionValidationError" in text
        assert "NE_MCP_EXECUTION_UNAVAILABLE" not in text

    run(scenario())

    assert log.calls == []
    denied = [e for e in sink.with_action("tool.validate") if getattr(e, "outcome", "") == "denied"]
    assert len(denied) == 1


def test_constructor_requires_principal() -> None:
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    with pytest.raises(TypeError):
        GovernedMcpServer(executor, principal=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        GovernedMcpServer(None, principal=make_principal())  # type: ignore[arg-type]


# --- NE-SCOPE-MCP: two-level action scope (client-signaled _meta turn id) --------


def test_no_meta_scope_is_the_connection_scope() -> None:
    """No ``_meta`` at all ⇒ byte-identical Level-1 fallback: one stable ``mcp:``-prefixed
    scope per connection, with no ``:turn:`` segment."""
    scopes: list[str | None] = []
    sink = RecordingAuditSink()
    executor = make_executor([_scope_observing_binding(scopes)], sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            r1 = await session.call_tool("read.docs.search", {"q": "hello"})
            r2 = await session.call_tool("read.docs.search", {"q": "hello"})
        assert r1.isError is False
        assert r2.isError is False

    run(scenario())

    assert len(scopes) == 2
    assert scopes[0] is not None
    assert scopes[0] == scopes[1]
    assert scopes[0].startswith("mcp:")
    assert ":turn:" not in scopes[0]


def test_valid_turn_token_composes_connection_prefixed_scope() -> None:
    """A valid ``TURN_META_KEY`` composes ``{connection}:turn:{token}`` exactly; distinct
    tokens yield distinct scopes, a repeated token yields the identical scope, and the
    base (no-meta) scope is unaffected by interleaving."""
    scopes: list[str | None] = []
    sink = RecordingAuditSink()
    executor = make_executor([_scope_observing_binding(scopes)], sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> None:
        async with create_connected_server_and_client_session(adapter.server) as session:
            await session.call_tool("read.docs.search", {"q": "hello"})
            await session.call_tool(
                "read.docs.search", {"q": "hello"}, meta={TURN_META_KEY: "turn-a"}
            )
            await session.call_tool(
                "read.docs.search", {"q": "hello"}, meta={TURN_META_KEY: "turn-b"}
            )
            await session.call_tool(
                "read.docs.search", {"q": "hello"}, meta={TURN_META_KEY: "turn-a"}
            )

    run(scenario())

    base = scopes[0]
    assert base is not None
    assert scopes == [base, f"{base}:turn:turn-a", f"{base}:turn:turn-b", f"{base}:turn:turn-a"]


def test_invalid_turn_token_falls_back_and_is_still_governed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed ``_meta`` turn tokens (space, overlong, empty, non-string, non-ASCII) all
    fall back to the connection scope; the call still executes fully governed
    (pdp.decide + tool.execute audited); a WARNING logs per invalid call; the raw token
    value is NEVER present in the log text (redaction)."""
    scopes: list[str | None] = []
    sink = RecordingAuditSink()
    executor = make_executor([_scope_observing_binding(scopes)], sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)
    invalid_values: list[object] = ["bad token", "x" * 129, "", 42, "café"]

    async def scenario() -> list:
        results = []
        async with create_connected_server_and_client_session(adapter.server) as session:
            base_result = await session.call_tool("read.docs.search", {"q": "hello"})
            results.append(base_result)
            for value in invalid_values:
                result = await session.call_tool(
                    "read.docs.search", {"q": "hello"}, meta={TURN_META_KEY: value}
                )
                results.append(result)
        return results

    with caplog.at_level(logging.WARNING, logger="ne.mcp.server"):
        results = run(scenario())

    assert all(r.isError is False for r in results)
    base_scope = scopes[0]
    assert base_scope is not None
    for scope in scopes[1:]:
        assert scope == base_scope
    # The pipeline stayed governed for every call (base + each invalid-token call).
    assert len(sink.with_action("pdp.decide")) == len(invalid_values) + 1
    assert len(sink.with_action("tool.execute")) == len(invalid_values) + 1
    # A warning fired for each invalid token; the raw values never appear in the log.
    assert len(caplog.records) >= len(invalid_values)
    assert "bad token" not in caplog.text
    assert "café" not in caplog.text


def test_server_module_holds_no_handlers() -> None:
    import ne.mcp.server as m

    src = inspect.getsource(m)
    assert "ToolBinding" not in src
    assert ".handler" not in src
