# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Default-off governed sweep for due Answer Ledger re-asks."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio.to_thread

from ne.api.answer_ledger import AnswerLedgerService
from ne.brain.answer_ledger import SqlAlchemyAnswerLedgerStore
from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.permissions.resolver import StorePrincipalResolver
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.tools import ToolBinding
from ne.worker.cron_seed import reconcile_prefixed_jobs

if TYPE_CHECKING:
    from ne.api.platform import Platform
    from ne.runtime.scheduler import CronScheduler
    from ne.worker.settings import WorkerSettings

_JOB_NAME = "answer-ledger-reask"
_TOOL = "write.internal.answer_ledger.reask_due"


@dataclass(frozen=True)
class AnswerLedgerReaskSurface:
    bindings: tuple[ToolBinding, ...]
    definitions: dict[str, AgentDefinition]
    principals: dict[str, Principal]
    jobs: tuple[ScheduledJob, ...]


_EMPTY = AnswerLedgerReaskSurface(bindings=(), definitions={}, principals={}, jobs=())


def _service_for(platform: Platform, store: SqlAlchemyAnswerLedgerStore) -> AnswerLedgerService:
    return AnswerLedgerService(
        store=store,
        retriever=platform.retriever,
        lineage=platform.document_lineage,
        gateway=platform.gateway,
        sink=platform.sink,
        expander=platform.expander,
        runtime_settings=platform.runtime_settings,
        config_hash=platform.config_hash,
        top_k=platform.api_settings.retrieval_top_k,
    )


async def run_due_reasks(platform: Platform, *, limit: int) -> int:
    """Re-ask a bounded due set, resolving every owner from current store groups."""

    store = SqlAlchemyAnswerLedgerStore(platform.audit_engine)
    due = await anyio.to_thread.run_sync(lambda: store.due_questions(limit=limit))
    resolver = StorePrincipalResolver(platform.expander)
    service = _service_for(platform, store)
    completed = 0
    for question in due:
        principal = await resolver.resolve(question.owner_id)
        await service.reask(principal, question)
        completed += 1
    return completed


def _handler(platform: Platform, limit: int) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    async def run(_args: Mapping[str, object]) -> str:
        completed = await run_due_reasks(platform, limit=limit)
        return json.dumps({"reasked": completed}, sort_keys=True)

    return run


def build_answer_ledger_reask_surface(
    platform: Platform, settings: WorkerSettings
) -> AnswerLedgerReaskSurface:
    """Build one governed sweep job, or no surface when explicitly disabled."""

    if not settings.answer_ledger_reask_enabled:
        return _EMPTY
    tool = ToolSpec(
        name=_TOOL,
        description="Re-ask the bounded set of due, explicitly scheduled Answer Ledger questions.",
        tier=ApprovalTier(1),
    )
    definition = AgentDefinition(
        name=_JOB_NAME,
        instructions=f"Call {_TOOL} exactly once, then report its counts-only result.",
        toolset=(_TOOL,),
        # Sole-tool: no scheduler-driven model loop on the cron tick path (the
        # 63279abe invariant, enforced at boot by require_sole_tool_definitions).
        # The re-ask sweep's own governed asks remain the tool's audited work.
        scheduled_execution="sole-tool",
    )
    job = ScheduledJob(
        name=_JOB_NAME,
        cron=settings.answer_ledger_reask_cron,
        agent=_JOB_NAME,
        toolset=(_TOOL,),
    )
    return AnswerLedgerReaskSurface(
        bindings=(
            ToolBinding(spec=tool, handler=_handler(platform, settings.answer_ledger_reask_limit)),
        ),
        definitions={_JOB_NAME: definition},
        principals={_JOB_NAME: Principal(id=f"agent:{_JOB_NAME}")},
        jobs=(job,),
    )


def seed_answer_ledger_reask_jobs(scheduler: CronScheduler, jobs: tuple[ScheduledJob, ...]) -> None:
    reconcile_prefixed_jobs(scheduler, _JOB_NAME, jobs)


__all__ = [
    "AnswerLedgerReaskSurface",
    "build_answer_ledger_reask_surface",
    "run_due_reasks",
    "seed_answer_ledger_reask_jobs",
]
