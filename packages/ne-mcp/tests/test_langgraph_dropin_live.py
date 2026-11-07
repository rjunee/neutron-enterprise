# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-LG-DROPIN — the TWO-PROCESS cross-venv governed drop-in proof (run-before-merge).

Opt-in (NE_MCP_LIVE_PROOFS=1) and skip-if-unavailable (`opa` on PATH / sidecar-venv
build), but MUST be RUN GREEN before this unit is called ready-to-merge
(verify-before-merge rule) — it is the first real exercise of
``GovernedMcpServer.run_stdio`` and the only proof no in-memory test can make: a REAL
langgraph agent in the isolated ``.langgraph-venv`` spawns the real ``python -m
ne.mcp.demo_server`` over stdio, discovers exactly the C7-granted toolset across the
venv boundary, and completes the scripted task where the Tier-2 write executes ONLY
after a NAMED approval recorded from THIS process over the shared SQLite store, the
rogue shell.exec is audited-denied, and the Tier-3 ticket update is rejected with zero
effect — then the mixed two-writer audit chain verifies valid (C10).

The PDP is the ``disposable_opa_url`` fixture's own OPA — started by this test run,
serving the shipped bundle from disk, torn down after. This module uploads NOTHING and
names no deployed address; see that fixture's docstring for the 90-second governance
outage the old ``PUT``-into-``NE_GOVERN_OPA_URL`` shape caused on 2026-09-09.

To run:

    NE_MCP_LIVE_PROOFS=1 \
        uv run pytest packages/ne-mcp/tests/test_langgraph_dropin_live.py -q -rs
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, create_engine_with_timeouts, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.envbuild import venv_python
from ne.mcp.langgraph_demo import ensure_langgraph_env
from ne.runtime import create_approval_gate
from ne.runtime.approvals import DurableApprovalGate

_CRM_NOTE = "write.internal.crm.note"
_TICKET = "write.external.ticket.update"
_APPROVER = "ada.lovelace@corp.example"


async def _approval_loop(
    gate: DurableApprovalGate, proc: subprocess.Popen[str], deadline: float
) -> None:
    approved_note = False  # never set: the note is Tier-1 and must never park.
    rejected_ticket = False
    while proc.poll() is None and time.monotonic() < deadline:
        for pending in await gate.list_pending():
            # This branch used to approve the CRM note. NE-CS-SEND (owner decision
            # 2026-08-27) made that write Tier-1, so it must never park -- and the
            # branch is kept as an ACTIVE GUARD rather than deleted, because deleting
            # it would turn that regression into a sidecar timeout: a two-minute hang
            # that reads as flake instead of as the governance change it would be.
            #
            # Kept in the SAME if/elif shape as before deliberately: restructuring it
            # into two flat ``if``s measurably shifts this loop's timing and races the
            # agent subprocess for the SQLite audit-write lock (observed in the rtswap
            # parity twin as ``database is locked`` on the ticket rejection).
            if pending.action == _CRM_NOTE and not approved_note:
                raise AssertionError(
                    "write.internal.crm.note parked for human approval: it is Tier-1 "
                    "(NE-CS-SEND, owner decision 2026-08-27) and must run autonomously"
                )
            elif pending.action == _TICKET and not rejected_ticket:
                await gate.reject(
                    pending.request_id, approver=_APPROVER, reason="live-test rejection"
                )
                rejected_ticket = True
        await asyncio.sleep(0.5)


def _audit_rows(engine: sa.Engine) -> list[sa.Row]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(
                    ne_audit_log.c.action,
                    ne_audit_log.c.resource,
                    ne_audit_log.c.outcome,
                    ne_audit_log.c.payload,
                ).order_by(ne_audit_log.c.seq)
            ).all()
        )


