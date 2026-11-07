# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The invocation-telemetry fold — idempotency, counters, R-26 no-double-count."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from ne.audit import append_record, ensure_transactional_sqlite
from ne.audit.store import ensure_audit_schema
from ne.contracts.skills import SkillInvocation
from ne.contracts.types import Principal
from ne.events import new_audit_event
from ne.schema import AuditOutcome
from ne.skills.registry import SqlAlchemySkillRegistry, ne_skill_usage
from ne.skills.telemetry import attribute_outcomes, fold_invocations
from ne.skills.tools import build_view_handler

from .conftest import make_pack


def _usage_row(engine: sa.Engine, name: str) -> sa.Row | None:  # type: ignore[type-arg]
    with engine.begin() as conn:
        return conn.execute(
            sa.select(ne_skill_usage).where(ne_skill_usage.c.skill_name == name)
        ).first()


def _fire(registry: SqlAlchemySkillRegistry, name: str, *, run_succeeded: bool | None) -> None:
    registry.append_invocation(
        SkillInvocation(
            skill_name=name,
            version=1,
            principal_id="u-1",
            session_id="s-1",
            fired_at=datetime.now(UTC),
            run_succeeded=run_succeeded,
        )
    )


def test_fold_counters_success_fail_unknown(registry: SqlAlchemySkillRegistry, engine) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    _fire(registry, "alpha", run_succeeded=True)
    _fire(registry, "alpha", run_succeeded=True)
    _fire(registry, "alpha", run_succeeded=False)
    _fire(registry, "alpha", run_succeeded=None)

    outcome = fold_invocations(engine)
    assert outcome.rows_folded == 4
    row = _usage_row(engine, "alpha")
    assert row is not None
    assert row.fired_count == 4
    assert row.success_count == 2
    assert row.fail_count == 1
    assert row.unknown_count == 1


def test_fold_is_cursor_idempotent(registry: SqlAlchemySkillRegistry, engine) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    _fire(registry, "alpha", run_succeeded=True)
    _fire(registry, "alpha", run_succeeded=True)

    first = fold_invocations(engine)
    assert first.rows_folded == 2
    # A second fold with NO new rows folds nothing and leaves counters unchanged.
    second = fold_invocations(engine)
    assert second.rows_folded == 0
    assert second.cursor == first.cursor
    row = _usage_row(engine, "alpha")
    assert row is not None and row.fired_count == 2

    # A new firing is picked up incrementally (folds exactly the delta).
    _fire(registry, "alpha", run_succeeded=False)
    third = fold_invocations(engine)
    assert third.rows_folded == 1
    row = _usage_row(engine, "alpha")
    assert row is not None and row.fired_count == 3 and row.fail_count == 1


def test_r26_no_double_count_through_real_view_handler(
    registry: SqlAlchemySkillRegistry, engine, expander, settings
) -> None:
    """The REAL view handler is the SOLE writer of ne_skill_invocations; two folds
    yield fired_count == N (never 2N — no drain-through-substrate re-insert)."""
    registry.register_pack(make_pack("alpha", version=1), origin="bundle")
    principal = Principal(id="u-1", claims={"sid": "session-xyz"})
    view = build_view_handler(registry, expander, settings, lambda: principal)

    n = 5
    for _ in range(n):
        asyncio.run(view({"name": "alpha"}))

    fold_invocations(engine)
    fold_invocations(engine)  # a second pass must NOT re-count the same firings
    row = _usage_row(engine, "alpha")
    assert row is not None
    assert row.fired_count == n  # exactly N, never 2N


def test_telemetry_source_never_references_collect_invocations() -> None:
    """R-26 structural proof: the fold NEVER round-trips through collect_invocations."""
    import ne.skills.telemetry as telemetry

    source = Path(telemetry.__file__).read_text(encoding="utf-8")
    # Only the module docstring may NAME the method (to explain the decision); no
    # call site exists. Assert there is no attribute access on it.
    assert ".collect_invocations(" not in source


def test_attribute_outcomes_backfills_from_terminal_audit(
    registry: SqlAlchemySkillRegistry, engine, tmp_path: Path, frozen_config: str
) -> None:
    audit_engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    ensure_transactional_sqlite(audit_engine)
    ensure_audit_schema(audit_engine)
    # A terminal session.run event whose trace_id == the firing's session_id.
    with audit_engine.begin() as conn:
        append_record(
            conn,
            new_audit_event(
                action="session.run",
                resource="session:trace-ok",
                outcome=AuditOutcome.success,
                actor="u-1",
                detail="{}",
                trace_id="trace-ok",
            ),
        )

    registry.register_pack(make_pack("alpha"), origin="bundle")
    # matchable firing (session_id == a terminal trace_id)
    registry.append_invocation(
        SkillInvocation(
            skill_name="alpha",
            version=1,
            principal_id="u-1",
            session_id="trace-ok",
            fired_at=datetime.now(UTC),
        )
    )
    # unmatchable firing (no terminal event for its session) — stays NULL/unknown
    registry.append_invocation(
        SkillInvocation(
            skill_name="alpha",
            version=1,
            principal_id="u-1",
            session_id="trace-missing",
            fired_at=datetime.now(UTC),
        )
    )

    updated = attribute_outcomes(engine, audit_engine)
    assert updated == 1

    fold_invocations(engine)
    row = _usage_row(engine, "alpha")
    assert row is not None
    assert row.fired_count == 2
    assert row.success_count == 1  # the matched firing
    assert row.unknown_count == 1  # the unmatchable firing stays NULL → unknown
    audit_engine.dispose()


def test_attribute_outcomes_paginates_past_the_batch_limit(
    registry: SqlAlchemySkillRegistry, engine, tmp_path: Path, frozen_config: str
) -> None:
    """Codex P1: attribution must reach EVERY attributable pre-cursor row before the
    fold advances the cursor — not just the first ``limit`` rows. With a tiny batch
    limit and more matchable firings than one batch, all of them are still resolved
    (a matchable row beyond the first batch must not be folded as ``unknown``)."""
    audit_engine = sa.create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    ensure_transactional_sqlite(audit_engine)
    ensure_audit_schema(audit_engine)

    n = 5
    with audit_engine.begin() as conn:
        for i in range(n):
            append_record(
                conn,
                new_audit_event(
                    action="session.run",
                    resource=f"session:trace-{i}",
                    outcome=AuditOutcome.success,
                    actor="u-1",
                    detail="{}",
                    trace_id=f"trace-{i}",
                ),
            )

    registry.register_pack(make_pack("alpha"), origin="bundle")
    for i in range(n):
        registry.append_invocation(
            SkillInvocation(
                skill_name="alpha",
                version=1,
                principal_id="u-1",
                session_id=f"trace-{i}",
                fired_at=datetime.now(UTC),
            )
        )

    # limit=1 forces multiple batches; every row must still be attributed.
    updated = attribute_outcomes(engine, audit_engine, limit=1)
    assert updated == n

    fold_invocations(engine)
    row = _usage_row(engine, "alpha")
    assert row is not None
    assert row.fired_count == n
    assert row.success_count == n  # ALL attributed — none left folded as unknown
    assert row.unknown_count == 0
    audit_engine.dispose()


def test_attribute_outcomes_no_audit_engine_is_zero(
    registry: SqlAlchemySkillRegistry, engine
) -> None:
    registry.register_pack(make_pack("alpha"), origin="bundle")
    _fire(registry, "alpha", run_succeeded=None)
    assert attribute_outcomes(engine, None) == 0
