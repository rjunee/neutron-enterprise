# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Domain-semantics proof for the generated event-spine Pydantic models, and the
mechanical proof of contract-freeze option (a): the generated events satisfy the
frozen ``ne.contracts`` EventLike surfaces structurally, without inheritance.

Importing ``ne.contracts`` here is TEST-ONLY (both are co-installed workspace
members in the dev venv); ne-schema's runtime dependency graph is unchanged —
it imports nothing from ne-contracts.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ne.contracts import (
    ApprovalEventLike,
    AuditEventLike,
    CorrectionSignalLike,
    CostEventLike,
    EventLike,
    ShapeViolationLike,
)
from ne.schema import (
    ApprovalDecision,
    ApprovalEvent,
    AuditEvent,
    AuditOutcome,
    CorrectionKind,
    CorrectionSignal,
    CostEvent,
    ShapeViolation,
    ViolationSeverity,
)

_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_HASH = "a" * 64  # a valid 64-char lowercase-hex SHA-256 digest


def _audit(config_hash: str = _HASH) -> AuditEvent:
    return AuditEvent(
        event_id="ev-audit-1",
        event_type="audit",
        config_hash=config_hash,
        actor="agent://demo",
        provenance={"source": "runtime"},
        occurred_at=_TS,
        action="tool.invoke",
        resource="urn:tool:search",
        outcome=AuditOutcome.success,
    )


def _correction(config_hash: str = _HASH) -> CorrectionSignal:
    return CorrectionSignal(
        event_id="ev-corr-1",
        event_type="correction_signal",
        config_hash=config_hash,
        actor="user:ada@acme.com",
        provenance={},
        occurred_at=_TS,
        signal_kind=CorrectionKind.thumbs_down,
        subject_ref="answer-42",
        value=0.5,
    )


def _approval(config_hash: str = _HASH) -> ApprovalEvent:
    return ApprovalEvent(
        event_id="ev-appr-1",
        event_type="approval",
        config_hash=config_hash,
        actor="system",
        provenance={},
        occurred_at=_TS,
        approval_id="appr-1",
        tier=2,
        decision=ApprovalDecision.approved,
        subject_ref="action-7",
    )


def _cost(config_hash: str = _HASH) -> CostEvent:
    return CostEvent(
        event_id="ev-cost-1",
        event_type="cost",
        config_hash=config_hash,
        actor="agent://flywheel",
        provenance={},
        occurred_at=_TS,
        model="claude-opus",
        lane="frontier-api",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.01,
    )


def _shape(config_hash: str = _HASH) -> ShapeViolation:
    return ShapeViolation(
        event_id="ev-shape-1",
        event_type="shape_violation",
        config_hash=config_hash,
        actor="system",
        provenance={},
        occurred_at=_TS,
        schema_name="operational",
        schema_version="1.0.0",
        subject_ref="rec-9",
        message="missing required field",
        severity=ViolationSeverity.error,
    )


# Every concrete event-class builder, keyed by name — the config_hash pattern
# validator is declared on the PlatformEvent base, but LinkML re-emits a
# per-class ``@field_validator`` on every generated subclass, so the rejection
# guarantee is proven per concrete class (not just AuditEvent) below.
_EVENT_BUILDERS = {
    "AuditEvent": _audit,
    "CorrectionSignal": _correction,
    "ApprovalEvent": _approval,
    "CostEvent": _cost,
    "ShapeViolation": _shape,
}


def test_all_five_events_construct() -> None:
    assert _audit().action == "tool.invoke"
    assert _correction().subject_ref == "answer-42"
    assert _approval().tier == 2
    assert _cost().cost_usd == 0.01
    assert _shape().quarantined is None


def test_kpi_tag_defaults_none() -> None:
    assert _audit().kpi_tag is None
    assert _cost().kpi_tag is None


def test_kpi_tag_settable() -> None:
    ev = AuditEvent(
        event_id="ev",
        event_type="audit",
        config_hash=_HASH,
        kpi_tag="latency",
        actor="a",
        provenance={},
        occurred_at=_TS,
        action="x",
        resource="y",
        outcome=AuditOutcome.success,
    )
    assert ev.kpi_tag == "latency"


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                "event_id": "e",
                "event_type": "audit",
                "config_hash": _HASH,
                "actor": "a",
                "provenance": {},
                "occurred_at": _TS,
                "action": "x",
                "resource": "y",
                "outcome": "success",
                "unknown_field": "boom",
            }
        )


def test_missing_actor_rejected() -> None:
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                "event_id": "e",
                "event_type": "audit",
                "config_hash": _HASH,
                "provenance": {},
                "occurred_at": _TS,
                "action": "x",
                "resource": "y",
                "outcome": "success",
            }
        )


@pytest.mark.parametrize("event_name", list(_EVENT_BUILDERS))
@pytest.mark.parametrize("bad", ["xyz", "A" * 64, "a" * 63, "a" * 65, "g" * 64])
def test_config_hash_pattern_enforced(bad: str, event_name: str) -> None:
    # A non-hex / wrong-length config_hash must be rejected on EVERY concrete
    # event class, not just AuditEvent (SPEC §4 config_hash pattern ^[0-9a-f]{64}$).
    build = _EVENT_BUILDERS[event_name]
    with pytest.raises(ValidationError):
        build(config_hash=bad)


def test_config_hash_pattern_accepts_valid_on_every_class() -> None:
    # Guard the negative test: the same builders DO construct with a valid hash,
    # so the rejections above are the pattern firing, not an unrelated failure.
    for build in _EVENT_BUILDERS.values():
        assert build().config_hash == _HASH


@pytest.mark.parametrize("tier", [-1, 4, 99])
def test_approval_tier_bounds_enforced(tier: int) -> None:
    with pytest.raises(ValidationError):
        ApprovalEvent(
            event_id="e",
            event_type="approval",
            config_hash=_HASH,
            actor="a",
            provenance={},
            occurred_at=_TS,
            approval_id="ap",
            tier=tier,
            decision=ApprovalDecision.requested,
            subject_ref="s",
        )


def test_invalid_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                "event_id": "e",
                "event_type": "audit",
                "config_hash": _HASH,
                "actor": "a",
                "provenance": {},
                "occurred_at": _TS,
                "action": "x",
                "resource": "y",
                "outcome": "exploded",
            }
        )


def test_enums_roundtrip() -> None:
    assert _audit().outcome == "success"
    assert _correction().signal_kind == "thumbs_down"
    assert _approval().decision == "approved"
    assert _shape().severity == "error"


def test_events_satisfy_event_like_surface() -> None:
    for ev in (_audit(), _correction(), _approval(), _cost(), _shape()):
        assert isinstance(ev, EventLike)


def test_events_satisfy_named_subsurfaces() -> None:
    assert isinstance(_audit(), AuditEventLike)
    assert isinstance(_correction(), CorrectionSignalLike)
    assert isinstance(_approval(), ApprovalEventLike)
    assert isinstance(_cost(), CostEventLike)
    assert isinstance(_shape(), ShapeViolationLike)


def _static(e: AuditEvent) -> EventLike:
    # pyright gates this: an AuditEvent is statically assignable to the frozen
    # EventLike Protocol — the mechanical proof of contract-freeze option (a).
    return e


def test_static_assignability_holds_at_runtime() -> None:
    audit = _audit()
    assert _static(audit) is audit
