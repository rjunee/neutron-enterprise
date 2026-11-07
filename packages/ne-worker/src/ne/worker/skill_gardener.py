# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-4: the governed Skill Gardener cron surface (mirrors ne.worker.skill_forge).

The scheduled monthly rot sweep is exposed as ONE tier-1 tool
``write.internal.skills.gardener_run`` + its handler + an :class:`AgentDefinition`
granting EXACTLY that tool + a :class:`ScheduledJob`, so it runs as an isolated,
toolset-scoped governed agent through the ONE ``GovernedToolExecutor`` (C6):
PDP-checked, guardrailed, audited (job.run + the tool.execute event).

UNLIKE the forge, the Gardener is MODEL-FREE — it dispatches no sub-agent, makes
no gateway call, adds no egress surface (C2/C3). The whole sweep is deterministic
SQLAlchemy + stdlib; the agent's only job is to call the one tool once.

Fail-closed default-off: the surface is EMPTY unless ``platform.skills_engine`` +
``platform.skills_settings`` are present AND ``skills_settings.gardener_enabled``
is True. An empty surface seeds zero jobs. The ``skill-gardener`` job-name prefix
is disjoint from ``skill-forge`` so the two non-destructive reconcilers never
touch each other's persisted schedules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import Principal
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.tools import ToolBinding
from ne.skills.gardener import SkillGardener
from ne.skills.governed_gardener import (
    GARDENER_TOOL_NAME,
    gardener_tool_handler,
    gardener_tool_spec,
    scheduled_gardener_job,
)
from ne.worker.cron_seed import reconcile_prefixed_jobs

if TYPE_CHECKING:
    from ne.api.platform import Platform
    from ne.runtime.scheduler import CronScheduler

_JOB_NAME = "skill-gardener"


@dataclass(frozen=True)
class SkillGardenerSurface:
    """The governed cron surface for the Skill Gardener (empty when default-off)."""

    bindings: tuple[ToolBinding, ...]
    definitions: dict[str, AgentDefinition]
    principals: dict[str, Principal]
    jobs: tuple[ScheduledJob, ...]


_EMPTY = SkillGardenerSurface(bindings=(), definitions={}, principals={}, jobs=())


def build_skill_gardener_surface(platform: Platform) -> SkillGardenerSurface:
    """Build the governed gardener cron surface, or the EMPTY surface (default-off).

    EMPTY unless ``skills_engine`` + ``skills_settings`` are present AND
    ``gardener_enabled`` is True. The gardener reads its thresholds from
    ``skills_settings`` and its tool catalogue from the frozen executor's tool
    specs (for the ``requires_tools`` cross-check)."""
    settings = platform.skills_settings
    if platform.skills_engine is None or settings is None or not settings.gardener_enabled:
        return _EMPTY

    skills_engine = platform.skills_engine
    gardener_principal = Principal(id="agent:skill-gardener")

    # The gardener READS the flywheel proposal + review tables (stale-proposal
    # flag) that ride the SAME signal DB as the skills registry. They exist
    # wherever the forge/review service runs, but the gardener can be enabled with
    # the forge OFF, so ensure them idempotently here (the forge surface's
    # ensure_forge_schema precedent).
    from ne.flywheel.proposals import ensure_proposal_schema
    from ne.flywheel.review import ensure_review_schema

    ensure_proposal_schema(skills_engine)
    ensure_review_schema(skills_engine)

    known_tools = frozenset(spec.name for spec in platform.executor.tool_specs)
    gardener = SkillGardener(
        skills_engine,
        settings=settings,
        audit_engine=platform.audit_engine,
        known_tools=known_tools,
    )

    tool: ToolSpec = gardener_tool_spec()
    binding = ToolBinding(spec=tool, handler=gardener_tool_handler(gardener))
    gardener_definition = AgentDefinition(
        name=_JOB_NAME,
        instructions=(
            f"Call the tool {GARDENER_TOOL_NAME} exactly once to run one scheduled "
            "governed Skill Gardener rot sweep, then report its JSON counts."
        ),
        toolset=(GARDENER_TOOL_NAME,),
        # Deterministic maintenance: the scheduler invokes the one governed tool
        # directly — no per-fire model loop, so the shared litellm Router stays
        # off the worker's cron tick loop (the 63279abe invariant, enforced at
        # boot by ne.worker.cron_seed.require_sole_tool_definitions).
        scheduled_execution="sole-tool",
    )
    job = scheduled_gardener_job(cron=settings.gardener_cron, name=_JOB_NAME, agent=_JOB_NAME)
    return SkillGardenerSurface(
        bindings=(binding,),
        definitions={_JOB_NAME: gardener_definition},
        principals={_JOB_NAME: gardener_principal},
        jobs=(job,),
    )


def seed_skill_gardener_jobs(scheduler: CronScheduler, jobs: tuple[ScheduledJob, ...]) -> None:
    """Reconcile the ``skill-gardener`` job on ``scheduler`` toward ``jobs``.

    Delegates to the shared non-destructive :func:`reconcile_prefixed_jobs`
    (prefix ``skill-gardener``, disjoint from ``skill-forge``) so a transiently-
    unconfigured worker reboot never wipes a peer's persisted schedule on the
    shared runtime DB."""
    reconcile_prefixed_jobs(scheduler, _JOB_NAME, jobs)
