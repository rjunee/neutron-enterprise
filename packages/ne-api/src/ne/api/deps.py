# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FastAPI dependencies + the app-state accessor.

``current_principal`` and ``current_authenticated_context`` are RE-EXPORTED from
``ne.identity.middleware``. The first serves synchronous request authorization;
the latter is the only sanctioned delayed-work snapshot and requires verifier-
authored expiry. Routes import them here so ne-api has one greppable source.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from fastapi import HTTPException, Request, Response

if TYPE_CHECKING:
    from ne.api.agent_catalog import AgentCatalog
    from ne.api.agent_preflight import AgentCapabilityPreflightService
    from ne.api.inference_lifecycle import InferenceLifecycleService
    from ne.api.model_routing import ModelRouteStore
    from ne.api.model_selection import ModelSelectionService
    from ne.api.network_egress import EgressPostureStore
    from ne.api.verified_curation import VerifiedCurationContext
    from ne.brain.current_source_filter import CurrentSourceDisclosureEvaluator
    from ne.brain.lineage import SqlAlchemyDocumentLineageStore
    from ne.brain_graph.read import EntityGraph
    from ne.connect.authority_attempts import AuthorityAttemptReader
    from ne.connect.runner import ConnectSyncSettings
    from ne.gateway.egress import EgressSettings
    from ne.runtime.dispatch import GovernedSubAgentDispatcher
    from ne.runtime.grounding import GroundingPolicy
    from ne.runtime.scheduler import ManualJobRequestStore
    from ne.runtime.verified_router import VerifiedRouter

from ne.api.serving import ServingStatusService
from ne.api.settings import ApiSettings
from ne.api.turn_relay import TurnEnqueuer, TurnFrameReader
from ne.api.verified_tools import OntologyReadState
from ne.audit import AnchoredChainVerifier
from ne.audit.config import AuditSettings
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.models import ModelGateway
from ne.contracts.secrets import SecretStore
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReader,
    ProtectedSourceExecutionWriter,
    ProtectedSourceOutputWriter,
)
from ne.eval.store import EvalRunStore
from ne.flywheel import (
    ProposalReviewService,
    SqlAlchemyCorrectionSignalStore,
    SqlAlchemyProposalStore,
)
from ne.flywheel.capture import CorrectionSignalCapture
from ne.gateway.config import GatewaySettings
from ne.govern.config import GovernSettings
from ne.identity.config import IdentitySettings
from ne.identity.middleware import (
    current_authenticated_context as current_authenticated_context,
)
from ne.identity.middleware import (
    current_principal as current_principal,
)
from ne.obs.config import ObsReportSettings
from ne.permissions.expand import PrincipalExpander
from ne.permissions.store import PrincipalStore
from ne.permissions.sync import PrincipalSyncService
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.config import RuntimeSettings
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.run_store import AgentRunStore
from ne.runtime.source_results import ProtectedSourceResultGuard, authenticated_request_scope
from ne.runtime.store import SqlAlchemySessionStore

_STATE_KEY = "ne_api"


async def bind_authenticated_request_context(
    request: Request,
    response: Response,
) -> AsyncIterator[None]:
    """Bind the verifier-authored snapshot for protected execution/materialization."""
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    try:
        context = current_authenticated_context(request)
    except HTTPException:
        # Legacy verifier doubles can still exercise source-free routes. Any
        # protected materialization remains fail-closed because no ambient
        # context is bound.
        yield
        return
    with authenticated_request_scope(context):
        yield


@dataclass(frozen=True)
class KpiSources:
    """The read-only seams ``ne.obs.collect_window`` consumes for /v1/kpi/summary.

    ALL read-only: ``collect_window`` calls only ``replay`` / ``list_runs`` /
    ``read_signals`` on these (never a write surface). ``None`` on
    :class:`ApiState` means the KPI route returns the documented-empty scaffold.
    """

    audit_engine: sa.Engine
    eval_store: EvalRunStore
    signal_store: SqlAlchemyCorrectionSignalStore
    obs_settings: ObsReportSettings


