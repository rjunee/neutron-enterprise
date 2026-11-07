# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ne-worker composition root + run loop — ``python -m ne.worker`` (NE-103c §6).

Mirrors the ne-api spec §3.2 normative order via the SAME
:func:`ne.api.platform.build_platform` (one recipe, two processes): settings/secrets
→ freeze the composed ``config_hash`` ONCE → the ONE audit sink / approval gate /
NE-070H shared ledger+counter / governed executor / session store. The worker then
adds the durable turn queue, the NE-034 cron scheduler (SAME shared state), the
outbox drain loop, a DB-ping heartbeat, and — in ``durability='dbos'`` mode — a real
DBOS-Postgres launch that RECOVERS pending workflows on start.

DECIDED DEVIATION (design + risks §1): DBOS is constructed DIRECTLY with
``run_admin_server=False`` — ``launch_durable_runtime`` (durable.py:90-105) cannot
pass the kwarg and ne-runtime src is untouchable in this lane, so its 3-line
durability guard is replicated here. The default ``run_admin_server=True`` would open
an UNAUTHENTICATED workflow-admin HTTP server in the hardened container. Follow-up
NE-RUNTIME-LAUNCH-KWARGS tracks giving ``launch_durable_runtime`` the kwarg.

HEALTH PROBE (NE-103c FIX-2): ``python -m ne.worker --health`` imports only
:class:`~ne.worker.settings.WorkerSettings` and reads the process-epoch heartbeat;
every heavy composition import lives inside :func:`_run`/:func:`_launch_dbos`. This
keeps the 5s-timeout healthcheck on the ``cpus:2`` read-only container from flapping.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any

# NE-103c FIX-2: the module top-level imports ONLY WorkerSettings (contracts-light:
# pydantic/pydantic-settings/ne.contracts.config) so `python -m ne.worker --health`
# — parsed FIRST in main() — never drags in the heavy composition graph
# (ne.api.platform → litellm/qdrant/fastembed, dbos, sqlalchemy). Every heavy import
# lives inside _run()/_launch_dbos()/the loop bodies where it is actually used.
from ne.worker.settings import WorkerSettings

if TYPE_CHECKING:
    from ne.api.platform import AppSeams, Platform
    from ne.api.skills_delivery import SkillDeliveryReceipt
    from ne.contracts.plugins import PluginRegistry
    from ne.runtime.scheduler import CronScheduler

_LOG = logging.getLogger("ne.worker")

#: The cron scheduler is a process-global so the DBOS-scheduled tick (registered
#: before launch) can reach it; set once in ``_run``.
_scheduler: CronScheduler | None = None


@dataclass(frozen=True, slots=True)
class _SkillDeliveryReceiptIdentity:
    """Persisted audit-chain identity for one worker-boot reconciliation."""

    event_id: str
    audit_sequence: int
    audit_record_hash: str


# --- --health (imports only WorkerSettings + reads the heartbeat marker) ------


def _process_epoch(pid: int) -> str:
    """Return a process-instance identity (Linux start ticks in production)."""

    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            stat = fh.read()
        # The command in field 2 may contain spaces and parentheses. Fields after
        # its final ')' begin at field 3; starttime is field 22 (offset 19).
        tail = stat.rsplit(")", 1)[1].split()
        return f"linux:{pid}:{tail[19]}"
    except (OSError, IndexError):
        # macOS developer tests have no /proc. PID liveness retains the same
        # fail-closed record shape there; production containers always use Linux.
        os.kill(pid, 0)
        return f"pid:{pid}"


