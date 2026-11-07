# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""collect_utilization against a REAL sqlite audit chain (NE-068)."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from ne.obs.collect import collect_utilization
from ne.obs.kpi import ReportPeriod
from ne.schema import AuditOutcome

from .conftest import seed_audit_event

JUNE = ReportPeriod(year=2026, month=6)


def test_turns_tool_calls_and_active_users(audit_engine: sa.Engine, frozen_config: str) -> None:
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, 5, tzinfo=UTC),
    )
    seed_audit_event(
        audit_engine,
        action="ne.gateway.stream",
        actor="user:grace@acme.com",
        occurred_at=datetime(2026, 6, 6, tzinfo=UTC),
    )
    seed_audit_event(
        audit_engine,
        action="tool.execute",
        actor="agent://gateway",
        occurred_at=datetime(2026, 6, 7, tzinfo=UTC),
    )

    metrics = collect_utilization(audit_engine, JUNE)

    assert metrics.agent_turns == 2
    assert metrics.tool_calls == 1
    assert metrics.active_users == 2
    assert metrics.events_total == 3


def test_next_month_start_excluded_half_open_window(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC),
    )
    # Exactly the first instant of the NEXT month — must be EXCLUDED.
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC),
    )

    metrics = collect_utilization(audit_engine, JUNE)

    assert metrics.agent_turns == 1
    assert metrics.events_total == 1


def test_denied_outcome_is_not_an_agent_turn(audit_engine: sa.Engine, frozen_config: str) -> None:
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, 5, tzinfo=UTC),
        outcome=AuditOutcome.denied,
    )

    metrics = collect_utilization(audit_engine, JUNE)

    assert metrics.agent_turns == 0
    assert metrics.events_total == 1


def test_agent_and_system_actors_excluded_from_active_users(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="agent://flywheel",
        occurred_at=datetime(2026, 6, 5, tzinfo=UTC),
    )
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="system",
        occurred_at=datetime(2026, 6, 6, tzinfo=UTC),
    )
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, 7, tzinfo=UTC),
    )

    metrics = collect_utilization(audit_engine, JUNE)

    assert metrics.active_users == 1


def test_sessions_none_when_no_provenance(audit_engine: sa.Engine, frozen_config: str) -> None:
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, 5, tzinfo=UTC),
    )

    metrics = collect_utilization(audit_engine, JUNE)

    assert metrics.sessions is None


def test_sessions_counted_plain_string_and_provenance_entry_shape(
    audit_engine: sa.Engine, frozen_config: str
) -> None:
    # Plain-string provenance value shape.
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, 5, tzinfo=UTC),
        provenance={"session_id": "sess-a"},
    )
    # ProvenanceEntry {"key", "value"}-shaped provenance value (as it round-trips
    # through JSON when a caller stamps a ProvenanceEntry rather than a plain str).
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:grace@acme.com",
        occurred_at=datetime(2026, 6, 6, tzinfo=UTC),
        provenance={"session_id": {"key": "session_id", "value": "sess-b"}},
    )
    # A repeat of sess-a must not double-count.
    seed_audit_event(
        audit_engine,
        action="ne.gateway.complete",
        actor="user:ada@acme.com",
        occurred_at=datetime(2026, 6, 7, tzinfo=UTC),
        provenance={"session_id": "sess-a"},
    )

    metrics = collect_utilization(audit_engine, JUNE)

    assert metrics.sessions == 2
