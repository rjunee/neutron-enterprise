# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DurableApprovalGate.read_payload — the named-approver-only disclosure read.

Owner ruling 2026-07-31: the payload is visible to the NAMED approver for a
request ONLY; other approver-role holders may see that an entry exists but may
not read it. THE LOAD-BEARING TEST is the refusal — an approver-role holder who
is NOT the named approver for a request is REFUSED the payload — proven on
BODY CONTENT (the exception message), not merely on the exception TYPE, with a
fixture containing >=2 approver principals so the discrimination is actually
exercised (a fixture with a single approver would make this untestable).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    ProtectedSourceInvocationRef,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundApprovalResolution,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.runtime.approvals import (
    PROTECTED_INVOCATION_KEY,
    DurableApprovalGate,
    reversibility_label,
)
from ne.runtime.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalPolicyError,
    ApprovalRequestNotFoundError,
    ApprovalSourceWithheldFromApproverError,
    RuntimeAuditError,
)
from ne.runtime.source_results import (
    MissingAuthenticatedContextError,
    ProtectedSourceResultGuard,
    SourceAuthorityRetryableError,
    authenticated_request_scope,
    encode_protected_reference,
)

from .conftest import (
    FailingAuditSink,
    RecordingAuditSink,
    approval_request,
    approval_settings,
)

MATERIAL_AMOUNT = "250,000.00"
MATERIAL_COUNTERPARTY = "ACME GLOBAL SUPPLY LTD"

_NAMED = ("user:alice", "user:bob")
_NON_NAMED = "user:mallory"


def _gate(store: Any, sink: Any, settings: Any = None, **kwargs: Any) -> DurableApprovalGate:
    return DurableApprovalGate(
        store=store, sink=sink, settings=settings or approval_settings(), **kwargs
    )


def _wire_payload() -> dict[str, Any]:
    """A Tier-3 wire-transfer disclosure payload carrying the two probe values."""
    return {
        "tool": "finance.wire.transfer",
        "disclosure": {
            "target_resource": "account:ops-payables",
            "description": "Wire transfer to a named counterparty.",
            "material_parameters": {
                "amount": MATERIAL_AMOUNT,
                "counterparty": MATERIAL_COUNTERPARTY,
            },
            "reversibility": reversibility_label(3),
        },
    }


async def _submit_tier3(gate: DurableApprovalGate) -> str:
    return await gate.submit(approval_request("finance.wire.transfer", 3, payload=_wire_payload()))


def test_non_named_approver_is_refused(approval_store: Any) -> None:
    """THE load-bearing guard: a role-holder who is NOT the named approver for
    this request is refused — and the refusal message carries NONE of the
    material values (body-content assertion, not status/type-only)."""
    settings = approval_settings(tier3_named_approvers=_NAMED)
    gate = _gate(approval_store, RecordingAuditSink(), settings)

    async def scenario() -> None:
        rid = await _submit_tier3(gate)
        with pytest.raises(ApprovalPolicyError) as exc_info:
            await gate.read_payload(rid, approver=_NON_NAMED)
        message = str(exc_info.value)
        assert MATERIAL_AMOUNT not in message
        assert MATERIAL_COUNTERPARTY not in message
        assert "named approver" in message

    asyncio.run(scenario())


def test_both_named_approvers_read(approval_store: Any) -> None:
    """Positive control: BOTH named approvers can read, and the probes ARE
    detectable in their disclosure — proving test_non_named_approver_is_refused
    actually discriminates rather than the fixture being unreadable to anyone."""
    settings = approval_settings(tier3_named_approvers=_NAMED)
    gate = _gate(approval_store, RecordingAuditSink(), settings)

    async def scenario() -> None:
        rid = await _submit_tier3(gate)
        for approver in _NAMED:
            disclosure = await gate.read_payload(rid, approver=approver)
            assert disclosure.material_parameters is not None
            assert disclosure.material_parameters["amount"] == MATERIAL_AMOUNT
            assert disclosure.material_parameters["counterparty"] == MATERIAL_COUNTERPARTY
            assert disclosure.target_resource == "account:ops-payables"
            assert "irreversible" in disclosure.reversibility

    asyncio.run(scenario())


