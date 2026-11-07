# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-contracts — the frozen seam layer for Neutron Enterprise.

Version 1.0.0 is the Checkpoint-A freeze baseline. Post-Checkpoint-A changes are
additive-only and require an ADR in ``docs/adr/`` plus a contract version bump
(SPEC section 6, plan section 0.5).

This module re-exports the entire public contract surface: all 32 SPEC section 4
Protocols, the six event surfaces, cross-domain value types/enums, the config
framework + ``config_hash`` API, the plugin registry, module lifecycle, and the
error hierarchy.

Version 1.1.0 adds the composed platform ``config_hash``
(``compute_platform_config_hash`` / ``freeze_platform_config_hash``) — ADR-0003,
additive.

Version 1.2.0 adds ``PolicyDecision.tier`` — the bundle-decided approval tier
carried to the HITL gate (ADR-0004, additive).

Version 1.3.0 adds graph-aware citation types (ADR-0005, additive):
``GraphEntityRef`` / ``GraphHop`` / ``GraphPath`` and a defaulted
``RetrievalResult.graph_paths``.

Version 1.6.0 adds ``VerifiedProvenance`` and a defaulted
``AgentRunResult.verified`` — structural verified-answer provenance
(ADR-0009, additive).

Version 1.7.0 adds ``GroundedProvenance`` and a defaulted
``AgentRunResult.grounded`` — structural grounded-answer provenance
(ADR-0010, additive).

Version 1.8.0 adds the Skill Forge contract surface (ADR-0011, additive):
``ne.contracts.skills`` — ``SkillPack``/``WorkflowSnapshot``/``WorkflowStep``/
``SkillInvocation``/``AuthoringStandard``, the two canonical hashes
(``content_hash`` labels-in, ``routing_hash`` labels-out),
``SnapshotPage``/``InvocationPage``, and the ``SkillSubstrate`` Protocol with
the ``ne.plugins.skill_substrate`` entry-point slot.

Version 1.9.0 adds the context-budget primitives (ADR-0012, additive):
``ne.contracts.tokens`` (``TokenEstimator``, ``ByteTokenEstimator``,
``ContextBudget``) and the operationally-raised governed
``ContextBudgetExceededError``.

Version 1.10.0 adds a defaulted ``GroundedProvenance.strictness`` tier stamp
(ADR-0013, additive).

Version 1.11.0 adds a defaulted ``assurance`` provenance label on
``GraphEntityRef``/``GraphHop`` (ADR-0014, additive): ``"source"`` (backbone
default) | ``"extracted"`` | ``"confirmed"`` — the NE-T2X read-payload honesty
channel.

Version 1.12.0 adds a defaulted ``source_ref`` evidence ref on
``GraphEntityRef`` (ADR-0015, additive) — the
NE-T2X-5-ENDPOINT-EVIDENCE-REF endpoint-evidence channel.

Version 1.13.0 promotes the existing frozen-map serializer to a public
``thaw`` export (ADR-0016, additive) — the sanctioned egress-boundary
converter for handing a frozen contract value to a third-party library that
type-checks for literal builtins (e.g. litellm's
``type_to_response_format_param``, ``isinstance(x, dict)``).

Version 1.14.0 adds a defaulted ``decision_event_id`` field on
``ApprovalOutcome`` (ADR-0017, additive) — the ``event_id`` of the decision
``AuditEvent`` a gate call actually appended to the tamper-evident chain, so
an approve/reject response can carry the real chain coordinates of its own
decision record back to the caller.

Version 1.15.0 adds a defaulted ``principal_id`` field on
``CompletionRequest`` (ADR-0018, additive) — the opaque identifier of the
calling principal, carried AS DATA so the gateway can stamp
``CostEvent.actor`` with the principal that caused the call rather than the
gateway's own service identity.

Version 1.16.0 adds a defaulted ``approval_id`` field on ``ApprovalOutcome``
(ADR-0019, additive) — the durable request id of the approval the outcome
decided, threaded by every gate path that knows its request so the C6
executor can stamp the execution→approval join onto ``tool.execute`` at the
execution site (B14) instead of anyone reconstructing it afterwards.

Version 1.17.0 adds a defaulted ``requested_model`` field on
``CompletionRequest`` (ADR-0020, additive) — the exact boot-registry alias a
governed caller selected for one completion, carried as typed data so the
gateway can distinguish the requested primary from the model that actually
served without putting a control-plane value in provider-bound ``params``.

