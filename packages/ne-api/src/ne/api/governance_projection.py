# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Projection of exact persisted Run governance joins into a safe typed view."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ne.api.evidence_wire import GovernanceReceiptView, GovernanceView

GovernanceReason = Literal[
    "delegation_join_unavailable",
    "service_identity_join_unavailable",
    "credential_owner_join_unavailable",
    "scope_join_unavailable",
    "policy_join_unavailable",
    "tool_decision_join_unavailable",
    "audit_receipt_join_unavailable",
]


@dataclass(frozen=True, slots=True)
class PersistedAuditReceipt:
    run_audit_receipt_id: str
    run_id: str
    run_event_id: str
    attempt_id: str
    decision_kind: str
    step_id: str | None
    tool_call_id: str | None
    approval_id: str | None
    audit_event_id: str
    audit_sequence: int
    audit_record_hash: str
    config_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExactGovernanceJoins:
    """Optional producer-authored facts already joined by exact persisted ids."""

    delegated_user_label: str | None = None
    agent_service_identity: str | None = None
    connector_credential_owner_label: str | None = None
    effective_scopes: tuple[str, ...] | None = None
    policy_environment: str | None = None
    tool_tier: int | None = None
    tool_decision: Literal["allow", "deny", "approval_required"] | None = None


def project_governance(
    *,
    run_id: str,
    principal_id: str,
    agent_id: str,
    agent_version: int,
    config_hash: str,
    capability_snapshot_hash: str,
    event_ids: frozenset[str],
    receipts: tuple[PersistedAuditReceipt, ...],
    joins: ExactGovernanceJoins | None,
) -> GovernanceView:
    """Project only receipts whose exact Run and RunEvent links are persisted.

    Trace ids, actor/action equality and timestamps are deliberately absent from the
    input contract, so they cannot become fallback correlation mechanisms.
    """

    valid: list[GovernanceReceiptView] = []
    for receipt in receipts:
        if not (
            hmac.compare_digest(receipt.run_id, run_id)
            and receipt.run_event_id in event_ids
            and hmac.compare_digest(receipt.config_hash, config_hash)
        ):
            continue
        valid.append(
            GovernanceReceiptView(
                run_audit_receipt_id=receipt.run_audit_receipt_id,
                run_event_id=receipt.run_event_id,
                attempt_id=receipt.attempt_id,
                decision_kind=receipt.decision_kind,  # type: ignore[arg-type]
                step_id=receipt.step_id,
                tool_call_id=receipt.tool_call_id,
                approval_id=receipt.approval_id,
                audit_event_id=receipt.audit_event_id,
                audit_sequence=receipt.audit_sequence,
                audit_record_hash=receipt.audit_record_hash,
                config_hash=receipt.config_hash,
                created_at=receipt.created_at,
            )
        )
    valid.sort(key=lambda item: (item.audit_sequence, item.run_audit_receipt_id))

    facts = joins or ExactGovernanceJoins()
    reasons: list[GovernanceReason] = []
    if facts.delegated_user_label is None:
        reasons.append("delegation_join_unavailable")
    if facts.agent_service_identity is None:
        reasons.append("service_identity_join_unavailable")
    if facts.connector_credential_owner_label is None:
        reasons.append("credential_owner_join_unavailable")
    if facts.effective_scopes is None:
        reasons.append("scope_join_unavailable")
    if facts.policy_environment is None:
        reasons.append("policy_join_unavailable")
    if facts.tool_tier is None or facts.tool_decision is None:
        reasons.append("tool_decision_join_unavailable")
    if not valid:
        reasons.append("audit_receipt_join_unavailable")

    status: Literal["complete", "partial", "unavailable"]
    if not valid:
        status = "unavailable"
    elif reasons:
        status = "partial"
    else:
        status = "complete"
    return GovernanceView(
        status=status,
        acting_for_principal_id=principal_id,
        agent_id=agent_id,
        agent_version=agent_version,
        config_hash=config_hash,
        capability_snapshot_hash=capability_snapshot_hash,
        delegated_user_label=facts.delegated_user_label,
        agent_service_identity=facts.agent_service_identity,
        connector_credential_owner_label=facts.connector_credential_owner_label,
        effective_scopes=tuple(sorted(set(facts.effective_scopes or ()))),
        policy_environment=facts.policy_environment,
        tool_tier=facts.tool_tier,
        tool_decision=facts.tool_decision,
        receipts=tuple(valid),
        unavailable_reasons=tuple(reasons),
    )
