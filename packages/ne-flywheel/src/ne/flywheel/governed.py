# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-034 integration surface: the curation agent as a cron-as-isolated-agent.

The curation run is exposed as a single tier-1 tool
``write.internal.flywheel.curation_run`` plus its handler and a
:class:`~ne.contracts.runtime.ScheduledJob`. The composition root wires a
``ToolBinding(curation_tool_spec(), curation_tool_handler(agent))`` into the ONE
``GovernedToolExecutor`` and schedules the job on the ``CronScheduler``. Because
the job grants EXACTLY one tool, the cron agent is isolated, governed, and
toolset-scoped by NE-034's existing ``ToolsetScopedExecutor``/``CronScheduler``
machinery — every tool call still routes through the full guardrails -> PDP ->
audit pipeline (C6), and the agent gets NO broad tool access.

``write.internal.`` classifies the tool at tier 1 under ``tiers.rego`` (SPEC
§4.2 tier 1 = autonomous + human-on-the-loop digest) — the right posture for a
proposals-only internal write that never applies a change.

Dependency direction: this module imports ONLY the frozen
``ne.contracts.runtime`` types — it does NOT import ``ne.runtime``. Importing
ne-runtime into ne-flywheel src would invert the dependency direction and drag
pydantic-ai/dbos into the flywheel closure. Tests may import ``ne.runtime`` (the
workspace installs every package) to assert the shipped spec validates under
``validate_binding``; the live wiring is composition-root work (LIVE-PARKABLE to
NE-070) — the README carries the snippet.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping

from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier
from ne.flywheel.curation import CurationAgent

#: The one tier-1 tool the cron agent is granted. ``write.internal.`` prefix =>
#: tier 1 under tiers.rego (and ``ne.runtime.actions.tier_for_action``).
CURATION_TOOL_NAME = "write.internal.flywheel.curation_run"


def curation_tool_spec() -> ToolSpec:
    """The frozen :class:`ToolSpec` for the curation run (tier 1, no model args).

    ``input_schema`` is empty: the run takes NO model-supplied arguments — the
    handler ignores any args entirely (there is no injectable surface).
    """
    return ToolSpec(
        name=CURATION_TOOL_NAME,
        description=(
            "Run one flywheel curation pass: cluster correction signals into "
            "typed fix proposals (proposals-only; never applies a change). "
            "Takes no arguments."
        ),
        tier=ApprovalTier(1),
    )


def curation_tool_handler(
    agent: CurationAgent,
) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    """Return the async tool handler that runs ``agent`` and returns counts JSON.

    The returned closure IGNORES the model-supplied args entirely: the curation
    run has no injectable surface (it reads the signal cursor and writes
    proposals — the model cannot steer it), so a prompt-injected argument map
    cannot change what the run does. Returns the counts-only
    :class:`~ne.flywheel.curation.CurationOutcome` as JSON (never signal
    content — C12).
    """

    async def _handler(_args: Mapping[str, object]) -> str:
        outcome = await agent.run_once()
        return json.dumps(outcome.model_dump(mode="json"))

    return _handler


def scheduled_curation_job(
    *,
    cron: str = "0 2 * * *",
    name: str = "flywheel-curation",
    agent: str = "flywheel-curation",
) -> ScheduledJob:
    """A :class:`ScheduledJob` granting EXACTLY the one curation tool.

    ``toolset=(CURATION_TOOL_NAME,)`` is the whole per-job grant, so the cron
    agent is isolated + governed + toolset-scoped by the existing NE-034
    machinery. Default schedule is 02:00 UTC daily.
    """
    return ScheduledJob(
        name=name,
        cron=cron,
        agent=agent,
        toolset=(CURATION_TOOL_NAME,),
    )


__all__ = [
    "CURATION_TOOL_NAME",
    "curation_tool_handler",
    "curation_tool_spec",
    "scheduled_curation_job",
]