Version 1.18.0 adds the closed inference-target lifecycle surface (ADR-0021,
additive): strict target-scoped status/start/stop request types, safe
manifest/observation values, and ``InferenceTargetOperator``. Executable paths,
digests, argv, environment, endpoints, credentials, and raw output are
structurally absent from the contract.

Version 1.19.0 adds source-authority identities, immutable artifact and authority
receipts, target-specific protected-source references/resolution envelopes,
bounded authorized retrieval/continuation types, least-privilege Protocols, and
their conformance kits (ADR-0022, additive). Existing Knowledge Protocol method
sets remain unchanged.

Version 1.20.0 adds a defaulted verifier-only ``TokenClaims.verified_expires_at``
and the strict minimal ``AuthenticatedRequestContext`` authorization snapshot
(ADR-0023, additive). ``TokenVerifier.verify`` remains source-compatible.

Version 1.21.0 adds the sixth closed resolver target, evidence projection
(ADR-0024, additive): the content-free ``SourceBoundEvidenceProjectionResolution``
target envelope and ``SourceBoundEvidenceProjectionResolver``, a Protocol extending
``SourceBoundResultResolver`` with ``for_evidence_projection`` over the closed
``ProtectedSourceOutputRef | ProtectedClaimSliceRef`` union. The five-target
Protocol is unchanged and every existing implementation of it stays valid.

Version 1.22.0 adds the independent ``EvidenceAssurance`` vocabulary and an
ordered ``SourceBoundToolResultSetRef`` while preserving the existing single-ref
shape (ADR-0025, additive; 1.21.0/ADR-0024 is reserved by PRE-015A1).

Version 1.25.0 adds ``Completion.finish_reason`` and the derived
``Completion.truncated`` so a completion cut off at the output budget can no
longer be served as if it were a whole answer (ADR-0028, additive).

Version 1.26.0 adds the OPTIONAL ``TurnContextBudget`` capability Protocol
(ADR-0029, additive): a gateway that can also name the ``ContextBudget`` of the
model IN FORCE for one turn, so the Layer-2 trim guard sizes itself to the model
about to serve the request instead of to whatever the composition root routed to
at boot. ``ModelGateway`` is untouched and no implementation is obliged to grow
the method; a gateway that lacks it fails the ``isinstance`` test and the caller
keeps its composed default. The method takes the caller's ``lane`` pin (with a
``None`` default, same release cycle) so the resolver names the FIRST candidate
the route actually yields under that pin, not a primary the pin filters out.
Round 4 of the same cycle (still 1.26.0, ADR-0029 amended in place) makes the
method ``async`` and has it answer a ``TurnModelBudget`` — the budget PLUS the
name of the model it belongs to — so an implementation resolves through the
same (possibly async, evidence-reading) function its own attempt path walks,
and a reporting surface can name the enforced model without re-deriving
candidate order.

Version 1.27.0 adds the OPTIONAL ``PolicyDecision.approval_window_seconds``
field (ADR-0030, additive): the policy-resolved per-action approval window,
carried FIRST-CLASS on the decision — not as an obligation, because
``obligations`` is the Rego/Cedar parity surface and Cedar obligations are
static per-rule annotations that cannot express a data-resolved per-action
value. Default ``None`` keeps every pre-1.27.0 producer valid and means "the
policy named no window; the deployment default applies" — never "expire
immediately".

Version 1.28.0 adds the OPTIONAL ``PolicyDecision.approval_window_declared``
bit beside it (additive, bundle 1.4.0): TRUE only when the window came from a
DEPLOYMENT DECLARATION, FALSE for the product's built-in floor. The runtime's
park-time coherence check refuses on a declaration and warns-and-proceeds on a
floor — refusing on a floor would punish a decision nobody took. Default
``False`` keeps every pre-1.28.0 producer valid and degrades to the
warn-and-proceed side.

