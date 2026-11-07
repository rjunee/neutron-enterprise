# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-IDENTITY-ROTATION-CRON: the governed NE-034 agent-credential rotation cron.

Wires the ``ne.identity.agents.service`` line-409 seam ("NE-034 cron wires this;
wiring out of scope [here]") to a real caller: ``AgentCredentialService.
rotation_due_agents()`` / ``.rotate()`` now run as a scheduled, governed job —
mirroring ``ne.worker.principal_sync`` byte-for-shape: one tier-1 tool
(``write.internal.identity.credential_rotation``) + its handler + an
:class:`AgentDefinition` granting EXACTLY that tool + a :class:`ScheduledJob`, so
the sweep runs as an isolated, toolset-scoped governed agent through the ONE
``GovernedToolExecutor`` (C6): PDP-checked, guardrailed, and audited (job.run
dual-append + the service's own ``agent.credential.rotate`` events).

ALWAYS-ON (unlike the pull-source-gated surfaces in this package): there is no
config precondition to check. ``rotate_after_seconds`` always exists and
validates (:class:`~ne.identity.agents.config.AgentCredentialConfig`'s
positive-value validators), so the sweep is unconditionally safe to seed — an
empty credential set simply makes it a cheap, audited no-op (``due=0``).

CHECK CADENCE IS THE MAX ROTATION LAG: :func:`rotation_check_cron` derives the
job's firing interval from ``rotate_after_seconds`` (see its docstring for the
exact formula), so the seeded cadence bounds how stale a rotation can get before
the sweep catches it — the same "encoded interval IS the max lag" discipline
``interval_to_cron`` documents for principal-sync's revocation lag.

KNOWN LIMIT (in-memory, per-process credential state): ``AgentCredentialService``
holds its ``_credentials``/``_pools`` state in process memory (see service.py's
``__init__``) — there is no cross-process or cross-restart persistence. This job
therefore only ever rotates credentials that were ``issue()``-d IN THE SAME WORKER
PROCESS that is running the sweep. Mirrors the honest caveat
``ne.worker.principal_sync`` documents for its ``oidc-claims`` mode (a fresh
worker-side instance is always empty — there is nothing to pull): TODAY,
production issues no agent credentials anywhere (repo-wide grep: only tests call
``issue()``), so this job's sweep is an empty no-op in every current deployment.
This unit does not change that; it makes the rotation property hold BY
CONSTRUCTION for every future worker-side issuer — the moment something in this
process calls ``credential_service.issue(...)``, that credential is swept on
schedule with no further wiring required.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.tools import ToolBinding
from ne.worker.cron_seed import reconcile_prefixed_jobs
from ne.worker.principal_sync import interval_to_cron

if TYPE_CHECKING:
    from ne.api.platform import Platform
    from ne.runtime.scheduler import CronScheduler

_JOB_NAME = "identity-credential-rotation"
_TOOL = "write.internal.identity.credential_rotation"


def rotation_check_cron(rotate_after_seconds: float) -> str:
    """Encode the rotation check cadence as a crontab spec.

    The lag between a credential going due and this job catching it is bounded
    by the fire interval: ``min(3600, max(60, int(rotate_after_seconds) // 24))``
    seconds, i.e. at most ~1/24 of the rotation window, floored at 60s (cron
    granularity) and capped at hourly (bounding the periodic model micro-call
    spend of an always-on job). The default ``rotate_after_seconds=86400``
    (24h) yields a 3600s (hourly) check ⇒ ``'0 */1 * * *'``.
    """
    seconds = min(3600, max(60, int(rotate_after_seconds) // 24))
    return interval_to_cron(seconds)


@dataclass(frozen=True)
class CredentialRotationSurface:
    """The governed cron surface for the agent-credential rotation sweep.

    Always non-empty (see the module docstring — this surface is ALWAYS-ON, no
    config gate). ``bindings`` are wired into the cron ``GovernedToolExecutor``;
    ``definitions`` / ``principals`` feed the scheduler + its
    :class:`~ne.runtime.principals.StaticAgentPrincipalProvider`; ``jobs`` are
    seeded via :func:`seed_credential_rotation_jobs`.
    """

    bindings: tuple[ToolBinding, ...]
    definitions: dict[str, AgentDefinition]
    principals: dict[str, Principal]
    jobs: tuple[ScheduledJob, ...]


def _rotation_handler(platform: Platform) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    """Return the tier-1 tool handler (IGNORES model args — C12).

    The scheduled sweep has NO injectable surface: the handler discards any
    model-supplied argument map, reads ``rotation_due_agents()`` off the
    platform's ONE composed :class:`~ne.identity.agents.service.
    AgentCredentialService`, and ``rotate()``s each due agent in turn.

    Fail-closed BULK discipline (mirrors :meth:`AgentCredentialService.
    revoke_agent`): a per-agent rotation failure must not block the rest of the
    sweep. Every due agent is attempted; the FIRST failure is captured and
    re-raised only AFTER the full sweep completes, so no due agent is ever
    starved of its rotation attempt by an earlier sibling's failure, and every
    successful rotation (already individually audited via
    ``agent.credential.rotate`` inside :meth:`AgentCredentialService.rotate`)
    stands regardless.

    NOTE on the re-raise's OBSERVED effect (sole-tool execution): this tool has
    exactly ONE governed call site — the scheduled job's single tool invocation
    — so a re-raised ``first_error`` propagates out of ``binding.handler`` into
    :class:`~ne.runtime.middleware.GovernedToolExecutor`, which audits a
    ``tool.execute`` ERROR event and wraps it as :class:`ToolExecutionError`.
    Because this definition opts in to ``scheduled_execution="sole-tool"``,
    there is NO model loop to launder that error into a ``BLOCKED:`` final
    answer: the wrapped error propagates out of the scheduler's
    ``_execute_sole_tool`` and the SCHEDULED JOB is recorded FAILED
    (``job.run`` with ``succeeded=False``, ``error_class="ToolExecutionError"``,
    output ``ERROR: ToolExecutionError``). A partial sweep is therefore visible
    in three places: the per-credential state, the audited ``tool.execute``
    ERROR event, and the failed ``job.run`` itself. (Under the old agent-mode
    execution the run was recorded ``succeeded`` because the sub-agent reported
    the BLOCKED result as its final answer — sole-tool removed that laundering.)

    Returns COUNTS-ONLY JSON on success (no due agent failed) — never agent
    keys/departments/token material (C12).
    """

    async def _handler(_args: Mapping[str, object]) -> str:
        service = platform.credential_service
        due = service.rotation_due_agents()
        rotated = 0
        failed = 0
        first_error: Exception | None = None
        for agent in due:
            try:
                await service.rotate(agent, actor=f"agent:{_JOB_NAME}")
            except Exception as exc:
                failed += 1
                if first_error is None:
                    first_error = exc
            else:
                rotated += 1
        if first_error is not None:
            raise first_error
        return json.dumps({"due": len(due), "rotated": rotated, "failed": failed})

    return _handler


def build_credential_rotation_surface(platform: Platform) -> CredentialRotationSurface:
    """Build the ALWAYS-ON governed cron surface for the rotation sweep.

    Unlike the pull-source-gated surfaces in this package, there is no config
    precondition: ``rotate_after_seconds`` always exists and validates, so the
    surface is unconditionally non-empty (an empty credential set just makes
    the sweep a cheap, audited no-op).
    """
    tool = ToolSpec(
        name=_TOOL,
        description=(
            "Run one governed agent-credential rotation sweep: rotate every agent "
            "credential past its configured rotation window (counts-only result). "
            "Takes no arguments."
        ),
        tier=ApprovalTier(1),
    )
    binding = ToolBinding(spec=tool, handler=_rotation_handler(platform))
    definition = AgentDefinition(
        name=_JOB_NAME,
        instructions=(
            f"Call the tool {_TOOL} exactly once to run the scheduled governed "
            "agent-credential rotation sweep, then report its JSON counts."
        ),
        toolset=(_TOOL,),
        # Deterministic maintenance: the scheduler invokes the one governed tool
        # directly (PDP/guardrail/audit unchanged) instead of a per-sweep model
        # loop. Besides being cheaper, this keeps the SHARED litellm Router off
        # the worker's cron-tick loop entirely — Router-cached provider clients
        # are event-loop-affine, and this sweep fires as often as every 60s
        # (2026-08-28 cron-offload loop-affinity incident).
        scheduled_execution="sole-tool",
    )
    # A plain non-credentialed principal: passes AgentScopeGuardrail as 'not an
    # agent principal', governed by the PDP — the documented pre-NE-036
    # StaticAgentPrincipalProvider seam (same as ingest.py / principal_sync.py).
    principal = Principal(id=f"agent:{_JOB_NAME}")
    job = ScheduledJob(
        name=_JOB_NAME,
        cron=rotation_check_cron(platform.agent_identity_settings.credentials.rotate_after_seconds),
        agent=_JOB_NAME,
        toolset=(_TOOL,),
    )
    return CredentialRotationSurface(
        bindings=(binding,),
        definitions={_JOB_NAME: definition},
        principals={_JOB_NAME: principal},
        jobs=(job,),
    )


def seed_credential_rotation_jobs(scheduler: CronScheduler, jobs: tuple[ScheduledJob, ...]) -> None:
    """Reconcile the ``identity-credential-rotation`` job on ``scheduler`` toward ``jobs``.

    Delegates to the shared :func:`~ne.worker.cron_seed.reconcile_prefixed_jobs`
    (prefix ``identity-credential-rotation``, disjoint from every sibling job
    namespace: ``connect-sync-*``, ``permissions-principal-sync``,
    ``skill-forge-*``, ``skill-gardener-*``). The surface built by
    :func:`build_credential_rotation_surface` is always non-empty, so the
    reconciler's NON-DESTRUCTIVE empty-desired-set arm is vestigial here — kept
    only because it is the SHARED reconciler every cron seed in this package
    delegates to.
    """
    reconcile_prefixed_jobs(scheduler, _JOB_NAME, jobs)


__all__ = [
    "CredentialRotationSurface",
    "build_credential_rotation_surface",
    "rotation_check_cron",
    "seed_credential_rotation_jobs",
]
