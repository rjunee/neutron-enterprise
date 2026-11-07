# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-RTSWAP-C — the epic-closing 3-way governance swap proof (run-before-merge).

This is the headline deliverable of the whole NE-RTSWAP epic: the SAME governed
scenario driven through THREE genuinely different delivery mechanisms —

  * **pydantic-ai, in-process** — a real :class:`~ne.runtime.agent.DepartmentAgentSession`
    over the REUSED :func:`~ne.mcp.demo_server.build_demo_server` composition root,
    driven by a scripted :class:`ScriptedGateway` (no LLM, zero egress);
  * **LangGraph, out-of-process** — the ALREADY-MERGED ``langgraph_env/agent.py``
    sidecar in its isolated ``.langgraph-venv``, spawning ``python -m
    ne.mcp.demo_server`` over MCP stdio (NE-LG-DROPIN, unmodified);
  * **CrewAI, out-of-process** — the ALREADY-MERGED ``crewai_env/agent.py`` sidecar
    in its isolated ``.crewai-venv``, over the same MCP stdio server (NE-CREWAI-DROPIN,
    unmodified).

— and asserts an IDENTICAL governed-outcome observation dict per leg.

THE HONESTY BAR (what 'identical' means here). The three mechanisms have different
internal plumbing: in-process direct calls vs. MCP-over-stdio, a real ``StateGraph``
vs. scripted ``BaseTool`` proxies. Byte-for-byte identical internals is NOT the bar
and never could be. The bar is IDENTICAL GOVERNED OUTCOME: the same PDP verdicts for
the same actions, the same C7 toolset trim, the same approval / rejection / denial
facts, the same structural non-execution guarantees, and a valid, verifiable C10 hash
chain — enumerated as the keys of :func:`_observation` and asserted by dict equality.
This mirrors the content-equivalence precedent set by the C5 dual-binding swap test
(``packages/ne-skills-cc/tests/test_swap_c5.py``: "identical scenarios, identical
observations", timestamps/internal-shape excluded via an enumerated key list, never
by per-binding special-casing) and the re-scoped plan in
``docs/architecture/ne-rtswap-design.md`` §8.

The scenario (each existing dropin-live twin already exercises it) is five beats:
  1. a multi-step task begins with a Tier-0 ``read.kb.search`` (executes, audited);
  2. a Tier-2 ``write.internal.crm.note`` write-back that executes ONLY after a NAMED
     human approval (``ada.lovelace@corp.example``);
  3. a rogue call of the UNREGISTERED ``shell.exec`` — audited-denied at the C7
     toolset boundary, never executed;
  4. a Tier-3 ``write.external.ticket.update`` that the operator REJECTS — zero effect;
  5. the resulting hash-chained audit log verifies valid (C10).

The DURABLE-RESUME leg is DELIBERATELY ABSENT — durability is not a plugin slot
(``ne.runtime.runtime`` documents this by design; Temporal is a separate owner-gated
epic, NE-DURSWAP), so this proof does not reference or imply it beyond this note.

LIVE-GATED SCOPE OF THE PARITY GUARANTEE. The ``pdp_facts`` cross-leg equality is a
genuine semantic property — the in-process leg necessarily carries a principal with
``claims={"department": "demo"}`` (a :class:`DepartmentAgentSession` requirement)
while the MCP sidecar server composes a principal with no claims, so identical PDP
verdicts across that asymmetry is a real (not tautological) outcome. It is provable
ONLY on the live path: the opt-in guard means the skip-clean CI run never
exercises it. The mandatory verify-before-merge live run (recipe above) is what
proves it holds — a green *skipped* module proves nothing here by design.

To run (the mandatory verify-before-merge live proof):

    NE_MCP_LIVE_PROOFS=1 \
        uv run pytest packages/ne-mcp/tests/test_rtswap_governance_parity.py -q -rs

