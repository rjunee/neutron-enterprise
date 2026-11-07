# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""S5 — the governed outreach ladder, ONE ask, MUTATING.

The full ladder (ASK_SCENARIOS S5 / DEMO_RUNBOOK Beat 5), driven end to end:

1. ops asks Meridian Customer Support to draft-and-send the mer-loan-001
   hardship outreach and record it in the CRM (one prompt, no hand-holding);
2. the run reads governed sources, creates a REAL unsent draft
   (``draft.email.create``, Tier 0, autonomous — MUST NOT pause);
3. the run PARKS at the Tier-2 ``write.external.email.send`` gate
   (``waiting_approval`` + an approval row);
4. ops CANNOT approve its own write — asserted by actually trying, and
   asserted on the REASON. In the live ``ne-pilot`` realm ``ops`` is not in
   ``/approver``, so what this leg can prove is the fail-closed approver-ROLE
   gate (``ne.api.authz.require_role``), NOT the requester≠approver rule. The
   separation-of-duties rule itself needs a principal holding BOTH roles, which
   no realm user does; it is proven where such a principal can be minted —
   ``ne-api/tests/test_network_airgap.py`` (the air-gap lift) and
   ``ne-api/tests/test_approvals_api.py`` (this exact run-parked send shape);
5. the DISTINCT approver reveals the payload and approves PROMPTLY (the run
   owner's authorization snapshot expires in ~12-15 minutes; the suite
   approves within seconds, so expiry never confounds the result);
6. the send actually executes (a ``tool.execute`` success audit row for
   ``tool:write.external.email.send`` minted after the approval).

KNOWN-BROKEN TAIL, encoded as STRICT xfails so the suite stays green while
the defects exist and goes LOUD the moment they are fixed (forcing marker
removal — the tripwire the owner asked for):

* POST_DEMO #2 — after a successful send the run never terminalizes
  ("Run delivery observer did not capture the complete receipt set").

