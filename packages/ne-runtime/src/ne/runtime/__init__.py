# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-runtime — the governed, durable agent runtime and THE C6 tool-call chokepoint.

This package wires the Pydantic-AI agent loop to the frozen platform contracts so
that EVERY tool call passes the NE-032 guardrails AND the NE-031 PDP AND (for
require-approval) a human gate — with no bypass path, by construction:

  * :class:`GatewayModel` is the only model path (routes through the frozen
    ``ModelGateway``; no provider SDK — C3);
  * :class:`GovernedToolExecutor` is the only object that holds a tool handler and
    the only structural route to executing one — its constructor REQUIRES the PDP,
    the guardrail engine, the approval gate, and the audit sink (C6);
  * :class:`DepartmentAgentSession` gives principal-owned persistent conversations
    over a SQLAlchemy store; legacy pair-keyed sessions are quarantined ops data;
  * ``@ne_workflow`` / ``@ne_step`` are thin DBOS durability wrappers
    (passthrough by default; real DBOS-Postgres live-parked).

HITL (NE-035): when the PDP returns ``require-approval`` the executor routes to the
production :class:`DurableApprovalGate` (``create_approval_gate``), which persists a
durable, restart-surviving approval request over ``SqlAlchemyApprovalStore`` and
blocks the tool until a NAMED human approves — Tier-2 one approver, Tier-3 a named
approver plus a distinct second, with never-auto-approve enforced at four layers.
Timeout/escalation/expiry follow SPEC §4.2; ``ApprovalMetrics`` emits approval
latency; :class:`PromotionTracker` records the 'assist → execute → own' evidence
trail (logged eligibility only — it changes no tier or policy).

Agent memory v1 (NE-037): an append-only, per-agent episodic store
(:class:`SqlAlchemyEpisodicStore`) implementing the frozen ``MemoryProvider``
Protocol via :class:`AgentMemory`, with a curation-gated promotion path
(:class:`MemoryPromoter`) into the brain through the frozen ``KnowledgeWrite``
seam (provenance ``learned-by-agent``).

Governed write-back actions (NE-050): the write path is FIRST-CLASS. A typed
:class:`ActionDefinition` (declared tier >= 2, source system, frozen
:class:`ActionInput`, a ``perform`` that touches an injected connector client) becomes
an ordinary :class:`ToolBinding` inside the ONE :class:`GovernedToolExecutor`, so a
source-system write can ONLY happen after guardrails → PDP → stricter-of tier gating →
the HITL gate → a config_hash-stamped audit event carrying what-action / what-target /
payload-SHAPE (C7-write). The :class:`GovernedAction` facade holds no ``perform`` and
can only delegate to the executor. Tier-3 (customer-visible) writes block on a named
approver + distinct second check; ``request_id`` gives at-least-once idempotency.
Because actions are ordinary governed tools they are servable through the NE-049
governed MCP server with zero extra wiring.

Handlers are NEVER exported: nothing in this surface exposes a raw tool callable.

Dispatch + scheduling (NE-034): :class:`GovernedSubAgentDispatcher` fans out typed,
server-capped, memory-isolated sub-agents; :class:`CronScheduler` runs each cron job
as its own isolated agent under per-job toolset gating; :class:`ProcessRegistry`
tracks live agents/jobs with lineage and atomic concurrency caps. C6 is preserved:
EVERY sub-agent and cron tool call still routes through the ONE
:class:`GovernedToolExecutor` — the :class:`ToolsetScopedExecutor` holds zero
handlers and can only delegate to it, and per-job/per-agent toolset gating is
ENFORCED (an out-of-set call is audited + PDP-consulted + terminally refused),
never advisory.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.runtime.action_tools import (
    ActionDedupStore,
    ActionDefinition,
    ActionInput,
    GovernedAction,
    build_action_bindings,
    create_governed_actions,
    ensure_action_dedup_schema,
)
from ne.runtime.actions import tier_for_action
from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.approval_metrics import ApprovalMetrics
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate, PendingApproval
from ne.runtime.config import ApprovalSettings, DispatchSettings, RuntimeSettings
from ne.runtime.correlation import (
    ConversationAttemptBinding,
    action_scope,
    conversation_attempt_scope,
    current_action_scope,
    current_conversation_attempt,
    current_run_principal_id,
    current_run_requested_model,
    current_run_requested_model_lane,
    current_run_trace_id,
    run_principal_scope,
    run_requested_model_scope,
    run_trace_scope,
)
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.durable import launch_durable_runtime, ne_step, ne_workflow
from ne.runtime.errors import (
    ActionIdempotencyError,
    ActionValidationError,
    AgentMemoryError,
    ApprovalAlreadyDecidedError,
    ApprovalIntegrityError,
    ApprovalNotGrantedError,
    ApprovalPolicyError,
    ApprovalRequestNotFoundError,
    ApprovalSourceWithheldFromApproverError,
    DispatchCapExceededError,
    DurabilityUnavailableError,
    MemoryEventNotFoundError,
    MemoryPromotionAuditError,
    MemoryPromotionError,
    NeRuntimeError,
    RuntimeAuditError,
    SchedulingError,
    SessionDepartmentMismatchError,
    SubAgentDispatchError,
    SubAgentRoutePolicyDeniedError,
    ToolDeniedError,
    ToolExecutionError,
    ToolNotRegisteredError,
    ToolRegistrationError,
    ToolsetConfigurationError,
    ToolsetViolationError,
    UnknownAgentTypeError,
)
from ne.runtime.factory import (
    create_agent_memory,
    create_approval_gate,
    create_cron_scheduler,
    create_department_session,
    create_memory_promoter,
    create_subagent_dispatcher,
    create_tool_executor,
    freeze_platform_config,
    rehydrate_department_session,
)
from ne.runtime.memory_promotion import LEARNED_BY_AGENT_PROVENANCE, MemoryPromoter
from ne.runtime.memory_store import (
    AgentMemory,
    MemoryEventKind,
    SqlAlchemyEpisodicStore,
    StoredMemoryEvent,
    ensure_memory_schema,
)
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.model import GatewayModel, UnknownToolHook
from ne.runtime.principals import AgentPrincipalProvider, StaticAgentPrincipalProvider
from ne.runtime.processes import ProcessRecord, ProcessRegistry
from ne.runtime.promotion import PromotionEvidence, PromotionTracker
from ne.runtime.provisional import (
    ProvisionalContext,
    ProvisionalContextAccumulator,
    current_provisional_context,
    current_provisional_recorder,
    provisional_context_scope,
)
from ne.runtime.reference_actions import (
    CRM_NOTE_ARG_POLICY,
    EMAIL_DRAFT_ARG_POLICY,
    TICKET_UPDATE_ARG_POLICY,
    CrmNoteClient,
    CrmNoteInput,
    DraftMailClient,
    EmailDraftInput,
    TicketUpdateInput,
    TicketWriteClient,
    crm_note_definition,
    email_draft_definition,
    ticket_update_definition,
)
from ne.runtime.runner import run_isolated_agent
from ne.runtime.scheduler import CronScheduler, JobRunOutcome, ensure_scheduler_schema
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import RuntimeToolRegistry, ToolBinding, ToolHandler
from ne.runtime.toolset import ToolsetScopedExecutor

