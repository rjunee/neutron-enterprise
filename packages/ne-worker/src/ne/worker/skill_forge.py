# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-SKILL-1b: the governed Skill Forge cron surface (mirrors ne.worker.principal_sync).

The scheduled forge run is exposed as ONE tier-1 tool
``write.internal.skills.forge_run`` + its handler + an :class:`AgentDefinition`
granting EXACTLY that tool + a :class:`ScheduledJob`, so it runs as an isolated,
toolset-scoped governed agent through the ONE ``GovernedToolExecutor`` (C6):
PDP-checked, guardrailed, audited (job.run + the forge's own skill.forge_run
event).

The forge's authoring agent is a TOOLLESS ``skill-author`` sub-agent dispatched
through a ``GovernedSubAgentDispatcher`` on the private-vllm lane (C3): snapshot
intents are customer text, so frontier is structurally refused
(``allow_frontier_subagent_routing`` stays default False). The dispatcher wrapper
is injected into the substrate so ne-skills never imports ne-runtime (the D-10
boundary).

Fail-closed default-off: the surface is EMPTY unless ``platform.skills_engine`` +
``platform.skills_settings`` are present AND ``skills_settings.forge_enabled`` is
True (R-6: no forge GA until NE-SKILL-2). An empty surface seeds zero jobs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ne.contracts.runtime import ScheduledJob, SubAgentSpec, ToolSpec
from ne.contracts.types import Principal
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.factory import create_subagent_dispatcher
from ne.runtime.tools import ToolBinding
from ne.skills.authoring import SKILL_AUTHOR_AGENT_NAME, SKILL_AUTHOR_INSTRUCTIONS
from ne.skills.errors import SkillAuthoringUnavailableError
from ne.skills.forge import SkillForge, ensure_forge_schema
from ne.skills.governed_forge import (
    FORGE_TOOL_NAME,
    forge_tool_handler,
    forge_tool_spec,
    scheduled_forge_job,
)
from ne.skills.substrate import PydanticAiSkillSubstrate
from ne.worker.cron_seed import reconcile_prefixed_jobs

if TYPE_CHECKING:
    from ne.api.platform import Platform
    from ne.runtime.scheduler import CronScheduler

_JOB_NAME = "skill-forge"


@dataclass(frozen=True)
class SkillForgeSurface:
    """The governed cron surface for the Skill Forge (empty when default-off)."""

    bindings: tuple[ToolBinding, ...]
    definitions: dict[str, AgentDefinition]
    principals: dict[str, Principal]
    jobs: tuple[ScheduledJob, ...]


_EMPTY = SkillForgeSurface(bindings=(), definitions={}, principals={}, jobs=())


def build_skill_forge_surface(platform: Platform) -> SkillForgeSurface:
    """Build the governed forge cron surface, or the EMPTY surface (default-off).

    EMPTY unless ``skills_engine`` + ``skills_settings`` are present AND
    ``forge_enabled`` is True. When enabled, the ``skill-authoring`` gateway task
    route MUST exist (a dev-stack config prerequisite) — a first fire that cannot
    route fails at the governed dispatch, audited.
    """
    settings = platform.skills_settings
    if platform.skills_engine is None or settings is None or not settings.forge_enabled:
        return _EMPTY

    skills_engine = platform.skills_engine
    forge_principal = Principal(id="agent:skill-forge")

    # The toolless skill-author sub-agent, dispatched via a GovernedSubAgentDispatcher
    # on the private-vllm lane (frontier structurally refused, C3).
    author_definition = AgentDefinition(
        name=SKILL_AUTHOR_AGENT_NAME,
        instructions=SKILL_AUTHOR_INSTRUCTIONS,
        toolset=(),
        route_task=settings.author_route_task,
        route_lane=settings.author_route_lane,
    )
    dispatcher, _registry = create_subagent_dispatcher(
        bindings=(),
        definitions={SKILL_AUTHOR_AGENT_NAME: author_definition},
        sink=platform.sink,
        approval_gate=platform.gate,
        gateway=platform.gateway,
        principal=forge_principal,
        settings=platform.runtime_settings,
        govern_settings=platform.govern_settings,
        ledger=platform.ledger,
        counter=platform.counter,
    )

    async def _author_dispatch(task: str) -> str:
        result = await dispatcher.dispatch(SubAgentSpec(agent=SKILL_AUTHOR_AGENT_NAME, task=task))
        if not result.succeeded:
            raise SkillAuthoringUnavailableError()
        return result.output

    substrate = PydanticAiSkillSubstrate(
        skills_engine,
        audit_engine=platform.audit_engine,
        author_dispatch=_author_dispatch,
    )
    ensure_forge_schema(skills_engine)
    forge = SkillForge(skills_engine, substrate, batch_limit=settings.forge_batch_limit)

    tool: ToolSpec = forge_tool_spec()
    binding = ToolBinding(spec=tool, handler=forge_tool_handler(forge))
    forge_definition = AgentDefinition(
        name=_JOB_NAME,
        instructions=(
            f"Call the tool {FORGE_TOOL_NAME} exactly once to run one scheduled "
            "governed Skill Forge pass, then report its JSON counts."
        ),
        toolset=(FORGE_TOOL_NAME,),
        # Sole-tool: no scheduler-driven model loop on the cron tick path (the
        # 63279abe invariant, enforced at boot by require_sole_tool_definitions).
        # The forge TOOL still dispatches its governed skill-author sub-agent —
        # that model call is the tool's own audited work, not model arbitration
        # of the scheduled job.
        scheduled_execution="sole-tool",
    )
    job = scheduled_forge_job(cron=settings.forge_cron, name=_JOB_NAME, agent=_JOB_NAME)
    return SkillForgeSurface(
        bindings=(binding,),
        definitions={_JOB_NAME: forge_definition},
        principals={_JOB_NAME: forge_principal},
        jobs=(job,),
    )


def seed_skill_forge_jobs(scheduler: CronScheduler, jobs: tuple[ScheduledJob, ...]) -> None:
    """Reconcile the ``skill-forge`` job on ``scheduler`` toward ``jobs``.

    Delegates to the shared non-destructive :func:`reconcile_prefixed_jobs`
    (prefix ``skill-forge``) so a transiently-unconfigured worker reboot never
    wipes a peer's persisted schedule on the shared runtime DB.
    """
    reconcile_prefixed_jobs(scheduler, _JOB_NAME, jobs)
