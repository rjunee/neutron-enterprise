# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""B14 (NE-OBS-U5) — ``tool.execute`` carries the id of the approval it consumed.

THE JOIN IS STAMPED AT THE EXECUTION SITE: ``GovernedToolExecutor`` threads the
``ApprovalOutcome.approval_id`` the gate returned into ``_run_handler``'s
``tool.execute`` emit. It is never reconstructed afterwards by matching on
time, action, or adjacency — an inferred join would be a guess presented as a
fact, in the one product whose claim is that it fabricates nothing.

THE TWO-LEG FIXTURE IS ADVERSARIAL BY DESIGN (mirrors the PR #377 live proof,
SPEC §11 "Live proof, both legs"). It is built to kill two specific wrong
implementations that a naive single-leg test stays green on:

  * a STAMPED CONSTANT — any fixed string passes a key-presence check; the
    exact-value equality with the leg's OWN durable ``request_id`` kills it;
  * LAST-ID-WINS — a "most recently decided approval" global. The fixture
    decides the GRANT first and the DENY second, both before the granted
    leg's parked task resumes, so a last-id-wins stamp carries the DENY
    leg's id onto the granted leg's ``tool.execute`` — failing both the
    equals-own and differs-from-deny assertions.

The decision block's atomicity is ASSERTED, not assumed: after approve+reject
the handler log must still be empty (the granted task provably did not resume
between the two decisions). On sqlite the approval store runs inline
(``ne.audit.engine.default_offload``) and the recording sink never suspends,
so the block cannot yield to the parked tasks; if a future change makes it
yield, the ``log.calls == []`` assertion fails loudly instead of silently
weakening the last-id-wins kill.

Mutation evidence (run manually while building this unit, recorded in the
commit): with the stamped-constant implementation the two-leg test REDs on the
equals-own assertion; with the last-id-wins implementation it REDs on both
join assertions; the pre-existing durable-gate control
(``test_middleware_with_durable_gate.py::
test_require_approval_creates_durable_request_and_tool_does_not_execute``)
stays GREEN under both mutants.

C12 is PROVEN, not asserted: the granted ``tool.execute`` record's provenance
key set is pinned exactly (``{source, component, approval_id}``) so nothing —
no note body, no recipient, no approver identity, no disclosure material —
rides along with the join, and both legs' probe bodies are swept against every
event's full serialization. The approver-on-``tool.approval`` control proves
the key-set pin is not trivially satisfiable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.governance import ApprovalStatus
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.errors import ApprovalNotGrantedError, ToolExecutionError
from ne.runtime.tools import ToolBinding

from .conftest import (
    ALLOWED_RECIPIENT,
    NamedApproverGate,
    RecordingAuditSink,
    ToolLog,
    UnusedGate,
    approval_settings,
    make_executor,
    make_principal,
    standard_bindings,
)

#: Planted C12 probes — material argument values that must never appear in any
#: audit event's serialization (the join carries an approval ID, nothing else).
PROBE_GRANT = "NE-B14-GRANT-PROBE-4e1f"
PROBE_DENY = "NE-B14-DENY-PROBE-9c2a"


def _wire(sink: Any) -> tuple[SqlAlchemyApprovalStore, DurableApprovalGate, ToolLog, Any]:
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    gate = DurableApprovalGate(store=store, sink=sink, settings=approval_settings())
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)
    return store, gate, log, executor


def _exec_events(sink: RecordingAuditSink) -> list[Any]:
    return sink.with_action("tool.execute")


def _prov(event: Any) -> dict[str, str]:
    return dict(event.provenance)


