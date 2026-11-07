# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Composition seams — the required-sink, required-gate construction of the C6 executor.

Mirrors ``ne.govern.factory``: there is NO path here that omits the audit sink or
the approval gate. :func:`create_tool_executor` builds the
:class:`~ne.runtime.middleware.GovernedToolExecutor` by internally constructing
``ne.govern.create_pdp(..., sink=sink)`` and
``ne.govern.create_guardrail_engine(..., sink=sink)`` — both coupled to the SAME
required sink — so the executor cannot be assembled without its full governance
stack (C6). :func:`create_department_session` always mints a principal-owned
conversation; :func:`rehydrate_department_session` performs an exact
conversation/owner/department/agent lookup. Neither runnable seam resolves the
legacy department-pair session tables.

:func:`freeze_platform_config` is the boot-time composed-freeze seam: a
composition root calls it ONCE at process start to freeze the SPEC §4 replay/audit
``config_hash`` over the COMPOSED platform mapping (ne-runtime + ne-govern + any
caller-passed modules; ADR-0003) rather than a single-module subset.

Shared guardrail state + injected rails (NE-CKD1)
-------------------------------------------------
A composition root that runs MORE THAN ONE executor (the session executor + the
sub-agent dispatcher + the cron scheduler) MUST share ONE spend ledger + ONE rate
counter across all three, and ride ne-identity's ``AgentScopeGuardrail`` (the sole
``CredentialScope.tools`` enforcer, NE-036) on ALL THREE paths via
``extra_guardrails``. Otherwise (boundary-audit findings #1/#7/#8) each executor
tallies caps in PRIVATE state — dispatch + cron multiply the effective spend/rate
cap ~Nx — and the least-privilege scope check is wired into ZERO roots, so a
prompt-injected agent scoped to ``{read.own_docs}`` can invoke any registered
tier-0/1 tool. The normative recipe::

    ledger, counter = create_shared_guardrail_state()
    scope_rail = AgentScopeGuardrail(credential_service)  # composition root/test
    #   MAY import ne-identity; ne-runtime SRC MUST NOT (Protocol-typed seam).
    executor = create_tool_executor(
        bindings, sink=sink, approval_gate=gate,
        ledger=ledger, counter=counter, extra_guardrails=(scope_rail,),
    )
    dispatcher, _ = create_subagent_dispatcher(
        ..., ledger=ledger, counter=counter, extra_guardrails=(scope_rail,),
    )
    scheduler = create_cron_scheduler(
        ..., ledger=ledger, counter=counter, extra_guardrails=(scope_rail,),
    )

EVERY executor of one composition then shares ONE ledger + ONE counter, and the
scope rail rides ``extra_guardrails`` on all three paths. Executable proof:
``packages/ne-runtime/tests/test_composition_guardrail_seams.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage

from ne.audit import ensure_transactional_sqlite
from ne.audit.engine import create_engine_with_timeouts
from ne.contracts.config import freeze_platform_config_hash
from ne.contracts.governance import ApprovalGate, AuditSink, Guardrail
from ne.contracts.knowledge import KnowledgeWrite
from ne.contracts.models import ModelGateway
from ne.contracts.plugins import PluginRegistry
from ne.contracts.runtime import MemoryProvider
from ne.contracts.types import Principal
from ne.govern import create_guardrail_engine, create_pdp
from ne.govern.config import GovernSettings
from ne.govern.guardrails.durable import (
    SqlAlchemySlidingWindowCounter,
    SqlAlchemySpendLedger,
    ensure_guardrail_state_schema,
)
from ne.govern.guardrails.rate import SlidingWindowCounter
from ne.govern.guardrails.spend import InMemorySpendLedger, SpendLedger
from ne.runtime.agent import AgentExecutionBinding, DepartmentAgentSession
from ne.runtime.approval_metrics import ApprovalMetrics
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.config import RuntimeSettings
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.grounding import GroundingPolicy
from ne.runtime.memory_promotion import MemoryPromoter
from ne.runtime.memory_store import (
    AgentMemory,
    SqlAlchemyEpisodicStore,
    ensure_memory_schema,
)
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.principals import AgentPrincipalProvider
from ne.runtime.processes import ProcessRegistry
from ne.runtime.run_audit_receipts import (
    ExistingRunEvidenceContext as ExistingRunEvidenceContext,
)
from ne.runtime.run_audit_receipts import (
    ExistingRunEvidenceProvider as ExistingRunEvidenceProvider,
)
from ne.runtime.run_checkpoint import RunApprovalCheckpointStore
from ne.runtime.run_store import AgentRunStore
from ne.runtime.runner import BoundedAgenticAdapter
from ne.runtime.scheduler import CronScheduler, ensure_scheduler_schema
from ne.runtime.source_results import ProtectedSourceResultGuard
from ne.runtime.store import SqlAlchemySessionStore
from ne.runtime.tools import ToolBinding
from ne.runtime.toolset import ToolsetScopedExecutor
from ne.runtime.verified_router import VerifiedRouter
from ne.runtime.workflow_execution_profile import ExecutionStrategy, WorkflowExecutionProfile


def require_existing_run_evidence(
    provider: ExistingRunEvidenceProvider | None,
) -> ExistingRunEvidenceContext:
    """Fail closed when a verified read is not inside an existing canonical Run."""
    if not isinstance(provider, ExistingRunEvidenceProvider):
        raise LookupError("existing canonical Run evidence context is unavailable")
    return provider.next_read()


def create_agent_execution_binding(
    *,
    agent_id: str,
    agent_version: int,
    strategy: str | ExecutionStrategy,
    workflow_profile: WorkflowExecutionProfile | None = None,
    declared_profile_id: str | None = None,
    declared_profile_version: int | None = None,
    declared_profile_config_hash: str | None = None,
    agentic_adapter: BoundedAgenticAdapter | None = None,
) -> AgentExecutionBinding:
    """Boot-validate one Agent version against its exact execution dependency.

    Deterministic definitions must pin all three profile coordinates and they must
    equal the self-validated profile object.  Agentic definitions instead bind the
    existing bounded adapter.  The two paths are disjoint, and ``"hybrid"`` is
    refused by the closed strategy parser before any binding can be returned.
    """
    parsed = ExecutionStrategy.parse(strategy)
    declared = (
        declared_profile_id,
        declared_profile_version,
        declared_profile_config_hash,
    )
    if parsed is ExecutionStrategy.DETERMINISTIC:
        if workflow_profile is None:
            raise ValueError("deterministic Agent has no WorkflowExecutionProfile")
        if any(value is None for value in declared):
            raise ValueError("deterministic Agent must pin profile id, version and config hash")
        if declared != (
            workflow_profile.workflow_id,
            workflow_profile.version,
            workflow_profile.config_hash,
        ):
            raise ValueError("Agent workflow profile pin does not match the executable profile")
    elif workflow_profile is not None or any(value is not None for value in declared):
        raise ValueError("agentic Agent cannot carry a deterministic workflow profile pin")
    return AgentExecutionBinding(
        agent_id=agent_id,
        agent_version=agent_version,
        strategy=parsed,
        workflow_profile=workflow_profile,
        agentic_adapter=agentic_adapter,
    )


def freeze_platform_config(
    *,
    settings: RuntimeSettings | None = None,
    govern_settings: GovernSettings | None = None,
    extra_modules: Mapping[str, BaseModel] | None = None,
) -> str:
    """Freeze the COMPOSED platform ``config_hash`` ONCE at process start (ADR-0003).

    Call this ONCE at process start, BEFORE constructing any component that emits
    events: ``new_audit_event`` (and every other event factory) resolves the
    frozen hash, so the SPEC §4 replay/audit ``config_hash`` stamped on every
    ``AuditEvent`` becomes the composed platform hash covering EVERY module passed
    here — not the NE-033 single-module subset (``freeze_config_hash``).

    The composition root always freezes at least ``ne-runtime`` (:class:`RuntimeSettings`)
    and ``ne-govern`` (:class:`GovernSettings`); hosts composing further modules
    (gateway/identity/permissions/secrets/serve/audit/eval) MUST pass them via
    ``extra_modules`` keyed by package name (e.g. ``"ne-gateway"``), so the platform
    hash covers their config too. A key that collides with a module the root
    already freezes (``"ne-runtime"``/``"ne-govern"``) raises :class:`ValueError` —
    pass those via ``settings=``/``govern_settings=`` instead.

    Idempotent when re-called with an identical mapping; raises
    :class:`~ne.contracts.errors.ConfigHashMismatchError` on a divergent mapping
    (configuration must not change mid-flight), AND when mixed in the same process
    with a prior single-module :func:`~ne.contracts.config.freeze_config_hash`
    whose digest differs — there is exactly ONE frozen hash per process.

    Determinism, insertion-order independence, and secret-invariance are inherited
    ENTIRELY from :func:`ne.contracts.config.freeze_platform_config_hash`; this
    function performs NO serialization of its own.
    """
    modules: dict[str, BaseModel] = {
        "ne-runtime": settings or RuntimeSettings(),
        "ne-govern": govern_settings or GovernSettings(),
    }
    for key, model in (extra_modules or {}).items():
        if key in modules:
            raise ValueError(
                f"extra_modules key {key!r} collides with a module the composition "
                "root already freezes ('ne-runtime', 'ne-govern'); pass settings=/"
                "govern_settings= instead"
            )
        modules[key] = model
    return freeze_platform_config_hash(modules)


def create_shared_guardrail_state(
    database_url: str | None = None,
) -> tuple[SpendLedger, SlidingWindowCounter]:
    """Construct the ONE (spend ledger, rate counter) pair a composition SHARES.

    C12 (boundary-audit finding #1): a principal's/department's spend cap and
    rate limit are platform-wide invariants. A root that runs MORE THAN ONE
    executor (session + sub-agent dispatch + cron) MUST construct this pair
    ONCE and pass ledger=/counter= to EVERY executor factory — otherwise each
    executor tallies caps in private state and dispatch/cron multiply the
    effective cap ~Nx.

    ``database_url`` (NE-070H) selects durability:

    * ``None`` → the in-memory pair (exactly today's behaviour; process-local,
      test/dev default).
    * a SQLAlchemy URL (the PILOT tier's platform Postgres) → a DURABLE, ATOMIC
      pair (:class:`SqlAlchemySpendLedger` + :class:`SqlAlchemySlidingWindowCounter`)
      over one engine, so caps/limits are ATOMIC + SHARED across ALL workers and
      SURVIVE restarts (NE-CKD residual closed). The engine gets the ne-audit
      transactional-SQLite recipe applied HERE (in ne-runtime, the caller), so
      ne-govern's src never imports ne.audit. Production wiring itself stays
      app-root-parked (NE-CKD1 precedent) — the pilot composition root passes the
      platform Postgres URL.
    """
    if database_url is None:
        return InMemorySpendLedger(), SlidingWindowCounter()
    # Default EngineTimeouts() here — this seam has no RuntimeSettings in scope; the
    # composition root's URL still gets connect/statement/lock/pool bounds (P2-16).
    engine = create_engine_with_timeouts(database_url)
    ensure_transactional_sqlite(engine)
    ensure_guardrail_state_schema(engine)
    return SqlAlchemySpendLedger(engine), SqlAlchemySlidingWindowCounter(engine)


def create_tool_executor(
    bindings: Sequence[ToolBinding],
    *,
    sink: AuditSink,
    approval_gate: ApprovalGate,
    govern_settings: GovernSettings | None = None,
    registry: PluginRegistry | None = None,
    ledger: SpendLedger | None = None,
    counter: SlidingWindowCounter | None = None,
    extra_guardrails: Sequence[Guardrail] = (),
) -> GovernedToolExecutor:
    """Build the governed tool executor with its PDP + guardrails coupled to ``sink``.

    ``sink`` and ``approval_gate`` are REQUIRED keyword arguments — there is no
    factory path that omits either, so the C6 audit coupling and the HITL gate are
    non-optional by construction.

    ``ledger`` / ``counter`` are the SHARED spend ledger + rate counter (see
    :func:`create_shared_guardrail_state`); ``extra_guardrails`` are additional
    Protocol-typed rails appended after the four base C12 rails — the composition
    root passes ne-identity's ``AgentScopeGuardrail`` here (NE-036) WITHOUT
    ne-runtime importing ne-identity (the seam is typed by the frozen
    :class:`ne.contracts.governance.Guardrail` Protocol).
    """
    govern_settings = govern_settings or GovernSettings()
    pdp = create_pdp(govern_settings, sink=sink, registry=registry)
    guardrails = create_guardrail_engine(
        govern_settings,
        sink=sink,
        ledger=ledger,
        counter=counter,
        extra_guardrails=extra_guardrails,
        registry=registry,
    )
    return GovernedToolExecutor(
        bindings,
        pdp=pdp,
        guardrails=guardrails,
        approval_gate=approval_gate,
        sink=sink,
    )


def create_approval_gate(
    *,
    sink: AuditSink,
    settings: RuntimeSettings | None = None,
    engine: sa.Engine | None = None,
    metrics: ApprovalMetrics | None = None,
    clock: Callable[[], datetime] | None = None,
    source_guard: ProtectedSourceResultGuard | None = None,
    run_store: AgentRunStore | None = None,
) -> DurableApprovalGate:
    """Build the production :class:`DurableApprovalGate` wired to a persisted store.

    ``engine`` defaults to one over ``settings.session_database_url`` (a ValueError
    is raised if both are empty, mirroring ``SqlAlchemySessionStore.from_settings``).
    The approval schema (and the ne-events outbox) are ensured on the engine.

    The composition-root pattern is::

        gate = create_approval_gate(sink=sink, settings=settings)
        executor = create_tool_executor(bindings, sink=sink, approval_gate=gate)
    """
    settings = settings or RuntimeSettings()
    if engine is None:
        if not settings.session_database_url:
            raise ValueError(
                "create_approval_gate needs an engine or a non-empty "
                "RuntimeSettings.session_database_url (set "
                "NE_RUNTIME_SESSION_DATABASE_URL or pass engine=...)"
            )
        engine = create_engine_with_timeouts(
            settings.session_database_url, timeouts=settings.engine_timeouts
        )
    ensure_transactional_sqlite(engine)
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    kwargs: dict[str, object] = {}
    if metrics is not None:
        kwargs["metrics"] = metrics
    if clock is not None:
        kwargs["clock"] = clock
    if source_guard is not None:
        kwargs["source_guard"] = source_guard
    gate = DurableApprovalGate(store=store, sink=sink, settings=settings, **kwargs)  # type: ignore[arg-type]
    # PRE-010C: one coordinator over the exact same engine/store. Kept on the
    # gate so both API and worker roots inherit it without a parallel machine.
    gate._run_approval_checkpoints = (  # type: ignore[attr-defined]
        None if run_store is None else RunApprovalCheckpointStore(run_store, store)
    )
    return gate


def create_subagent_dispatcher(
    *,
    bindings: Sequence[ToolBinding],
    definitions: Mapping[str, AgentDefinition],
    sink: AuditSink,
    approval_gate: ApprovalGate,
    gateway: ModelGateway,
    principal: Principal,
    settings: RuntimeSettings | None = None,
    registry: ProcessRegistry | None = None,
    govern_settings: GovernSettings | None = None,
    ledger: SpendLedger | None = None,
    counter: SlidingWindowCounter | None = None,
    extra_guardrails: Sequence[Guardrail] = (),
    plugin_registry: PluginRegistry | None = None,
    source_guard: ProtectedSourceResultGuard | None = None,
) -> tuple[GovernedSubAgentDispatcher, ProcessRegistry]:
    """Build a :class:`GovernedSubAgentDispatcher` with its PDP/guardrails/executor
    coupled to ONE ``sink`` and the SAME ``pdp`` handed to the dispatcher (so the
    toolset-denied record and the executed-tool decisions share one audited PDP).

    ``sink`` and ``approval_gate`` are REQUIRED keyword arguments — there is no path
    that omits either, so the C6 audit coupling and HITL gate are non-optional
    (mirrors :func:`create_tool_executor`). Returns the dispatcher AND the
    :class:`ProcessRegistry` it counts global concurrency in (a fresh one when not
    supplied, so the caller can observe live/finished records).

    ``ledger`` / ``counter`` are the SHARED spend ledger + rate counter this
    dispatcher's executor tallies into — a composition root passes the ONE pair
    from :func:`create_shared_guardrail_state` so a department's cap is NOT reset
    per executor (C12, finding #1). ``extra_guardrails`` rides the same
    Protocol-typed seam as :func:`create_tool_executor` — the root passes
    ne-identity's ``AgentScopeGuardrail`` here so every dispatched sub-agent's tool
    calls are scope-checked (NE-036, findings #7/#8). ``plugin_registry`` is the
    :class:`ne.contracts.plugins.PluginRegistry` used to resolve the PDP (mirrors
    :func:`create_tool_executor`'s ``registry``); it is DISTINCT from the
    ``registry`` param above, which is the :class:`ProcessRegistry`.
    """
    settings = settings or RuntimeSettings()
    govern_settings = govern_settings or GovernSettings()
    registry = registry if registry is not None else ProcessRegistry()
    pdp = create_pdp(govern_settings, sink=sink, registry=plugin_registry)
    guardrails = create_guardrail_engine(
        govern_settings,
        sink=sink,
        ledger=ledger,
        counter=counter,
        extra_guardrails=extra_guardrails,
        registry=plugin_registry,
    )
    executor = GovernedToolExecutor(
        bindings, pdp=pdp, guardrails=guardrails, approval_gate=approval_gate, sink=sink
    )
    dispatcher = GovernedSubAgentDispatcher(
        definitions=definitions,
        executor=executor,
        gateway=gateway,
        registry=registry,
        sink=sink,
        settings=settings,
        principal=principal,
        pdp=pdp,
        source_guard=source_guard,
    )
    return dispatcher, registry


def create_cron_scheduler(
    *,
    engine: sa.Engine,
    bindings: Sequence[ToolBinding],
    definitions: Mapping[str, AgentDefinition],
    sink: AuditSink,
    approval_gate: ApprovalGate,
    gateway: ModelGateway,
    principals: AgentPrincipalProvider,
    settings: RuntimeSettings | None = None,
    registry: ProcessRegistry | None = None,
    govern_settings: GovernSettings | None = None,
    clock: Callable[[], datetime] | None = None,
    ledger: SpendLedger | None = None,
    counter: SlidingWindowCounter | None = None,
    extra_guardrails: Sequence[Guardrail] = (),
    plugin_registry: PluginRegistry | None = None,
) -> CronScheduler:
    """Build a :class:`CronScheduler` over ``engine`` with the SAME coupled PDP/guardrails.

    Ensures the scheduler schema (and the ne-events outbox) on ``engine`` first. As
    with the dispatcher, the SAME ``pdp`` instance is handed to both the executor and
    the scheduler so a per-job toolset-denied record and the executed-tool decisions
    share one audited PDP. ``sink`` and ``approval_gate`` are REQUIRED (C6).

    ``ledger`` / ``counter`` are the SHARED spend ledger + rate counter (the ONE
    pair from :func:`create_shared_guardrail_state`) so a cron run does NOT reset a
    department's cap that the session/dispatch executors already tallied (C12,
    finding #1). ``extra_guardrails`` rides the Protocol-typed seam — the root
    passes ne-identity's ``AgentScopeGuardrail`` here so scheduled agents are
    scope-checked (NE-036, findings #7/#8). ``plugin_registry`` is the
    :class:`ne.contracts.plugins.PluginRegistry` resolving the PDP; it is DISTINCT
    from ``registry`` (the :class:`ProcessRegistry`).
    """
    settings = settings or RuntimeSettings()
    govern_settings = govern_settings or GovernSettings()
    registry = registry if registry is not None else ProcessRegistry()
    ensure_transactional_sqlite(engine)
    ensure_scheduler_schema(engine)
    pdp = create_pdp(govern_settings, sink=sink, registry=plugin_registry)
    guardrails = create_guardrail_engine(
        govern_settings,
        sink=sink,
        ledger=ledger,
        counter=counter,
        extra_guardrails=extra_guardrails,
        registry=plugin_registry,
    )
    executor = GovernedToolExecutor(
        bindings, pdp=pdp, guardrails=guardrails, approval_gate=approval_gate, sink=sink
    )
    return CronScheduler(
        engine=engine,
        definitions=definitions,
        executor=executor,
        pdp=pdp,
        gateway=gateway,
        registry=registry,
        sink=sink,
        settings=settings,
        principals=principals,
        clock=clock,
    )


async def create_department_session(
    agent_name: str,
    department: str,
    *,
    owner_principal_id: str,
    store: SqlAlchemySessionStore,
    gateway: ModelGateway,
    executor: GovernedToolExecutor,
    settings: RuntimeSettings | None = None,
    router: VerifiedRouter[Any] | None = None,
    grounding: GroundingPolicy | None = None,
    instructions: str | None = None,
    sink: AuditSink | None = None,
    source_guard: ProtectedSourceResultGuard | None = None,
    history_loader: Callable[[int], Awaitable[list[ModelMessage]]] | None = None,
) -> DepartmentAgentSession:
    """Create a fresh principal-owned conversation for ``agent_name``.

    Exact-id rehydration is deliberately a separate factory below.

    ``router`` is the composition-root-built NE-VAF-U4 :class:`VerifiedRouter`;
    ``None`` (default) leaves every turn on the unchanged model loop.

    ``grounding`` is the composition-root-built NE-GA-G3 :class:`GroundingPolicy`
    (G5 supplies it); ``None`` (default) leaves every turn on the unchanged
    model loop, and ``executor`` is passed through to the session UNWRAPPED
    (identity-preserving). When set, ``executor`` is wrapped as
    ``ToolsetScopedExecutor(executor, granted=grounding.granted_tools, ...)``
    so the grounded agent's toolset is restricted to verified-data reads
    BEFORE the session ever sees it. Note the per-run
    :class:`~ne.runtime.grounding.GroundingRecorder` is NOT built here — it is
    constructed fresh inside ``DepartmentAgentSession.run`` for every call, so
    a session object reused across turns can never let a prior turn's fetches
    ground the current one (the load-bearing per-run-freshness invariant).

    ``instructions`` is the composed session instruction text — the Run-frozen
    skills index in the PRE-011B join. ``None`` (default) leaves every existing
    caller byte-identical: ``DepartmentAgentSession`` passes it straight to both
    ``Agent()`` constructions, and pydantic-ai treats ``None`` as absent.
    """
    settings = settings or RuntimeSettings()
    conversation = await store.create_conversation(agent_name, department, owner_principal_id)
    session_id = conversation.conversation_id
    return _compose_department_session(
        session_id,
        agent_name,
        department,
        owner_principal_id=owner_principal_id,
        store=store,
        gateway=gateway,
        executor=executor,
        settings=settings,
        router=router,
        grounding=grounding,
        instructions=instructions,
        sink=sink,
        source_guard=source_guard,
        history_loader=history_loader,
    )


async def rehydrate_department_session(
    conversation_id: str,
    agent_name: str,
    department: str,
    *,
    owner_principal_id: str,
    store: SqlAlchemySessionStore,
    gateway: ModelGateway,
    executor: GovernedToolExecutor,
    settings: RuntimeSettings | None = None,
    router: VerifiedRouter[Any] | None = None,
    grounding: GroundingPolicy | None = None,
    instructions: str | None = None,
    sink: AuditSink | None = None,
    source_guard: ProtectedSourceResultGuard | None = None,
    history_loader: Callable[[int], Awaitable[list[ModelMessage]]] | None = None,
) -> DepartmentAgentSession | None:
    """Rehydrate exactly one owned conversation; never derive from a tuple.

    ``instructions`` is the composed session instruction text (the skills index in
    the PRE-011B join); ``None`` — the default — leaves every existing caller
    byte-identical.
    """
    conversation = await store.get_conversation(conversation_id, owner_principal_id, department)
    if conversation is None or conversation.agent_name != agent_name:
        return None
    return _compose_department_session(
        conversation_id,
        agent_name,
        department,
        owner_principal_id=owner_principal_id,
        store=store,
        gateway=gateway,
        executor=executor,
        settings=settings or RuntimeSettings(),
        router=router,
        grounding=grounding,
        instructions=instructions,
        sink=sink,
        source_guard=source_guard,
        history_loader=history_loader,
    )


def _compose_department_session(
    session_id: str,
    agent_name: str,
    department: str,
    *,
    owner_principal_id: str,
    store: SqlAlchemySessionStore,
    gateway: ModelGateway,
    executor: GovernedToolExecutor,
    settings: RuntimeSettings,
    router: VerifiedRouter[Any] | None,
    grounding: GroundingPolicy | None,
    instructions: str | None,
    sink: AuditSink | None,
    source_guard: ProtectedSourceResultGuard | None,
    history_loader: Callable[[int], Awaitable[list[ModelMessage]]] | None,
) -> DepartmentAgentSession:
    session_executor: GovernedToolExecutor | ToolsetScopedExecutor = executor
    if grounding is not None:
        session_executor = ToolsetScopedExecutor(
            executor,
            granted=grounding.granted_tools,
            sink=grounding.sink,
            scope=f"grounded:{agent_name}",
            pdp=grounding.pdp,
        )
    return DepartmentAgentSession(
        session_id=session_id,
        agent_name=agent_name,
        department=department,
        owner_principal_id=owner_principal_id,
        store=store,
        executor=session_executor,
        gateway=gateway,
        settings=settings,
        router=router,
        grounding=grounding,
        instructions=instructions,
        sink=sink,
        source_guard=source_guard,
        history_loader=history_loader,
    )


def _memory_engine(
    factory: str,
    *,
    settings: RuntimeSettings | None,
    engine: sa.Engine | None,
) -> sa.Engine:
    """Resolve (and schema-ensure) the episodic-memory engine for a factory.

    ``engine`` defaults to one over ``settings.session_database_url`` (memory
    rides the same platform-Postgres URL as the session store); a ValueError is
    raised if both are empty.
    """
    settings = settings or RuntimeSettings()
    if engine is None:
        if not settings.session_database_url:
            raise ValueError(
                f"{factory} needs an engine or a non-empty "
                "RuntimeSettings.session_database_url (set "
                "NE_RUNTIME_SESSION_DATABASE_URL or pass engine=...)"
            )
        engine = create_engine_with_timeouts(
            settings.session_database_url, timeouts=settings.engine_timeouts
        )
    ensure_transactional_sqlite(engine)
    ensure_memory_schema(engine)
    return engine


def create_agent_memory(
    agent_id: str,
    *,
    settings: RuntimeSettings | None = None,
    engine: sa.Engine | None = None,
    session_id: str | None = None,
) -> MemoryProvider:
    """Build the per-agent memory facade over the episodic store.

    Returns the frozen ``MemoryProvider`` — ``AgentMemory`` (NE-037) by default, or
    the NE-083 ``TemporalAgentMemory`` when ``settings.memory_backend ==
    'temporal-graph'``. The temporal backend is loaded via the
    ``ne.plugins.memory_provider`` entry point so ne-runtime's src never imports
    ne-memory-graph (the entry point keeps the src boundary); flipping the flag back
    to ``'episodic'`` is an instant rollback.
    """
    resolved = settings or RuntimeSettings()
    engine = _memory_engine("create_agent_memory", settings=resolved, engine=engine)
    store = SqlAlchemyEpisodicStore(engine)
    if resolved.memory_backend == "temporal-graph":
        factory = _load_memory_provider_plugin("temporal-graph")
        return factory(store, agent_id, session_id=session_id)
    return AgentMemory(store, agent_id, session_id=session_id)


def _load_memory_provider_plugin(
    name: str,
) -> Callable[..., MemoryProvider]:
    """Resolve a ``ne.plugins.memory_provider`` entry point by name (fail-loud)."""
    from importlib.metadata import entry_points

    for entry in entry_points(group="ne.plugins.memory_provider"):
        if entry.name == name:
            return entry.load()
    raise ValueError(
        f'memory_backend="{name}" requires the ne-memory-graph package (entry point '
        f"ne.plugins.memory_provider:{name} not found)"
    )


def create_memory_promoter(
    *,
    writer: KnowledgeWrite,
    sink: AuditSink,
    settings: RuntimeSettings | None = None,
    engine: sa.Engine | None = None,
) -> MemoryPromoter:
    """Build the curation-gated :class:`MemoryPromoter`.

    ``writer`` (the frozen NE-023 ``KnowledgeWrite`` seam) and ``sink`` are
    REQUIRED keyword arguments — no promoter exists without its governed writer
    and audit sink.

    NORMATIVE construction requirement (closes NE-037 Codex P2): the composition
    root MUST construct the injected NE-023 writer with
    ``default_provenance_class=ProvenanceClass.LEARNED_BY_AGENT`` (the ne-brain
    ``KnowledgeWriter`` kwarg, ``packages/ne-brain/src/ne/brain/write/writer.py:93``).
    The ``KnowledgeItem.provenance`` SOURCE string ``"learned-by-agent"`` alone
    does NOT set the queryable provenance CLASS: a plain :class:`KnowledgeItem`
    takes the writer's default class via ``_class_for`` (writer.py:317-320), so a
    CURATED-default writer stamps promoted agent-learnings as class ``"curated"``
    and they are NOT retrievable via the store filter
    ``metadata.provenance.provenance_class == "learned-by-agent"``. See the
    executable proof at
    ``packages/ne-runtime/tests/test_memory_promotion_provenance_class.py``.

    The injected ``KnowledgeWriter`` MUST additionally be constructed with
    ``audit_sink=`` AND ``violation_sink=`` — both are now REQUIRED constructor
    arguments that raise ``SinkRequiredError`` when omitted (Checkpoint-D
    boundary-audit #5). A recipe that omitted ``audit_sink`` previously shipped
    UNAUDITED ``knowledge.upsert`` / ``curate`` / ``quarantine`` — a fail-open
    hole. The full normative recipe is therefore::

        writer = KnowledgeWriter(
            store, quarantine, contracts,
            default_shape=..., actor=...,
            default_provenance_class=ProvenanceClass.LEARNED_BY_AGENT,  # class-queryable
            audit_sink=audit_sink,          # REQUIRED — no governed write without audit
            violation_sink=violation_sink,  # REQUIRED — shape-violation transport
        )
        promoter = create_memory_promoter(writer=writer, sink=audit_sink, engine=engine)

    Sharing ONE ``audit_sink`` instance between the ``KnowledgeWriter`` and the
    promoter (as this recipe does) is SAFE (NE-CKD6): the writer makes each
    governed write ATOMIC (store+audit together or neither) under a single sink
    fault — on an internal ``knowledge.upsert`` audit-append failure it unwinds
    its own store writes (park-before-delete) and raises
    ``ne.brain.write.KnowledgeAuditError``, so a single shared-sink outage can
    never surface a durable+retrievable chunk without an audit record through the
    promoter. Executable proof:
    ``packages/ne-runtime/tests/test_memory_promotion_shared_sink_atomicity.py``.

    Its audited behaviour is executably proven end-to-end in
    ``packages/ne-runtime/tests/test_memory_promotion_audit_failclosed.py``.
    """
    engine = _memory_engine("create_memory_promoter", settings=settings, engine=engine)
    store = SqlAlchemyEpisodicStore(engine)
    return MemoryPromoter(store, writer, sink=sink)
