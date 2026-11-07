# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Codex-P2: fail-closed governance-INFRA errors must survive the MCP SDK boundary.

The mcp 1.x lowlevel server wraps our ``call_tool`` handler in
``except Exception: return _make_error_result(str(e))`` (mcp/server/lowlevel/server.py:589),
so any exception :class:`GovernedMcpServer` lets propagate would be converted AT THE SDK
SEAM into an ``isError`` result carrying the RAW internal exception string (C12 status-only
violation) while silently downgrading the documented withhold semantics.

Each test drives a REAL :class:`GovernedMcpServer` + REAL
:class:`~ne.runtime.middleware.GovernedToolExecutor` (real guardrail engine, real
:class:`~ne.govern.pdp.GovernedPdp`) over the in-process MCP MEMORY transport — the exact
``_handle_request`` path where the leak occurred — with an injected failing ``AuditSink`` /
raising guardrail / unreachable OPA transport. The assertions pin OUR contract: exact
sanitized status-only text, the injected SECRET marker provably ABSENT, and the tool
handler never invoked (result withheld).
"""

from __future__ import annotations

import logging
import traceback

import pytest

from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier
from ne.govern import GuardrailEngine
from ne.mcp._sdk import create_connected_server_and_client_session
from ne.mcp.server import (
    _SANITIZED_AUDIT_UNAVAILABLE,
    _SANITIZED_GUARDRAIL_UNAVAILABLE,
    _SANITIZED_INTERNAL,
    _SANITIZED_POLICY_UNAVAILABLE,
    GovernedMcpServer,
)
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.tools import ToolBinding

from .conftest import (
    FailingAuditSink,
    RaisingGuardrail,
    RecordingAuditSink,
    ToolLog,
    UnusedGate,
    make_executor,
    make_guardrails,
    make_principal,
    make_unreachable_pdp,
    result_text,
    run,
    standard_bindings,
)


def _adapter(executor) -> GovernedMcpServer:
    return GovernedMcpServer(executor, principal=make_principal())


def _boom_tool_specs(self: GovernedToolExecutor) -> tuple[ToolSpec, ...]:
    raise RuntimeError("BOOM-tool-specs /internal/specs.cfg")


def _arm_raising_tool_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``GovernedToolExecutor.tool_specs`` raise for the rest of THIS test only —
    the ``monkeypatch`` fixture is per-test and auto-restores the real property
    afterward (no xdist in this repo's pytest lanes, so no cross-test bleed)."""
    monkeypatch.setattr(GovernedToolExecutor, "tool_specs", property(_boom_tool_specs))


def test_failing_audit_sink_sanitized_iserror_no_execution() -> None:
    # An always-failing sink: the pdp.decide append fails -> AuditEmissionError, which the
    # adapter must sanitize before the SDK stringifies it.
    log = ToolLog()
    sink = FailingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("read.docs.search", {"q": "hello"})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert text == _SANITIZED_AUDIT_UNAVAILABLE
    assert FailingAuditSink.SECRET not in text
    assert "audit append failed" not in text
    assert "pdp.decide" not in text
    assert log.calls == []


def test_unknown_tool_with_failing_audit_sink_sanitized() -> None:
    # The unknown-tool branch's FIRST action is a tool.unknown emit -> RuntimeAuditError.
    log = ToolLog()
    sink = FailingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("write.internal.nope.x", {})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert text == _SANITIZED_AUDIT_UNAVAILABLE
    assert FailingAuditSink.SECRET not in text
    assert "tool.unknown" not in text
    assert log.calls == []


def test_audit_failure_after_handler_withholds_result() -> None:
    # pdp.decide records fine; the handler RUNS; then the success tool.execute emit fails ->
    # RuntimeAuditError. The successful result must be WITHHELD, not returned.
    log = ToolLog()
    sink = FailingAuditSink(fail_actions=frozenset({"tool.execute"}))
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("read.docs.search", {"q": "hello"})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert log.names() == ["read.docs.search"]  # the handler DID run
    assert "docs: 3 results" not in text  # ...but its result is withheld, not returned
    assert text == _SANITIZED_AUDIT_UNAVAILABLE
    assert FailingAuditSink.SECRET not in text