def test_tier2_role_gated_reader_reads(approval_store: Any) -> None:
    """Tier < 3: the gate itself performs no named-approver check — the ne-api
    approver_roles pre-check IS the tier-2 authorization layer (see the router's
    docstring); any non-empty approver id reads at the gate level."""
    gate = _gate(approval_store, RecordingAuditSink())

    async def scenario() -> None:
        rid = await gate.submit(
            approval_request(
                "write.external.email.send",
                2,
                payload={
                    "tool": "write.external.email.send",
                    "disclosure": {
                        "target_resource": "email:ops@corp.example",
                        "description": "Send an external email.",
                        "material_parameters": {"to": "ops@corp.example"},
                        "reversibility": reversibility_label(2),
                    },
                },
            )
        )
        disclosure = await gate.read_payload(rid, approver="user:any-approver")
        assert disclosure.material_parameters == {"to": "ops@corp.example"}
        assert "external-facing" in disclosure.reversibility

    asyncio.run(scenario())


def test_tier3_empty_named_list_refuses_everyone(approval_store: Any) -> None:
    """An empty tier3_named_approvers list ⇒ nobody reads (fail closed)."""
    gate = _gate(approval_store, RecordingAuditSink(), approval_settings(tier3_named_approvers=()))

    async def scenario() -> None:
        rid = await _submit_tier3(gate)
        with pytest.raises(ApprovalPolicyError):
            await gate.read_payload(rid, approver="user:anyone")

    asyncio.run(scenario())


def test_payload_read_is_audited(approval_store: Any) -> None:
    settings = approval_settings(tier3_named_approvers=_NAMED)
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink, settings)

    async def scenario() -> None:
        rid = await _submit_tier3(gate)
        await gate.read_payload(rid, approver="user:alice")

    asyncio.run(scenario())

    reads = sink.with_action("approval.payload_read")
    assert len(reads) == 1
    assert reads[0].actor == "user:alice"
    # C12: no sink event anywhere carries a material value.
    for event in sink.events:
        blob = str(getattr(event, "detail", None)) + str(getattr(event, "provenance", None))
        assert MATERIAL_AMOUNT not in blob
        assert MATERIAL_COUNTERPARTY not in blob


def test_audit_failure_withholds_payload(approval_store: Any) -> None:
    settings = approval_settings(tier3_named_approvers=_NAMED)
    # Submit with a working sink first so the row actually persists, then swap
    # to a failing sink for the read itself.
    recording = RecordingAuditSink()
    gate = _gate(approval_store, recording, settings)

    async def submit() -> str:
        return await _submit_tier3(gate)

    rid = asyncio.run(submit())

    failing_gate = _gate(approval_store, FailingAuditSink(), settings)

    async def scenario() -> None:
        with pytest.raises(RuntimeAuditError):
            await failing_gate.read_payload(rid, approver="user:alice")

    asyncio.run(scenario())


def test_terminal_row_raises_already_decided(approval_store: Any) -> None:
    settings = approval_settings(tier3_named_approvers=_NAMED)
    gate = _gate(approval_store, RecordingAuditSink(), settings)

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        await gate.approve(rid, approver="user:carol")
        with pytest.raises(ApprovalAlreadyDecidedError):
            await gate.read_payload(rid, approver="user:carol")

    asyncio.run(scenario())


def test_unknown_request_raises_not_found(approval_store: Any) -> None:
    gate = _gate(approval_store, RecordingAuditSink())

    async def scenario() -> None:
        with pytest.raises(ApprovalRequestNotFoundError):
            await gate.read_payload("does-not-exist", approver="user:anyone")

    asyncio.run(scenario())


def test_legacy_row_material_parameters_none(approval_store: Any) -> None:
    """A row shaped like today's (pre-ruling) payload — {'tool': name}, no
    'disclosure' key — degrades to material_parameters=None, never raises, and
    reversibility still falls back to the tier-derived label."""
    settings = approval_settings(tier3_named_approvers=_NAMED)
    gate = _gate(approval_store, RecordingAuditSink(), settings)

    async def scenario() -> None:
        rid = await gate.submit(
            approval_request("finance.wire.transfer", 3, payload={"tool": "finance.wire.transfer"})
        )
        disclosure = await gate.read_payload(rid, approver="user:alice")
        assert disclosure.material_parameters is None
        assert "irreversible" in disclosure.reversibility

    asyncio.run(scenario())


def test_reversibility_labels() -> None:
    assert "irreversible" in reversibility_label(3)
    assert "irreversible" not in reversibility_label(2)
    assert "irreversible" not in reversibility_label(1)
    assert "irreversible" not in reversibility_label(0)


