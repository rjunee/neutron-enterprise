# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FIX-SYNC-INVOKERS (P2-20): the governed NE-034 principal-sync cron surface.

Closes the P2-20 gap from the 2026-07-10 bulletproofing review: ``PrincipalSyncService.
run_once`` was constructed + threaded into the platform but had NO production caller,
so nested / transitive / >200-group-overage memberships were never populated and the
SPEC §5 ≤15-min revocation target was unrealizable. This module mirrors
``ne.worker.ingest`` EXACTLY: one tier-1 tool ``write.internal.permissions.principal_sync``
+ its handler + an :class:`AgentDefinition` granting EXACTLY that tool + a
:class:`ScheduledJob`, so the scheduled sync runs as an isolated, toolset-scoped
governed agent through the ONE ``GovernedToolExecutor`` (C6): PDP-checked,
guardrailed, and audited (job.run dual-append + the service's own
``permissions.principal_sync`` event).

Fail-closed default-off: only a PULL-capable principal source (``mode`` ``entra`` or
``fixture``) seeds a cron. The default ``oidc-claims`` mode is a JIT observation
buffer that lives in the ne-api process (a fresh worker-side instance is always empty
— there is nothing to pull) and SCIM is push-provisioned via ne-api routes; neither
needs nor benefits from a cron, so both build the EMPTY surface (zero jobs). A
listed pull source that is misconfigured fails LOUD at surface build (worker boot),
naming the mode — the source is constructed EAGERLY here.

REVOCATION LAG: the seeded interval IS the max revocation lag. ``sync_interval_seconds``
(default 900s = the ≤15-min target) is encoded into the cron by
:func:`interval_to_cron`, which floors to whole minutes (cron granularity). A larger
interval moves the revocation-lag SLA (documented, never silently capped).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.permissions.factory import build_principal_source
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.tools import ToolBinding
from ne.worker.cron_seed import reconcile_prefixed_jobs

if TYPE_CHECKING:
    from ne.api.platform import Platform
    from ne.runtime.scheduler import CronScheduler

_JOB_NAME = "permissions-principal-sync"
_TOOL = "write.internal.permissions.principal_sync"
#: Only these modes are PULL-capable (a snapshot to fetch on a schedule). oidc-claims
#: (JIT, in the ne-api process) and SCIM (push via routes) seed nothing.
_PULL_MODES = ("entra", "fixture")


