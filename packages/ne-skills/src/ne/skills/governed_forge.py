# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The forge as a cron-as-isolated-agent — a structural copy of ne.flywheel.governed.

The forge run is exposed as a single tier-1 tool
``write.internal.skills.forge_run`` plus its handler and a
:class:`~ne.contracts.runtime.ScheduledJob`. The composition root wires a
``ToolBinding(forge_tool_spec(), forge_tool_handler(forge))`` into the ONE
``GovernedToolExecutor`` and schedules the job on the ``CronScheduler``. Because
the job grants EXACTLY one tool, the cron agent is isolated, governed, and
toolset-scoped by the existing NE-034 machinery — every call routes through
guardrails → PDP → audit (C6).

``write.internal.`` classifies the tool at tier 1 under ``tiers.rego``
(actions.py:47-48) — NO Rego change. Dependency direction: this module imports
ONLY the frozen ``ne.contracts.runtime`` / ``ne.contracts.types`` + the
in-package ``ne.skills.forge`` — it does NOT import ``ne.runtime``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping

from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier
from ne.skills.forge import SkillForge

#: The one tier-1 tool the cron agent is granted. ``write.internal.`` ⇒ tier 1.
FORGE_TOOL_NAME = "write.internal.skills.forge_run"


def forge_tool_spec() -> ToolSpec:
    """The frozen :class:`ToolSpec` for the forge run (tier 1, no model args)."""
    return ToolSpec(
        name=FORGE_TOOL_NAME,
        description=(
            "Run one Skill Forge pass: fold completed workflows into a "
            "skill_candidate proposal (proposals-only; never applies a skill). "
            "Takes no arguments."
        ),
        tier=ApprovalTier(1),
    )


def forge_tool_handler(
    forge: SkillForge,
) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    """Return the async tool handler that runs ``forge`` and returns counts JSON.

    The closure IGNORES the model-supplied args entirely: the forge run has no
    injectable surface (it reads the snapshot cursor and writes a proposal — the
    model cannot steer it), so a prompt-injected argument map cannot change what
    the run does. Returns the counts-only :class:`~ne.skills.forge.ForgeOutcome`
    as JSON (never snapshot/pack content — C12).
    """

    async def _handler(_args: Mapping[str, object]) -> str:
        outcome = await forge.run_once()
        return json.dumps(outcome.model_dump(mode="json"))

    return _handler


def scheduled_forge_job(
    *,
    cron: str = "15 3 * * *",
    name: str = "skill-forge",
    agent: str = "skill-forge",
) -> ScheduledJob:
    """A :class:`ScheduledJob` granting EXACTLY the one forge tool."""
    return ScheduledJob(
        name=name,
        cron=cron,
        agent=agent,
        toolset=(FORGE_TOOL_NAME,),
    )


__all__ = [
    "FORGE_TOOL_NAME",
    "forge_tool_handler",
    "forge_tool_spec",
    "scheduled_forge_job",
]
