# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the ne-obs KPI/U-P-B reporting tests (NE-068).

Runs against REAL sqlite engines seeded through the source packages' own
factories/appenders (``ne.events.new_audit_event``/``new_correction_signal``,
``ne.audit.append_record``, ``ne.eval.store.EvalRunStore.append``,
``ne.flywheel.SqlAlchemyCorrectionSignalStore.append``) — nothing stubs
``replay``/``list_runs``/``read_signals`` (plan §1.4: no mocking past the
seam). Mirrors ``packages/ne-flywheel/tests/conftest.py`` in style.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit import append_record, ensure_audit_schema
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.eval.store import EvalRunRecord, EvalRunStore, ensure_eval_schema
from ne.events import ensure_outbox_schema, new_audit_event, new_correction_signal
from ne.flywheel import SqlAlchemyCorrectionSignalStore, ensure_flywheel_schema
from ne.schema import AuditOutcome, CorrectionKind


class _DemoSettings(NeSettings):
    demo: int = 7


@pytest.fixture
def frozen_config() -> Iterator[str]:
    """Freeze a deterministic config_hash for the duration of a test."""
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


@pytest.fixture
def audit_engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_audit_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def eval_engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_eval_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def eval_store(eval_engine: sa.Engine) -> EvalRunStore:
    return EvalRunStore(eval_engine)


@pytest.fixture
def signal_engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine("sqlite://")
    ensure_flywheel_schema(eng)
    ensure_outbox_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def signal_store(signal_engine: sa.Engine) -> SqlAlchemyCorrectionSignalStore:
    return SqlAlchemyCorrectionSignalStore(signal_engine)


def seed_audit_event(
    engine: sa.Engine,
    *,
    action: str,
    actor: str,
    occurred_at: datetime,
    outcome: AuditOutcome = AuditOutcome.success,
    provenance: dict[str, Any] | None = None,
    trace_id: str | None = None,
    kpi_tag: str | None = None,
) -> None:
    """Seed one audit-chain row via the real ``new_audit_event``/``append_record`` seam."""
    event = new_audit_event(
        action=action,
        resource="res:test",
        outcome=outcome,
        actor=actor,
        provenance=provenance,
        trace_id=trace_id,
        kpi_tag=kpi_tag,
        occurred_at=occurred_at,
    )
    with engine.begin() as conn:
        append_record(conn, event)


def seed_eval_run(
    store: EvalRunStore,
    *,
    run_id: str,
    created_at: datetime,
    passed: bool,
    suite: str = "rag",
) -> EvalRunRecord:
    """Seed one eval run via the real ``EvalRunStore.append`` seam."""
    record = EvalRunRecord(
        run_id=run_id,
        suite=suite,
        config_hash="test-config-hash",
        golden_set_hash="golden-hash",
        judge_model_id=None,
        judge_family=None,
        rubric_versions={},
        metrics={},
        thresholds={},
        statuses={},
        judge_error_rate=0.0,
        passed=passed,
        created_at=created_at,
    )
    return store.append(record)


def seed_signal(
    store_engine: SqlAlchemyCorrectionSignalStore,
    *,
    kind: CorrectionKind,
    occurred_at: datetime,
    session_id: str = "sess-1",
    principal_id: str = "user:ada@acme.com",
    value: float | None = None,
) -> int:
    """Seed one correction signal via the real ``new_correction_signal``/``append`` seam."""
    signal = new_correction_signal(
        signal_kind=kind,
        subject_ref=f"ref-{occurred_at.isoformat()}-{kind.value}",
        actor=principal_id,
        value=value,
        occurred_at=occurred_at,
    )
    with store_engine.engine.begin() as conn:
        return store_engine.append(
            conn, signal, surface="console", principal_id=principal_id, session_id=session_id
        )