@dataclass
class ApiState:
    """The constructed governed singletons, attached to the inner FastAPI app."""

    settings: ApiSettings
    config_hash: str
    sink: AuditSink
    gate: DurableApprovalGate
    executor: GovernedToolExecutor
    retriever: KnowledgeRead
    current_retriever: KnowledgeRead
    session_store: SqlAlchemySessionStore
    gateway: ModelGateway
    agent_catalog: AgentCatalog
    runtime_settings: RuntimeSettings
    govern_settings: GovernSettings
    identity_settings: IdentitySettings
    audit_settings: AuditSettings
    secret_store: SecretStore
    perm_store: PrincipalStore
    expander: PrincipalExpander
    perm_sync: PrincipalSyncService
    engines: list[sa.Engine]
    audit_engine: sa.Engine
    document_lineage: SqlAlchemyDocumentLineageStore
    eval_store: EvalRunStore | None
    kpi_sources: KpiSources | None
    #: The REAL NE-066 review gate + its proposal store (steward surfaces, §1.7).
    #: ``None`` ⇒ the steward routes answer 503 (the evals ``eval_store`` precedent).
    review_service: ProposalReviewService | None
    proposal_store: SqlAlchemyProposalStore | None
    #: NE-103c enqueue-mode relay (both ``None`` in inline mode — the chat router
    #: runs the in-process turn as today; both set in enqueue mode).
    turn_enqueuer: TurnEnqueuer | None
    turn_reader: TurnFrameReader | None
    #: NE-133: None ⇔ graph tier disabled ⇒ /v1/graph/* answer the honest 404.
    entity_graph: EntityGraph | None
    #: NE-131: the composed gateway settings (already frozen into config_hash),
    #: surfaced so /v1/posture reports the model-registry lanes. None ⇒ honest empty.
    gateway_settings: GatewaySettings | None
    #: NE-ONTOLOGY-FLYWHEEL — the governed HITL verified-answer-template curation
    #: seam (boot dir + config_hash projector). ``None`` unless verified answers are
    #: enabled AND the flywheel store is configured ⇒ the verified-template steward
    #: routes answer 409 ("verified answers are not enabled on this deployment").
    verified_curation: VerifiedCurationContext | None = None
    #: NE-135 — the precomputed T3 ontology-read surface (compiled shapes + quarantine
    #: lane + verified setup over the ONE C12-gated gateway). ``None`` ⇒ the ontology
    #: routes answer 503 ``ontology-disabled`` (the eval_store 503 precedent). Defaulted
    #: so no existing ``ApiState(...)`` construction breaks.
    ontology: OntologyReadState | None = None
    #: NE-132 — the resolved connector-sync config (env-driven, ``NE_CONNECT_SYNC_*``),
    #: surfaced so /v1/connect/status derives per-connector freshness/quarantine from the
    #: real state. ``None`` / ``sources=()`` ⇒ /v1/connect/status reports no pull
    #: connectors. Trailing-defaulted so no existing ``ApiState(...)`` construction breaks.
    connect_sync_settings: ConnectSyncSettings | None = None
    #: Short-lived manual sync requests on the existing runtime scheduler database.
    connect_sync_requests: ManualJobRequestStore | None = None
    #: NE-137 — the flywheel intake for the chat thumbs / wrong-source signals, over the
    #: SAME flywheel engine as the steward review service (dual store+outbox write).
    #: ``None`` ⇒ the signal routes answer 503 (no flywheel database configured — the
    #: eval_store 503 precedent).
    signal_capture: CorrectionSignalCapture | None = None
    #: NE-UI-P2-CLEANUP — the SAME boot-frozen EgressSettings instance the live gateway
    #: guard enforces (threaded from build_platform like the frozen config_hash);
    #: /v1/posture reports THIS, never a per-request re-read. Trailing-defaulted so no
    #: existing ``ApiState(...)`` construction breaks.
    egress_settings: EgressSettings | None = None
    #: NE-AIRGAP-TOGGLE — the per-call effective-posture provider + the durable
    #: posture register. /v1/posture reports the PROVIDER (the same read the live
    #: guard enforces — reported==enforced supersedes the boot-frozen read above
    #: when set); POST /v1/network/air-gap consults the provider for the current
    #: posture and drives changes through the governed executor. Trailing-defaulted
    #: so existing ApiState constructions break nowhere.
    egress_settings_provider: Callable[[], EgressSettings] | None = None
    egress_store: EgressPostureStore | None = None
    #: NE-MODEL-ROUTING — the durable runtime model-route register. GET
    #: /v1/models/routing reports the SAME register the live route provider the
    #: gateway reads enforces (reported==enforced); POST /v1/models/routing never
    #: writes it and drives every change through the governed executor.
    #: ``None`` ⇒ the routing read answers an honest empty and the write 503s.
    route_store: ModelRouteStore | None = None
    #: NE-168 — the demo governed multi-lane delegation dispatcher (``platform.py``
    #: composes it via ``create_subagent_dispatcher`` ONLY when
    #: ``ApiSettings.demo_delegation``). ``None`` ⇒ ``POST /v1/demo/delegate``
    #: answers the honest 404 (the ``entity_graph``/``ontology`` 404/503 precedent).
    demo_dispatcher: GovernedSubAgentDispatcher | None = None
    #: NE-WORKFLOWS — the governed sub-agent dispatcher the workflow drafting leg
    #: uses (``platform.py`` composes it ONLY when ``ApiSettings.workflows_enabled``).
    #: ``None`` ⇒ the workflow routes answer the honest 404, and a run that somehow
    #: reached the engine drafts from its own deterministic sentence rather than
    #: silently skipping the account.
    workflow_dispatcher: GovernedSubAgentDispatcher | None = None
    #: NE-VAF-U4 — build_platform's deterministic verified-answer router. ``None``
    #: ⇒ chat turns run the unchanged model loop (byte-identical default-off).
    verified_router: VerifiedRouter[Any] | None = None
    #: NE-GA-G5 — build_platform's GroundingPolicy. ``None`` ⇒ no session is ever
    #: built grounded (byte-identical default-off); ne-worker inherits it via the
    #: same recipe.
    grounding: GroundingPolicy | None = None
    #: NE-GA-G5 — the ONE agent_name the grounding policy applies to. ``None``
    #: exactly when ``grounding`` is ``None``; call sites compare the request's
    #: agent_name against this before threading ``grounding=``.
    grounded_agent_name: str | None = None
    #: NE-SKILL-1a — the Skill Forge registry engine (over the flywheel signal DB).
    #: ``None`` ⇒ GET /v1/steward/skills answers 503 (skills not configured — the
    #: eval_store 503 precedent). Trailing-defaulted so no existing ``ApiState(...)``
    #: construction breaks.
    skills_engine: sa.Engine | None = None
    #: NE-SERVING-STATUS — the live serving-status prober for the private lanes
    #: (GET /v1/models/serving). Holds the observation cache + last-success
    #: bookkeeping, so it is ONE instance per app, threaded here. ``None`` ⇒ the
    #: route answers an honest empty lane list. Trailing-defaulted so no existing
    #: ``ApiState(...)`` construction breaks.
    serving_status: ServingStatusService | None = None
    #: BD-15 — the ONE server authority for live per-run model choices and
    #: governed selection. Ask and agentic workflows share this exact instance;
    #: ``None`` makes both choice/selection surfaces fail closed with 503 while
    #: legacy requests that name no model retain their historical task route.
    model_selection: ModelSelectionService | None = None
    #: Governed durable inference lifecycle authority. ``None`` keeps the API
    #: read-only and transition endpoints fail closed.
    inference_lifecycle: InferenceLifecycleService | None = None
    #: PRE-011E — the composed Agents capability preflight service. Trailing-
    #: defaulted like every other late seam, but production ALWAYS composes it:
    #: ``None`` here is not "unavailable", it is "no preflight at all", and it
    #: puts the router's not-configured 503 back on the production path.
    agent_preflight: AgentCapabilityPreflightService | None = None
    #: 015A0-AUTHORITY-CORE — the READ-ONLY current-source authority evaluator.
    #: This is the ONLY source-authority seam request state may hold: the ne-brain
    #: reconciliation Writer is producer capability, and an API projector that
    #: could claim/renew/finalize could reassign authority to itself
    #: (ADR-0022 §4). ``None`` while no runtime engine composed
    #: one. Trailing-defaulted like every other late seam.
    source_authority_reader: CurrentSourceAuthorityReader | None = None
    #: Metadata-only current authority attempts. This reader has no Writer,
    #: force, delete, receipt-minting, or direct stage-advance capability.
    authority_attempts: AuthorityAttemptReader | None = None
    source_authority_evaluator: CurrentSourceDisclosureEvaluator | None = None
    source_guard: ProtectedSourceResultGuard | None = None
    current_document_authorizer: Callable[..., Any] | None = None
    protected_source_output_writer: ProtectedSourceOutputWriter | None = None
    protected_source_execution_writer: ProtectedSourceExecutionWriter | None = None
    #: PRE-009A A3 — the composed Agent Run store, or ``None`` while dormant.
    #: Trailing-defaulted so `app.py` is untouched by this phase: A3 COMPOSES the
    #: store on ``Platform`` and declares the accessor contract here; 009C owns
    #: passing it through and activating the routes. An accessor that 503s today
    #: is the honest shape for a seam nothing routes to yet.
    agent_run_store: AgentRunStore | None = None
    #: Exact protected legacy-transport generation admitted by both roots.
    agent_transport_generation: str | None = None
    default_ask_submission: Any | None = None
    agent_run_enqueuer: Any | None = None
    #: NE-SELECT-LATENCY — THE process's ONE chain-integrity trust root
    #: (:class:`ne.audit.AnchoredChainVerifier`) over ``audit_engine``.  ONE per
    #: process per engine, by the verifier's own contract: a second instance
    #: would carry a second, independently aged anchor and cost a second
    #: O(chain) full verify for no gain.  The SAME instance is injected into
    #: ``ModelSelectionService`` (whose ``run_chain_anchor_refresher`` loop is
    #: what re-blesses it in the background) and read by the audit router for
    #: the UNFILTERED page verdict.
    #:
    #: ``None`` ⇒ the audit page falls back to the unbounded ``verify_chain`` —
    #: the same verdict and the same guarantee at today's O(chain) cost, never a
    #: weaker answer.  Trailing-defaulted like every other late seam so no
    #: existing ``ApiState(...)`` construction breaks.
    chain_verifier: AnchoredChainVerifier | None = None


def api_state(request: Request) -> ApiState:
    """Return the :class:`ApiState` attached to the inner app (routes read it)."""
    state = getattr(request.app.state, _STATE_KEY)
    assert isinstance(state, ApiState)
    return state


def attach_state(app_state: object, state: ApiState) -> None:
    """Attach ``state`` under the canonical key on the inner app's ``state``."""
    setattr(app_state, _STATE_KEY, state)


def get_agent_run_store(request: Request) -> AgentRunStore:
    """The composed Agent Run store, or an honest 503 while dormant.

    Mirrors the ``eval_store``/``entity_graph`` precedent: a disabled tier answers
    503 rather than pretending. PRE-009A composes and proves the store; it routes
    nothing to it.
    """
    store = api_state(request).agent_run_store
    if store is None:
        raise HTTPException(status_code=503, detail="agent run storage is not active")
    return store