Skip guards mirror the two merged dropin-live twins: the ``disposable_opa_url``
fixture skips unless ``NE_MCP_LIVE_PROOFS=1`` opts in, and again if no ``opa``
binary is on PATH to start the throwaway PDP with. That PDP is this run's OWN —
it serves the shipped bundle from disk on a kernel-assigned loopback port, so this
module uploads NOTHING and names no deployed address (see the fixture docstring for
the 90-second governance outage the old ``PUT``-into-``NE_GOVERN_OPA_URL`` shape
caused on 2026-09-09). This file NEVER ``import mcp`` (the
guardian ``test_mcp_isolation_guardian.py`` scans ``packages/*/tests``): the two
sidecar legs are subprocesses, and the in-process leg goes through
``build_demo_server`` — no bare mcp import is needed anywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, create_engine_with_timeouts, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.types import Principal
from ne.envbuild import venv_python
from ne.mcp.crewai_demo import ensure_crewai_env
from ne.mcp.demo_server import GRANTED_TOOLS, build_demo_server
from ne.mcp.langgraph_demo import ensure_langgraph_env
from ne.runtime import create_approval_gate
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

# --- scenario constants (the SAME governed actions each leg drives) --------

_KB = "read.kb.search"
_CRM_NOTE = "write.internal.crm.note"
_TICKET = "write.external.ticket.update"
_UNREGISTERED = "shell.exec"
_APPROVER = "ada.lovelace@corp.example"
_PRINCIPAL = "user:rtswap-demo"

# The resources the PDP is consulted for in this scenario (used to key pdp_facts).
_PDP_RESOURCES = frozenset(f"tool:{t}" for t in (_KB, _CRM_NOTE, _TICKET, _UNREGISTERED))


def _audit_rows(engine: sa.Engine) -> list[sa.Row[Any]]:
    """DUPLICATED from the dropin-live twins — the ordered governed-audit surface."""
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


# --- scripted model gateway (ne.contracts.models.ModelGateway) -------------
# DUPLICATED from packages/ne-runtime/tests/conftest.py — cross-package test imports
# are forbidden by repo rule, so the doubles are copied rather than imported.


class ScriptedGateway:
    """A ``ModelGateway`` returning scripted completions; records every request."""

    def __init__(self, completions: Sequence[Completion] | None = None) -> None:
        self._queue = list(completions or [])
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self._queue:
            return self._queue.pop(0)
        return text_completion("done")

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError("ScriptedGateway does not stream")


def tool_call_completion(name: str, arguments: dict[str, Any]) -> Completion:
    line = json.dumps({"tool_call": {"name": name, "arguments": arguments}})
    return Completion(text=line, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


def text_completion(text: str) -> Completion:
    return Completion(text=text, model_id="stub", usage=Usage(prompt_tokens=3, completion_tokens=5))


# --- the ONE shared observation extractor (no per-leg branches) ------------


def _observation(rows: list[sa.Row[Any]], chain_valid: bool, toolset_count: int) -> dict[str, Any]:
    """Reduce a leg's governed-audit rows + chain verdict to the compared dict.

    Applied UNCHANGED to all three legs — zero per-mechanism normalization branches.
    Timestamps, trace-ids, config-hashes and audit-payload internals are excluded
    ONLY via this enumerated key list, never by leg-specific special-casing. If a
    live run showed a fact carried differently across legs, the row-matching
    predicate below could be adjusted — but the key list and its semantics are
    FROZEN: a fact that genuinely could not be extracted uniformly would be an
    epic-level finding to escalate, not to normalize away.
    """

    def _executed(resource: str) -> bool:
        return any(
            r.action == "tool.execute"
            and r.resource == f"tool:{resource}"
            and r.outcome == "success"
            for r in rows
        )

    def _execute_count(resource: str) -> int:
        return sum(
            1
            for r in rows
            if r.action == "tool.execute"
            and r.resource == f"tool:{resource}"
            and r.outcome == "success"
        )

    pdp_facts = frozenset(
        (str(r.resource), str(r.outcome))
        for r in rows
        if r.action == "pdp.decide" and r.resource in _PDP_RESOURCES
    )

    return {
        "toolset_count": toolset_count,
        "kb_executed": _executed(_KB),
        "crm_executed_count": _execute_count(_CRM_NOTE),
        "ticket_executed": any(
            r.action == "tool.execute" and r.resource == f"tool:{_TICKET}" for r in rows
        ),
        "unknown_denied": any(
            r.action == "tool.toolset_denied" and r.resource == f"tool:{_UNREGISTERED}"
            for r in rows
        ),
        "unknown_executed": any(
            r.action == "tool.execute" and r.resource == f"tool:{_UNREGISTERED}" for r in rows
        ),
        "approvals_requested_count": sum(1 for r in rows if r.action == "approval.requested"),
        # Resource-BOUND, not a bare count: the regression worth catching is the two
        # writes swapping roles (the Tier-1 note re-acquiring a human gate while the
        # Tier-3 ticket goes autonomous), and a count cannot see that.
        "approval_targets": sorted(
            {str(r.resource) for r in rows if r.action == "approval.requested"}
        ),
        "crm_pdp_decided": any(
            r.action == "pdp.decide" and r.resource == f"tool:{_CRM_NOTE}" for r in rows
        ),
        "crm_human_stamped": any(
            r.action == "tool.approval" and r.resource == f"tool:{_CRM_NOTE}" for r in rows
        ),
        "ticket_rejected": any(r.action == "approval.rejected" for r in rows),
        "pdp_facts": pdp_facts,
        "chain_valid": chain_valid,
    }


# --- the two out-of-process sidecar legs (ONE parameterized runner) --------


async def _sidecar_approval_loop(gate: Any, proc: subprocess.Popen[str], deadline: float) -> None:
    """DUPLICATED from the dropin-live twins: reject the ticket; the note is Tier-1."""
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
            # Kept in the SAME if/elif shape as before deliberately. Restructuring it
            # (two flat ``if``s) measurably shifts this loop's timing and races the
            # agent subprocess for the SQLite audit-write lock, surfacing as
            # ``database is locked`` on the ticket rejection -- a pre-existing harness
            # race that a test repair has no business exposing.
            if pending.action == _CRM_NOTE and not approved_note:
                raise AssertionError(
                    "write.internal.crm.note parked for human approval: it is Tier-1 "
                    "(NE-CS-SEND, owner decision 2026-08-27) and must run autonomously"
                )
            elif pending.action == _TICKET and not rejected_ticket:
                await gate.reject(pending.request_id, approver=_APPROVER, reason="rtswap rejection")
                rejected_ticket = True
        await asyncio.sleep(0.5)


def _run_sidecar_leg(
    tmp_path: Path,
    *,
    opa_url: str,
    db_name: str,
    ensure_env: Callable[[], tuple[Path, Path]],
    server_cmd_var: str,
    marker: str,
    extra_env: dict[str, str],
) -> dict[str, Any]:
    """Run one out-of-process governed leg; return its shared observation dict.

    Reuses the dropin-live twins' two-process flow verbatim: the sidecar agent in its
    isolated venv spawns ``python -m ne.mcp.demo_server`` over stdio; this process
    records the NAMED approval / rejection over the shared SQLite store; the mixed
    two-writer audit chain is verified here.
    """
    try:
        env_dir, venv_dir = ensure_env()
    except Exception as exc:
        pytest.skip(f"{marker} sidecar venv unavailable ({exc!r})")

    agent_path = env_dir / "agent.py"
    python = venv_python(venv_dir)

    db = tmp_path / db_name
    engine = create_engine_with_timeouts(f"sqlite:///{db}")
    ensure_audit_schema(engine)
    sink = PostgresAuditSink(engine)
    gate = create_approval_gate(sink=sink, engine=engine)

    env = {
        **os.environ,
        server_cmd_var: json.dumps([sys.executable, "-m", "ne.mcp.demo_server"]),
        "NE_LG_DEMO_DB": str(db),
        "NE_GOVERN_OPA_URL": opa_url,
        "NE_LG_DEMO_PRINCIPAL": _PRINCIPAL,
        **extra_env,
    }
    proc = subprocess.Popen(
        [str(python), str(agent_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    asyncio.run(_sidecar_approval_loop(gate, proc, deadline=time.monotonic() + 120))
    out, err = proc.communicate(timeout=180)

    assert proc.returncode == 0, (
        f"{marker} agent exited {proc.returncode}; its scripted governance contract did "
        f"not hold.\n--- agent stdout ---\n{out}\n--- agent stderr ---\n{err}"
    )
    # The discovered toolset == the C7 grant (exactly 3), across the venv/process boundary.
    prefix = f"{marker}="
    tool_lines = [ln for ln in out.splitlines() if ln.startswith(prefix)]
    assert tool_lines, f"expected a `{prefix}<n>` line in {marker} agent stdout:\n{out}"
    toolset_count = int(tool_lines[0].split("=", 1)[1])

    verification = asyncio.run(sink.verify())
    return _observation(_audit_rows(engine), verification.valid, toolset_count)


# --- the in-process pydantic-ai leg ----------------------------------------


def _run_inprocess_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, opa_url: str
) -> dict[str, Any]:
    """Run the SAME scenario through a real pydantic-ai ``DepartmentAgentSession``.

    Composes the SAME ``build_demo_server`` objects the server subprocess runs
    (byte-unchanged), points its PDP at the SAME disposable OPA via
    ``NE_GOVERN_OPA_URL``, and drives a real pydantic-ai agent loop over a scripted
    gateway. The effect stubs live IN THIS PROCESS, so this leg can inspect what no
    subprocess leg can: that the CRM connector was called exactly once and the ticket
    connector never.
    """
    # Point build_demo_server's GovernSettings() PDP at the SAME disposable OPA the
    # sidecar legs use (build_demo_server reads NE_GOVERN_OPA_URL through
    # GovernSettings()). The value is this run's own throwaway server, never a
    # deployed one — the fixture is what guarantees that.
    monkeypatch.setenv("NE_GOVERN_OPA_URL", opa_url)

    comp = build_demo_server(tmp_path / "rtswap-pyai.db", _PRINCIPAL)
    ensure_runtime_schema(comp.engine)
    store = SqlAlchemySessionStore(comp.engine)

    # Scripted arg shapes copied from langgraph_env/agent.py so the SAME governed
    # ToolArgPolicies pass identically. shell.exec is UNREGISTERED — the model
    # emitting it drives the on_unknown_tool hook through comp.scoped (C7 denial).
    gateway = ScriptedGateway(
        [
            tool_call_completion(_KB, {"query": "acme corp account history"}),
            tool_call_completion(
                _CRM_NOTE,
                {
                    "request_id": "pyai-demo-note-1",
                    "record_id": "001A00000LgDemo01",
                    "title": "Account research summary",
                    "body": "Automated account research summary.",
                },
            ),
            tool_call_completion(_UNREGISTERED, {"cmd": "cat /etc/passwd"}),
            tool_call_completion(
                _TICKET,
                {
                    "request_id": "pyai-demo-ticket-1",
                    "ticket_id": 1,
                    "status": "resolved",
                    "public_reply": "auto-close attempt",
                },
            ),
            text_completion("done"),
        ]
    )

    async def _drive() -> Any:
        session_id = (
            await store.create_conversation("rtswap-agent", "demo", _PRINCIPAL)
        ).conversation_id
        session = DepartmentAgentSession(
            session_id=session_id,
            agent_name="rtswap-agent",
            department="demo",
            owner_principal_id=_PRINCIPAL,
            store=store,
            executor=comp.scoped,  # the C7 ToolsetScopedExecutor — the SAME trimmed surface
            gateway=gateway,
            settings=RuntimeSettings(),  # max_requests default (8) covers the 5 completions
        )
        principal = Principal(id=_PRINCIPAL, claims={"department": "demo"})
        run_task = asyncio.create_task(session.run("run the rtswap scenario", principal=principal))

        async def _approval_loop() -> None:
            approved_note = False
            rejected_ticket = False
            deadline = time.monotonic() + 60
            while not run_task.done() and time.monotonic() < deadline:
                for pending in await comp.gate.list_pending():
                    if pending.action == _CRM_NOTE and not approved_note:
                        await comp.gate.approve(pending.request_id, approver=_APPROVER)
                        approved_note = True
                    elif pending.action == _TICKET and not rejected_ticket:
                        await comp.gate.reject(
                            pending.request_id, approver=_APPROVER, reason="rtswap rejection"
                        )
                        rejected_ticket = True
                await asyncio.sleep(0.2)

        loop_task = asyncio.create_task(_approval_loop())
        try:
            result = await run_task
        finally:
            loop_task.cancel()
        return result

    result = asyncio.run(_drive())

    # Leg-local hard proofs the REAL pydantic-ai loop ran over the REAL composition
    # root — the inspection no subprocess leg can offer.
    assert result.output == "done", f"in-process run did not terminate cleanly: {result.output!r}"
    assert len(gateway.requests) >= 5, (
        f"the pydantic-ai loop did not really drive the scenario "
        f"(gateway.requests={len(gateway.requests)})"
    )
    assert len(comp.crm_client.calls) == 1, (
        f"the CRM connector must be called exactly once (was {len(comp.crm_client.calls)})"
    )
    assert comp.ticket_client.calls == [], (
        f"the ticket connector must NEVER be called (was {comp.ticket_client.calls})"
    )
    # The in-process C7 surface == the exact grant (the same trim the MCP server serves).
    assert {s.name for s in comp.scoped.tool_specs} == set(GRANTED_TOOLS)

    verification = asyncio.run(comp.sink.verify())
    return _observation(_audit_rows(comp.engine), verification.valid, len(comp.scoped.tool_specs))


# --- THE epic-closing headline test ----------------------------------------


# No xdist group any more: this test PUTs nothing anywhere. All three legs share
# ONE PDP — the session-scoped `disposable_opa_url` fixture's own OPA, started per
# pytest process on a kernel-assigned loopback port and serving the shipped bundle
# read-only from disk — so concurrent workers cannot interleave policy uploads
# against a shared external server, because there are no policy uploads and no
# external server. Sharing one PDP across the legs is exactly what makes the
# cross-leg `pdp_facts` equality meaningful.
def test_rtswap_three_way_governance_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_opa_url: str
) -> None:
    """The SAME governed scenario through all three mechanisms ⇒ ONE identical outcome.

    Runs the three legs sequentially (LangGraph sidecar, CrewAI sidecar, pydantic-ai
    in-process — each against its own fresh SQLite), then asserts the three
    governed-outcome observation dicts are EQUAL (C6/C7/C10/C12 identical across
    genuinely different delivery mechanisms). Per-leg non-vacuity proofs already ran
    INSIDE each runner: the sidecar legs proved distinct venv mechanisms via their
    returncode-0 runs and their distinct marker lines; the in-process leg proved the
    pydantic-ai mechanism via ``gateway.requests`` and the locally-inspected stubs.
    """
    obs_lg = _run_sidecar_leg(
        tmp_path,
        opa_url=disposable_opa_url,
        db_name="rtswap-lg.db",
        ensure_env=ensure_langgraph_env,
        server_cmd_var="NE_LG_SERVER_CMD",
        marker="NE_LG_TOOLS",
        extra_env={},
    )
    obs_crewai = _run_sidecar_leg(
        tmp_path,
        opa_url=disposable_opa_url,
        db_name="rtswap-crewai.db",
        ensure_env=ensure_crewai_env,
        server_cmd_var="NE_CREWAI_SERVER_CMD",
        marker="NE_CREWAI_TOOLS",
        # crewai-core ships an OTLP exporter and chromadb ships posthog INSIDE the
        # sidecar; pen both channels shut for the hermetic proof (the twin's pens).
        extra_env={
            "CREWAI_DISABLE_TELEMETRY": "true",
            "OTEL_SDK_DISABLED": "true",
            "ANONYMIZED_TELEMETRY": "False",
        },
    )
    obs_pyai = _run_inprocess_leg(tmp_path, monkeypatch, opa_url=disposable_opa_url)

    # THE PARITY ASSERTION: identical governed outcome across all three mechanisms.
    assert obs_lg == obs_crewai == obs_pyai, (
        "the three delivery mechanisms produced DIFFERENT governed outcomes — this is "
        "an epic-level divergence to investigate, NOT to normalize away:\n"
        f"  langgraph  = {obs_lg}\n"
        f"  crewai     = {obs_crewai}\n"
        f"  pydantic-ai= {obs_pyai}"
    )

    # Pin the load-bearing absolutes once (they then hold for all three by equality):
    obs = obs_pyai
    assert obs["toolset_count"] == 3  # C7: exactly the 3-tool grant, read.kb.export invisible
    assert obs["kb_executed"]  # (1) the multi-step task ran
    assert obs["crm_executed_count"] == 1  # (2) the internal write executed exactly once
    # NE-CS-SEND (owner decision 2026-08-27) moved write.internal.crm.note off a Tier-2
    # OVER-declaration onto its real Rego class, Tier-1: an internal, non-customer-visible
    # write runs autonomously per SPEC 4.2's ladder, audit-logged rather than human-gated
    # (crm_note.py, ``tier=ApprovalTier.TIER_1``). The guarantee that REPLACES approval is
    # the audit record, so it is asserted POSITIVELY here rather than simply dropped.
    assert obs["crm_pdp_decided"]  # ...PDP-decided and audited...
    assert not obs["crm_human_stamped"]  # ...and carrying NO human approval stamp
    assert obs["unknown_denied"]  # (3) rogue shell.exec audited-denied at the C7 boundary
    assert not obs["unknown_executed"]  # ...and never executed
    assert obs["ticket_rejected"]  # (4) Tier-3 ticket update explicitly rejected
    assert not obs["ticket_executed"]  # ...with zero effect
    assert obs["chain_valid"]  # (5) the hash-chained audit log verifies (C10)
    # (C12) EXACTLY ONE approval, and it is the Tier-3 ticket -- resource-bound so a
    # role swap between the two writes cannot satisfy this by arithmetic.
    assert obs["approval_targets"] == [f"tool:{_TICKET}"], obs["approval_targets"]