def _write_heartbeat_marker(heartbeat_path: str) -> None:
    """Atomically publish one marker bound to this worker process instance."""

    pid = os.getpid()
    payload = json.dumps(
        {"pid": pid, "process_epoch": _process_epoch(pid)},
        sort_keys=True,
        separators=(",", ":"),
    )
    temporary = f"{heartbeat_path}.tmp.{pid}"
    with open(temporary, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(temporary, heartbeat_path)


def _invalidate_heartbeat_marker(heartbeat_path: str) -> None:
    """Remove any prior-container marker before normal worker startup."""

    with contextlib.suppress(FileNotFoundError):
        os.unlink(heartbeat_path)


def _health_ok(heartbeat_path: str, *, max_age_s: float = 60.0) -> bool:
    """Healthy iff the marker is fresh and bound to one live process epoch.

    A dead DB stops the heartbeat touch, so the marker goes stale and this returns
    False — the design §8 "DB ping + launched-marker" liveness, without a full
    per-probe re-import of the composition root.
    """
    try:
        with open(heartbeat_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict) or set(payload) != {"pid", "process_epoch"}:
            return False
        pid = payload["pid"]
        epoch = payload["process_epoch"]
        if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(epoch, str):
            return False
        if epoch != _process_epoch(pid):
            return False
        age = time.time() - os.path.getmtime(heartbeat_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return 0 <= age < max_age_s


def _resolve_seams(dotted: str) -> AppSeams:
    """Resolve a ``module:callable`` seam factory (CI-only; empty ⇒ real edges)."""
    from ne.api.platform import AppSeams

    if not dotted:
        return AppSeams()
    module_name, _, attr = dotted.partition(":")
    if not attr:
        raise ValueError(f"NE_WORKER_SEAMS must be 'module:callable', got {dotted!r}")
    factory = getattr(import_module(module_name), attr)
    seams = factory()
    if not isinstance(seams, AppSeams):
        raise TypeError(f"seam factory {dotted!r} must return AppSeams, got {type(seams)!r}")
    return seams


async def _append_skill_delivery_receipt(
    platform: Platform, receipt: SkillDeliveryReceipt
) -> _SkillDeliveryReceiptIdentity:
    """Persist one exact content-free reconciliation event or fail worker boot."""

    from ne.api.skills_delivery import skill_delivery_audit_event
    from ne.audit import records_by_event_ids

    event = skill_delivery_audit_event(receipt, config_hash=platform.config_hash)
    await platform.sink.append(event)
    records = records_by_event_ids(platform.audit_engine, [event.event_id])
    if len(records) != 1 or records[0].payload != event.model_dump(mode="json"):
        raise RuntimeError("Skill delivery reconciliation audit receipt was not persisted exactly")
    record = records[0]
    return _SkillDeliveryReceiptIdentity(
        event_id=record.event_id,
        audit_sequence=record.seq,
        audit_record_hash=record.record_hash,
    )


# --- the run loop -----------------------------------------------------------


def _compose_cron_scheduler(
    platform: Platform,
    worker_settings: WorkerSettings,
    plugin_registry: PluginRegistry | None = None,
) -> CronScheduler:
    """Assemble the worker's ONE cron scheduler from every governed surface.

    This is the single production assembly point for the worker cron path
    (``_run`` calls it once at boot, before any ticker/DBOS task exists), and it
    is deliberately a module-level seam: the composition tests in
    ``test_cron_sole_tool_invariant.py`` drive THIS function with real and rogue
    surfaces, so the sole-tool boot enforcement below is proven where production
    actually runs it — removing the ``require_sole_tool_definitions`` invocation
    here turns those tests red (the vacuous-guard lesson: a guardian tested only
    as a free function proves nothing about the boot path that must call it).

    Steps, in order:

    * Build the six governed cron surfaces (connector-sync, principal-sync,
      skill-forge, skill-gardener, credential-rotation, answer-ledger-reask).
      Each fails LOUD at boot for a listed-but-misconfigured source.
    * Enforce 63279abe's invariant STRUCTURALLY over the FULL merged definition
      map: every definition the scheduler can run must be
      ``scheduled_execution="sole-tool"`` — a surface that keeps the ``"agent"``
      default would put a model-arbitration loop (and the shared litellm
      Router) back on the cron tick path the moment its flag flips on. Fails
      BOOT loud, naming the offenders, before the scheduler exists.
    * Construct the scheduler with THE SAME shared sink/gate/ledger/counter/
      scope-rail as the turn executor (NE-CKD1 recipe).
    * Seed every surface's jobs (idempotent + reconciling, each scoped to its
      own name prefix; empty config is a NON-DESTRUCTIVE no-op — see
      ``reconcile_prefixed_jobs``).
    """
    from ne.runtime.factory import create_cron_scheduler
    from ne.runtime.principals import StaticAgentPrincipalProvider
    from ne.worker.answer_ledger_reask import (
        build_answer_ledger_reask_surface,
        seed_answer_ledger_reask_jobs,
    )
    from ne.worker.credential_rotation import (
        build_credential_rotation_surface,
        seed_credential_rotation_jobs,
    )
    from ne.worker.cron_seed import require_sole_tool_definitions
    from ne.worker.ingest import build_connector_sync_surface, seed_connector_sync_jobs
    from ne.worker.principal_sync import (
        build_principal_sync_surface,
        seed_principal_sync_jobs,
    )
    from ne.worker.skill_forge import build_skill_forge_surface, seed_skill_forge_jobs
    from ne.worker.skill_gardener import (
        build_skill_gardener_surface,
        seed_skill_gardener_jobs,
    )

    # NE-INGEST (P1-4): ONE tier-1 tool + AgentDefinition + Principal +
    # ScheduledJob per configured source; empty config ⇒ empty surface ⇒ zero
    # jobs (fail-closed no-op).
    surface = build_connector_sync_surface(platform)
    # NE-FIX-SYNC-INVOKERS (P2-20): pull-capable principal source (entra/fixture)
    # only; default oidc-claims / SCIM ⇒ empty surface.
    psync = build_principal_sync_surface(platform)
    # NE-SKILL-1b / NE-SKILL-4: default-off skill surfaces (disjoint namespaces).
    skforge = build_skill_forge_surface(platform)
    skgarden = build_skill_gardener_surface(platform)
    # NE-IDENTITY-ROTATION-CRON: ALWAYS-ON (an empty credential set makes the
    # sweep a cheap, audited no-op).
    crot = build_credential_rotation_surface(platform)
    # BD-10: explicitly default-off governed scheduled re-ask sweep.
    alreask = build_answer_ledger_reask_surface(platform, worker_settings)

    definitions = {
        **surface.definitions,
        **psync.definitions,
        **skforge.definitions,
        **skgarden.definitions,
        **crot.definitions,
        **alreask.definitions,
    }
    require_sole_tool_definitions(definitions)

    scheduler = create_cron_scheduler(
        engine=platform.runtime_engine,
        bindings=surface.bindings
        + psync.bindings
        + skforge.bindings
        + skgarden.bindings
        + crot.bindings
        + alreask.bindings,
        definitions=definitions,
        sink=platform.sink,
        approval_gate=platform.gate,
        gateway=platform.gateway,
        principals=StaticAgentPrincipalProvider(
            {
                **surface.principals,
                **psync.principals,
                **skforge.principals,
                **skgarden.principals,
                **crot.principals,
                **alreask.principals,
            }
        ),
        settings=platform.runtime_settings,
        govern_settings=platform.govern_settings,
        ledger=platform.ledger,
        counter=platform.counter,
        extra_guardrails=(platform.scope_rail,),
        # The composed PDP plugin, forwarded so the CRON path resolves the same
        # policy_decision_point adapter as every other executor. Omitting it made
        # create_cron_scheduler fall back to the entry-point default against
        # GovernSettings.opa_url, so a deployment (or a CI seam) that registers a
        # PDP plugin had it SILENTLY IGNORED on the entire scheduled-job path --
        # every other create_pdp call site in the repo already passes a registry.
        plugin_registry=plugin_registry,
    )
    seed_connector_sync_jobs(
        scheduler,
        surface.jobs,
        authoritative_empty=platform.connect_sync_settings.runnable_sources is not None,
    )
    seed_principal_sync_jobs(scheduler, psync.jobs)
    seed_skill_forge_jobs(scheduler, skforge.jobs)
    seed_skill_gardener_jobs(scheduler, skgarden.jobs)
    seed_credential_rotation_jobs(scheduler, crot.jobs)
    seed_answer_ledger_reask_jobs(scheduler, alreask.jobs)
    return scheduler


async def _run() -> None:
    global _scheduler
    # NE-103c FIX-2: the heavy composition graph is imported HERE (not at module
    # top-level) so the --health probe stays light.
    from ne.api.platform import build_platform
    from ne.api.settings import ApiSettings
    from ne.worker.audit_checkpoint import (
        build_audit_checkpoint_loop,
        run_audit_checkpoint_loop,
    )
    from ne.worker.relay import RecordingFrameWriter
    from ne.worker.runtime_state import set_frame_writer_factory, set_worker_platform

    logging.basicConfig(level=logging.INFO)
    # (1) Settings + resolve seams.
    api_settings = ApiSettings()  # type: ignore[call-arg]
    worker_settings = WorkerSettings()  # type: ignore[call-arg]
    seams = _resolve_seams(worker_settings.seams)

    # (2) PRE-011F: the worker is the SOLE bundle reconciler. Use a temporary
    # shared-registry engine, validate the exact mounted/raw/Agent pins, reconcile,
    # re-read the admitted row, and dispose that engine BEFORE platform/catalog
    # construction. A stale catalog can therefore never be frozen before admission.
    from ne.api.skills_delivery import reconcile_required_agent_skills_from_environment

    skill_receipt = await asyncio.to_thread(reconcile_required_agent_skills_from_environment)
    _LOG.info(
        "ne-worker Skill delivery ready: pins=%d fingerprint=%s",
        len(skill_receipt.pins),
        skill_receipt.fingerprint,
    )

    # (3) Compose the platform — freezes the composed hash ONCE (the §6.2 boot
    # cross-check: both processes log it and it MUST match).
    platform = build_platform(api_settings, seams)
    _LOG.info("ne-worker composed platform config_hash=%s", platform.config_hash)
    skill_receipt_identity = await _append_skill_delivery_receipt(platform, skill_receipt)
    _LOG.info(
        "ne-worker Skill delivery receipt event_id=%s audit_seq=%d audit_record_hash=%s",
        skill_receipt_identity.event_id,
        skill_receipt_identity.audit_sequence,
        skill_receipt_identity.audit_record_hash,
    )
    # PRE-009A A3: the Agent Run store, its schema, cipher and CURRENT-KEY readiness
    # are composed inside build_platform, so the worker inherits the API's recipe
    # byte for byte -- one recipe, two processes. Duplicating the composition here
    # is exactly how the two roots come to disagree about which key is current, so
    # the worker only REPORTS what it inherited. A root whose retained private rows
    # are unopenable never reaches this line: build_platform raises first.
    _LOG.info(
        "ne-worker agent run storage active=%s",
        platform.agent_run_store is not None,
    )
    # PRE-010B: install only the additive lease/checkpoint/audit-intent schema.
    # The workflow is registered below in DBOS mode but remains execution-dormant
    # until 010D supplies an accepted profile/effective-identity adapter.
    if platform.agent_run_store is not None:
        from ne.runtime.run_workflow import ensure_run_workflow_schema

        ensure_run_workflow_schema(platform.runtime_engine)
    # PRE-009C: DBOS recovery must never run under the legacy/plaintext wire or
    # against a process whose private store was not admitted with that generation.
    # Keep this before process-global installation and therefore before launch.
    from ne.api.turn_wire import PROTECTED_TRANSPORT_GENERATION

    if platform.default_ask_generation:
        if (
            platform.agent_transport_generation != PROTECTED_TRANSPORT_GENERATION
            or platform.default_ask_generation != "default-ask/canonical-run/v1"
            or platform.agent_run_store is None
        ):
            raise RuntimeError("canonical default Ask generation is not ready")
        platform.agent_run_store.activate_default_ask_at_boot(platform.default_ask_generation)

    # Install process globals only after Skill delivery is verified.  DBOS recovery
    # and every worker-hosted catalog consumer therefore see one admitted generation.
    set_worker_platform(platform)

    runtime_settings = platform.runtime_settings

    # NE-AUDIT-CRON: build the periodic signed-WORM audit-checkpoint loop's seams.
    # Default-off (EMPTY NE_WORKER_AUDIT_CHECKPOINT_OBJECT_STORE) ⇒ None ⇒ no task is
    # created below (default-safe, mirrors outbox_consumer). A configured-but-unknown
    # ne.plugins.object_store adapter name fails worker BOOT loud here, before any
    # task starts — never a silent no-op. This is a PLAIN maintenance loop (like the
    # heartbeat/outbox drain), NOT a ScheduledJob — see audit_checkpoint.py's module
    # docstring for the decision.
    audit_ckpt = build_audit_checkpoint_loop(platform, worker_settings)

    # (6) Build every governed cron surface, enforce the sole-tool invariant over
    # the FULL merged definition map, construct the ONE scheduler and seed all
    # jobs — the single production assembly point for the worker cron path
    # (_compose_cron_scheduler, exercised directly by the composition tests in
    # test_cron_sole_tool_invariant.py so the enforcement cannot silently
    # detach from boot).
    _scheduler = _compose_cron_scheduler(platform, worker_settings, seams.plugin_registry)

    dbos_instance: Any = None
    stop = asyncio.Event()

    if runtime_settings.durability == "dbos":
        dbos_instance = _launch_dbos(worker_settings)
    else:
        # (5) durability='off': no DBOS; the recording writer; the plain asyncio
        # ticker drives cron.
        set_frame_writer_factory(lambda turn_id: RecordingFrameWriter(turn_id))

    # (8/7/5) Background tasks: heartbeat, optional outbox drain, and — in off mode —
    # the plain cron ticker.
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_heartbeat_loop(platform, worker_settings, stop)),
    ]
    if worker_settings.outbox_consumer:
        # P2-21: build the drain handler ONCE. A configured consumer REQUIRES a real
        # in-cluster OTLP sink — an external endpoint (C2) or an EMPTY endpoint both
        # fail worker BOOT loud via _build_outbox_handler (fail-closed): a sink-less
        # drain would ack + prune never-exported events (silent loss). Building here
        # (not per-tick) means a misconfigured sink never starts a hot-looping drain.
        handler = _build_outbox_handler(worker_settings)
        tasks.append(
            asyncio.create_task(_outbox_drain_loop(platform, worker_settings, handler, stop))
        )
    # NE-AUDIT-CRON: the periodic signed-WORM audit-checkpoint loop. Runs in BOTH
    # durability modes (like heartbeat/outbox) — default-off, so absent config never
    # creates this task at all.
    if audit_ckpt is not None:
        tasks.append(asyncio.create_task(run_audit_checkpoint_loop(audit_ckpt, stop)))
    if runtime_settings.durability != "dbos":
        tasks.append(asyncio.create_task(_cron_ticker_loop(worker_settings, stop)))

    _install_signal_handlers(stop)
    _LOG.info("ne-worker ready (durability=%s)", runtime_settings.durability)
    await stop.wait()

    # (9) Graceful shutdown: cancel tasks + DBOS.destroy().
    _LOG.info("ne-worker shutting down")
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if dbos_instance is not None:
        with contextlib.suppress(Exception):
            from dbos import DBOS

            DBOS.destroy()
        # Stop the persistent cron-tick loop AFTER DBOS.destroy() so no tick can
        # fire against a stopped runner (destroy tears the scheduler thread down).
        _cron_tick_runner.stop()
    await _shutdown_platform(platform)


