# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed constructors for the ne.schema event classes.

Each factory stamps the SPEC §4 non-optional fields so callers cannot forget
them: a fresh ``event_id`` (uuid4), ``occurred_at`` = now (UTC), an explicit
``provenance`` map (``{}`` by default), a fixed canonical ``event_type`` per
class, and — critically — ``config_hash`` resolved from the FROZEN process hash
via :func:`ne.contracts.get_config_hash`. config_hash stamping is non-optional
(plan §1.4 / SPEC §4): if the hash is not frozen, ``ConfigHashNotFrozenError``
propagates rather than an event being emitted unstamped.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ne.contracts import get_config_hash
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


def _base_fields(
    *,
    event_type: str,
    actor: str,
    kpi_tag: str | None,
    trace_id: str | None,
    provenance: Mapping[str, str] | None,
    config_hash: str | None,
    event_id: str | None,
    occurred_at: datetime | None,
) -> dict[str, Any]:
    return {
        "event_id": event_id if event_id is not None else str(uuid.uuid4()),
        "event_type": event_type,
        "config_hash": config_hash if config_hash is not None else get_config_hash(),
        "kpi_tag": kpi_tag,
        "actor": actor,
        "provenance": dict(provenance) if provenance is not None else {},
        "occurred_at": occurred_at if occurred_at is not None else datetime.now(UTC),
        "trace_id": trace_id,
    }


def new_audit_event(
    *,
    action: str,
    resource: str,
    outcome: AuditOutcome,
    actor: str,
    detail: str | None = None,
    kpi_tag: str | None = None,
    trace_id: str | None = None,
    provenance: Mapping[str, str] | None = None,
    config_hash: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> AuditEvent:
    return AuditEvent(
        **_base_fields(
            event_type="audit",
            actor=actor,
            kpi_tag=kpi_tag,
            trace_id=trace_id,
            provenance=provenance,
            config_hash=config_hash,
            event_id=event_id,
            occurred_at=occurred_at,
        ),
        action=action,
        resource=resource,
        outcome=outcome,
        detail=detail,
    )


def new_correction_signal(
    *,
    signal_kind: CorrectionKind,
    subject_ref: str,
    actor: str,
    value: float | None = None,
    comment: str | None = None,
    kpi_tag: str | None = None,
    trace_id: str | None = None,
    provenance: Mapping[str, str] | None = None,
    config_hash: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> CorrectionSignal:
    return CorrectionSignal(
        **_base_fields(
            event_type="correction_signal",
            actor=actor,
            kpi_tag=kpi_tag,
            trace_id=trace_id,
            provenance=provenance,
            config_hash=config_hash,
            event_id=event_id,
            occurred_at=occurred_at,
        ),
        signal_kind=signal_kind,
        subject_ref=subject_ref,
        value=value,
        comment=comment,
    )


def new_approval_event(
    *,
    approval_id: str,
    tier: int,
    decision: ApprovalDecision,
    subject_ref: str,
    actor: str,
    sla_deadline: datetime | None = None,
    kpi_tag: str | None = None,
    trace_id: str | None = None,
    provenance: Mapping[str, str] | None = None,
    config_hash: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> ApprovalEvent:
    return ApprovalEvent(
        **_base_fields(
            event_type="approval",
            actor=actor,
            kpi_tag=kpi_tag,
            trace_id=trace_id,
            provenance=provenance,
            config_hash=config_hash,
            event_id=event_id,
            occurred_at=occurred_at,
        ),
        approval_id=approval_id,
        tier=tier,
        decision=decision,
        subject_ref=subject_ref,
        sla_deadline=sla_deadline,
    )


def new_cost_event(
    *,
    model: str,
    lane: str,
    actor: str,
    provider: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    task: str | None = None,
    latency_ms: int | None = None,
    ttft_ms: int | None = None,
    kpi_tag: str | None = None,
    trace_id: str | None = None,
    provenance: Mapping[str, str] | None = None,
    config_hash: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> CostEvent:
    return CostEvent(
        **_base_fields(
            event_type="cost",
            actor=actor,
            kpi_tag=kpi_tag,
            trace_id=trace_id,
            provenance=provenance,
            config_hash=config_hash,
            event_id=event_id,
            occurred_at=occurred_at,
        ),
        model=model,
        provider=provider,
        lane=lane,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        task=task,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
    )


def new_shape_violation(
    *,
    schema_name: str,
    schema_version: str,
    subject_ref: str,
    message: str,
    severity: ViolationSeverity,
    actor: str,
    quarantined: bool | None = None,
    kpi_tag: str | None = None,
    trace_id: str | None = None,
    provenance: Mapping[str, str] | None = None,
    config_hash: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> ShapeViolation:
    return ShapeViolation(
        **_base_fields(
            event_type="shape_violation",
            actor=actor,
            kpi_tag=kpi_tag,
            trace_id=trace_id,
            provenance=provenance,
            config_hash=config_hash,
            event_id=event_id,
            occurred_at=occurred_at,
        ),
        schema_name=schema_name,
        schema_version=schema_version,
        subject_ref=subject_ref,
        message=message,
        severity=severity,
        quarantined=quarantined,
    )