# No xdist group any more: this test brings its OWN PDP (the session-scoped
# `disposable_opa_url` fixture starts one per pytest process, on a kernel-assigned
# loopback port), so there is no shared external server for concurrent workers to
# race policy uploads against — because there are no policy uploads.
def test_langgraph_dropin_two_process_governed(tmp_path: Path, disposable_opa_url: str) -> None:
    # The ne-mcp conftest autouse `frozen_config_hash` fixture already froze this
    # (test) process's config_hash. The server SUBPROCESS freezes its own composed
    # hash independently — the two processes legitimately stamp different config_hash
    # values, which the hash CHAIN (linkage, not hash uniformity) tolerates.
    try:
        _env_dir, venv_dir = ensure_langgraph_env()
    except Exception as exc:
        pytest.skip(f"langgraph sidecar venv unavailable ({exc!r})")

    agent_path = _env_dir / "agent.py"
    python = venv_python(venv_dir)

    db = tmp_path / "lg-live.db"
    engine = create_engine_with_timeouts(f"sqlite:///{db}")
    ensure_audit_schema(engine)
    sink = PostgresAuditSink(engine)
    gate = create_approval_gate(sink=sink, engine=engine)

    env = {
        **os.environ,
        "NE_LG_SERVER_CMD": json.dumps([sys.executable, "-m", "ne.mcp.demo_server"]),
        "NE_LG_DEMO_DB": str(db),
        "NE_GOVERN_OPA_URL": disposable_opa_url,
    }
    proc = subprocess.Popen(
        [str(python), str(agent_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    asyncio.run(_approval_loop(gate, proc, deadline=time.monotonic() + 120))
    out, err = proc.communicate(timeout=180)

    assert proc.returncode == 0, (
        f"agent exited {proc.returncode}; its scripted governance contract did not hold.\n"
        f"--- agent stdout ---\n{out}\n--- agent stderr ---\n{err}"
    )
    # Discovered toolset == the C7 grant (exactly 3), across the venv/process boundary.
    assert "NE_LG_TOOLS=3" in out, f"expected NE_LG_TOOLS=3 in agent stdout:\n{out}"

    rows = _audit_rows(engine)
    actions = [str(r.action) for r in rows]

    def _executed(resource: str) -> bool:
        return any(
            r.action == "tool.execute"
            and r.resource == f"tool:{resource}"
            and r.outcome == "success"
            for r in rows
        )

    # The reads + the autonomous internal write executed; the rogue + rejected did not.
    assert _executed("read.kb.search"), "read.kb.search should have executed"
    assert _executed(_CRM_NOTE), "the autonomous CRM note should have executed"
    assert not any(r.action == "tool.execute" and r.resource == f"tool:{_TICKET}" for r in rows), (
        "the rejected ticket update must NOT have executed"
    )

    # NE-CS-SEND (owner decision 2026-08-27) moved write.internal.crm.note from a
    # Tier-2 OVER-declaration to its real Rego class, Tier-1: an internal write that
    # is not customer-visible runs autonomously per SPEC 4.2's ladder, audit-logged
    # rather than human-gated (crm_note.py, ``tier=ApprovalTier.TIER_1``). So EXACTLY
    # ONE approval is requested here, and it is the Tier-3 ticket.
    #
    # Bound to the resource on purpose. A bare ``count(...) == 1`` would still pass if
    # the two writes swapped roles -- the CRM note re-acquiring a human gate while the
    # Tier-3 ticket went autonomous is the exact regression this file exists to catch,
    # and it is a strictly WORSE posture that a mere count cannot see.
    approval_targets = {str(r.resource) for r in rows if r.action == "approval.requested"}
    assert approval_targets == {f"tool:{_TICKET}"}, (
        "exactly one approval must be requested, and it must be the Tier-3 ticket update; "
        f"got {approval_targets!r}"
    )

    # The guarantee that REPLACES approval for a Tier-1 write is the audit record, so
    # assert it positively rather than merely dropping the approval assertion: the note
    # was decided by the PDP and executed, and no human ever stamped it.
    assert any(r.action == "pdp.decide" and r.resource == f"tool:{_CRM_NOTE}" for r in rows), (
        "the autonomous Tier-1 write must still be PDP-decided and audited"
    )
    assert not any(
        r.action == "tool.approval" and r.resource == f"tool:{_CRM_NOTE}" for r in rows
    ), "a Tier-1 internal write must carry NO human approval stamp"
    assert "approval.rejected" in actions, "the Tier-3 ticket update should have been rejected"
    assert any(
        r.action == "tool.toolset_denied" and r.resource == "tool:shell.exec" for r in rows
    ), "the rogue shell.exec must be audited-denied"

    verification = asyncio.run(sink.verify())
    assert verification.valid, "the two-process audit chain must verify (C10)"