async def _shutdown_platform(platform: Platform) -> None:
    """Close the retained ReBAC client (NE-087 FIX-2), then dispose the engines.

    Mirrors the ne-api lifespan teardown so BOTH process roots release the confined
    OpenFGA client's owned async HTTP session. Default-off (``rebac_client is None``)
    is a no-op; engine disposal always runs (``finally``).
    """
    try:
        if platform.rebac_client is not None:
            await platform.rebac_client.close()
    finally:
        for engine in platform.engines:
            engine.dispose()


def _launch_dbos(worker_settings: WorkerSettings) -> Any:
    """Register the workflow + queue + cron tick, then construct + launch DBOS.

    DBOS recovery of PENDING workflows begins at ``launch()`` — hence the platform
    globals are set FIRST (see ``_run``). ``run_admin_server=False`` is the decided
    deviation (see the module docstring).
    """
    from ne.api.turn_relay import normalize_dbos_url
    from ne.worker.relay import DbosStreamFrameWriter
    from ne.worker.runtime_state import set_frame_writer_factory

    if not worker_settings.dbos_database_url:
        raise ValueError(
            "NE_RUNTIME_DURABILITY=dbos requires NE_WORKER_DBOS_DATABASE_URL "
            "(the dedicated ne_dbos system database)"
        )
    import dbos as dbos_mod

    # Import registers the @ne_workflow('chat.turn') under real DBOS decoration.
    import_module("ne.worker.workflows")

    # The named queue gives a global worker-side concurrency cap under the C12
    # per-principal guardrail caps (design §2.1).
    dbos_mod.Queue(worker_settings.turn_queue, worker_concurrency=worker_settings.turn_concurrency)
    # Canonical Agent Runs are a distinct fixed queue; their DBOS workflow id is
    # the durable enqueue_id supplied by ne-api.
    dbos_mod.Queue("agent-runs", worker_concurrency=worker_settings.turn_concurrency)

    # Register the cron tick (signature (scheduled_time, actual_time)); v1 ships
    # zero jobs, so the governed tick surface is the deliverable.
    _register_cron_tick(dbos_mod)

    # Frames are written from workflow context = exactly-once (design §5.1).
    set_frame_writer_factory(lambda turn_id: DbosStreamFrameWriter(turn_id))

    # DECIDED DEVIATION: construct DBOS directly with run_admin_server=False (the
    # unauthenticated admin HTTP server must not run in the hardened container).
    #
    # NE-103c DURABILITY FIX (readiness ⟹ enqueue-safe): pass the dedicated ne_dbos
    # database as DBOS's SYSTEM database (``system_database_url``), NOT as
    # ``database_url``. Under DBOS 2.26 a bare ``database_url`` is the APPLICATION
    # database and the system schema — ``dbos.workflow_status``, the row every enqueue
    # INSERTs into — is migrated into a DERIVED ``<db>_dbos_sys`` database. But the
    # enqueuers (ne-api's ``DbosTurnEnqueuer`` and the ``DBOSClient`` in the live
    # proofs) connect to THIS url AS the system database, so the worker MUST migrate
    # the very same database or every cross-process enqueue hits
    # ``relation "dbos.workflow_status" does not exist``. Providing only
    # ``system_database_url`` also creates NO application database — correct here, as
    # the turn workflow has zero ``@DBOS.transaction`` steps (durable state flows
    # through the governed sink, not a DBOS app DB).
    system_url = normalize_dbos_url(worker_settings.dbos_database_url)
    instance = dbos_mod.DBOS(
        config=dbos_mod.DBOSConfig(
            name=worker_settings.dbos_app_name,
            system_database_url=system_url,
            run_admin_server=False,
        )
    )
    dbos_mod.DBOS.launch()
    # DBOS.launch() runs the system-schema migrations synchronously; assert that
    # explicitly so the readiness marker the enqueuer waits on can NEVER be written
    # before an enqueue into ``dbos.workflow_status`` would succeed. Runs BEFORE the
    # heartbeat loop (and thus the first marker) starts in ``_run``. Idempotent and
    # safe across the sigkill-recovery restart. Uses the SQLAlchemy ``+psycopg``
    # (house driver) form of the URL, NOT the DBOS-normalized plain DSN.
    _verify_dbos_enqueue_ready(worker_settings.dbos_database_url, worker_settings.turn_queue)
    return instance