__all__ = [
    "CRM_NOTE_ARG_POLICY",
    "EMAIL_DRAFT_ARG_POLICY",
    "LEARNED_BY_AGENT_PROVENANCE",
    "TICKET_UPDATE_ARG_POLICY",
    "ActionDedupStore",
    "ActionDefinition",
    "ActionIdempotencyError",
    "ActionInput",
    "ActionValidationError",
    "AgentDefinition",
    "AgentMemory",
    "AgentMemoryError",
    "AgentPrincipalProvider",
    "ApprovalAlreadyDecidedError",
    "ApprovalIntegrityError",
    "ApprovalMetrics",
    "ApprovalNotGrantedError",
    "ApprovalPolicyError",
    "ApprovalRequestNotFoundError",
    "ApprovalSettings",
    "ApprovalSourceWithheldFromApproverError",
    "ConversationAttemptBinding",
    "CrmNoteClient",
    "CrmNoteInput",
    "CronScheduler",
    "DepartmentAgentSession",
    "DispatchCapExceededError",
    "DispatchSettings",
    "DraftMailClient",
    "DurabilityUnavailableError",
    "DurableApprovalGate",
    "EmailDraftInput",
    "GatewayModel",
    "GovernedAction",
    "GovernedSubAgentDispatcher",
    "GovernedToolExecutor",
    "JobRunOutcome",
    "MemoryEventKind",
    "MemoryEventNotFoundError",
    "MemoryPromoter",
    "MemoryPromotionAuditError",
    "MemoryPromotionError",
    "NeRuntimeError",
    "PendingApproval",
    "ProcessRecord",
    "ProcessRegistry",
    "PromotionEvidence",
    "PromotionTracker",
    "ProvisionalContext",
    "ProvisionalContextAccumulator",
    "RuntimeAuditError",
    "RuntimeSettings",
    "RuntimeToolRegistry",
    "SchedulingError",
    "SessionDepartmentMismatchError",
    "SqlAlchemyApprovalStore",
    "SqlAlchemyEpisodicStore",
    "SqlAlchemySessionStore",
    "StaticAgentPrincipalProvider",
    "StoredMemoryEvent",
    "SubAgentDispatchError",
    "SubAgentRoutePolicyDeniedError",
    "TicketUpdateInput",
    "TicketWriteClient",
    "ToolBinding",
    "ToolDeniedError",
    "ToolExecutionError",
    "ToolHandler",
    "ToolNotRegisteredError",
    "ToolRegistrationError",
    "ToolsetConfigurationError",
    "ToolsetScopedExecutor",
    "ToolsetViolationError",
    "UnknownAgentTypeError",
    "UnknownToolHook",
    "__version__",
    "action_scope",
    "build_action_bindings",
    "conversation_attempt_scope",
    "create_agent_memory",
    "create_approval_gate",
    "create_cron_scheduler",
    "create_department_session",
    "create_governed_actions",
    "create_memory_promoter",
    "create_subagent_dispatcher",
    "create_tool_executor",
    "crm_note_definition",
    "current_action_scope",
    "current_conversation_attempt",
    "current_provisional_context",
    "current_provisional_recorder",
    "current_run_principal_id",
    "current_run_requested_model",
    "current_run_requested_model_lane",
    "current_run_trace_id",
    "email_draft_definition",
    "ensure_action_dedup_schema",
    "ensure_approval_schema",
    "ensure_memory_schema",
    "ensure_runtime_schema",
    "ensure_scheduler_schema",
    "freeze_platform_config",
    "launch_durable_runtime",
    "ne_step",
    "ne_workflow",
    "provisional_context_scope",
    "rehydrate_department_session",
    "run_isolated_agent",
    "run_principal_scope",
    "run_requested_model_scope",
    "run_trace_scope",
    "ticket_update_definition",
    "tier_for_action",
]
