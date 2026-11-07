# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The shared prefix-scoped ScheduledJob reconciler for the ne-worker cron seeds.

Both governed cron surfaces (NE-INGEST's ``connect-sync-*`` and
NE-FIX-SYNC-INVOKERS's ``permissions-principal-sync``) reconcile a desired set of
:class:`~ne.contracts.runtime.ScheduledJob` rows onto the SAME shared
:class:`~ne.runtime.scheduler.CronScheduler`. This module holds the ONE
reconciler both seeds delegate to so the NON-DESTRUCTIVE-empty-set lesson lives in
one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ne.contracts.runtime import ScheduledJob
    from ne.runtime.dispatch import AgentDefinition
    from ne.runtime.scheduler import CronScheduler


def require_sole_tool_definitions(definitions: Mapping[str, AgentDefinition]) -> None:
    """Fail worker boot LOUD if any cron-path agent definition is model-arbitrated.

    The cron tick body runs on the worker's persistent tick loop (a7a4343d), and
    63279abe's stated invariant is that the SHARED litellm Router is never
    touched from that path again — every scheduled job must be
    ``scheduled_execution="sole-tool"`` (the scheduler invokes the job's exact
    singleton governed tool, no per-fire model loop). That invariant used to be
    enforced by convention only: a new surface whose AgentDefinition kept the
    ``"agent"`` default silently re-opened the Router-on-tick-loop path the
    moment its config flag flipped on, with nothing red anywhere. This check
    runs at worker boot over the FULL merged definition map, immediately before
    the scheduler is constructed, so the next such surface fails the boot with
    the offending names instead of arming quietly. (A sole-tool handler may
    still perform governed model work of its own — e.g. skill-forge's authoring
    sub-agent dispatch — which is a visible, audited call, not scheduler-driven
    model arbitration.)
    """
    agent_mode = sorted(
        name
        for name, definition in definitions.items()
        if definition.scheduled_execution != "sole-tool"
    )
    if agent_mode:
        raise RuntimeError(
            "worker cron surfaces must opt in to scheduled_execution='sole-tool' "
            "(no model-arbitrated job may run on the cron tick path); "
            f"agent-mode definitions: {agent_mode!r}"
        )


def reconcile_prefixed_jobs(
    scheduler: CronScheduler,
    prefix: str,
    jobs: tuple[ScheduledJob, ...],
    *,
    authoritative_empty: bool = False,
) -> None:
    """Reconcile the ``prefix``-scoped jobs on ``scheduler`` toward ``jobs``.

    SCOPED to the ``prefix`` name prefix ONLY — never touches other jobs. For each
    desired job: an existing job with identical ``(cron, agent, toolset)`` is left
    as-is (idempotent restart, no audit spam); a changed one is cancelled +
    rescheduled; a new one is scheduled. When ``jobs`` is a NON-EMPTY authoritative
    desired set, any existing ``prefix``-scoped job NOT in it is cancelled (a
    deliberately removed source). Every schedule/cancel is a governed, dual-appended
    NE-034 mutation.

    NON-DESTRUCTIVE DEFAULT-OFF (the NE-INGEST Codex-P1 lesson): an EMPTY ``jobs``
    tuple is NOT normally an authoritative "remove every ``prefix``-scoped schedule" signal —
    in this design it is *only* ever produced by an empty/unconfigured source set
    (the default-off / unconfigured / transiently-omitted case; a deliberate removal
    of a specific source is expressed as a non-empty set that omits it).
    Scheduled-job rows live in the SHARED runtime DB, so treating empty config as
    authoritative would let a single worker that boots with the source env absent (a
    rolling-deploy env glitch or an api/worker config mismatch) DESTRUCTIVELY delete
    the persisted schedules of correctly-configured peers — a silent outage that
    survives until some worker reboots with the config restored. So when no source is
    configured we reconcile NOTHING: existing ``prefix``-scoped jobs are left intact.
    The loop is still effectively off for THIS process (it holds no binding for those
    jobs, so a fire here just fails-closed and is audited), while the schedule
    survives for peers and for a corrected reboot. Turning the loop off deliberately
    is an explicit operator action (cancel the jobs / scale the seeding worker down),
    not a side effect of an absent env var. A caller with an independently validated,
    explicit empty desired set may pass ``authoritative_empty=True``; that signal is
    distinct from absence and cancels only this prefix's persisted jobs.
    """
    desired = {job.name: job for job in jobs}
    if not desired and not authoritative_empty:
        return

    existing = {job.name: job for job in scheduler.list_jobs() if job.name.startswith(prefix)}

    for name, job in existing.items():
        if name not in desired and job.job_id is not None:
            scheduler.cancel(job.job_id)

    for name, job in desired.items():
        current = existing.get(name)
        if current is not None:
            if (current.cron, current.agent, current.toolset) == (
                job.cron,
                job.agent,
                job.toolset,
            ):
                continue  # unchanged — idempotent, no re-schedule
            if current.job_id is not None:
                scheduler.cancel(current.job_id)
        scheduler.schedule(job)