def _verify_dbos_enqueue_ready(dbos_database_url: str, queue_name: str) -> None:
    """Confirm the DBOS system schema is migrated + enqueue-ready before readiness.

    The contract the worker's readiness marker MUST honour: "worker signals ready" ⟹
    "an enqueue into its DBOS system database will succeed". This probes the ACTUAL
    schema state (``dbos.workflow_status`` present) rather than sleeping — a bounded
    poll only to tolerate any lag between ``DBOS.launch()`` returning and the migrated
    relation becoming visible to a fresh connection. Idempotent; safe to re-run on the
    worker restart the exactly-once proof performs.

    ``dbos_database_url`` is the SQLAlchemy house form (``postgresql+psycopg://``);
    coerce a bare ``postgresql://`` onto the psycopg-v3 driver so the probe engine
    never reaches for psycopg2 (not a project dependency).
    """
    import sqlalchemy as sa

    probe_url = dbos_database_url
    if probe_url.startswith("postgresql://"):
        probe_url = "postgresql+psycopg://" + probe_url[len("postgresql://") :]
    engine = sa.create_engine(probe_url)
    try:
        deadline = time.monotonic() + 30.0
        last_err: BaseException | None = None
        while True:
            try:
                with engine.connect() as conn:
                    present = conn.execute(
                        sa.text("SELECT to_regclass('dbos.workflow_status')")
                    ).scalar()
                if present is not None:
                    return
                last_err = None
            except Exception as exc:  # transient boot/connection race — retry within budget
                last_err = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "DBOS system schema not enqueue-ready after launch(): "
                    "dbos.workflow_status missing in the worker system database "
                    f"(queue {queue_name!r}); last error: {last_err!r}"
                )
            time.sleep(0.2)
    finally:
        engine.dispose()


