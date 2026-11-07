# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Skill Gardener as a cron-as-isolated-agent — a structural copy of governed_forge.

The monthly rot sweep is exposed as a single tier-1 tool
``write.internal.skills.gardener_run`` plus its handler and a
:class:`~ne.contracts.runtime.ScheduledJob`. The composition root wires a
``ToolBinding(gardener_tool_spec(), gardener_tool_handler(gardener))`` into the ONE
``GovernedToolExecutor`` and schedules the job on the ``CronScheduler``. Because
the job grants EXACTLY one tool, the cron agent is isolated, governed, and
toolset-scoped by the existing NE-034 machinery — every call routes through
guardrails → PDP → audit (C6).

``write.internal.`` classifies the tool at tier 1 under ``tiers.rego`` — NO Rego
change. The Gardener is model-free, so the handler IGNORES every model-supplied
argument (no injectable surface) and returns a counts-only outcome (C12).
Dependency direction: this module imports ONLY the frozen ``ne.contracts.runtime``
/ ``ne.contracts.types`` + the in-package ``ne.skills.gardener`` — it does NOT
import ``ne.runtime``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping

from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier
from ne.skills.gardener import SkillGardener

#: The one tier-1 tool the cron agent is granted. ``write.internal.`` ⇒ tier 1.
GARDENER_TOOL_NAME = "write.internal.skills.gardener_run"


def gardener_tool_spec() -> ToolSpec:
    """The frozen :class:`ToolSpec` for the gardener run (tier 1, no model args)."""
    return ToolSpec(
        name=GARDENER_TOOL_NAME,
        description=(
            "Run one deterministic monthly Skill Gardener rot sweep: fold "
            "invocation telemetry, surface retirement candidates, failure flags, "
            "trigger overlaps, near-dups, unknown required tools, bundle hash "
            "drift, and stale proposals into ONE persisted digest card. Takes no "
            "arguments."
        ),
        tier=ApprovalTier(1),
    )


def gardener_tool_handler(
    gardener: SkillGardener,
) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    """Return the async tool handler that runs ``gardener`` and returns counts JSON.

    The closure IGNORES the model-supplied args entirely: the gardener run has no
    injectable surface (it reads registry/rollup state and writes ONE report — the
    model cannot steer it), so a prompt-injected argument map cannot change what
    the run does. Returns the counts-only
    :class:`~ne.skills.gardener.GardenerOutcome` as JSON (never pack/proposal
    content — C12)."""

    async def _handler(_args: Mapping[str, object]) -> str:
        import anyio.to_thread

        outcome = await anyio.to_thread.run_sync(gardener.run_once)
        return json.dumps(outcome.model_dump(mode="json"))

    return _handler


def scheduled_gardener_job(
    *,
    cron: str = "30 4 1 * *",
    name: str = "skill-gardener",
    agent: str = "skill-gardener",
) -> ScheduledJob:
    """A :class:`ScheduledJob` granting EXACTLY the one gardener tool."""
    return ScheduledJob(
        name=name,
        cron=cron,
        agent=agent,
        toolset=(GARDENER_TOOL_NAME,),
    )


__all__ = [
    "GARDENER_TOOL_NAME",
    "gardener_tool_handler",
    "gardener_tool_spec",
    "scheduled_gardener_job",
]