AND ONE THAT IS NO LONGER A DEFECT, because the platform's behaviour is the
CORRECT one and the CLAIMS around it were what was wrong. POST_DEMO #4 used to
sit here as a second strict xfail: the approval resume executes exactly ONE
pinned effect, so a Tier-1 ``write.internal.crm.note`` ordered after the send
never ran. That IS the design — a resume performs the one effect a human
approved and terminalizes rather than re-deriving a plan hours later
(the agent loop is "DETERMINISTIC AND BOUNDED AGENTIC PRIMITIVES
ONLY — NOT BUILT AS A GENERAL DURABLE LOOP") — and the deployed surfaces that
promised otherwise have been corrected instead: the pinned Skill (v3) now ends
at the send, and the registry's prose and per-call budget no longer count a
call that cannot happen. So the xfail is gone along with the EXPECTATION it
guarded, and what stands in its place asserts the real rule: NOTHING follows
the approved send.

MUTATION WARNING: a real email leaves the building (to the CRM-recorded
fixture address, allowlisted server-side to ["owner@example.com"]). Opt-in via
NE_E2E_MUTATING=1.
"""

from __future__ import annotations

import time

import pytest
from nelib.client import (
    CUSTOMER_SUPPORT,
    TERMINAL_STATUSES,
    NeutronClient,
    RunResult,
)

pytestmark = [pytest.mark.mutating, pytest.mark.frontier]

_PROMPT = (
    "Draft and send a disaster-hardship outreach email for loan mer-loan-001, and update "
    "the CRM to record it."
)
_SEND_TOOL = "write.external.email.send"
_DRAFT_TOOL = "draft.email.create"
_NOTE_TOOL = "write.internal.crm.note"
_PARK_STOP = TERMINAL_STATUSES | {"waiting_approval"}


def _tool_success_seqs(client: NeutronClient, tool: str) -> set[int]:
    return {
        int(event["seq"])
        for event in client.audit_events(action="tool.execute", limit=500)
        if event.get("resource") == f"tool:{tool}" and event.get("outcome") == "success"
    }


@pytest.fixture(scope="module")
def ladder(
    ops: NeutronClient,
    approver: NeutronClient,
    warm_box: None,
    mutating_enabled: None,
) -> dict[str, object]:
    del warm_box, mutating_enabled
    send_baseline = _tool_success_seqs(ops, _SEND_TOOL)

    parked = ops.run_turn(
        CUSTOMER_SUPPORT,
        _PROMPT,
        requested_model="frontier-default",
        label="s5-park",
        deadline_s=420.0,
        stop_statuses=_PARK_STOP,
    )
    state: dict[str, object] = {
        "parked": parked,
        "send_baseline": send_baseline,
        "approved": False,
    }
    if parked.status != "waiting_approval":
        return state  # the park assertions below will fail with the real state

    approval_id = str(parked.detail.get("approval_id") or "")
    state["approval_id"] = approval_id

    # The requester attempts its own decision (see the module docstring for
    # WHICH rule this can prove live — the reason is captured, not just the code).
    own_approve = ops.approve(approval_id)
    state["own_approve_status"] = own_approve.status_code
    state["own_approve_detail"] = own_approve.text

    # The window this scenario actually reasons about opens with the approver's
    # decision. Snapshotting here — with the run PARKED, so nothing of its own
    # can execute — separates a note the model wrote before submitting the send
    # (legal) from one that followed the approval (which the platform does not
    # allow, and which `test_s5_nothing_executes_after_the_approved_send` is the
    # tripwire for).
    state["note_seqs_at_park"] = _tool_success_seqs(ops, _NOTE_TOOL)

    payload = approver.approval_payload(approval_id)
    state["payload_status"] = payload.status_code
    state["payload"] = payload.json() if payload.status_code == 200 else payload.text
    decision = approver.approve(approval_id)
    state["approve_status"] = decision.status_code
    state["approved"] = decision.status_code == 200
    state["approved_at"] = time.monotonic()

    # Give the resumed continuation time to execute the send, then collect.
    deadline = time.monotonic() + 240.0
    while time.monotonic() < deadline:
        if _tool_success_seqs(ops, _SEND_TOOL) - send_baseline:
            break
        time.sleep(5.0)
    state["send_seqs_after"] = _tool_success_seqs(ops, _SEND_TOOL)
    state["final"] = ops.wait_for_status(CUSTOMER_SUPPORT, parked.run_id, deadline_s=300.0)
    state["note_seqs_after"] = _tool_success_seqs(ops, _NOTE_TOOL)
    ops.archive(
        "s5-final",
        ops.collect(CUSTOMER_SUPPORT, parked.run_id, parked.conversation_id, parked.elapsed_s),
    )
    return state


def test_s5_parks_at_the_tier2_gate(ladder: dict[str, object]) -> None:
    parked: RunResult = ladder["parked"]  # type: ignore[assignment]
    assert parked.status == "waiting_approval", (
        f"S5 must PARK at the Tier-2 send gate; run ended {parked.status!r} "
        f"(reason={parked.failure_reason!r}). A completed run here means a send happened "
        "WITHOUT an approval gate — a governance P0, not a test nit."
    )
    assert parked.detail.get("approval_id"), "parked run carries no approval id"


def test_s5_draft_created_tier0_before_the_park(ladder: dict[str, object]) -> None:
    parked: RunResult = ladder["parked"]  # type: ignore[assignment]
    assert any(_DRAFT_TOOL in call for call in parked.tool_calls), (
        f"the Tier-0 draft ({_DRAFT_TOOL}) did not execute before the park; tool chain: "
        f"{parked.tool_calls}"
    )


def test_s5_ops_cannot_approve_its_own_write(ladder: dict[str, object]) -> None:
    """403 AND the reason — this assertion may not pass for the wrong reason.

    Pinning only the status code made this test vacuous as a separation-of-duties
    proof: ``ops`` holds no approver role, so a 403 here is the approver-ROLE gate
    doing its job and would stand unchanged even if requester≠approver were
    enforced nowhere at all. It is exactly that reading that let the claim in
    ``docs/ops/air-gap-recovery.md`` §2 go untested. So: assert the reason, and
    let the docstring say where the real rule is proven.
    """
    assert ladder.get("own_approve_status") == 403, (
        f"ops approving its own Tier-2 write returned {ladder.get('own_approve_status')} — "
        "the approver-role gate must refuse a non-approver (403)."
    )
    detail = str(ladder.get("own_approve_detail") or "")
    assert "not an authorized approver" in detail or "separation of duties" in detail, (
        "the 403 came from neither the approver-role gate nor separation of duties; "
        f"body was {detail[:300]!r} — a 403 for a THIRD reason (a stale row, an "
        "expired horizon) would make this leg prove nothing."
    )


def test_s5_distinct_approver_reveal_and_approve_succeed(ladder: dict[str, object]) -> None:
    assert ladder.get("payload_status") == 200, (
        f"approver payload reveal failed: {ladder.get('payload_status')} "
        f"{str(ladder.get('payload'))[:300]}"
    )
    assert ladder.get("approve_status") == 200, (
        f"approve returned {ladder.get('approve_status')} — if 409 'authorization expired', "
        "the run-owner token lapsed before the decision (POST_DEMO #5); the suite approves "
        "within seconds, so investigate before blaming expiry."
    )


def test_s5_send_executes_after_approval(ladder: dict[str, object]) -> None:
    if not ladder.get("approved"):
        pytest.fail("approval did not succeed; the send leg cannot be assessed")
    new_sends = set(ladder["send_seqs_after"]) - set(ladder["send_baseline"])  # type: ignore[arg-type]
    assert new_sends, (
        "no tool.execute success for tool:write.external.email.send appeared within 240s of "
        "approval — approve-then-strand (the 2026-08-28 11:21Z defect family) has returned, "
        "or the resume is dead."
    )


@pytest.mark.xfail(
    strict=True,
    reason="POST_DEMO #2: runs do not terminalize after a successful send ('Run delivery "
    "observer did not capture the complete receipt set'); the run hangs in 'running' "
    "forever. STRICT: when the finalizer is fixed this XPASSes loudly — delete this marker.",
)
def test_s5_run_reaches_a_terminal_state(ladder: dict[str, object]) -> None:
    final: dict[str, object] = ladder["final"]  # type: ignore[assignment]
    assert final["status"] in TERMINAL_STATUSES, (
        f"run still {final['status']!r} after the post-approval wait"
    )


def test_s5_nothing_executes_after_the_approved_send(ladder: dict[str, object]) -> None:
    """The approval horizon, asserted rather than wished away.

    A human approved ONE effect. The resume executes THAT effect and the run
    terminalizes; it does not re-enter the agent loop, so no governed write
    ordered after the gated send can run. This test is the tripwire for the
    OPPOSITE drift from the one the old xfail encoded: if a durable-loop resume
    ever lands, a trailing ``write.internal.crm.note`` appears here and this
    goes red, which is exactly when the four corrected surfaces (Skill v3, the
    registry prose and budget, the console's "Completed the approved action",
    this docstring) need re-deriving.

    The baseline is taken AT THE PARK, not at run start, which is the whole
    precision of the claim: a note the model chose to write BEFORE submitting
    the send is legal and is not what this asserts. Only the window that opens
    with the approver's decision is examined, and the run is parked throughout
    it, so nothing can slip in between the snapshot and the decision.

    Its positive control is ``test_s5_send_executes_after_approval`` above: the
    approved effect really did run, so an empty set here is "the resume stopped
    where it should", never "the resume did nothing".
    """
    if not ladder.get("approved"):
        pytest.fail("approval did not succeed; the resume leg cannot be assessed")
    followed = set(ladder["note_seqs_after"]) - set(ladder["note_seqs_at_park"])  # type: ignore[arg-type]
    assert not followed, (
        f"a governed write executed AFTER the approved send (tool.execute seqs {sorted(followed)} "
        f"for tool:{_NOTE_TOOL}). The approval resume is supposed to perform exactly the one "
        "pinned effect and terminalize; if the loop now resumes, every surface that states that "
        "limit (the pinned Skill v3, deploy/agents/registry.toml's budget derivation, the "
        "console's approved-effect receipt word) is stale and must be re-derived."
    )