def test_two_leg_join_grant_executes_with_its_own_approval_id() -> None:
    """THE GUARD. Granted leg's ``tool.execute.approval_id`` equals its OWN
    durable approval's id and differs from the deny leg's; the deny leg never
    executes at all (no ``tool.execute`` anywhere on it)."""
    sink = RecordingAuditSink()
    store, gate, log, executor = _wire(sink)
    principal = make_principal()

    async def scenario() -> tuple[str, str]:
        task_grant = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": PROBE_GRANT},
                principal=principal,
            )
        )
        task_deny = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": PROBE_DENY},
                principal=principal,
            )
        )
        while len(await store.list_pending()) < 2:
            await asyncio.sleep(0)
        assert log.calls == []  # nothing executed pre-decision

        # Map each pending row to its leg by the governed disclosure read —
        # the recorded material, never a guess from ordering.
        grant_rid: str | None = None
        deny_rid: str | None = None
        for row in await store.list_pending():
            disclosure = await gate.read_payload(row.request_id, approver="carol")
            body = (disclosure.material_parameters or {}).get("body")
            if body == PROBE_GRANT:
                grant_rid = row.request_id
            elif body == PROBE_DENY:
                deny_rid = row.request_id
        assert grant_rid is not None and deny_rid is not None

        # THE LAST-ID-WINS KILL ORDER: decide the grant FIRST, the deny SECOND,
        # atomically w.r.t. the two parked executor tasks (see module docstring).
        await gate.approve(grant_rid, approver="carol")
        await gate.reject(deny_rid, approver="carol", reason="not authorised")
        # Atomicity asserted, not assumed: the granted task has NOT resumed yet,
        # so at its eventual execute the most recent decision is the DENY.
        assert log.calls == []

        result = await asyncio.wait_for(task_grant, timeout=10)
        assert result == "sent"
        try:
            await asyncio.wait_for(task_deny, timeout=10)
            raise AssertionError("deny leg executed")
        except ApprovalNotGrantedError as exc:
            assert exc.status is ApprovalStatus.REJECTED
        return grant_rid, deny_rid

    grant_rid, deny_rid = asyncio.run(scenario())

    # Fixture non-degeneracy control: the discrimination is real.
    assert grant_rid != deny_rid
    assert log.names() == ["write.external.email.send"]

    # Exactly ONE execution in the whole run — the deny leg has no
    # tool.execute anywhere (mirrors the PR #377 live-proof shape).
    execs = _exec_events(sink)
    assert len(execs) == 1
    assert execs[0].outcome == "success"

    prov = _prov(execs[0])
    # THE JOIN: equality with the leg's OWN approval id (kills a stamped
    # constant), and difference from the deny leg's (names the last-id-wins
    # failure — under that mutant BOTH assertions fire, prov == deny_rid).
    assert prov["approval_id"] == grant_rid
    assert prov["approval_id"] != deny_rid

    # C12 PROVEN: the join adds an id and NOTHING else to tool.execute.
    assert set(prov) == {"source", "component", "approval_id"}
    dump = execs[0].model_dump_json()
    assert "carol" not in dump  # approver identity does not ride the join

    # Both legs' tool.approval records carry their OWN approval id (the same
    # recorded fact, stamped at the same site — the deny leg's trace record
    # names the approval that REFUSED it).
    approvals = sink.with_action("tool.approval")
    granted_approvals = [e for e in approvals if e.outcome == "success"]
    denied_approvals = [e for e in approvals if e.outcome == "denied"]
    assert len(granted_approvals) == 1 and len(denied_approvals) == 1
    assert _prov(granted_approvals[0])["approval_id"] == grant_rid
    assert _prov(denied_approvals[0])["approval_id"] == deny_rid
    # Control for the tool.execute key-set pin: tool.approval DOES carry the
    # approver — the pin above is not satisfied by everything.
    assert _prov(granted_approvals[0])["approver"] == "carol"

    # C12 probe sweep: neither leg's material body appears in ANY event.
    for event in sink.events:
        blob = str(event)
        model_dump = getattr(event, "model_dump_json", None)
        if callable(model_dump):
            blob += str(model_dump())
        assert PROBE_GRANT not in blob
        assert PROBE_DENY not in blob


def test_ungated_execution_carries_no_approval_id_key() -> None:
    """ABSENCE GUARD (bidirectional with the two-leg test): a tier-0/1 call
    that never consulted the gate emits ``tool.execute`` with NO
    ``approval_id`` key — omitted, never ``"None"``, never invented."""
    sink = RecordingAuditSink()
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    asyncio.run(
        executor.execute(
            "write.internal.notes.append", {"note": "routine"}, principal=make_principal()
        )
    )

    execs = _exec_events(sink)
    assert len(execs) == 1
    prov = _prov(execs[0])
    assert "approval_id" not in prov
    assert set(prov) == {"source", "component"}
    assert "None" not in prov.values()


def test_gate_without_durable_id_stamps_nothing() -> None:
    """A protocol-typed gate whose outcome records no durable id (the
    pre-1.16.0 producer shape) yields a ``tool.execute`` with NO
    ``approval_id`` key — the executor never fabricates an id the gate did
    not record."""
    sink = RecordingAuditSink()
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=NamedApproverGate("alice"))

    asyncio.run(
        executor.execute(
            "write.external.email.send",
            {"to": ALLOWED_RECIPIENT, "body": "hello"},
            principal=make_principal(),
        )
    )

    execs = _exec_events(sink)
    assert len(execs) == 1
    assert "approval_id" not in _prov(execs[0])


def test_error_execution_still_carries_the_consumed_approval_id() -> None:
    """A granted approval whose handler then RAISES still authorised the
    attempt: ``tool.execute[error]`` carries the same recorded join."""
    sink = RecordingAuditSink()
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    gate = DurableApprovalGate(store=store, sink=sink, settings=approval_settings())
    log = ToolLog()
    bindings = [b for b in standard_bindings(log) if b.spec.name != "write.external.email.send"]
    failing = next(b for b in standard_bindings(log) if b.spec.name == "write.external.email.send")
    bindings.append(
        ToolBinding(
            spec=failing.spec,
            handler=log.raising_handler("write.external.email.send", RuntimeError("smtp down")),
            extract_recipients=failing.extract_recipients,
        )
    )
    executor = make_executor(bindings, sink, approval_gate=gate)
    principal = make_principal()

    async def scenario() -> str:
        task = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        rid = (await store.list_pending())[0].request_id
        await gate.approve(rid, approver="carol")
        with pytest.raises(ToolExecutionError):
            await asyncio.wait_for(task, timeout=10)
        return rid

    rid = asyncio.run(scenario())

    execs = _exec_events(sink)
    assert len(execs) == 1
    assert execs[0].outcome == "error"
    assert _prov(execs[0])["approval_id"] == rid
