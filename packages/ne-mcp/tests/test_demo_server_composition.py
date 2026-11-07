# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-LG-DROPIN — the in-memory C6/C12/C7 non-bypass suite over the demo server.

Drives the REAL composed ``build_demo_server`` (real GovernedToolExecutor, real
guardrail engine, real DurableApprovalGate over SQLite, real GovernedPdp with ONLY
the OPA HTTP wire MockTransport-stubbed Rego-faithfully) through the in-process MCP
transport — NO langchain anywhere. The ``test_mcp_end_to_end.py`` template.

The structural crux this proves: an agent whose entire toolset is this server has no
ungoverned path to any effect.

  * list_tools == the granted 3-tool set exactly; the ungranted read.kb.export is
    invisible AND fail-closed on call (C7 trim).
  * an entirely unknown tool (shell.exec) is audited-denied with zero effects, at the
    toolset boundary (C12) — and the inner executor's own unknown-tool discipline
    (ToolNotRegisteredError + tool.unknown) is exercised directly.
  * the Tier-1 CRM-note write (NE-CS-SEND ``1df6a113``, owner-reaffirmed
    2026-08-29) executes AUTONOMOUSLY — exactly ONCE, zero approval rows, with
    the pdp.decide → tool.execute audit spine under one trace id (C6/C10).
  * a Tier-3 write REJECTED ⇒ zero effects, approval.requested + rejection
    audited (C12) — the gated legs of the ladder ride the ticket action now
    that the CRM note is Tier 1.

NOTE (design §7 vs. the composed server): the demo wraps the executor in a
``ToolsetScopedExecutor`` (the C7 grant, design §4), whose grant check fires BEFORE
the inner registration check. So an ungranted name — registered (read.kb.export) OR
unknown (shell.exec) — surfaces over MCP as ``ToolsetViolationError`` (audited
``tool.toolset_denied``), not ``ToolNotRegisteredError``. The design's §7 wording
("shell.exec → ToolNotRegisteredError") describes the RAW executor; the honest
composed-server behaviour is asserted here, and the raw-executor discipline is
exercised via ``test_inner_executor_unknown_tool_is_registration_denied``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.contracts.plugins import PluginRegistry
from ne.contracts.types import Principal
from ne.mcp._sdk import create_connected_server_and_client_session
from ne.mcp.demo_server import build_demo_server
from ne.mcp.server import TURN_META_KEY, GovernedMcpServer
from ne.runtime.approval_store import ne_approval_requests
from ne.runtime.correlation import run_trace_scope
from ne.runtime.errors import ToolNotRegisteredError

from .conftest import make_pdp, result_text, run

_CRM_NOTE = "write.internal.crm.note"
_TICKET = "write.external.ticket.update"

_CRM_ARGS = {
    "request_id": "lg-demo-note-1",
    "record_id": "001A00000LgDemo01",
    "title": "Account research summary",
    "body": "Automated account research summary for the demo.",
}
_TICKET_ARGS = {
    "request_id": "lg-demo-ticket-1",
    "ticket_id": 1,
    "status": "resolved",
    "public_reply": "auto-close attempt",
}


class _StubPdpFactory:
    """Resolved via the registry under ('policy_decision_point', 'opa').

    ``create_pdp`` calls ``cls.from_settings(settings, sink=sink)`` — so this returns
    the REAL GovernedPdp with only the OPA HTTP wire MockTransport-stubbed (the
    established no-mock-past-the-seam pattern).
    """

    @staticmethod
    def from_settings(settings: object, *, sink: object) -> object:
        return make_pdp(sink)


def _registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register("policy_decision_point", "opa", _StubPdpFactory)
    return registry


def _build(tmp_path: Path):
    return build_demo_server(tmp_path / "demo.db", "user:lg-demo", registry=_registry())


