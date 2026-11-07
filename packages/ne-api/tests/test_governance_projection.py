# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Focused proof that governance is exact-join typed truth, never correlation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ne.api.evidence_wire import EvidenceEnvelope, EvidenceLocatorView, GovernanceView
from ne.api.governance_projection import (
    ExactGovernanceJoins,
    PersistedAuditReceipt,
    project_governance,
)

NOW = datetime(2026, 8, 22, tzinfo=UTC)
H = "a" * 64


def _receipt(*, run_id: str, event_id: str, receipt_id: str, sequence: int):
    return PersistedAuditReceipt(
        run_audit_receipt_id=receipt_id,
        run_id=run_id,
        run_event_id=event_id,
        attempt_id="attempt-1",
        decision_kind="terminal",
        step_id=None,
        tool_call_id=None,
        approval_id=None,
        audit_event_id=f"audit-{receipt_id}",
        audit_sequence=sequence,
        audit_record_hash="b" * 64,
        config_hash=H,
        created_at=NOW,
    )


def _project(receipts: tuple[PersistedAuditReceipt, ...], joins=None):
    return project_governance(
        run_id="run-1",
        principal_id="owner-1",
        agent_id="agent-1",
        agent_version=2,
        config_hash=H,
        capability_snapshot_hash="c" * 64,
        event_ids=frozenset({"event-exact"}),
        receipts=receipts,
        joins=joins,
    )


def test_receipts_require_exact_run_event_and_config_not_trace_or_time_correlation() -> None:
    exact = _receipt(run_id="run-1", event_id="event-exact", receipt_id="exact", sequence=2)
    foreign = _receipt(run_id="run-2", event_id="event-exact", receipt_id="foreign", sequence=1)
    nearby = _receipt(run_id="run-1", event_id="event-nearby", receipt_id="nearby", sequence=3)

    result = _project((foreign, nearby, exact))

    assert [item.run_audit_receipt_id for item in result.receipts] == ["exact"]
    assert result.status == "partial"
    assert "policy_join_unavailable" in result.unavailable_reasons


def test_full_exact_governance_facts_have_a_complete_typed_envelope() -> None:
    joins = ExactGovernanceJoins(
        delegated_user_label="Meridian analyst",
        agent_service_identity="agent-service",
        connector_credential_owner_label="Meridian read credential",
        effective_scopes=("read.knowledge.search", "read.knowledge.graph_path"),
        policy_environment="pilot",
        tool_tier=1,
        tool_decision="allow",
    )

    result = _project(
        (_receipt(run_id="run-1", event_id="event-exact", receipt_id="exact", sequence=2),),
        joins,
    )

    assert result.status == "complete"
    assert result.unavailable_reasons == ()
    assert result.tool_decision == "allow"
    assert result.receipts[0].audit_sequence == 2


def test_governance_wire_rejects_raw_fields_and_is_required_by_evidence_envelope() -> None:
    with pytest.raises(ValidationError):
        EvidenceLocatorView()

    payload = _project(()).model_dump(mode="json")
    payload["raw_trace"] = {"prompt": "secret", "arguments": ["secret"]}
    with pytest.raises(ValidationError):
        GovernanceView.model_validate(payload)

    with pytest.raises(ValidationError) as missing:
        EvidenceEnvelope.model_validate(
            {
                "agent_id": "agent-1",
                "run_id": "run-1",
                "run_status": "completed",
                "mapping_state": "unavailable",
                "mapping_message": "Claim mapping unavailable",
                "output": {
                    "verdict": "unavailable",
                    "state": "unavailable",
                    "message": "Answer unavailable",
                    "display": None,
                },
                "claims": [],
                "evidence": [],
            }
        )
    assert "governance" in str(missing.value)