def test_a_lying_payload_cannot_downgrade_the_reversibility_class(approval_store: Any) -> None:
    """The gate's safety classification is derived from the ROW, not the blob.

    `reversibility_label`'s docstring promised this — "pure function of tier,
    never derived from model output (C12)" — while `read_payload` preferred the
    stored string and only fell back to the row. Review demonstrated the
    consequence: a Tier-3 request disclosing "reversible — internal action
    class". The one field an approver reads to decide *can I undo this?* was the
    one the payload's author could set.

    That is the written-rules-disarm-review shape: a reviewer who checks the
    rule reads the docstring and stops. So the guarantee is asserted here, at a
    lying payload, rather than trusted to prose.

    NOTE the deliberate asymmetry this test also pins: `target_resource` STAYS
    tool-supplied. Nothing on the row knows which account a wire hits, and
    `tool:finance.wire.transfer` would strip the approver of the fact they most
    need. Description is description; the SAFETY CLASS is the gate's.
    """
    payload = _wire_payload()
    payload["disclosure"]["reversibility"] = reversibility_label(0)  # a lie: Tier-3 row
    payload["disclosure"]["target_resource"] = "account:petty-cash"  # harmless-looking

    settings = approval_settings(tier3_named_approvers=_NAMED)
    gate = _gate(approval_store, RecordingAuditSink(), settings)

    async def _run() -> None:
        request_id = await gate.submit(
            approval_request("finance.wire.transfer", 3, payload=payload)
        )
        disclosure = await gate.read_payload(request_id, approver=_NAMED[0])

        # The gate's own classification wins over the planted one.
        assert disclosure.reversibility == reversibility_label(3)
        assert "irreversible" in disclosure.reversibility
        assert reversibility_label(0) not in disclosure.reversibility

        # ...and the tool's own description of WHAT it touches is preserved,
        # because no other source for it exists.
        assert disclosure.target_resource == "account:petty-cash"

    asyncio.run(_run())


def test_an_honest_payload_still_reads_back_unchanged(approval_store: Any) -> None:
    """CONTROL. Without it, a `read_payload` that hardcoded the Tier-3 string —
    or dropped the disclosure entirely — would satisfy the test above and look,
    to an approver, exactly like the feature working."""
    settings = approval_settings(tier3_named_approvers=_NAMED)
    gate = _gate(approval_store, RecordingAuditSink(), settings)

    async def _run() -> None:
        request_id = await _submit_tier3(gate)
        disclosure = await gate.read_payload(request_id, approver=_NAMED[0])
        assert disclosure.reversibility == reversibility_label(3)
        assert disclosure.target_resource == "account:ops-payables"
        assert disclosure.material_parameters is not None
        assert disclosure.material_parameters["amount"] == MATERIAL_AMOUNT
        assert disclosure.material_parameters["counterparty"] == MATERIAL_COUNTERPARTY

    asyncio.run(_run())


def test_canonical_args_hash_handles_the_nested_disclosure_payload() -> None:
    """`submit()` uses this as the scoped-dedup fallback when no explicit
    args_hash is supplied, and this unit gave the payload a NESTED disclosure
    block. `dict(data)` is shallow, so the nested FrozenDict reached json.dumps
    and raised TypeError. The same bug forty lines away was fixed with thaw() in
    this unit; this call site was missed."""
    from ne.contracts.frozen import _coerce
    from ne.runtime.approvals import canonical_args_hash

    frozen = _coerce(_wire_payload())
    digest = canonical_args_hash(frozen)  # must not raise
    assert len(digest) == 64

    # Key-order independence must survive the thaw — the property the function
    # exists for, asserted on the nested shape rather than only a flat one.
    reordered = _coerce(
        {
            "disclosure": _wire_payload()["disclosure"],
            "tool": _wire_payload()["tool"],
        }
    )
    assert canonical_args_hash(reordered) == digest


# --- PRE-015A0-ENFORCEMENT: source-derived approval arguments -----------------
#
# A source-derived request stores only an opaque invocation reference. The
# arguments are re-authorized at every disclosure under the READING approver's own
# current authorization, so a drift between submit and read withholds them.