def _audit_rows(engine: sa.Engine) -> list[sa.Row]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(
                    ne_audit_log.c.seq,
                    ne_audit_log.c.action,
                    ne_audit_log.c.resource,
                    ne_audit_log.c.outcome,
                    ne_audit_log.c.trace_id,
                ).order_by(ne_audit_log.c.seq)
            ).all()
        )


def _actions(engine: sa.Engine) -> list[str]:
    return [str(r.action) for r in _audit_rows(engine)]


# --- (a) C7: list_tools == the grant exactly -------------------------------------


def test_list_tools_is_exactly_the_granted_set(tmp_path: Path) -> None:
    comp = _build(tmp_path)

    async def scenario() -> set[str]:
        async with create_connected_server_and_client_session(comp.server.server) as session:
            listed = await session.list_tools()
        return {t.name for t in listed.tools}

    names = run(scenario())
    assert names == {"read.kb.search", _CRM_NOTE, _TICKET}
    assert "read.kb.export" not in names  # C7 trim: registered but ungranted, invisible


# --- (b) C7: an ungranted registered tool is fail-closed -------------------------


def test_ungranted_tool_is_fail_closed(tmp_path: Path) -> None:
    comp = _build(tmp_path)

    async def scenario():
        async with create_connected_server_and_client_session(comp.server.server) as session:
            return await session.call_tool("read.kb.export", {})

    result = run(scenario())
    assert result.isError is True
    assert "ToolsetViolationError" in result_text(result)
    # Audited as a toolset denial; the export handler was never reached (no tool.execute).
    rows = _audit_rows(comp.engine)
    denied = [
        r for r in rows if r.action == "tool.toolset_denied" and r.resource == "tool:read.kb.export"
    ]
    assert denied, "expected a tool.toolset_denied audit row for read.kb.export"
    assert not any(r.action == "tool.execute" and r.resource == "tool:read.kb.export" for r in rows)


# --- (c) C12: an unknown tool is audited-denied with zero effects ----------------


def test_unknown_tool_is_audited_denied(tmp_path: Path) -> None:
    comp = _build(tmp_path)

    async def scenario():
        async with create_connected_server_and_client_session(comp.server.server) as session:
            return await session.call_tool("shell.exec", {"cmd": "rm -rf /"})

    result = run(scenario())
    assert result.isError is True
    assert "ToolsetViolationError" in result_text(result)
    rows = _audit_rows(comp.engine)
    assert any(r.action == "tool.toolset_denied" and r.resource == "tool:shell.exec" for r in rows)
    # Zero connector-stub effects.
    assert comp.crm_client.calls == []
    assert comp.ticket_client.calls == []


def test_inner_executor_unknown_tool_is_registration_denied(tmp_path: Path) -> None:
    """The RAW executor's unknown-tool discipline (design §7): ToolNotRegisteredError."""
    comp = _build(tmp_path)

    async def scenario() -> None:
        await comp.executor.execute(
            "shell.exec", {"cmd": "rm -rf /"}, principal=Principal(id="user:lg-demo")
        )

    raised = False
    try:
        run(scenario())
    except ToolNotRegisteredError:
        raised = True
    assert raised, "the inner executor must raise ToolNotRegisteredError for an unknown tool"
    assert any(
        r.action == "tool.unknown" and r.resource == "tool:shell.exec"
        for r in _audit_rows(comp.engine)
    )


# --- (d) NE-CS-SEND: the Tier-1 CRM write crosses NO approval gate ---------------


def _drive_write_with_decision(
    comp,
    tool: str,
    args: dict,
    *,
    approve: bool,
    approver: str,
    meta: dict[str, Any] | None = None,
):
    async def scenario():
        async with create_connected_server_and_client_session(comp.server.server) as session:

            async def do_call():
                return await session.call_tool(tool, args, meta=meta)

            async def decider():
                for _ in range(500):
                    pending = [p for p in await comp.gate.list_pending() if p.action == tool]
                    if pending:
                        rid = pending[0].request_id
                        if approve:
                            await comp.gate.approve(rid, approver=approver)
                        else:
                            await comp.gate.reject(rid, approver=approver, reason="not authorized")
                        return
                    await asyncio.sleep(0.02)
                raise AssertionError(f"no pending approval for {tool} appeared")

            result, _ = await asyncio.gather(do_call(), decider())
            return result

    return run(scenario())