Version 1.29.0 adds the OPTIONAL ``PolicyDecision.standing_approval_seconds``
field (ADR-0030 amended, additive): how long an approver's DECISION stays
usable once the attempt it was asked for has terminalized
``authorization_expired`` — the standing-approval lifetime, resolved by the
policy bundle beside ``approval_window_seconds`` and carried the same way.
Two clocks, two questions: the window bounds how long a human has to ANSWER,
the standing lifetime bounds how long that ANSWER outlives the attempt.
Default ``None`` keeps every pre-1.29.0 producer valid and means "the policy
named no standing lifetime", which the runtime reads as "mint no standing
approval" — the fail-safe direction, never an unbounded one.
"""

from __future__ import annotations

__version__ = "1.29.0"

from ne.contracts.config import (
    NeSettings,
    canonical_platform_config_tree,
    compute_config_hash,
    compute_platform_config_hash,
    compute_platform_config_tree_hash,
    freeze_config_hash,
    freeze_platform_config_hash,
    get_config_hash,
    reset_config_hash_for_testing,
)
from ne.contracts.errors import (
    ConfigFileNotFoundError,
    ConfigHashMismatchError,
    ConfigHashNotFrozenError,
    ContextBudgetExceededError,
    NeContractsError,
    ObjectNotFoundError,
    PluginNotFoundError,
    SecretNotFoundError,
    WormViolationError,
)
from ne.contracts.evalobs import EvalRunResult, EvalSuite, KpiRecorder, SignalEmitter
from ne.contracts.events import (
    ApprovalEventLike,
    AuditEventLike,
    CorrectionSignalLike,
    CostEventLike,
    EventLike,
    ShapeViolationLike,
)
from ne.contracts.frozen import AnyMap, FloatMap, FrozenDict, StrMap, thaw
from ne.contracts.governance import (
    ApprovalGate,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
    AuditSink,
    AuditVerification,
    DecisionEffect,
    Guardrail,
    GuardrailVerdict,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
    RedactionResult,
    Redactor,
)
from ne.contracts.identity import (
    AuthenticatedRequestContext,
    PrincipalResolver,
    TokenClaims,
    TokenVerifier,
)
from ne.contracts.inference import (
    InferenceManifestTarget,
    InferenceOperatorCode,
    InferenceOperatorManifest,
    InferenceOperatorObservation,
    InferenceStartRequest,
    InferenceStatusRequest,
    InferenceStopRequest,
    InferenceTargetOperation,
    InferenceTargetOperator,
    InferenceTargetState,
    ManifestHash,
    SafeInferenceId,
)
from ne.contracts.integration import (
    AclEntry,
    ActionResult,
    ActionTool,
    Connector,
    DataContract,
    Record,
    SourceObject,
)
from ne.contracts.knowledge import (
    ChunkStore,
    Embedder,
    GraphStore,
    KnowledgeItem,
    KnowledgeRead,
    KnowledgeWrite,
    Reranker,
    ShapeReport,
    ShapeValidator,
    SparseEncoder,
    TripleStore,
    UpsertReceipt,
)
from ne.contracts.lifecycle import HealthCheck, HealthState, ModuleLifecycle
from ne.contracts.models import (
    Completion,
    CompletionChunk,
    CompletionRequest,
    ModelEntry,
    ModelGateway,
    ModelRegistry,
    TurnContextBudget,
    TurnModelBudget,
    Usage,
)
from ne.contracts.plugins import (
    PLUGIN_GROUP_PREFIX,
    PluginRegistry,
    iter_plugins,
    load_plugin,
)
from ne.contracts.runtime import (
    AgentRunResult,
    AgentSession,
    ChatAdapter,
    GroundedProvenance,
    InboundMessage,
    MemoryEvent,
    MemoryProvider,
    OutboundMessage,
    ScheduledJob,
    Scheduler,
    SubAgentDispatcher,
    SubAgentResult,
    SubAgentSpec,
    ToolRegistry,
    ToolSpec,
    VerifiedProvenance,
)
from ne.contracts.secrets import SecretStore, SecretVersion
from ne.contracts.skills import (
    AuthoringStandard,
    InvocationPage,
    SkillInvocation,
    SkillPack,
    SkillSource,
    SkillSubstrate,
    SnapshotPage,
    WorkflowSnapshot,
    WorkflowStep,
    skill_content_hash,
    skill_routing_hash,
)
from ne.contracts.source_authority import (
    AuthorizedArtifact,
    AuthorizedArtifactPage,
    AuthorizedArtifactPageStatus,
    AuthorizedContinuationReplay,
    AuthorizedContinuationState,
    AuthorizedContinuationTokenCodec,
    AuthorizedContinuationTokenLocator,
    AuthorizedRetrievalEnvelope,
    CurrentAuthorizedArtifactSelector,
    CurrentSourceAuthorityReader,
    CurrentSourceAuthorityReceipt,
    CurrentSourceAuthorityWriter,
    CurrentSourceContractHead,
    EvidenceAssurance,
    GovernedLocalT3SourcePoint,
    ImmutableArtifactCreateReceipt,
    ImmutableKnowledgeArtifactWrite,
    M365SourcePoint,
    ProtectedClaimSliceRef,
    ProtectedSourceExecutionWriter,
    ProtectedSourceInvocationRef,
    ProtectedSourceOutputRef,
    ProtectedSourceOutputWriter,
    ProtectedWorkflowStateRef,
    SalesforceSourcePoint,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityDecision,
    SourceAuthorityResolution,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundApprovalResolution,
    SourceBoundDirectResolution,
    SourceBoundDispatchResolution,
    SourceBoundEvidenceProjectionResolution,
    SourceBoundEvidenceProjectionResolver,
    SourceBoundModelSendResolution,
    SourceBoundResultResolver,
    SourceBoundToolResultRef,
    SourceBoundToolResultSetRef,
    SourceBoundWorkflowProjectionResolution,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceDeletionObservation,
    SourceIdentity,
    SourceReconciliationClaim,
    SourceReconciliationScope,
    VerifiedEmptyResultReceipt,
    VerifiedRowContributorReceipt,
    canonical_source_authority_stamps,
)
from ne.contracts.storage import ArchiveReceipt, ObjectStat, ObjectStore
from ne.contracts.tokens import (
    DEFAULT_BYTES_PER_TOKEN,
    DEFAULT_PER_CALL_OVERHEAD,
    ByteTokenEstimator,
    ContextBudget,
    TokenEstimator,
)
from ne.contracts.types import (
    ApprovalTier,
    ChatMessage,
    Chunk,
    Citation,
    GraphEntityRef,
    GraphHop,
    GraphPath,
    Lane,
    Principal,
    RetrievalResult,
    ScoredChunk,
    SparseVector,
)

# Sorted per ruff RUF022. Groups are documented in the module docstring and the
# package README (the 8-domain protocol inventory maps 1:1 to SPEC section 4).
__all__ = [
    "DEFAULT_BYTES_PER_TOKEN",
    "DEFAULT_PER_CALL_OVERHEAD",
    "PLUGIN_GROUP_PREFIX",
    "AclEntry",
    "ActionResult",
    "ActionTool",
    "AgentRunResult",
    "AgentSession",
    "AnyMap",
    "ApprovalEventLike",
    "ApprovalGate",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalStatus",
    "ApprovalTier",
    "ArchiveReceipt",
    "AuditEventLike",
    "AuditSink",
    "AuditVerification",
    "AuthenticatedRequestContext",
    "AuthoringStandard",
    "AuthorizedArtifact",
    "AuthorizedArtifactPage",
    "AuthorizedArtifactPageStatus",
    "AuthorizedContinuationReplay",
    "AuthorizedContinuationState",
    "AuthorizedContinuationTokenCodec",
    "AuthorizedContinuationTokenLocator",
    "AuthorizedRetrievalEnvelope",
    "ByteTokenEstimator",
    "ChatAdapter",
    "ChatMessage",
    "Chunk",
    "ChunkStore",
    "Citation",
    "Completion",
    "CompletionChunk",
    "CompletionRequest",
    "ConfigFileNotFoundError",
    "ConfigHashMismatchError",
    "ConfigHashNotFrozenError",
    "Connector",
    "ContextBudget",
    "ContextBudgetExceededError",
    "CorrectionSignalLike",
    "CostEventLike",
    "CurrentAuthorizedArtifactSelector",
    "CurrentSourceAuthorityReader",
    "CurrentSourceAuthorityReceipt",
    "CurrentSourceAuthorityWriter",
    "CurrentSourceContractHead",
    "DataContract",
    "DecisionEffect",
    "Embedder",
    "EvalRunResult",
    "EvalSuite",
    "EventLike",
    "EvidenceAssurance",
    "FloatMap",
    "FrozenDict",
    "GovernedLocalT3SourcePoint",
    "GraphEntityRef",
    "GraphHop",
    "GraphPath",
    "GraphStore",
    "GroundedProvenance",
    "Guardrail",
    "GuardrailVerdict",
    "HealthCheck",
    "HealthState",
    "ImmutableArtifactCreateReceipt",
    "ImmutableKnowledgeArtifactWrite",
    "InboundMessage",
    "InferenceManifestTarget",
    "InferenceOperatorCode",
    "InferenceOperatorManifest",
    "InferenceOperatorObservation",
    "InferenceStartRequest",
    "InferenceStatusRequest",
    "InferenceStopRequest",
    "InferenceTargetOperation",
    "InferenceTargetOperator",
    "InferenceTargetState",
    "InvocationPage",
    "KnowledgeItem",
    "KnowledgeRead",
    "KnowledgeWrite",
    "KpiRecorder",
    "Lane",
    "M365SourcePoint",
    "ManifestHash",
    "MemoryEvent",
    "MemoryProvider",
    "ModelEntry",
    "ModelGateway",
    "ModelRegistry",
    "ModuleLifecycle",
    "NeContractsError",
    "NeSettings",
    "ObjectNotFoundError",
    "ObjectStat",
    "ObjectStore",
    "OutboundMessage",
    "PluginNotFoundError",
    "PluginRegistry",
    "PolicyDecision",
    "PolicyDecisionPoint",
    "PolicyRequest",
    "Principal",
    "PrincipalResolver",
    "ProtectedClaimSliceRef",
    "ProtectedSourceExecutionWriter",
    "ProtectedSourceInvocationRef",
    "ProtectedSourceOutputRef",
    "ProtectedSourceOutputWriter",
    "ProtectedWorkflowStateRef",
    "Record",
    "RedactionResult",
    "Redactor",
    "Reranker",
    "RetrievalResult",
    "SafeInferenceId",
    "SalesforceSourcePoint",
    "ScheduledJob",
    "Scheduler",
    "ScoredChunk",
    "SecretNotFoundError",
    "SecretStore",
    "SecretVersion",
    "ShapeReport",
    "ShapeValidator",
    "ShapeViolationLike",
    "SignalEmitter",
    "SkillInvocation",
    "SkillPack",
    "SkillSource",
    "SkillSubstrate",
    "SnapshotPage",
    "SourceAdmissionFence",
    "SourceAuthorityCandidate",
    "SourceAuthorityDecision",
    "SourceAuthorityResolution",
    "SourceAuthorityStamp",
    "SourceAuthorityState",
    "SourceBoundApprovalResolution",
    "SourceBoundDirectResolution",
    "SourceBoundDispatchResolution",
    "SourceBoundEvidenceProjectionResolution",
    "SourceBoundEvidenceProjectionResolver",
    "SourceBoundModelSendResolution",
    "SourceBoundResultResolver",
    "SourceBoundToolResultRef",
    "SourceBoundToolResultSetRef",
    "SourceBoundWorkflowProjectionResolution",
    "SourceContentArtifactIdentity",
    "SourceContractIdentity",
    "SourceDeletionObservation",
    "SourceIdentity",
    "SourceObject",
    "SourceReconciliationClaim",
    "SourceReconciliationScope",
    "SparseEncoder",
    "SparseVector",
    "StrMap",
    "SubAgentDispatcher",
    "SubAgentResult",
    "SubAgentSpec",
    "TokenClaims",
    "TokenEstimator",
    "TokenVerifier",
    "ToolRegistry",
    "ToolSpec",
    "TripleStore",
    "TurnContextBudget",
    "TurnModelBudget",
    "UpsertReceipt",
    "Usage",
    "VerifiedEmptyResultReceipt",
    "VerifiedProvenance",
    "VerifiedRowContributorReceipt",
    "WorkflowSnapshot",
    "WorkflowStep",
    "WormViolationError",
    "__version__",
    "canonical_platform_config_tree",
    "canonical_source_authority_stamps",
    "compute_config_hash",
    "compute_platform_config_hash",
    "compute_platform_config_tree_hash",
    "freeze_config_hash",
    "freeze_platform_config_hash",
    "get_config_hash",
    "iter_plugins",
    "load_plugin",
    "reset_config_hash_for_testing",
    "skill_content_hash",
    "skill_routing_hash",
    "thaw",
]