_EXPIRES_AT = 1_800_000_000
_BEFORE_EXPIRY = datetime.fromtimestamp(_EXPIRES_AT - 1, tz=UTC)
_PROTECTED_ARGUMENTS = {"amount": MATERIAL_AMOUNT, "counterparty": MATERIAL_COUNTERPARTY}


def _stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id="item-a"
        ),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


def _invocation_ref() -> ProtectedSourceInvocationRef:
    return ProtectedSourceInvocationRef(
        reference_id="invocation-1", stamps=(_stamp(),), invocation_hash="e" * 64
    )


def _context() -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id="user:ada",
        authorization_groups=("group:finance",),
        department="finance",
        issuer="https://oidc.test/realms/ne",
        expires_at=_EXPIRES_AT,
    )


class ApprovalResolver:
    """A ``SourceBoundResultResolver`` scripting one approval decision at a time."""

    def __init__(self, decision: SourceAuthorityDecision, text: str | None) -> None:
        self.decision = decision
        self.text = text
        self.calls: list[tuple[str, str, str]] = []

    async def for_approval(
        self,
        reference: Any,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution:
        self.calls.append((principal_id, approval_id, decision_attempt_id))
        authorized = self.decision is SourceAuthorityDecision.AUTHORIZED
        return SourceBoundApprovalResolution(
            decision=self.decision,
            materialized_text=self.text if authorized else None,
            stamps=(_stamp(),) if authorized else (),
            safe_reason=None if authorized else "safe",
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )

    async def for_model_send(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_direct_result(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_dispatch(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_workflow_projection(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _protected_payload() -> dict[str, Any]:
    """The same wire request, but source-derived: an opaque reference, no arguments."""
    return {
        "tool": "finance.wire.transfer",
        "disclosure": {
            "target_resource": "account:ops-payables",
            "description": "Wire transfer to a named counterparty.",
            PROTECTED_INVOCATION_KEY: encode_protected_reference(_invocation_ref()),
        },
    }


def _protected_gate(store: Any, sink: Any, guard: Any) -> DurableApprovalGate:
    return DurableApprovalGate(
        store=store,
        sink=sink,
        settings=approval_settings(tier3_named_approvers=_NAMED),
        source_guard=guard,
    )


def _guard_for(resolver: ApprovalResolver) -> ProtectedSourceResultGuard:
    return ProtectedSourceResultGuard(resolver, clock=lambda: _BEFORE_EXPIRY)


async def _submit_protected(gate: DurableApprovalGate) -> str:
    return await gate.submit(
        approval_request("finance.wire.transfer", 3, payload=_protected_payload())
    )


def test_protected_request_discloses_arguments_materialized_for_the_reading_approver(
    approval_store: Any,
) -> None:
    resolver = ApprovalResolver(
        SourceAuthorityDecision.AUTHORIZED, json.dumps(_PROTECTED_ARGUMENTS)
    )
    gate = _protected_gate(approval_store, RecordingAuditSink(), _guard_for(resolver))

    async def scenario() -> None:
        rid = await _submit_protected(gate)
        with authenticated_request_scope(_context()):
            disclosure = await gate.read_payload(rid, approver=_NAMED[0])
        assert disclosure.material_parameters == _PROTECTED_ARGUMENTS
        # The resolver was asked about THIS approval under THIS request's snapshot.
        assert [(principal, approval) for principal, approval, _ in resolver.calls] == [
            ("user:ada", rid)
        ]

    asyncio.run(scenario())


def test_withheld_reader_is_refused_and_the_request_stays_actionable(
    approval_store: Any,
) -> None:
    """A reader current authority does not admit is refused — nothing is destroyed.

    U2 adversarial review (standalone-gate ruling): the guard evaluates under the
    READER's bound context, so a non-authorized outcome is that reader's ACL
    standing, never a proven source revocation. The earlier
    revision of this test enshrined the defect — a mere read CAS'd the request
    terminal ``source_withheld``. Now: typed refusal carrying no material value,
    the row untouched and still pending, no ``source_withheld`` audit — and an
    admitted reader still discloses the SAME request afterwards.
    """
    resolver = ApprovalResolver(SourceAuthorityDecision.PERMANENT_WITHHELD, None)
    sink = RecordingAuditSink()
    gate = _protected_gate(approval_store, sink, _guard_for(resolver))

    async def scenario() -> None:
        rid = await _submit_protected(gate)
        with (
            authenticated_request_scope(_context()),
            pytest.raises(ApprovalSourceWithheldFromApproverError) as excinfo,
        ):
            await gate.read_payload(rid, approver=_NAMED[0])
        assert MATERIAL_AMOUNT not in str(excinfo.value)
        # The row is untouched: still pending and actionable, no terminal fact.
        row = await approval_store.get(rid)
        assert row.status == "pending"
        assert row.decided_reason is None
        assert row.decided_at is None
        # No revocation was asserted anywhere for a reader's ACL standing.
        assert "approval.source_withheld" not in [e.action for e in sink.events]
        # An admitted reader still discloses the SAME pending request.
        admitted = _protected_gate(
            approval_store,
            sink,
            _guard_for(
                ApprovalResolver(
                    SourceAuthorityDecision.AUTHORIZED, json.dumps(_PROTECTED_ARGUMENTS)
                )
            ),
        )
        with authenticated_request_scope(_context()):
            disclosure = await admitted.read_payload(rid, approver=_NAMED[0])
        assert disclosure.material_parameters == _PROTECTED_ARGUMENTS
        assert disclosure.reversibility == reversibility_label(3)

    asyncio.run(scenario())


def test_retryable_authority_discloses_nothing_and_leaves_the_request_pending(
    approval_store: Any,
) -> None:
    resolver = ApprovalResolver(SourceAuthorityDecision.RETRYABLE_UNKNOWN, None)
    gate = _protected_gate(approval_store, RecordingAuditSink(), _guard_for(resolver))

    async def scenario() -> None:
        rid = await _submit_protected(gate)
        with authenticated_request_scope(_context()):
            with pytest.raises(SourceAuthorityRetryableError):
                await gate.read_payload(rid, approver=_NAMED[0])
            row = await approval_store.get(rid)
            assert row.status == "pending"
            # The SAME row becomes readable once authority is established again —
            # a retryable unknown was never upgraded to a terminal denial.
            resolver.decision = SourceAuthorityDecision.AUTHORIZED
            resolver.text = json.dumps(_PROTECTED_ARGUMENTS)
            disclosure = await gate.read_payload(rid, approver=_NAMED[0])
        assert disclosure.material_parameters == _PROTECTED_ARGUMENTS

    asyncio.run(scenario())


def test_gate_composed_without_a_resolver_cannot_disclose_a_protected_request(
    approval_store: Any,
) -> None:
    """An unwired composition is not a permission to skip the check."""
    gate = _protected_gate(approval_store, RecordingAuditSink(), None)

    async def scenario() -> None:
        rid = await _submit_protected(gate)
        with authenticated_request_scope(_context()), pytest.raises(SourceAuthorityRetryableError):
            await gate.read_payload(rid, approver=_NAMED[0])
        row = await approval_store.get(rid)
        assert row.status == "pending"

    asyncio.run(scenario())


def test_protected_disclosure_refuses_without_a_bound_authenticated_context(
    approval_store: Any,
) -> None:
    resolver = ApprovalResolver(
        SourceAuthorityDecision.AUTHORIZED, json.dumps(_PROTECTED_ARGUMENTS)
    )
    gate = _protected_gate(approval_store, RecordingAuditSink(), _guard_for(resolver))

    async def scenario() -> None:
        rid = await _submit_protected(gate)
        with pytest.raises(MissingAuthenticatedContextError):
            await gate.read_payload(rid, approver=_NAMED[0])
        assert resolver.calls == []
        # Positive control: the same read succeeds under a bound snapshot.
        with authenticated_request_scope(_context()):
            disclosure = await gate.read_payload(rid, approver=_NAMED[0])
        assert disclosure.material_parameters == _PROTECTED_ARGUMENTS

    asyncio.run(scenario())


def test_a_source_free_request_never_consults_the_resolver(approval_store: Any) -> None:
    """Only a stored protected reference makes a request protected."""
    resolver = ApprovalResolver(SourceAuthorityDecision.PERMANENT_WITHHELD, None)
    gate = _protected_gate(approval_store, RecordingAuditSink(), _guard_for(resolver))

    async def scenario() -> None:
        rid = await gate.submit(
            approval_request("finance.wire.transfer", 3, payload=_wire_payload())
        )
        disclosure = await gate.read_payload(rid, approver=_NAMED[0])
        assert disclosure.material_parameters == _PROTECTED_ARGUMENTS
        assert resolver.calls == []

    asyncio.run(scenario())