def _drive_write(comp, tool: str, args: dict, *, meta: dict[str, Any] | None = None):
    """Call one governed write and return the MCP result (no approver task).

    ``write.internal.crm.note`` is Tier 1 since NE-CS-SEND (``1df6a113``):
    autonomous, so there is never a pending approval to grant. The scope/dedup
    tests below pin derived-dedup scoping — orthogonal to the tier — and use
    this helper; gated (Tier-3) writes still drive
    :func:`_drive_write_with_decision`.
    """

    async def scenario():
        async with create_connected_server_and_client_session(comp.server.server) as session:
            return await session.call_tool(tool, args, meta=meta)

    return run(scenario())


def test_tier1_crm_write_crosses_no_approval_gate(tmp_path: Path) -> None:
    """NE-CS-SEND (``1df6a113``, owner-reaffirmed 2026-08-29: "writing an internal
    CRM note should stay tier 1 … Sending an external email remains tier 2"):
    ``write.internal.crm.note`` is a Tier-1 internal write — autonomous, so it
    parks NOTHING: no pending row, no approval audit events, and the connector
    runs without any human in the loop. This replaced the Tier-2 rejected leg
    for this action (there is no gate left to reject at); the C12
    rejected-⇒-zero-effects property on this composed server stays pinned by
    :func:`test_tier3_reject_never_executes` over the SAME MCP path. If this
    action ever returns to Tier 2, restore the rejected leg here."""
    comp = _build(tmp_path)
    result = _drive_write(comp, _CRM_NOTE, _CRM_ARGS)
    assert result.isError is False, result_text(result)
    assert len(comp.crm_client.calls) == 1
    with comp.engine.connect() as conn:
        rows = list(conn.execute(sa.select(ne_approval_requests)))
    assert rows == []
    actions = _actions(comp.engine)
    assert "approval.requested" not in actions
    assert "tool.approval" not in actions
    assert "approval.rejected" not in actions
    assert "tool.execute" in actions


# --- (e) C6/C10: the Tier-1 CRM write executes once, full autonomous spine --------


def test_tier1_crm_write_executes_once_full_spine(tmp_path: Path) -> None:
    """The C6/C10 spine for the Tier-1 CRM note (NE-CS-SEND ``1df6a113``):
    executes exactly ONCE with pdp.decide → tool.execute under one trace id,
    and — because a Tier-1 write consumes NO approval — the written Body is
    byte-identical to the input: "No approval ⇒ no stamp AT ALL — a Requested
    by-only stamp is forbidden" (crm_note.py ``_provenance_stamp``). This
    replaced the Tier-2 named-approval leg; if the action returns to Tier 2,
    restore the approver task, the approval spine events and the
    Approved-by/Approval-ID Body stamp assertion (NE-CRM-NOTE-PROVENANCE)."""
    comp = _build(tmp_path)

    async def scenario():
        with run_trace_scope("trace-lg-demo"):
            async with create_connected_server_and_client_session(comp.server.server) as session:
                return await session.call_tool(_CRM_NOTE, _CRM_ARGS)

    result = run(scenario())
    assert result.isError is False, result_text(result)
    # Executed EXACTLY once through the demo connector stub.
    assert len(comp.crm_client.calls) == 1
    # No approval consumed ⇒ the Body carries NO provenance stamp (crm_note.py:
    # a Requested by-only stamp would present the requester as the authoriser).
    _, fields = comp.crm_client.calls[0]
    assert fields["Body"] == _CRM_ARGS["body"]
    rows = _audit_rows(comp.engine)
    actions = [str(r.action) for r in rows]
    for expected in ("pdp.decide", "tool.execute"):
        assert expected in actions, f"missing {expected} in {actions}"
    assert "approval.requested" not in actions
    assert "tool.approval" not in actions
    # tool.execute carries the run trace id.
    execs = [r for r in rows if r.action == "tool.execute" and r.resource == f"tool:{_CRM_NOTE}"]
    assert execs and any(r.trace_id == "trace-lg-demo" for r in execs)


