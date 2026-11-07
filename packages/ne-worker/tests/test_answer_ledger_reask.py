# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

import ne.worker.answer_ledger_reask as subject
from ne.api.platform import Platform
from ne.brain.answer_ledger import SqlAlchemyAnswerLedgerStore
from ne.worker.settings import WorkerSettings


def _engine() -> sa.Engine:
    return sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _platform(**values: object) -> Platform:
    return cast(Platform, SimpleNamespace(**values))


class _Expander:
    def __init__(self) -> None:
        self.groups = {"user:ada", "group:old"}

    def expand(self, principal_id: str) -> set[str]:
        assert principal_id == "user:ada"
        return set(self.groups)


class _Service:
    def __init__(self) -> None:
        self.seen = []

    async def reask(self, principal, question) -> None:
        self.seen.append((principal, question))


def test_surface_is_default_off() -> None:
    surface = subject.build_answer_ledger_reask_surface(
        _platform(), WorkerSettings(answer_ledger_reask_enabled=False)
    )
    assert surface.bindings == ()
    assert surface.jobs == ()


def test_enabled_surface_is_one_bounded_governed_job() -> None:
    surface = subject.build_answer_ledger_reask_surface(
        _platform(),
        WorkerSettings(
            answer_ledger_reask_enabled=True,
            answer_ledger_reask_cron="*/15 * * * *",
            answer_ledger_reask_limit=7,
        ),
    )
    assert [binding.spec.name for binding in surface.bindings] == [
        "write.internal.answer_ledger.reask_due"
    ]
    assert [(job.name, job.cron, job.toolset) for job in surface.jobs] == [
        (
            "answer-ledger-reask",
            "*/15 * * * *",
            ("write.internal.answer_ledger.reask_due",),
        )
    ]
    # 2026-08-29 U13 review (F6): sole-tool — no scheduler-driven model loop on
    # the cron tick path (require_sole_tool_definitions enforces this at boot).
    assert surface.definitions["answer-ledger-reask"].scheduled_execution == "sole-tool"


def test_due_sweep_is_bounded_and_resolves_current_groups_at_execution(monkeypatch) -> None:
    engine = _engine()
    now = datetime.now(UTC)
    store = SqlAlchemyAnswerLedgerStore(engine, clock=lambda: now - timedelta(hours=2))
    store.pin("user:ada", "What changed?")
    store.set_reask_schedule("user:ada", "hourly")
    expander = _Expander()
    platform = _platform(audit_engine=engine, expander=expander)
    service = _Service()
    monkeypatch.setattr(subject, "_service_for", lambda _platform, _store: service)

    # Membership changes after scheduling. The handler must resolve this value now,
    # never retain the old expansion in durable schedule state.
    expander.groups = {"user:ada", "group:current"}
    assert asyncio.run(subject.run_due_reasks(platform, limit=1)) == 1
    [(principal, question)] = service.seen
    assert principal.id == "user:ada"
    assert principal.groups == ("group:current",)
    assert question.reask_schedule == "hourly"
