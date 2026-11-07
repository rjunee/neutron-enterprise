# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The event factories stamp SPEC §4 fields and REFUSE to emit unstamped events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.contracts import (
    ConfigHashNotFrozenError,
    EventLike,
    reset_config_hash_for_testing,
)
from ne.events import (
    append,
    new_approval_event,
    new_audit_event,
    new_correction_signal,
    new_cost_event,
    new_shape_violation,
)
from ne.schema import ApprovalDecision, AuditOutcome, CorrectionKind, ViolationSeverity


def test_config_hash_stamped_from_frozen_process_hash(frozen_config: str) -> None:
    ev = new_audit_event(action="a", resource="r", outcome=AuditOutcome.success, actor="sys")
    assert ev.config_hash == frozen_config


def test_all_factories_stamp_the_frozen_hash(frozen_config: str) -> None:
    events = [
        new_audit_event(action="a", resource="r", outcome=AuditOutcome.denied, actor="s"),
        new_correction_signal(signal_kind=CorrectionKind.thumbs_up, subject_ref="x", actor="s"),
        new_approval_event(
            approval_id="ap",
            tier=1,
            decision=ApprovalDecision.approved,
            subject_ref="x",
            actor="s",
        ),
        new_cost_event(model="m", lane="private-vllm", actor="s"),
        new_shape_violation(
            schema_name="operational",
            schema_version="1.0.0",
            subject_ref="x",
            message="bad",
            severity=ViolationSeverity.error,
            actor="s",
        ),
    ]
    for ev in events:
        assert ev.config_hash == frozen_config
        assert isinstance(ev, EventLike)


def test_refuses_to_emit_without_frozen_hash() -> None:
    reset_config_hash_for_testing()
    try:
        with pytest.raises(ConfigHashNotFrozenError):
            new_audit_event(action="a", resource="r", outcome=AuditOutcome.success, actor="sys")
    finally:
        reset_config_hash_for_testing()


def test_explicit_config_hash_overrides_lookup() -> None:
    # An explicitly supplied hash bypasses get_config_hash() (no freeze needed).
    reset_config_hash_for_testing()
    ev = new_audit_event(
        action="a",
        resource="r",
        outcome=AuditOutcome.success,
        actor="sys",
        config_hash="b" * 64,
    )
    assert ev.config_hash == "b" * 64


def test_defaults_uuid_event_id_occurred_at_and_empty_provenance(frozen_config: str) -> None:
    before = datetime.now(UTC)
    ev = new_cost_event(model="m", lane="frontier-api", actor="s")
    after = datetime.now(UTC)
    uuid.UUID(ev.event_id)  # parses -> a valid uuid4 string
    assert ev.event_type == "cost"
    assert before <= ev.occurred_at <= after
    assert ev.provenance == {}
    assert ev.kpi_tag is None


def test_provenance_and_kpi_tag_pass_through(frozen_config: str) -> None:
    ev = new_audit_event(
        action="a",
        resource="r",
        outcome=AuditOutcome.success,
        actor="s",
        kpi_tag="latency",
        provenance={"source": "runtime"},
    )
    assert ev.kpi_tag == "latency"
    assert ev.provenance == {"source": "runtime"}


def test_factory_output_appends_cleanly(engine: sa.Engine, frozen_config: str) -> None:
    ev = new_shape_violation(
        schema_name="operational",
        schema_version="1.0.0",
        subject_ref="rec-1",
        message="missing field",
        severity=ViolationSeverity.warning,
        actor="sys",
    )
    with engine.begin() as conn:
        new_id = append(conn, ev)
    assert new_id == 1