# --- (e2) NE-ACTION-VALIDATE-BEFORE-GATE: the live-defect reproduction -----------


def test_invalid_crm_write_refused_before_execution(tmp_path: Path) -> None:
    """The exact live defect (demo.example.host, 2026-08-03): a CRM-note write
    missing ``record_id`` (only ``request_id``/``title``/``body`` present) must be
    refused BEFORE any effect — never a pending row, never an
    ``approval.requested`` audit event, never a connector call. (The action was
    Tier 2 when the defect fired; at Tier 1 — NE-CS-SEND — validation-before-
    execution is the same obligation with no queue in between.) Control pair:
    :func:`test_tier1_crm_write_executes_once_full_spine` (the SAME tool, valid
    args, executes exactly once — unaffected by this unit)."""
    comp = _build(tmp_path)
    args = {k: v for k, v in _CRM_ARGS.items() if k != "record_id"}

    async def scenario():
        async with create_connected_server_and_client_session(comp.server.server) as session:
            # Bounded (Argus round-1 P2, ne-runtime leg): under a moved-back-post-gate
            # regression the call parks on the REAL DurableApprovalGate with no
            # approver task running, which would otherwise hang this test forever
            # instead of failing it.
            return await asyncio.wait_for(session.call_tool(_CRM_NOTE, args), timeout=5)

    result = run(scenario())
    assert result.isError is True
    text = result_text(result)
    assert "ActionValidationError" in text
    assert "record_id" in text
    assert "NE_MCP_EXECUTION_UNAVAILABLE" not in text

    assert comp.crm_client.calls == []
    with comp.engine.connect() as conn:
        rows = list(conn.execute(sa.select(ne_approval_requests)))
    assert rows == []
    assert "approval.requested" not in _actions(comp.engine)


# --- (f) C12: Tier-3 write rejected ⇒ zero effects -------------------------------


def test_tier3_reject_never_executes(tmp_path: Path) -> None:
    comp = _build(tmp_path)
    result = _drive_write_with_decision(
        comp, _TICKET, _TICKET_ARGS, approve=False, approver="ada.lovelace@corp.example"
    )
    assert result.isError is True
    assert "ApprovalNotGrantedError" in result_text(result)
    assert comp.ticket_client.calls == []
    actions = _actions(comp.engine)
    assert "approval.requested" in actions
    assert "approval.rejected" in actions
    assert "tool.execute" not in actions


# --- (g) NE-ACTION-DETERMINISTIC-REQID: per-instance derived-dedup scope ---------


def test_action_scope_dedups_within_instance_not_across_instances(tmp_path: Path) -> None:
    """NE-ACTION-DETERMINISTIC-REQID: one GovernedMcpServer instance = one derived-dedup id."""
    args = {k: v for k, v in _CRM_ARGS.items() if k != "request_id"}  # no explicit id: derived path
    comp = _build(tmp_path)
    first = _drive_write(comp, _CRM_NOTE, args)
    assert first.isError is False, result_text(first)
    assert json.loads(result_text(first))["detail"] == "created"
    assert len(comp.crm_client.calls) == 1
    # Same instance, same payload: derived key matches -> deduplicated, connector NOT re-called.
    second = _drive_write(comp, _CRM_NOTE, args)
    assert second.isError is False, result_text(second)
    assert json.loads(result_text(second))["detail"] == "deduplicated"
    assert len(comp.crm_client.calls) == 1
    # A DIFFERENT server instance over the SAME executor/dedup store mints a FRESH scope:
    # the identical payload is a real second write (no cross-session over-dedup).
    comp2 = replace(
        comp, server=GovernedMcpServer(comp.scoped, principal=Principal(id="user:lg-demo"))
    )
    third = _drive_write(comp2, _CRM_NOTE, args)
    assert third.isError is False, result_text(third)
    assert json.loads(result_text(third))["detail"] == "created"
    assert len(comp.crm_client.calls) == 2