def test_failing_guardrail_engine_sanitized_and_failure_audited() -> None:
    # A real GuardrailEngine whose guardrail raises internally -> GuardrailEvaluationError,
    # AFTER the engine audits a guardrail.error event to the (working) sink.
    log = ToolLog()
    sink = RecordingAuditSink()
    engine = GuardrailEngine([RaisingGuardrail()], sink=sink)
    executor = make_executor(
        standard_bindings(log), sink, approval_gate=UnusedGate(), guardrails=engine
    )
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("read.docs.search", {"q": "hello"})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert text == _SANITIZED_GUARDRAIL_UNAVAILABLE
    assert RaisingGuardrail.SECRET not in text
    assert log.calls == []
    # Observability: the engine audited the infra failure before raising.
    assert sink.with_action("guardrail.error")
    assert sink.with_action("tool.execute") == []


def test_guardrail_failure_with_failing_audit_sink_maps_to_audit_code() -> None:
    # The engine's own guardrail.error append fails -> AuditEmissionError: the DISTINCT
    # audit-down code fires when the sink itself is the failed component.
    log = ToolLog()
    sink = FailingAuditSink()
    engine = GuardrailEngine([RaisingGuardrail()], sink=sink)
    executor = make_executor(
        standard_bindings(log), sink, approval_gate=UnusedGate(), guardrails=engine
    )
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("read.docs.search", {"q": "hello"})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert text == _SANITIZED_AUDIT_UNAVAILABLE
    assert RaisingGuardrail.SECRET not in text
    assert FailingAuditSink.SECRET not in text
    assert log.calls == []


def test_policy_backend_failure_sanitized_and_audited() -> None:
    # An unreachable OPA transport -> PolicyBackendError, AFTER GovernedPdp audits the
    # pdp.decide error to the (working) sink.
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=UnusedGate(),
        pdp=make_unreachable_pdp(sink),
    )
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("read.docs.search", {"q": "hello"})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert text == _SANITIZED_POLICY_UNAVAILABLE
    assert "opa" not in text
    assert "BOOM-opa" not in text
    assert log.calls == []
    # Observability: the PDP audited the backend failure before re-raising.
    decides = sink.with_action("pdp.decide")
    assert decides
    assert any(getattr(e, "outcome", "") == "error" for e in decides)


def test_unexpected_error_sanitized_defense_in_depth() -> None:
    # An extractor callback raises a RAW RuntimeError inside execute() BEFORE guardrails,
    # outside all executor try blocks -> only the blanket except-Exception guard catches it.
    log = ToolLog()
    sink = RecordingAuditSink()

    def _boom(_args: object) -> tuple[str, ...]:
        raise RuntimeError("BOOM-recipients /internal/recipients.cfg")

    binding = ToolBinding(
        spec=ToolSpec(
            name="write.external.email.send",
            description="Send an external email.",
            tier=ApprovalTier.TIER_2,
            input_schema={"type": "object", "properties": {"to": {"type": "string"}}},
        ),
        handler=log.handler("write.external.email.send", "sent"),
        extract_recipients=_boom,
    )
    executor = make_executor(
        [binding], sink, approval_gate=UnusedGate(), guardrails=make_guardrails(sink)
    )
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("write.external.email.send", {"to": "x@y.z"})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert text == _SANITIZED_INTERNAL
    assert "BOOM-recipients" not in text
    assert "/internal/" not in text
    assert log.calls == []


def test_unexpected_error_logged_server_side(caplog: pytest.LogCaptureFixture) -> None:
    # The blanket guard (Argus P3): the raw exception is logged SERVER-SIDE (with traceback,
    # logger ne.mcp.server, ERROR) for diagnosability, while the client-visible result is
    # still exactly _SANITIZED_INTERNAL with no raw marker (C12 — the log never crosses).
    log = ToolLog()
    sink = RecordingAuditSink()

    def _boom(_args: object) -> tuple[str, ...]:
        raise RuntimeError("BOOM-recipients /internal/recipients.cfg")

    binding = ToolBinding(
        spec=ToolSpec(
            name="write.external.email.send",
            description="Send an external email.",
            tier=ApprovalTier.TIER_2,
            input_schema={"type": "object", "properties": {"to": {"type": "string"}}},
        ),
        handler=log.handler("write.external.email.send", "sent"),
        extract_recipients=_boom,
    )
    executor = make_executor(
        [binding], sink, approval_gate=UnusedGate(), guardrails=make_guardrails(sink)
    )
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("write.external.email.send", {"to": "x@y.z"})
        assert result.isError is True
        return result_text(result)

    with caplog.at_level(logging.ERROR, logger="ne.mcp.server"):
        text = run(scenario())

    # Client surface: sanitized only, no raw marker.
    assert text == _SANITIZED_INTERNAL
    assert "BOOM-recipients" not in text
    # Server-side: exactly the diagnostic record, with the raw marker in its traceback.
    assert any(
        r.name == "ne.mcp.server"
        and r.levelno == logging.ERROR
        and r.exc_info is not None
        and "BOOM-recipients" in "".join(traceback.format_exception(*r.exc_info))
        for r in caplog.records
    )


