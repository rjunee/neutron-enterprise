# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-INGEST: the governed NE-034 connector-sync cron surface for ne-worker (P1-4).

Closes the ADR-001 keystone gap in the OWNER-APPROVED LIGHTWEIGHT form (SPEC §11
decision 2026-07-10): NO new long-running process — instead ONE governed cron job per
configured connector source, seeded on the existing :class:`~ne.runtime.scheduler.
CronScheduler` (which now actually FIRES in DBOS mode post-NE-FIX-WORKER). The
flywheel ``governed.py`` cron module is the normative pattern: each
source becomes ONE tier-1 tool ``write.internal.connect.sync_<source>`` + its handler
+ an :class:`AgentDefinition` granting EXACTLY that tool + a :class:`ScheduledJob`. So
every scheduled sync deterministically invokes its exact singleton tool through the
ONE ``GovernedToolExecutor`` (C6), with no model arbitration: PDP-checked,
guardrailed, and audited (job.run dual-append + the connector's own
``connector.sync`` event).

Fail-closed: no configured source ⇒ an empty surface ⇒ zero jobs seeded ⇒ a clean
no-op, never a crash. A LISTED-but-misconfigured source fails LOUD at surface build
(worker boot), naming the source — decided semantics; an UNLISTED source is simply
never seeded.

RISK (jobs owner, carried forward from ``main.py``): in ``durability='dbos'`` mode a
scheduled sync runs sync SQLAlchemy/HTTP I/O on DBOS's background event loop. We are
the first scheduled-job owner; this is accepted today (the turn workflow already does
sync work in async steps) — offloading is a tracked follow-up.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from cronsim import CronSim

from ne.connect.runner import (
    ConnectSyncSettings,
    build_ingest_stack,
    build_sync_settings,
    run_configured_sync,
    sync_tool_name,
)
from ne.contracts.runtime import ScheduledJob, ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.dispatch import AgentDefinition
from ne.runtime.middleware import current_tool_principal
from ne.runtime.tools import ToolBinding
from ne.worker.cron_seed import reconcile_prefixed_jobs

if TYPE_CHECKING:
    from ne.api.platform import Platform
    from ne.runtime.scheduler import CronScheduler

_JOB_PREFIX = "connect-sync-"

#: The coarsest ``*/N`` minute cron an authority sweep may be seeded at. Capping
#: only ever makes the job fire MORE often than its cadence, so the cap can
#: never make a scope's sweep miss its SLA.
_MAX_AUTHORITY_CRON_MINUTES = 30