def interval_to_cron(seconds: int) -> str:
    """Encode a sync interval (seconds) as a crontab spec, flooring to whole minutes.

    The cron granularity is one minute, so a sub-60s interval floors to every-minute
    and the encoded interval IS the max revocation lag. ``seconds < 3600`` maps to
    ``*/N * * * *`` (N minutes, clamped to 1..59); ``seconds >= 3600`` maps to
    ``0 */H * * *`` (top of every H-th hour, H clamped to 1..23). Examples:
    900 ⇒ ``*/15 * * * *`` (the default ≤15-min target); 3600 ⇒ ``0 */1 * * *``;
    172800 ⇒ ``0 */23 * * *`` (clamped).
    """
    if seconds < 3600:
        minutes = min(59, max(1, seconds // 60))
        return f"*/{minutes} * * * *"
    hours = min(23, seconds // 3600)
    return f"0 */{hours} * * *"


@dataclass(frozen=True)
class PrincipalSyncSurface:
    """The governed cron surface for the configured principal-sync source.

    ``bindings`` are wired into the cron ``GovernedToolExecutor``; ``definitions`` /
    ``principals`` feed the scheduler + its :class:`~ne.runtime.principals.
    StaticAgentPrincipalProvider`; ``jobs`` are seeded via
    :func:`seed_principal_sync_jobs`. Every collection is empty when no pull source is
    configured (fail-closed no-op).
    """

    bindings: tuple[ToolBinding, ...]
    definitions: dict[str, AgentDefinition]
    principals: dict[str, Principal]
    jobs: tuple[ScheduledJob, ...]


_EMPTY = PrincipalSyncSurface(bindings=(), definitions={}, principals={}, jobs=())


def _sync_handler(platform: Platform) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    """Return the tier-1 tool handler (IGNORES model args — C12).

    The scheduled sync has NO injectable surface: the handler discards any
    model-supplied argument map, builds the configured source, runs
    ``PrincipalSyncService.run_once`` (which emits its own governed
    ``permissions.principal_sync`` audit event), and returns COUNTS-ONLY JSON.
    ``changed_members`` are principal ids and are EXCLUDED from the output — they must
    not leave in tool output (C12); the per-member audit events already carry the
    detail.
    """

    async def _handler(_args: Mapping[str, object]) -> str:
        source = build_principal_source(platform.permissions_settings)
        report = await platform.perm_sync.run_once(source)
        return json.dumps(report.model_dump(mode="json", exclude={"changed_members"}))

    return _handler


def build_principal_sync_surface(platform: Platform) -> PrincipalSyncSurface:
    """Build the governed cron surface from ``platform.permissions_settings``.

    A non-pull ``mode`` (``oidc-claims`` / SCIM) ⇒ the EMPTY surface (fail-closed
    no-op). Otherwise the source is constructed EAGERLY via
    :func:`~ne.permissions.factory.build_principal_source` so a misconfigured pull
    source fails LOUD at worker boot naming the mode — rather than silently at first
    fire.
    """
    ps = platform.permissions_settings
    if ps.mode not in _PULL_MODES:
        return _EMPTY

    # EAGER — a misconfigured pull source raises here (worker boot), naming the mode.
    build_principal_source(ps)

    tool = ToolSpec(
        name=_TOOL,
        description=(
            "Run one governed principal-directory sync into the principal store "
            "(counts-only result). Takes no arguments."
        ),
        tier=ApprovalTier(1),
    )
    binding = ToolBinding(spec=tool, handler=_sync_handler(platform))
    definition = AgentDefinition(
        name=_JOB_NAME,
        instructions=(
            f"Call the tool {_TOOL} exactly once to run the scheduled governed "
            "principal-directory sync, then report its JSON counts."
        ),
        toolset=(_TOOL,),
        # Deterministic maintenance: direct sole-tool invocation (PDP/guardrail/
        # audit unchanged) — keeps the SHARED litellm Router off the worker's
        # cron-tick loop; its cached provider clients are event-loop-affine
        # (2026-08-28 cron-offload loop-affinity incident).
        scheduled_execution="sole-tool",
    )
    # A plain non-credentialed principal: passes AgentScopeGuardrail as 'not an agent
    # principal', governed by the PDP — the documented pre-NE-036
    # StaticAgentPrincipalProvider seam (same as ingest.py).
    principal = Principal(id=f"agent:{_JOB_NAME}")
    job = ScheduledJob(
        name=_JOB_NAME,
        cron=interval_to_cron(ps.sync_interval_seconds),
        agent=_JOB_NAME,
        toolset=(_TOOL,),
    )
    return PrincipalSyncSurface(
        bindings=(binding,),
        definitions={_JOB_NAME: definition},
        principals={_JOB_NAME: principal},
        jobs=(job,),
    )


def seed_principal_sync_jobs(scheduler: CronScheduler, jobs: tuple[ScheduledJob, ...]) -> None:
    """Reconcile the ``permissions-principal-sync`` job on ``scheduler`` toward ``jobs``.

    Delegates to the shared :func:`~ne.worker.cron_seed.reconcile_prefixed_jobs`
    (prefix ``permissions-principal-sync``), including the NON-DESTRUCTIVE
    empty-desired-set no-op so a transiently-unconfigured worker reboot never wipes a
    peer's persisted principal-sync schedule on the shared runtime DB.
    """
    reconcile_prefixed_jobs(scheduler, _JOB_NAME, jobs)
