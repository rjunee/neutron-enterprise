# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CRM-NOTE-PROVENANCE — the written CRM note names who approved it.

Every governed write reaches Salesforce as ONE integration identity, so the
CRM's own ``CreatedBy`` can never name the human who authorised it. These
guards pin the fix: with the executor's carriers injected
(``approval_provider=current_tool_approval`` /
``principal_provider=current_tool_principal`` — the composition-root wiring in
``ne-api/platform.py`` and ``ne-mcp/demo_server.py``), the POSTed ``Note.Body``
carries a trailing stamp naming the GATE-RECORDED approver, the durable
approval id, and the requesting principal — and nothing else.

Full real-governance harness (test_reference_actions.py house style): real
``GovernedToolExecutor`` + rego-faithful PDP + real guardrail engine + (where
the durable id matters) the real ``DurableApprovalGate`` over sqlite; only the
Salesforce HTTP wire is a MockTransport.

Guard directions (each bidirectional — see the module's mutation ledger in the
unit's commit message):

* the approved leg's body ends with its OWN durable approval id and the
  gate-recorded approver — with the approver and the requester DISTINCT by
  fixture construction, so a mutant stamping the requester where the approver
  belongs ("Approved by: user:ada") goes RED on exact equality, as does a
  stamped constant id;
* an unwired definition (today's default) and a wired-but-ungoverned
  invocation both produce a BYTE-IDENTICAL body — no stamp, no marker;
* the 4000-char ``Note.Body`` cap holds with the stamp attached, pinned one
  char each side of the boundary, stamp intact, body tail visibly elided;
* a gate that records no durable id yields a stamp with the Approval-ID
  segment OMITTED — never ``None``, never a placeholder;
* nothing beyond the three identifiers crosses into the wire body: principal
  claims/groups sentinels are swept ABSENT against the raw POST bytes while
  the requester-id/approver/approval-id sentinels are asserted PRESENT on the
  same bytes (the presence control proving the sweep reads the real wire).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import sqlalchemy as sa

from ne.contracts.governance import ApprovalOutcome, ApprovalRequest, ApprovalStatus
from ne.contracts.types import Principal
from ne.runtime.action_tools import (
    ActionDedupStore,
    build_action_binding,
    ensure_action_dedup_schema,
)
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.middleware import (
    GovernedToolExecutor,
    current_tool_approval,
    current_tool_principal,
)
from ne.runtime.reference_actions import crm_note_definition

from ._write_fixtures import (
    SF_RECORD_ID,
    salesforce_write_client,
    write_guardrails,
)
from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    approval_settings,
    make_pdp,
    make_principal,
)

TOOL = "write.internal.crm.note"
BODY = "Called the customer."
STAMP_HEAD = "[Governed by Neutron] "

#: The requester and the approver are DIFFERENT people BY FIXTURE DESIGN —
#: Tier-2 exists precisely so the asker is not the authoriser. Every stamp
#: assertion below leans on this distinctness to kill the mislabel mutant.
REQUESTER = "user:ada"
APPROVER = "approver:alice"


class _FixedIdGate:
    """APPROVED with a named approver AND a FIXED durable-style approval id.

    Protocol-typed (not a ``DurableApprovalGate``), so stamp lengths are
    deterministic for the boundary pins below.
    """

    def __init__(self, approver: str, approval_id: str) -> None:
        self.approver = approver
        self.approval_id = approval_id

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome(
            status=ApprovalStatus.APPROVED, approver=self.approver, approval_id=self.approval_id
        )


def _wired_definition(client: Any) -> Any:
    """The definition AS THE COMPOSITION ROOTS BUILD IT — carriers injected."""
    return crm_note_definition(
        client,
        approval_provider=current_tool_approval,
        principal_provider=current_tool_principal,
    )


def _dedup() -> ActionDedupStore:
    engine = sa.create_engine("sqlite://")
    ensure_action_dedup_schema(engine)
    return ActionDedupStore(engine)


#: NE-CS-SEND: `write.internal.crm.note` is Tier-1 (autonomous) now, so the
#: approval these stamp tests exercise no longer arises from the tier gate. The
#: stamp contract is about what happens WHEN an approval is consumed — e.g. a
#: deployment PDP escalating the write (design §6.3) — so these tests force that
#: with a decision override rather than quietly losing their subject.
_ESCALATED = {
    "effect": "require-approval",
    "tier": 2,
    "reason": "escalated by deployment policy",
    "obligations": {},
    "policy_version": "1.0.0",
}


def _executor(
    definition: Any,
    sink: Any,
    *,
    gate: Any,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> GovernedToolExecutor:
    return GovernedToolExecutor(
        [build_action_binding(definition, dedup=_dedup())],
        pdp=make_pdp(sink, overrides=overrides),
        guardrails=write_guardrails(sink),
        approval_gate=gate,
        sink=sink,
    )


def _durable_gate(sink: Any) -> tuple[SqlAlchemyApprovalStore, DurableApprovalGate]:
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    return store, DurableApprovalGate(
        store=store, sink=sink, settings=approval_settings(poll_interval_s=0.0)
    )


def _posted_note_fields(recorder: list[httpx.Request]) -> dict[str, str]:
    posts = [r for r in recorder if r.url.path.endswith("/sobjects/Note")]
    assert len(posts) == 1, f"expected exactly one Note POST, saw {len(posts)}"
    fields = json.loads(posts[0].content.decode())
    assert isinstance(fields, dict)
    return fields


def _args(request_id: str, body: str = BODY) -> dict[str, str]:
    return {
        "request_id": request_id,
        "record_id": SF_RECORD_ID,
        "title": "Follow-up",
        "body": body,
    }


# --- (1) THE GUARD: the approved write is self-describing --------------------------


def test_approved_write_stamps_gate_recorded_approver_and_own_durable_approval_id() -> None:
    """Body ends with ``Approved by: <gate-recorded approver> | Approval ID: <its
    OWN durable id> | Requested by: <requesting principal>``.

    Exact-equality against the leg's own durable ``request_id`` kills a stamped
    constant; exact-equality on the approver segment — with ``APPROVER !=
    REQUESTER`` asserted as a fixture precondition — kills the mutant that
    stamps the requester where the approver belongs (the worse-than-the-gap
    mislabel defect this unit's brief forbids)."""
    assert APPROVER != REQUESTER  # the mislabel kill relies on this distinctness
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = _wired_definition(salesforce_write_client(recorder=recorder))
    store, gate = _durable_gate(sink)
    executor = _executor(definition, sink, gate=gate, overrides={TOOL: _ESCALATED})

    async def scenario() -> str:
        task = asyncio.create_task(
            executor.execute(TOOL, _args("req-provstamp1"), principal=make_principal(REQUESTER))
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        rid = (await store.list_pending())[0].request_id
        await gate.approve(rid, approver=APPROVER)
        await asyncio.wait_for(task, timeout=10)
        return rid

    rid = asyncio.run(scenario())

    expected_body = (
        f"{BODY}\n\n{STAMP_HEAD}"
        f"Approved by: {APPROVER} | Approval ID: {rid} | Requested by: {REQUESTER}"
    )
    # Exact-dict equality doubles as the field-set pin: ParentId/Title/Body and
    # NOTHING else crosses the wire.
    assert _posted_note_fields(recorder) == {
        "ParentId": SF_RECORD_ID,
        "Title": "Follow-up",
        "Body": expected_body,
    }
    # The carrier is reset once the governed call returns (same lifecycle as the
    # D-1 principal carrier).
    assert current_tool_approval() is None


# --- (2) no approval ⇒ no stamp, byte-identical ------------------------------------


def test_unwired_definition_writes_byte_identical_body_under_full_governance() -> None:
    """Today's default (no providers injected) is byte-identical to before this
    unit even on a fully governed, human-approved write — the control that the
    stamp is provider-wiring, not a silent global behavior change."""
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = crm_note_definition(salesforce_write_client(recorder=recorder))
    executor = _executor(
        definition, sink, gate=NamedApproverGate(APPROVER), overrides={TOOL: _ESCALATED}
    )

    asyncio.run(
        executor.execute(TOOL, _args("req-unwired001"), principal=make_principal(REQUESTER))
    )

    fields = _posted_note_fields(recorder)
    assert fields == {"ParentId": SF_RECORD_ID, "Title": "Follow-up", "Body": BODY}
    assert STAMP_HEAD not in fields["Body"]  # belt: no stamp fragment either


def test_wired_definition_without_consumed_approval_is_byte_identical() -> None:
    """The WIRED definition invoked outside any governed approval context (the
    ungoverned/test-double path) stamps NOTHING — byte-identical body. The
    bidirectional partner of test (1): same wiring, no approval, no stamp.

    The principal provider DELIBERATELY returns a real principal here while the
    approval carrier is genuinely empty: a mutant that stamps a requester-only
    line when no approval exists (a named human where no authorisation happened
    — the mislabel hazard) goes RED on the byte-identity below, instead of
    hiding behind both providers happening to return ``None`` together."""
    recorder: list[httpx.Request] = []
    definition = crm_note_definition(
        salesforce_write_client(recorder=recorder),
        approval_provider=current_tool_approval,
        principal_provider=lambda: make_principal(REQUESTER),
    )
    binding = build_action_binding(definition, dedup=_dedup())

    assert current_tool_approval() is None  # precondition: nothing to stamp

    async def _direct() -> str:
        return await binding.handler(_args("req-direct0001"))

    asyncio.run(_direct())

    fields = _posted_note_fields(recorder)
    assert fields == {"ParentId": SF_RECORD_ID, "Title": "Follow-up", "Body": BODY}
    assert STAMP_HEAD not in fields["Body"]
    assert REQUESTER not in fields["Body"]  # no requester-only attribution either


# --- (3) the 4000-char Salesforce cap holds, pinned at the boundary ----------------


def _fixed_stamp(approval_id: str) -> str:
    return (
        f"\n\n{STAMP_HEAD}Approved by: {APPROVER} | "
        f"Approval ID: {approval_id} | Requested by: {REQUESTER}"
    )


def test_stamp_never_pushes_body_over_the_4000_char_cap() -> None:
    """One char each side of the boundary: a body that exactly fits the cap with
    the stamp is untouched; one char more and the BODY tail is elided (marked
    ``…``) while the stamp lands INTACT and the total is exactly 4000."""
    approval_id = "req-fixed-00001"
    stamp = _fixed_stamp(approval_id)

    # (a) exact fit: len(body) + len(stamp) == 4000 — untruncated, no marker.
    fits = "B" * (4000 - len(stamp))
    # (b) one over: the body loses exactly its last 2 chars for the 1-char marker.
    over = "B" * (4000 - len(stamp) + 1)
    # (c) the maximum legal input body.
    full = "B" * 4000

    for request_id, body in (
        ("req-bnd-fit01", fits),
        ("req-bnd-over1", over),
        ("req-bnd-full1", full),
    ):
        sink = RecordingAuditSink()
        recorder: list[httpx.Request] = []
        definition = _wired_definition(salesforce_write_client(recorder=recorder))
        executor = _executor(
            definition,
            sink,
            gate=_FixedIdGate(APPROVER, approval_id),
            overrides={TOOL: _ESCALATED},
        )
        asyncio.run(
            executor.execute(
                TOOL, _args(request_id, body=body), principal=make_principal(REQUESTER)
            )
        )
        posted = _posted_note_fields(recorder)["Body"]
        assert len(posted) <= 4000, f"cap breached for {request_id}: {len(posted)}"
        assert posted.endswith(stamp), f"stamp not intact for {request_id}"
        if body is fits:
            assert posted == body + stamp  # untruncated, marker-free
            assert len(posted) == 4000
        else:
            assert len(posted) == 4000  # truncation lands EXACTLY on the cap
            kept = 4000 - len(stamp) - 1
            assert posted == body[:kept] + "…" + stamp


# --- (4) a missing durable id is OMITTED, never a placeholder ----------------------


def test_missing_durable_approval_id_segment_is_omitted_never_placeholder() -> None:
    """A gate that records no durable id (``approval_id=None`` — the in-memory
    protocol gates) yields a stamp WITHOUT the Approval-ID segment: truthfulness
    over completeness. ``"None"`` never appears; the segment label itself never
    appears. Bidirectional partner: tests (1)/(3) pin the segment PRESENT when
    the id exists."""
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = _wired_definition(salesforce_write_client(recorder=recorder))
    executor = _executor(
        definition, sink, gate=NamedApproverGate(APPROVER), overrides={TOOL: _ESCALATED}
    )

    asyncio.run(executor.execute(TOOL, _args("req-noid00001"), principal=make_principal(REQUESTER)))

    body = _posted_note_fields(recorder)["Body"]
    assert body == f"{BODY}\n\n{STAMP_HEAD}Approved by: {APPROVER} | Requested by: {REQUESTER}"
    assert "Approval ID" not in body
    assert "None" not in body


# --- (5) C12: identifiers only — sentinel sweep with presence controls -------------


def test_no_principal_claims_or_groups_cross_into_the_wire_body() -> None:
    """The stamp carries the three identifiers already on the audit chain and
    NOTHING else. Claims (department) and groups sentinels planted on the
    requesting principal are swept ABSENT against the RAW POST bytes; the
    presence controls — the requester id, the approver, and the durable id, on
    the SAME bytes — prove the sweep reads the real wire and stamping is live
    (a dead sweep and a dead stamp cannot both pass)."""
    dept_sentinel = "DEPT-SENTINEL-77"
    group_sentinel = "GROUP-SENTINEL-88"
    requester_sentinel = "user:REQ-SENTINEL-55"
    principal = Principal(
        id=requester_sentinel,
        groups=(group_sentinel,),
        claims={"department": dept_sentinel},
    )

    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = _wired_definition(salesforce_write_client(recorder=recorder))
    store, gate = _durable_gate(sink)
    executor = _executor(definition, sink, gate=gate, overrides={TOOL: _ESCALATED})

    async def scenario() -> str:
        task = asyncio.create_task(
            executor.execute(TOOL, _args("req-sentinel01"), principal=principal)
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        rid = (await store.list_pending())[0].request_id
        await gate.approve(rid, approver=APPROVER)
        await asyncio.wait_for(task, timeout=10)
        return rid

    rid = asyncio.run(scenario())

    posts = [r for r in recorder if r.url.path.endswith("/sobjects/Note")]
    assert len(posts) == 1
    wire = posts[0].content.decode()
    # Presence controls FIRST: the sweep is reading bytes the stamp reached.
    assert requester_sentinel in wire
    assert APPROVER in wire
    assert rid in wire
    # The sweep: no claim/group material crosses into the customer's CRM.
    assert dept_sentinel not in wire
    assert group_sentinel not in wire
    # And the field SET is exactly the three Note fields.
    assert set(json.loads(wire)) == {"ParentId", "Title", "Body"}