def authority_sweep_cron(cadence_seconds: float) -> str:
    """DERIVE the seeded cron for an authority-bearing source from its cadence.

    An authority-enabled M365 source must not inherit the generic connector cron
    — with a PT1H contract SLA the hourly default can never re-read every point
    inside the SLA. Deriving the expression makes that structural: the generic
    ``cs.cron`` DEFAULT is not consulted, so there is no configuration in which
    it is what gets seeded.

    An EXPLICIT per-source ``cron_overrides`` entry may outrank this — see
    :func:`_seeded_cron` — but ONLY when it fires at least as often as the
    cadence. An override is an operator statement about a real provider bound
    (a daily API cap); it is not a licence to seed a job slower than the sweep
    plan was proven against.

    The minute step FLOORS the cadence (never rounds up), so the seeded job's
    firing gap is at most the cadence the sweep plan was proven against.
    """
    if cadence_seconds <= 0:
        raise ValueError("an authority sweep cadence must be positive")
    minutes = min(_MAX_AUTHORITY_CRON_MINUTES, max(1, int(cadence_seconds // 60)))
    return f"*/{minutes} * * * *"


class AuthoritySweepCronError(ValueError):
    """An explicit cron override cannot carry an authority source's sweep."""


#: A fixed, arbitrary UTC anchor for the probe. Every expression this module is
#: willing to bound repeats on a whole number of DAYS (see
#: :func:`_bounding_period_days`), so the answer does not depend on the anchor;
#: pinning it keeps the seeded surface deterministic instead of drifting with
#: the clock. UTC also means cronsim does no DST fixup, so "a day" is exactly
#: 86400 seconds here.
_CRON_PROBE_ANCHOR = datetime(2026, 1, 5, tzinfo=UTC)

#: Every month, i.e. a month field that constrains nothing.
_ALL_MONTHS = frozenset(range(1, 13))

#: Every day-of-month, i.e. a day-of-month field that constrains nothing. cronsim
#: parses ``*`` in that field to exactly this set, and represents the ``L`` /
#: ``LW`` forms as negative sentinels, which therefore never compare equal.
_ALL_DAYS_OF_MONTH = frozenset(range(1, 32))

#: Every day-of-week, taken mod 7 because cron accepts both ``0`` and ``7`` for
#: Sunday and ``*`` parses to ``{0..7}``.
_ALL_DAYS_OF_WEEK = frozenset(range(7))


def _bounding_period_days(sim: CronSim) -> int | None:
    """The number of DAYS after which ``sim``'s firing set repeats EXACTLY.

    ``None`` when this module cannot pin such a period — and that is the
    fail-closed answer, not an inconclusive one: the caller must refuse.

    Why this is decided analytically rather than sampled. A finite probe can
    only ever be too SHORT, so "no long gap was observed in the window" is not
    evidence of "no long gap exists": ``*/5 * * 1 *`` fires every five minutes
    all through January and then sleeps for eleven months, and ANY probe that
    starts in January and ends inside it reports a 300-second worst case. The
    only way a sampled maximum can be a real bound is if the sample provably
    covers a whole period of the expression, so the period must be established
    first, from the parsed fields — never from what the sample happened to show.

    A cron expression's within-a-day part (second/minute/hour) is the same every
    day by construction, so the whole period comes from the day selection:

    * A month field that names anything less than all twelve months has a
      dormant stretch of at least a month, and where that stretch falls depends
      on the calendar — refused.
    * A day-of-month field that names anything less than all 31 days likewise
      makes the pattern depend on month lengths and leap years (a period of 400
      years, in general) — refused. This deliberately also refuses expressions
      that happen to be safe under cron's OR semantics between day-of-month and
      day-of-week (``1-31`` OR some weekday covers every day); those are not
      worth a special case, and the refusal direction is the safe one.
    * ``#`` / ``L`` day-of-week forms ("third Friday", "last Monday") are
      positions WITHIN a month, so they are not week-periodic either — refused.
      cronsim represents exactly those as tuples in ``weekdays``.

    What survives is an expression whose day selection is either unconditional
    (period one day) or a plain set of weekdays (period seven days). Those are
    the shapes a sweep cadence can actually be carried by, so refusing the rest
    costs nothing real: an authority sweep that must re-read every point inside
    an SLA measured in minutes cannot be seeded on a seasonal expression.
    """
    if set(sim.months) != _ALL_MONTHS:
        return None
    if set(sim.days) != _ALL_DAYS_OF_MONTH:
        return None
    weekdays: set[int] = set()
    for weekday in sim.weekdays:
        if not isinstance(weekday, int):
            # cronsim represents ``5#3`` / ``1L`` as a tuple: a position inside a
            # MONTH, so the firing set is not week-periodic.
            return None
        weekdays.add(weekday % 7)
    if weekdays == _ALL_DAYS_OF_WEEK:
        return 1
    return 7


def max_firing_gap_seconds(expression: str) -> float | None:
    """The LONGEST gap between two consecutive firings of ``expression``, EVER.

    ``None`` when this module cannot BOUND that gap — see
    :func:`_bounding_period_days` for which expressions those are. ``None`` is
    NOT "probably fine": it is "no bound was established", and every caller must
    fail closed on it.

    When a period IS established the returned number is exact rather than
    observed. The probe runs over TWO periods from a fixed anchor, which is
    enough to see every gap the expression will ever produce: the first firing
    ``f0`` lands within one period of the anchor, ``f0 + period`` is a firing by
    periodicity and is still inside the window, so the firings in ``[f0, f0 +
    period]`` are all present and their consecutive gaps are one complete cycle
    of the gap sequence. Sampling further cannot find anything new.
    """
    sim = CronSim(expression, _CRON_PROBE_ANCHOR)
    period_days = _bounding_period_days(sim)
    if period_days is None:
        return None
    horizon = _CRON_PROBE_ANCHOR + timedelta(days=2 * period_days)
    previous: datetime | None = None
    widest = 0.0
    for fired in sim:
        if fired > horizon:
            break
        if previous is not None:
            widest = max(widest, (fired - previous).total_seconds())
        previous = fired
    return widest or None


def _seeded_cron(
    cs: ConnectSyncSettings,
    source: str,
    settings: Any,
    *,
    authority_enabled: bool,
) -> str:
    """The cron this source's job is seeded at.

    Precedence, highest first:

    1. An EXPLICIT per-source entry in ``cron_overrides``, for a source that
       bears no authority. An operator who names a source in
       ``NE_CONNECT_SYNC_CRON_OVERRIDES`` has stated the cadence that deployment
       must run at, and a provider daily-API cap is a real bound.
    2. For an AUTHORITY-BEARING source the same explicit entry is honoured only
       while its widest firing gap is no wider than
       ``authority_sweep_cadence_seconds``. An override that fires MORE often is
       safe by construction. One that fires LESS often is REFUSED, loudly —
       see below. So is one whose worst-case gap cannot be BOUNDED at all (a
       seasonal or day-of-month expression): an unproven bound is not a passing
       one, and the failure this guard exists to stop — a corpus blackout —
       is exactly what an unbounded expression hides.
    3. The cadence DERIVED from the sweep plan, for an authority-bearing source
       with no explicit entry. This is the M365 path.
    4. The generic ``cs.cron`` default, for a source with neither.

    Why case 2 refuses rather than quietly widening: the cadence is not a
    preference, it is the number ``SweepCadencePlan.require_sub_sla`` already
    PROVED a full permission rotation against
    (``ceil(max_points / batch_size) * (cadence + max_batch_duration) < SLA``).
    That proof is what admits the authority producer at composition time, and it
    reads ``authority_sweep_cadence_seconds`` — NOT the cron the job is actually
    seeded at. Letting a slower override through therefore does not relax the
    guard; it makes the guard certify a rotation that no longer happens, while
    the sweep's own runtime check
    (``SqlAlchemyHierarchicalAdmission.require_points_swept_within_sla``) starts
    failing every run, degrading the whole source to ``unavailable`` after
    ``TRANSIENT_SYNC_FAILURE_LIMIT`` consecutive failures. Refusing at seed time
    states the contradiction where an operator can still fix it.

    The honest remedy for a provider API cap is to raise
    ``NE_<SOURCE>_AUTHORITY_SWEEP_CADENCE_SECONDS`` (and, if needed, the batch
    size) so cadence, seeded cron and proven rotation stay the SAME number.
    """
    explicit = cs.cron_overrides.get(source)
    if explicit is None:
        if authority_enabled:
            return authority_sweep_cron(float(settings.authority_sweep_cadence_seconds))
        return str(cs.cron_for(source))
    expression = str(explicit)
    if not authority_enabled:
        return expression
    cadence = float(settings.authority_sweep_cadence_seconds)
    widest = max_firing_gap_seconds(expression)
    if widest is None or widest > cadence:
        observed = (
            "unbounded — the expression restricts day-of-month, month, or uses a "
            "'#'/'L' weekday form, so its firing set does not repeat on a fixed "
            "number of days and no probe can prove a worst-case gap for it"
            if widest is None
            else f"{widest:g}s"
        )
        raise AuthoritySweepCronError(
            f"cron_overrides[{source!r}]={expression!r} seeds a job whose widest firing "
            f"gap is {observed}, which is slower than the {cadence:g}s sweep cadence the "
            "permission rotation was proven sub-SLA against; the seeded cron and "
            "authority_sweep_cadence_seconds would no longer describe the same job. "
            f"Raise NE_{source.upper()}_AUTHORITY_SWEEP_CADENCE_SECONDS instead — it "
            "moves the proof and the schedule together."
        )
    return expression


@dataclass(frozen=True)
class ConnectorSyncSurface:
    """The governed cron surface for the configured connector sources.

    ``bindings`` are wired into the cron ``GovernedToolExecutor``; ``definitions`` /
    ``principals`` feed the scheduler + its :class:`~ne.runtime.principals.
    StaticAgentPrincipalProvider`; ``jobs`` are seeded via :func:`seed_connector_sync_jobs`.
    Every tuple is empty when no source is configured (fail-closed no-op).
    """

    bindings: tuple[ToolBinding, ...]
    definitions: dict[str, AgentDefinition]
    principals: dict[str, Principal]
    jobs: tuple[ScheduledJob, ...]


def _sync_handler(
    source: str,
    *,
    platform: Platform,
    stack: Any,
    settings: Any,
    kpi_tag: str | None,
    principal_provider: Callable[[], Principal | None],
) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    """Return the tier-1 tool handler for one source (IGNORES model args — C12).

    The scheduled sync has NO injectable surface: the handler discards any
    model-supplied argument map and runs the pre-built connector sync, returning
    COUNTS-ONLY JSON. ``cursors`` are excluded from the output — a deltaLink /
    cursor token never leaves in tool output (C12; the connector already keeps them
    in its durable state store).
    """

    async def _handler(_args: Mapping[str, object]) -> str:
        principal = principal_provider()
        if principal is None:
            raise RuntimeError("connector sync tool has no executor-bound principal")

        # The Tier-2 graph projection runs INSIDE the connector_sync_gate hold
        # (run_configured_sync's post_sync seam), never after it. The graph run
        # opens its own long BEGIN IMMEDIATE mapper refresh on the SAME shared
        # ConnectorStateStore; released-gate ordering let a sibling source's
        # sync (admitted by a later tick) overlap that refresh and fail against
        # it every tick for the whole crawl — enough consecutive failures walk
        # the sibling's lane stale → unavailable and the read path then
        # withholds that connector's entire corpus.
        graph_host = platform.m365_graph_sync_host if source == "m365" else None
        graph_result: dict[str, object] = {}

        async def _graph_projection() -> None:
            assert graph_host is not None
            graph_report = await graph_host.run(
                settings,
                state_store=stack.state_store,
                actor=principal.id,
                kpi_tag=kpi_tag,
            )
            graph_result["graph"] = graph_report.model_dump(mode="json")

        report = await run_configured_sync(
            source,
            writer=stack.writer,
            state_store=stack.state_store,
            audit_sink=platform.sink,
            actor=principal.id,
            kpi_tag=kpi_tag,
            settings=settings,
            quarantine=stack.quarantine,
            authority=stack.authority_for(source),
            chunk_reader=stack.chunk_reader,
            post_sync=None if graph_host is None else _graph_projection,
        )
        result = report.model_dump(mode="json", exclude={"cursors"})
        result.update(graph_result)
        return json.dumps(result)

    return _handler


def build_connector_sync_surface(platform: Platform) -> ConnectorSyncSurface:
    """Build the governed cron surface from ``platform.connect_sync_settings``.

    Empty ``sources`` ⇒ an empty surface (fail-closed no-op). Otherwise the ONE
    governed :class:`~ne.connect.runner.IngestStack` is built over the SAME composed
    ``platform.chunk_store`` (C7 governed KnowledgeWriter path), and per source the
    connector settings are constructed EAGERLY (so a listed-but-misconfigured source
    raises :class:`~ne.connect.runner.SyncNotConfiguredError` — naming the source —
    at worker boot rather than at first fire). Configured sources outside the
    exact ``runnable_sources`` subset remain available to API status/read-only
    inspection, but deliberately gain no request binding, worker agent, or cron job.

    WHY THAT IS SUFFICIENT TO STOP A SYNC, and where the defence in depth sits:

    * ONLY ne-worker ever fires a job. ``CronScheduler.run_due_jobs`` and
      ``run_requested_jobs`` have exactly two call sites, both in
      :mod:`ne.worker.main` (the DBOS tick and the plain-asyncio ticker). The API
      never fires one, so a source with no runnable binding here has no executor
      anywhere in the deployment.
    * A STALE persisted job row cannot execute on such a worker. Scheduled jobs
      live in the SHARED runtime DB, so a ``connect-sync-<source>`` row can
      outlive the config that created it. Boot-time reconciliation cancels it
      (a non-empty desired set is authoritative — see
      :func:`ne.worker.cron_seed.reconcile_prefixed_jobs`), and seeding runs
      BEFORE any ticker task is created, so there is no boot race. Even if a row
      did survive and fire, :class:`~ne.runtime.toolset.ToolsetScopedExecutor`
      raises ``ToolsetConfigurationError`` at construction for a granted tool the
      executor does not expose, so the run fails closed and is audited rather
      than reaching the provider.

    The primary guard is still that the worker only ever BOOTS with a runnable
    set that omits the frozen source; the two facts above are why a mistake in
    the surrounding deployment cannot turn that into a provider pull.
    """
    cs = platform.connect_sync_settings
    if not cs.sources:
        return ConnectorSyncSurface(bindings=(), definitions={}, principals={}, jobs=())

    kpi_tag = cs.kpi_tag or None
    stack = build_ingest_stack(
        chunk_store=platform.chunk_store,
        runtime_engine=platform.runtime_engine,
        sink=platform.sink,
        settings=cs,
        embedder=platform.embedder,
        document_lineage=platform.document_lineage,
    )

    bindings: list[ToolBinding] = []
    definitions: dict[str, AgentDefinition] = {}
    principals: dict[str, Principal] = {}
    jobs: list[ScheduledJob] = []

    for source in cs.sources_to_run:
        settings = build_sync_settings(source)  # EAGER — fails loud if misconfigured
        authority_enabled = bool(getattr(settings, "authority_enabled", False))
        if authority_enabled and stack.authority_for(source) is None:
            # The scope stays Unknown and the source is NOT advertised: no tool
            # binding, no agent, no job. Seeding it anyway would schedule an
            # authority-bearing sync that cannot issue authority.
            continue
        if source == "m365" and platform.m365_graph_sync_host is not None:
            # EAGER + I/O-free: the configured contract source_name must have one
            # exact graph mapping before this scheduled capability can exist.
            platform.m365_graph_sync_host.mapping_for(settings)
        tool_name = sync_tool_name(source)
        agent_name = f"{_JOB_PREFIX}{source}"
        tool = ToolSpec(
            name=tool_name,
            description=(
                f"Run one governed {source} connector sync onto the brain corpus "
                + (
                    "then its version-bound Tier-2 graph projection "
                    if source == "m365" and platform.m365_graph_sync_host is not None
                    else ""
                )
                + "(counts-only result). Takes no arguments."
            ),
            tier=ApprovalTier(1),
        )
        handler = _sync_handler(
            source,
            platform=platform,
            stack=stack,
            settings=settings,
            kpi_tag=kpi_tag,
            principal_provider=current_tool_principal,
        )
        bindings.append(ToolBinding(spec=tool, handler=handler))
        definitions[agent_name] = AgentDefinition(
            name=agent_name,
            instructions=(
                f"Call the tool {tool_name} exactly once to run the scheduled governed "
                f"{source} connector sync, then report its JSON counts."
            ),
            toolset=(tool_name,),
            scheduled_execution="sole-tool",
            # C8 — the SAME ``cs.kpi_tag`` already stamped on the sync's own audit
            # events, now also on the agent that runs it. The sync events were
            # attributed while the completion that drives them — the only part of
            # this job that costs money — was landing unattributed.
            kpi_tag=kpi_tag,
        )
        # A plain non-credentialed principal: passes AgentScopeGuardrail as 'not an
        # agent principal', governed by the PDP — the documented pre-NE-036
        # StaticAgentPrincipalProvider seam.
        principals[agent_name] = Principal(id=f"agent:{agent_name}")
        jobs.append(
            ScheduledJob(
                name=agent_name,
                cron=_seeded_cron(cs, source, settings, authority_enabled=authority_enabled),
                agent=agent_name,
                toolset=(tool_name,),
            )
        )

    return ConnectorSyncSurface(
        bindings=tuple(bindings),
        definitions=definitions,
        principals=principals,
        jobs=tuple(jobs),
    )


def seed_connector_sync_jobs(
    scheduler: CronScheduler,
    jobs: tuple[ScheduledJob, ...],
    *,
    authoritative_empty: bool = False,
) -> None:
    """Reconcile the ``connect-sync-*`` jobs on ``scheduler`` toward ``jobs``.

    Delegates to the shared :func:`~ne.worker.cron_seed.reconcile_prefixed_jobs`
    (prefix ``connect-sync-``) — SAME public name + signature (the
    ``test_connect_sync_cron.py`` fence imports it), including the NON-DESTRUCTIVE
    empty-desired-set no-op (the NE-INGEST Codex-P1 lesson now lives in the reconciler).
    """
    reconcile_prefixed_jobs(scheduler, _JOB_PREFIX, jobs, authoritative_empty=authoritative_empty)