def _register_cron_tick(dbos_mod: Any) -> Any:
    """Register the cron tick as a REAL DBOS workflow, THEN schedule it (P1-6).

    P1-6: ``DBOS.scheduled(...)`` handed the PLAIN :func:`_dbos_cron_tick` registers a
    function that is NOT decorated with ``@DBOS.workflow``. At every fire the scheduler
    thread calls ``get_dbos_func_name`` on it (dbos 2.26 ``_scheduler_decorator.py``),
    which raises ``DBOSWorkflowFunctionNotFoundError``; the scheduler loop's blanket
    ``except`` swallows it into a log warning, so the governed tick NEVER runs in
    production durability mode while the worker stays 'healthy'. The workflow wrap MUST
    precede ``scheduled`` — we register ``cron.tick`` as a workflow first, then schedule
    the WRAPPED workflow so every fire enqueues a real ``sched-cron.tick-*`` workflow.
    """
    workflow = dbos_mod.DBOS.workflow(name="cron.tick")(_dbos_cron_tick)
    dbos_mod.DBOS.scheduled("* * * * *")(workflow)
    return workflow


class _CronTickLoopRunner:
    """ONE persistent event loop on ONE dedicated thread for every offloaded tick,
    admitting AT MOST ONE tick body in flight.

    Why persistence is load-bearing and a per-tick ``asyncio.run`` is FORBIDDEN
    here: the tick body reaches SHARED platform singletons whose internals are
    event-loop-affine once first used (the chunk store's ``AsyncQdrantClient``,
    the TEI embedder's cached ``httpx.AsyncClient``, the scheduler's own OPA PDP
    client). A fresh loop per tick (a) re-binds those internals to a loop that is
    then CLOSED, so the next user — including a ``read.knowledge.search`` on the
    DBOS loop — dies with "bound to a different event loop" / "Event loop is
    closed" (observed live on ne-pilot-ne-worker-1, 2026-08-28), and (b) breaks
    litellm's client cache, which keys async clients by ``id(running_loop)`` — a
    closed loop's id can be reused. With ONE long-lived tick loop, every
    loop-affine resource the tick touches lives on exactly one loop forever, and
    the per-loop client seams in ne-brain (qdrant/embedder) hand the DBOS loop
    and this loop each their OWN clients.

    Why the in-flight guard is EQUALLY load-bearing: before the offload the cron
    body BLOCKED the DBOS loop for the whole sweep (182-653s measured live), so
    tick N+1 structurally could not start until tick N finished — serialization
    was accidental, but it was what kept different jobs' sync bodies from
    overlapping on the shared ``IngestStack``/``ConnectorStateStore``. Freeing
    the loop removed that serialization and put nothing in its place (measured:
    3 concurrent tick bodies). The scheduler's per-job lease only suppresses
    re-entry of the SAME job; two DIFFERENT connector jobs overlapping is what
    armed the cross-source mapper-refresh corruption (ne.connect.state F1/F2).
    A tick that fires while the previous body is still running is therefore
    SKIPPED — logged, never queued: the work is periodic, so the next admitted
    tick runs the same sweep, and queuing would only build an unbounded backlog
    behind a slow crawl.
    """

    #: stop() budgets. DRAIN is how long a graceful shutdown WAITS for the
    #: in-flight body to finish on its own — the body this runner exists to host
    #: is a synchronous blocking sweep, which cancellation cannot interrupt, so
    #: waiting IS the drain; cancelling first would return in microseconds while
    #: the sweep kept running (the reviewed defect). CANCEL_GRACE is the bounded
    #: last resort after the drain budget: how long to wait for a cancellable
    #: body to honour the cancel before the loop thread is abandoned.
    _STOP_DRAIN_TIMEOUT_S: float = 15.0
    _STOP_CANCEL_GRACE_S: float = 5.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        #: (future, done). ``done`` is set in the body wrapper's ``finally`` ON
        #: THE TICK LOOP, so it is the honest "body no longer running" signal.
        #: The future's own ``done()`` is NOT: cancelling it flips it CANCELLED
        #: immediately while the body keeps executing (concurrent.futures
        #: semantics), which is exactly how the first stop() implementation
        #: "drained" in microseconds.
        self._inflight: tuple[concurrent.futures.Future[None], threading.Event] | None = None

    def _ensure_loop_locked(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._thread is None or not self._thread.is_alive():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="ne-cron-tick-loop", daemon=True
            )
            thread.start()
            self._loop = loop
            self._thread = thread
        return self._loop

    async def run(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run ``coro`` on the persistent tick loop; await without blocking the caller.

        With the previous tick body still in flight, the new one is SKIPPED (the
        coroutine is closed unawaited) — one tick body at a time, see the class
        docstring. Admission and submission happen under ONE lock hold, so two
        concurrent callers cannot both slip past the in-flight check.
        """
        with self._lock:
            inflight = self._inflight
            if inflight is not None and not inflight[1].is_set():
                coro.close()
                _LOG.warning(
                    "cron tick skipped: the previous tick body is still running "
                    "(one tick body at a time; the next admitted tick runs the sweep)"
                )
                return
            done = threading.Event()
            future = asyncio.run_coroutine_threadsafe(
                self._tracked(coro, done), self._ensure_loop_locked()
            )
            self._inflight = (future, done)
        await asyncio.wrap_future(future)

    @staticmethod
    async def _tracked(coro: Coroutine[Any, Any, None], done: threading.Event) -> None:
        try:
            await coro
        finally:
            done.set()

    def stop(self) -> None:
        """Stop the loop thread (graceful shutdown / tests). Safe to call twice.

        DRAIN FIRST, cancel as a bounded last resort. The body is typically a
        synchronous blocking sweep (the reason this runner exists), and a
        blocked coroutine cannot observe cancellation — worse, cancelling the
        ``run_coroutine_threadsafe`` future flips it CANCELLED immediately
        while the body keeps running, so cancel-then-wait waits for nothing
        (cross-model review, 2026-08-29: measured 4µs) and the loop was then
        stopped and closed underneath live SQL. Order here:

        1. WAIT up to ``_STOP_DRAIN_TIMEOUT_S`` for the body's own completion
           signal (the tick-loop-side ``done`` event, not the future).
        2. Past the budget, CANCEL (propagates to the body's task) and wait up
           to ``_STOP_CANCEL_GRACE_S`` for the cancellation to land.
        3. Past both bounds the body is truly wedged in blocking code: log an
           ERROR and ABANDON its daemon loop thread — ``loop.stop`` is queued
           so the thread exits when the body finally returns, and the
           still-running loop is deliberately NOT closed.
        """
        with self._lock:
            loop, thread, inflight = self._loop, self._thread, self._inflight
            self._loop = None
            self._thread = None
            self._inflight = None
        if loop is None:
            return
        abandoned = False
        if inflight is not None:
            future, done = inflight
            if not done.is_set() and not done.wait(self._STOP_DRAIN_TIMEOUT_S):
                _LOG.warning(
                    "cron tick loop stop: in-flight tick body still running after the "
                    "%.0fs drain budget; cancelling it as a last resort",
                    self._STOP_DRAIN_TIMEOUT_S,
                )
                future.cancel()
                if not done.wait(self._STOP_CANCEL_GRACE_S):
                    _LOG.error(
                        "cron tick loop stop: in-flight tick body ignored cancellation "
                        "for %.0fs (blocked in synchronous code); abandoning its daemon "
                        "loop thread — it will exit when the body returns",
                        self._STOP_CANCEL_GRACE_S,
                    )
                    abandoned = True
        loop.call_soon_threadsafe(loop.stop)
        if abandoned:
            # The loop thread cannot exit until the wedged body returns, so
            # joining here would silently re-spend the wait the bounds already
            # refused. loop.stop is queued: the daemon thread exits the moment
            # the body yields back, and the still-running loop is NOT closed.
            return
        if thread is not None:
            thread.join(timeout=5.0)
        if not loop.is_running():
            loop.close()


#: Process-global (like ``_scheduler``): the DBOS-registered tick reaches it.
_cron_tick_runner = _CronTickLoopRunner()


async def _dbos_cron_tick(_scheduled_time: object, _actual_time: object) -> None:
    """The governed cron tick — wrapped as the REAL workflow 'cron.tick' by
    :func:`_register_cron_tick` in dbos mode, so DBOS runs it on its OWN event loop.

    The tick BODY is offloaded off the DBOS loop (PR #886's diagnosis, applied):
    the scheduler's jobs — the m365/salesforce authority sweeps — run sync
    SQLAlchemy and multi-minute connector I/O, and awaiting them inline froze the
    shared DBOS event loop for the whole sweep (measured live: cron.tick
    durations of 182-653s), stalling every in-flight Run workflow's step
    bookkeeping on that loop. Two governed-outreach Runs wedged permanently
    mid-ladder on demo.example.host (2026-08-28, d2bf7583/7d8635b8).

    It runs on :data:`_cron_tick_runner`'s ONE persistent loop — NEVER a fresh
    ``asyncio.run`` per tick, which poisoned the shared loop-affine clients for
    the DBOS loop's own tool reads (see :class:`_CronTickLoopRunner`). A job
    failure stays contained to the tick exactly as before.
    """
    scheduler = _scheduler
    if scheduler is None:
        return

    async def _body() -> None:
        await scheduler.run_requested_jobs()
        await scheduler.run_due_jobs()

    await _cron_tick_runner.run(_body())


async def _heartbeat_loop(
    platform: Platform, worker_settings: WorkerSettings, stop: asyncio.Event
) -> None:
    """Every ``heartbeat_s``: ``SELECT 1`` then touch the marker (DB-dead ⇒ stale)."""
    import sqlalchemy as sa

    path = worker_settings.heartbeat_path
    while not stop.is_set():
        try:
            with platform.runtime_engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            _write_heartbeat_marker(path)
        except Exception:
            # marker simply goes stale and the healthcheck reports unhealthy.
            _LOG.warning("ne-worker heartbeat DB ping failed; marker will go stale")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=worker_settings.heartbeat_s)


async def _cron_ticker_loop(worker_settings: WorkerSettings, stop: asyncio.Event) -> None:
    """The plain-asyncio cron ticker for ``durability='off'`` mode."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=worker_settings.cron_tick_s)
        if stop.is_set():
            return
        scheduler = _scheduler
        if scheduler is not None:
            with contextlib.suppress(Exception):
                await scheduler.run_requested_jobs()
                await scheduler.run_due_jobs()


def _build_outbox_handler(worker_settings: WorkerSettings) -> Callable[[list[Any]], None]:
    """Construct the outbox drain handler (P2-21). Called ONCE at boot, ONLY when
    ``outbox_consumer`` is set.

    A configured drain consumer REQUIRES a real in-cluster OTLP sink. The exporter's
    ``validate_in_vpc_endpoint`` construction rejects an external SIEM host, so a worker
    pointed off-cluster fails BOOT loud (fail-closed, C2). An EMPTY endpoint ALSO fails
    boot: a count-only fallback handler would return success and let ``drain_once`` ACK
    (advance the cursor) though NOTHING was exported, and this unit's P2-21 retention
    prune (:func:`ne.events.outbox.prune_acked`) would then PERMANENTLY DELETE those
    never-exported audit/cost/approval events — a silent at-least-once data-loss hole
    (Codex P1). The drain's ONLY sanctioned handler is the real exporter; enabling the
    consumer without a sink is a boot error, not a silent no-op.
    """
    if not worker_settings.outbox_otlp_endpoint:
        raise ValueError(
            "NE_WORKER_OUTBOX_CONSUMER is set but NE_WORKER_OUTBOX_OTLP_ENDPOINT is "
            "empty. A drain consumer with no OTLP sink would ack (advance the cursor) "
            "without exporting, and the retention prune would then permanently delete "
            "never-exported outbox events (silent at-least-once loss). Set the "
            "in-cluster collector endpoint (e.g. http://otel-collector:4318), or clear "
            "NE_WORKER_OUTBOX_CONSUMER to disable the drain."
        )
    from ne.obs.otlp import OutboxOtelLogHandler, build_otlp_log_record_exporter

    return OutboxOtelLogHandler(
        build_otlp_log_record_exporter(worker_settings.outbox_otlp_endpoint)
    )


async def _outbox_drain_loop(
    platform: Platform,
    worker_settings: WorkerSettings,
    handler: Callable[[list[Any]], None],
    stop: asyncio.Event,
) -> None:
    """Tick ``drain_once`` (SKIP LOCKED, multi-replica-safe) every ``outbox_drain_s``.

    P2-21: the ``handler`` (the real OTel exporter — the ONLY sanctioned sink) ships
    each drained window. An export/DB fault is throttle-logged (class name only, at most
    once per 60s) and leaves the cursor unmoved (fail-open, at-least-once — the window
    redelivers). Every ``outbox_prune_interval_s`` the loop runs the retention prune so
    ``ne_outbox_events`` stays bounded.
    """
    from ne.events.outbox import drain_once, prune_acked

    consumer = worker_settings.outbox_consumer
    last_prune = time.monotonic()
    last_warn = 0.0

    while not stop.is_set():
        try:
            result = await asyncio.to_thread(drain_once, platform.runtime_engine, consumer, handler)
            error = result.error
        except Exception as exc:  # a DB fault in the read/ack transactions
            error = repr(type(exc).__name__)
        if error is not None:
            now = time.monotonic()
            if now - last_warn >= 60.0:
                last_warn = now
                _LOG.warning("ne-worker outbox drain error (throttled 60s): %s", error)

        now = time.monotonic()
        if now - last_prune >= worker_settings.outbox_prune_interval_s:
            last_prune = now
            with contextlib.suppress(Exception):
                pruned = await asyncio.to_thread(
                    prune_acked,
                    platform.runtime_engine,
                    older_than_s=worker_settings.outbox_retention_s,
                )
                if pruned > 0:
                    _LOG.info("ne-worker outbox prune: removed %d acked row(s)", pruned)

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=worker_settings.outbox_drain_s)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _run_eval_command(args: argparse.Namespace) -> int:
    """Compose the real worker platform and persist one measured Route-A run."""

    import json
    from dataclasses import replace

    from ne.api.platform import build_platform
    from ne.api.settings import ApiSettings
    from ne.eval.config import EvalSettings
    from ne.worker.eval_runner import run_route_a_once

    api_settings = ApiSettings()  # type: ignore[call-arg]
    worker_settings = WorkerSettings()  # type: ignore[call-arg]
    seams = _resolve_seams(worker_settings.seams)
    eval_settings = seams.eval_settings or EvalSettings()
    # Bind the exact settings instance into the platform hash, rather than
    # independently reconstructing an allegedly equivalent configuration.
    platform = build_platform(api_settings, replace(seams, eval_settings=eval_settings))
    result = await run_route_a_once(
        platform=platform,
        database_url=api_settings.eval_database_url,
        settings=eval_settings,
        principal_id=args.principal_id,
        suite_name=args.suite,
        golden_path=args.golden or None,
    )
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> None:
    """Entry point. ``--health`` is parsed FIRST and touches only WorkerSettings +
    the heartbeat-marker mtime — no heavy composition import (NE-103c FIX-2)."""
    parser = argparse.ArgumentParser(prog="ne.worker")
    parser.add_argument(
        "--health",
        action="store_true",
        help="exit 0 iff the heartbeat marker is fresh (< 60s), else 1",
    )
    commands = parser.add_subparsers(dest="command")
    eval_run = commands.add_parser(
        "eval-run", help="run one persisted Route-A evaluation over the composed reader"
    )
    eval_run.add_argument("--principal-id", required=True)
    eval_run.add_argument("--suite", default="route-a")
    eval_run.add_argument("--golden", default="")
    args = parser.parse_args(argv)
    if args.health:
        heartbeat_path = WorkerSettings().heartbeat_path  # type: ignore[call-arg]
        sys.exit(0 if _health_ok(heartbeat_path) else 1)
    if args.command == "eval-run":
        sys.exit(asyncio.run(_run_eval_command(args)))
    # An emptyDir/tmpfs can survive a same-Pod container restart. Invalidate the
    # old process's marker synchronously before any normal startup work; the marker
    # is also process-epoch bound so even a probe racing this unlink fails closed.
    _invalidate_heartbeat_marker(
        WorkerSettings().heartbeat_path  # type: ignore[call-arg]
    )
    asyncio.run(_run())


__all__ = ["main"]