def test_ordinary_governed_errors_keep_informative_surface() -> None:
    # The boundary: ordinary governed errors keep the "{ErrorClass}: {msg}" surface; only
    # the fail-closed infra classes are code-sanitized.
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("write.internal.nope.x", {})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert text.startswith("ToolNotRegisteredError")
    assert "NE_MCP_" not in text


# --- NE-INV-MCP-LISTTOOLS-SAFETY: Finding D (construction-time-eager catalog) -----


def test_list_tools_serves_construction_time_snapshot_after_specs_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The catalog is captured ONCE at construction. A LATER break in the executor's
    # tool_specs property must not affect the already-frozen snapshot the handler
    # serves. Fails on the pre-hardening lazy comprehension, which re-reads
    # (and would raise from) tool_specs on every list_tools() call.
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    expected_names = {s.name for s in executor.tool_specs}
    adapter = _adapter(executor)  # snapshot taken HERE, before the break

    _arm_raising_tool_specs(monkeypatch)

    async def scenario():
        async with create_connected_server_and_client_session(adapter.server) as session:
            return await session.list_tools()

    listed = run(scenario())
    assert {t.name for t in listed.tools} == expected_names


def test_tool_cache_refresh_path_cannot_leak_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Finding D regression pin. An unknown tool name guarantees a tool-cache MISS in
    # the SDK's call_tool handler, driving the exact audit scenario:
    # _get_cached_tool_definition -> ListToolsRequest handler re-invocation (mcp
    # lowlevel server.py:531 / 487-489), INSIDE the SDK's own
    # `except Exception: return _make_error_result(str(e))` catch-all (lines 589-590) —
    # entirely BEFORE our sanitized _call_tool chain runs. On the pre-hardening lazy
    # code this leaks the raw "BOOM-tool-specs /internal/specs.cfg" text straight to the
    # client. On the hardened code the catalog is a frozen construction-time snapshot,
    # so this call never re-touches the broken tool_specs property at all — the SDK
    # tool-cache refresh succeeds off the frozen snapshot, "no.such.tool" is correctly
    # absent from it, and control falls through to our OWN governed unknown-tool path.
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)  # snapshot taken HERE, before the break

    _arm_raising_tool_specs(monkeypatch)

    async def scenario() -> str:
        async with create_connected_server_and_client_session(adapter.server) as session:
            result = await session.call_tool("no.such.tool", {})
        assert result.isError is True
        return result_text(result)

    text = run(scenario())
    assert "BOOM-tool-specs" not in text
    assert "/internal/" not in text
    assert text.startswith("ToolNotRegisteredError:")
    assert log.calls == []


def test_broken_tool_specs_fail_loudly_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins the chosen tradeoff: eager construction means a broken tool_specs property
    # aborts GovernedMcpServer construction LOUDLY, before any transport exists — it
    # never reaches a client-visible seam.
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    _arm_raising_tool_specs(monkeypatch)

    with pytest.raises(RuntimeError, match="BOOM-tool-specs"):
        _adapter(executor)


def test_governed_server_lists_no_tool_output_schema() -> None:
    # GHSA-rwrf-2pqf-9j8j (mcp 1.10.0-1.29.1): a ClientSession validates every call_tool
    # result against the server-declared ``outputSchema`` with jsonschema's DEFAULT
    # resolver, which fetches any external ``$ref`` (file://, http://, https://) from the
    # client's own network position. The platform lock and langgraph_env are on >= 1.30.0,
    # but crewai_env is walled at mcp 1.28.1 by crewai's own ``mcp~=1.28.1`` pin, and its
    # ClientSession talks ONLY to this governed server over stdio — so that residue is
    # neutralised exactly as long as this server declares NO outputSchema on any tool.
    # To be deleted when crewai_env moves to mcp >= 1.30.0.
    log = ToolLog()
    sink = RecordingAuditSink()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())
    adapter = _adapter(executor)

    async def scenario():
        async with create_connected_server_and_client_session(adapter.server) as session:
            return await session.list_tools()

    listed = run(scenario())
    # Non-vacuity: the catalog is populated and is exactly the executor's specs.
    assert listed.tools
    assert {t.name for t in listed.tools} == {s.name for s in executor.tool_specs}
    for tool in listed.tools:
        assert tool.outputSchema is None, tool.name
