# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-034 integration-surface tests for the curation agent (NE-065).

These tests MAY import ``ne.runtime`` (the workspace installs every package) to
prove the shipped ToolSpec registers under the ONE governed executor. The
production wiring is composition-root work (LIVE-PARKABLE to NE-070) — the
README snippet is the artifact; the full CronScheduler -> ToolsetScopedExecutor
cron tick is not exercised here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import sqlalchemy as sa

from ne.contracts.types import ApprovalTier
from ne.flywheel.curation import CurationAgent
from ne.flywheel.governed import (
    CURATION_TOOL_NAME,
    curation_tool_handler,
    curation_tool_spec,
    scheduled_curation_job,
)
from ne.runtime.tools import ToolBinding, validate_binding


def test_curation_tool_name_classifies_tier1() -> None:
    spec = curation_tool_spec()
    assert spec.name == CURATION_TOOL_NAME
    assert spec.name.startswith("write.internal.")
    assert spec.tier == ApprovalTier(1)


def test_tool_binding_validates_under_runtime_registry(
    curation_engine: sa.Engine,
) -> None:
    agent = CurationAgent(curation_engine)
    binding = ToolBinding(
        spec=curation_tool_spec(),
        handler=curation_tool_handler(agent),
    )
    # Does not raise: the shipped spec is registrable in the governed executor.
    validate_binding(binding)


def test_handler_ignores_model_args_and_returns_counts_json(
    curation_engine: sa.Engine,
    frozen_config: str,
    seed_signal: Callable[..., str],
) -> None:
    from ne.flywheel.signals import ThumbsSignal

    seed_signal(ThumbsSignal(up=False, target_ref="doc-1"))
    agent = CurationAgent(curation_engine)
    handler = curation_tool_handler(agent)

    # Hostile args are ignored — the run has no injectable surface.
    async def _invoke() -> str:
        return await handler({"batch_limit": 999999, "cursor": -1})

    result = asyncio.run(_invoke())
    payload = json.loads(result)
    assert payload["signals_read"] == 1
    assert set(payload).issuperset(
        {"run_id", "cursor_start", "cursor_end", "clusters", "lane_counts"}
    )

    # The run was audited (exactly one curation.run audit-typed outbox event;
    # the seeded signal adds its own correction_signal event to the same outbox).
    from ne.events.outbox import ne_outbox_events

    with curation_engine.connect() as conn:
        audit_count = conn.execute(
            sa.select(sa.func.count())
            .select_from(ne_outbox_events)
            .where(ne_outbox_events.c.event_type == "audit")
        ).scalar_one()
    assert audit_count == 1


def test_scheduled_curation_job_grants_exactly_one_tool() -> None:
    job = scheduled_curation_job()
    assert job.toolset == (CURATION_TOOL_NAME,)
    assert job.cron == "0 2 * * *"