# --- (h) NE-SCOPE-MCP: two-level action scope via client-signaled _meta ----------


def test_meta_turn_ids_subdivide_the_connection_scope(tmp_path: Path) -> None:
    """A client that signals distinct turn ids over `_meta` gets REAL per-turn dedup:
    turn-a -> created, turn-b -> created (two real writes), turn-a again -> deduplicated."""
    args = {k: v for k, v in _CRM_ARGS.items() if k != "request_id"}  # derived path
    comp = _build(tmp_path)

    first = _drive_write(comp, _CRM_NOTE, args, meta={TURN_META_KEY: "turn-a"})
    assert first.isError is False, result_text(first)
    assert json.loads(result_text(first))["detail"] == "created"
    assert len(comp.crm_client.calls) == 1

    second = _drive_write(comp, _CRM_NOTE, args, meta={TURN_META_KEY: "turn-b"})
    assert second.isError is False, result_text(second)
    assert json.loads(result_text(second))["detail"] == "created"
    assert len(comp.crm_client.calls) == 2

    third = _drive_write(comp, _CRM_NOTE, args, meta={TURN_META_KEY: "turn-a"})
    assert third.isError is False, result_text(third)
    assert json.loads(result_text(third))["detail"] == "deduplicated"
    assert len(comp.crm_client.calls) == 2


def test_same_turn_token_across_connections_does_not_collide(tmp_path: Path) -> None:
    """The connection prefix is the security property: the SAME client-chosen turn
    token, sent by two different GovernedMcpServer instances over the SAME
    executor/dedup store, produces two REAL writes (no cross-session collision)."""
    args = {k: v for k, v in _CRM_ARGS.items() if k != "request_id"}  # derived path
    comp = _build(tmp_path)
    comp2 = replace(
        comp, server=GovernedMcpServer(comp.scoped, principal=Principal(id="user:lg-demo"))
    )

    first = _drive_write(comp, _CRM_NOTE, args, meta={TURN_META_KEY: "turn-shared"})
    assert first.isError is False, result_text(first)
    assert json.loads(result_text(first))["detail"] == "created"

    second = _drive_write(comp2, _CRM_NOTE, args, meta={TURN_META_KEY: "turn-shared"})
    assert second.isError is False, result_text(second)
    assert json.loads(result_text(second))["detail"] == "created"
    assert len(comp.crm_client.calls) == 2


def test_invalid_meta_token_falls_back_to_connection_scope(tmp_path: Path) -> None:
    """An invalid `_meta` turn token is governed (not rejected) and falls back to the
    connection scope: two calls with two DIFFERENT invalid tokens land in the SAME
    scope, so the second call dedups against the first."""
    args = {k: v for k, v in _CRM_ARGS.items() if k != "request_id"}  # derived path
    comp = _build(tmp_path)

    first = _drive_write(comp, _CRM_NOTE, args, meta={TURN_META_KEY: "not ok!"})
    assert first.isError is False, result_text(first)
    assert json.loads(result_text(first))["detail"] == "created"
    assert len(comp.crm_client.calls) == 1

    second = _drive_write(comp, _CRM_NOTE, args, meta={TURN_META_KEY: "also bad!"})
    assert second.isError is False, result_text(second)
    assert json.loads(result_text(second))["detail"] == "deduplicated"
    assert len(comp.crm_client.calls) == 1
