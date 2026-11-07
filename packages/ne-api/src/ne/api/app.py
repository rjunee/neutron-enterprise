# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The ne-api composition root — ``create_app`` (spec §3.1 / §3.2).

DECIDED interpretation of §3.2 (see the unit's risks §1): the 12 normative steps
run EAGERLY at factory time IN ORDER — the two-layer mount needs the constructed
middleware and every construction is synchronous, so ``create_app`` performs the
wiring itself; the ASGI lifespan asserts the frozen ``config_hash``, owns bounded
reconcilers for durable work already committed to an outbox, and disposes engines
at shutdown. A reviewer should check the ORDER, not the location.

D2 (NE-103c): steps 1-11 are now the SHARED :func:`ne.api.platform.build_platform`
recipe, called by BOTH ne-api and ne-worker (one recipe, two processes — the
strongest structural guarantee the worker cannot compose a weaker governed stack).
``create_app`` calls it and keeps ONLY the ne-api-specific surfaces: the eval /
flywheel / kpi READ stores, ``ApiState`` construction, routers, the OIDC wrap, the
``/healthz`` + ``/api`` + StaticFiles mount topology, and the lifespan. ``AppSeams``
is re-exported from :mod:`ne.api.platform` so ``from ne.api.app import AppSeams``
still works.

HASH-SCHEMA CHANGE (NE-103c, deliberate, release-level — flagged in the PR):
``ApiSettings`` gains ``turn_execution`` + ``dbos_system_database_url``, so the
composed ``config_hash`` differs from the just-merged NE-103b (the ne-api settings
model freezes into the hash under the ``"ne-api"`` key). This is the 104b/104c
release-level precedent, NOT a mid-process mutation; the freeze still runs ONCE.

HASH-SCHEMA CHANGE (104c, deliberate, release-level — flagged in the PR):
``_ensure_api_claims`` now unions BOTH ``'department'`` AND ``'sid'`` into the JIT
claim allowlist (spec §7-R4: the steward provenance ``session_id`` is the verified
token's ``sid`` claim when present else ``uuid4``). Adding ``'sid'`` to
``IdentitySettings.principal_claims`` changes the composed ``config_hash`` for
identical deployment config vs merged 104b — an EXPECTED release-level change (the
exact precedent of 104b's ne-obs/eval_database_url change above), NOT a mid-process
mutation; the freeze still runs ONCE at factory time before any emitter.

NE-103b (console static serving — owner dec iii): when
``ApiSettings.serve_console_dist`` is set, ``create_app`` appends a SPA-fallback
``StaticFiles`` mount at ``/`` as the LAST route — OUTSIDE ``build_identity``'s
OIDC wrap. The mount topology IS the auth boundary: ``/api`` stays fully
OIDC-guarded (its Mount is matched BEFORE the ``/`` mount by route order), and
``/healthz`` is an exact-match Route matched first. The static server never sees
an ``/api`` or ``/healthz`` request, so it can never bypass the OIDC middleware.
``serve_console_dist`` was ALREADY an ``ApiSettings`` field, so this unit makes
NO config_hash schema change — only per-deployment values change, as normal.

HASH-SCHEMA CHANGE (NE-089, deliberate, release-level — flagged in the PR):
this slice adds ``ApiSettings.scim_bearer_token`` (a ``SecretStr``, masked by
``compute_config_hash``), so the composed ``config_hash`` differs from the
just-merged 104c for identical deployment config — the exact 104b/104c
precedent above (ne-obs/eval_database_url, then the ``'sid'`` claim union),
NOT a mid-process mutation.

NE-CONSOLE-ISSUER (runtime console config): ``GET /config.json`` is an additive,
unauthenticated, exact-match Route serving the deployment's PUBLIC OIDC discovery
values (``issuer``/``client_id``/``audience``/``api_base``) — NEVER a secret. Its
``issuer``/``audience`` come from the SAME ``platform.identity_settings.oidc`` the
OIDC verifier trusts (ONE issuer source of truth, so the console and the backend
can never disagree). It sits beside ``/healthz`` OUTSIDE the OIDC wrap (ordered
after ``/healthz`` and BEFORE the ``/api`` Mount + the StaticFiles mount), so the
SPA reads it pre-login. It is NOT added to the middleware exempt list — the guarded
alias ``/api/config.json`` must stay 401.

HASH-SCHEMA CHANGE (NE-CONSOLE-ISSUER, deliberate, release-level — flagged in the
PR): ``ApiSettings`` gains ``console_client_id`` (the public SPA client_id the
``/config.json`` payload advertises), so the composed ``config_hash`` differs from
merged main for identical deployment config — the 104b/104c precedent above, NOT a
mid-process mutation; the freeze still runs ONCE at factory time.

SCIM mount (NE-089): ``/scim/v2`` is a SEPARATE small FastAPI app (built by
``ne.api.scim.build_scim_app``), gated by its OWN bearer secret, mounted
OUTSIDE ``build_identity``'s OIDC wrap. It is constructed only when
``ApiSettings.scim_bearer_token`` is non-empty (feature off ⇒ mount absent ⇒
404, fail-closed default); a configured secret shorter than 16 chars fails
LOUD at factory time (``ValueError``, the weak-secret guard — never a silent
runtime 401). Mount ORDER IS LOAD-BEARING: after the exact-match ``/healthz``
Route, BEFORE the OIDC-wrapped ``/api`` Mount — so it can never shadow either.

CDC mount (NE-084): ``/cdc/v1`` is a SEPARATE small FastAPI app (built by
``ne.api.cdc.build_cdc_sink_app``), gated by its OWN Standard-Webhooks HMAC
signature (the Debezium HTTP-sink scheme — a machine caller, NOT an OIDC user),
mounted OUTSIDE ``build_identity``'s OIDC wrap, after ``/scim/v2`` and before the
``/api`` Mount. It is constructed only when ``platform.cdc_service`` is wired
(i.e. ``ApiSettings.cdc_webhook_secret`` is configured; feature off ⇒ mount
absent ⇒ 404, fail-closed default); a malformed secret / missing table map /
missing quarantine path fails LOUD at factory time (``ValueError``, never a
silent runtime 401). Change events flow through the SAME governed KnowledgeWriter
path as the polling connectors (fail-closed ACL ingestion). NOTE: the three new
``ApiSettings`` CDC fields are an EXPECTED release-level ``config_hash`` schema
change (the NE-089 ``scim_bearer_token`` precedent verbatim — see the PR body).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio.to_thread
import sqlalchemy as sa
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from ne.api.agent_model_preflight import AgentModelPreflightService
from ne.api.agent_profile_projection import AgentProfileProjectionState
from ne.api.cdc import build_cdc_sink_app
from ne.api.default_ask_submission import (
    DefaultAskActivationPin,
    compose_default_ask_submission_service,
    select_default_ask_agent,
)
from ne.api.deps import ApiState, KpiSources, attach_state
from ne.api.errors import install_error_handlers
from ne.api.evidence_projection import (
    EvidenceProjector,
    RunStoreCurrentEvidenceMaterializer,
    ServedVersionKey,
    SourceBoundRunOutputReader,
    SqlAlchemyEvidenceProjectionReader,
    catalog_visibility,
)
from ne.api.evidence_wire import ServedDocumentVersionView
from ne.api.frontier_warm import GatewayFrontierWarm
from ne.api.model_routing import build_route_provider
from ne.api.model_selection import ModelSelectionService
from ne.api.platform import AppSeams, Platform, build_platform
from ne.api.posture import effective_egress
from ne.api.routers import (
    agent_profiles,
    agent_run_updates,
    agents,
    answer_ledger,
    approvals,
    audit,
    brain,
    chat,
    connect,
    context_ledger,
    conversations,
    demo,
    documents,
    evals,
    evidence,
    flowise,
    graph,
    kpi,
    me,
    models,
    network,
    ontology,
    posture,
    principals,
    runs,
    signals,
    skills,
    steward,
    trace,
    workflows,
)
from ne.api.run_submission import RUN_SUBMISSION_STATE_KEY, compose_run_submission_service
from ne.api.scim import ScimProvisioningService, build_scim_app
from ne.api.serving import ServingStatusService
from ne.api.settings import ApiSettings
from ne.api.turn_relay import (
    DbosAgentRunEnqueuer,
    DbosStreamTurnFrameReader,
    DbosTurnEnqueuer,
    TurnEnqueuer,
    TurnFrameReader,
)
from ne.audit import (
    AnchoredChainVerifier,
    create_engine_with_timeouts,
    ensure_transactional_sqlite,
)
from ne.brain.answer_ledger import ensure_answer_ledger_schema
from ne.brain.current_source_filter import HistoricalSourceDisclosureEvaluator
from ne.connect.cdc import decode_webhook_secret
from ne.contracts import get_config_hash
from ne.contracts.source_authority import SourceAuthorityDecision
from ne.contracts.types import Principal
from ne.eval.store import EvalRunStore, ensure_eval_schema
from ne.flywheel import (
    ProposalReviewService,
    SqlAlchemyCorrectionSignalStore,
    SqlAlchemyProposalStore,
    ensure_review_schema,
)
from ne.flywheel.capture import CorrectionSignalCapture
from ne.flywheel.config import FlywheelSettings
from ne.govern import create_pdp
from ne.identity.config import IdentitySettings
from ne.identity.factory import build_identity
from ne.obs.config import ObsReportSettings

__all__ = ["AppSeams", "Platform", "app_factory", "create_app"]

_logger = logging.getLogger(__name__)


class _SpaStaticFiles(StaticFiles):
    """StaticFiles with SPA (client-side routing) fallback to ``index.html``.

    A miss (Starlette raises ``HTTPException(404)``) serves ``index.html`` so the
    SPA's client-side router owns unknown paths. Every OTHER status propagates
    unchanged — a ``405`` (non-GET/HEAD), a ``401`` (unreadable file), etc. must
    NOT be swallowed into an index-page 200.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def create_app(settings: ApiSettings | None = None, *, seams: AppSeams | None = None) -> Starlette:
    """Construct the governed ne-api ASGI application (spec §3.1/§3.2)."""
    api_settings = settings if settings is not None else ApiSettings()  # type: ignore[call-arg]
    seams = seams if seams is not None else AppSeams()

    # Steps 1-11: the SHARED composition recipe (also run by ne-worker).
    platform = build_platform(api_settings, seams)
    engines = platform.engines
    ensure_answer_ledger_schema(platform.audit_engine)
    if api_settings.workflows_enabled:
        # BD-6: the model-planned run record is durable governed state.  It rides
        # the existing platform audit database so no second database URL or
        # best-effort in-memory fallback can appear between deployments.
        from ne.api.agentic_workflows import ensure_agentic_workflow_schema

        ensure_agentic_workflow_schema(platform.audit_engine)

    # (7b) SCIM 2.0 provisioning (NE-089) — a governed, audited write path over the
    # SAME PrincipalStore + the ONE audit sink from build_platform. Constructed only
    # when a bearer secret is configured (fail-closed default: unset ⇒ no mount ⇒
    # 404). A configured-but-weak secret fails LOUD here, not at request time.
    scim_app: Starlette | None = None
    scim_token = api_settings.scim_bearer_token.get_secret_value()
    if scim_token:
        if len(scim_token) < 16:
            raise ValueError(
                "ApiSettings.scim_bearer_token is set but shorter than 16 characters "
                "— set a stronger NE_API_SCIM_BEARER_TOKEN (weak-secret guard)"
            )
        scim_service = ScimProvisioningService(platform.perm_store, platform.sink)
        scim_app = build_scim_app(scim_service, scim_token)

    # (7c) CDC sink mount (NE-084) — the governed Debezium change-event webhook.
    # Constructed only when build_platform wired a cdc_service (i.e. the webhook
    # secret is configured; fail-closed default ⇒ no service ⇒ no mount ⇒ 404).
    # The secret was already validated in build_platform (loud factory error on a
    # malformed value), so decode here cannot fail.
    cdc_app: Starlette | None = None
    if platform.cdc_service is not None:
        cdc_app = build_cdc_sink_app(
            platform.cdc_service,
            decode_webhook_secret(api_settings.cdc_webhook_secret.get_secret_value()),
        )

    # (11b) The ONE flywheel engine shared by the steward review service, its
    # proposal store, AND the KPI signal read store — the same-transaction
    # dual-append requirement (review.py:185-198). ensure_review_schema is a
    # SUPERSET of ensure_flywheel_schema, so the KPI signal store reuses this engine.
    flywheel_settings = seams.flywheel_settings or FlywheelSettings()
    flywheel_engine: sa.Engine | None = None
    review_service: ProposalReviewService | None = None
    proposal_store: SqlAlchemyProposalStore | None = None
    signal_capture: CorrectionSignalCapture | None = None
    verified_curation = None
    if flywheel_settings.signal_database_url:
        flywheel_engine = create_engine_with_timeouts(flywheel_settings.signal_database_url)
        ensure_transactional_sqlite(flywheel_engine)
        ensure_review_schema(flywheel_engine)
        # NE-ONTOLOGY-FLYWHEEL: the governed HITL template store rides the SAME
        # flywheel engine (the same-transaction dual-append requirement). Ensure its
        # table alongside the review schema ONLY when verified curation is active
        # (verified answers enabled ⇒ build_platform already imported ne-brain-ontology,
        # so the lazy import here is free; disabled ⇒ no rdflib import, no table).
        if platform.verified_curation is not None:
            from ne.brain_ontology.verified.store import ensure_verified_template_schema

            ensure_verified_template_schema(flywheel_engine)
            verified_curation = platform.verified_curation
        engines.append(flywheel_engine)
        review_service = ProposalReviewService(
            flywheel_engine, max_text_chars=flywheel_settings.max_text_chars
        )
        proposal_store = SqlAlchemyProposalStore(flywheel_engine)
        # NE-137: the chat thumbs / wrong-source intake rides the SAME flywheel engine
        # (ensure_review_schema already ensured the flywheel + outbox closure on it —
        # the same-transaction dual-append requirement, factory.py). None otherwise ⇒
        # the signal routes answer 503.
        signal_capture = CorrectionSignalCapture(
            flywheel_engine, max_text_chars=flywheel_settings.max_text_chars
        )

    eval_store: EvalRunStore | None = None
    if api_settings.eval_database_url:
        eval_engine = create_engine_with_timeouts(api_settings.eval_database_url)
        ensure_eval_schema(eval_engine)
        engines.append(eval_engine)
        eval_store = EvalRunStore(eval_engine)

    obs_settings = seams.obs_settings or ObsReportSettings()
    kpi_sources: KpiSources | None = None
    if eval_store is not None and flywheel_engine is not None:
        kpi_sources = KpiSources(
            audit_engine=platform.audit_engine,
            eval_store=eval_store,
            signal_store=SqlAlchemyCorrectionSignalStore(flywheel_engine),
            obs_settings=obs_settings,
        )

    # NE-103c enqueue mode: when NE_API_TURN_EXECUTION=enqueue, construct the ONE
    # DBOSClient + the enqueuer/reader relay impls (inline mode leaves them None,
    # and the chat router runs the in-process turn as today — the passthrough
    # default). Test seams may inject fakes to exercise the relay without DBOS.
    turn_enqueuer, turn_reader = _build_relay(api_settings, seams)
    # PRE-014A: the same server authority resolves both Agent preflight and
    # canonical Run admission. Build it before the submission service so no
    # client-supplied lane can cross the atomic Run boundary unchecked.
    serving_status = seams.serving_probe or ServingStatusService()

    def _selection_air_gap() -> bool:
        provider = platform.egress_settings_provider
        if provider is None:
            return True
        return effective_egress(provider())[0]

    model_selection_pdp = create_pdp(
        platform.govern_settings,
        sink=platform.sink,
        registry=seams.plugin_registry,
    )
    if platform.gateway_settings is not None and platform.route_store is None:
        raise ValueError("model selection requires the governed live route store")
    route_provider = (
        None if platform.route_store is None else build_route_provider(platform.route_store)
    )
    # NE-FRONTIER-LIVE: default OFF ⇒ None ⇒ the selection authority issues no
    # provider call of its own (byte-identical). On, it warms through the SAME
    # governed gateway every run uses — never a second client.
    frontier_warm = (
        GatewayFrontierWarm(platform.gateway)
        if platform.api_settings.frontier_warm_enabled
        else None
    )
    # NE-SELECT-LATENCY: THE process's one chain-integrity trust root, built
    # HERE rather than inside ModelSelectionService so the audit router can share
    # the identical instance (ApiState.chain_verifier). The verifier's contract is
    # one per process per engine — two would carry two independently aged anchors
    # and pay two O(chain) full verifies — and the audit page and the selection
    # authority verify the SAME ledger. The background loop that re-blesses it is
    # `model_selection.run_chain_anchor_refresher`, wired into the lifespan below;
    # when `model_selection` is None (no gateway settings composed) there is no
    # loop, so the anchor is only ever blessed by an inline request-path full
    # verify and ages out every CHAIN_ANCHOR_MAX_AGE_S. That degrades LOUDLY to
    # today's cost on one read in fifteen minutes; it never degrades the verdict.
    chain_verifier = AnchoredChainVerifier(platform.audit_engine)
    model_selection = (
        ModelSelectionService(
            chain_verifier=chain_verifier,
            gateway_settings=platform.gateway_settings,
            serving_status=serving_status,
            audit_engine=platform.audit_engine,
            pdp=model_selection_pdp,
            sink=platform.sink,
            config_hash=platform.config_hash,
            air_gap_provider=_selection_air_gap,
            lifecycle_availability=platform.inference_lifecycle.availability
            if platform.inference_lifecycle is not None
            else None,
            route_provider=route_provider,
            frontier_warm=frontier_warm,
        )
        if platform.gateway_settings is not None
        else None
    )
    run_submission_service = compose_run_submission_service(
        transactions=platform.agent_run_store,
        agent_catalog=platform.agent_catalog,
        agent_preflight=platform.agent_preflight,
        model_selection=model_selection,
        execution_bindings=platform.agent_execution_bindings,
        config_hash=platform.config_hash,
    )
    default_ask_submission = None
    agent_run_enqueuer = (
        DbosAgentRunEnqueuer(system_database_url=api_settings.dbos_system_database_url)
        if run_submission_service is not None and api_settings.dbos_system_database_url
        else None
    )
    if api_settings.default_ask_generation:
        if platform.agent_run_store is None or not api_settings.dbos_system_database_url:
            raise ValueError("canonical default Ask requires Run storage and DBOS system database")
        catalog_agent = select_default_ask_agent(
            platform.agent_catalog.agents,
            allow_knowledge_bootstrap=True,
        )
        # A fresh Graph deployment has no authority receipt until its first
        # governed sync.  Keep the activation fence unwritten on that one exact
        # cold state; chat routes key off the configured generation and refuse
        # every legacy writer until a post-sync boot can compose canonical Ask.
        if catalog_agent.available:
            definition = catalog_agent.definition
            platform.agent_run_store.activate_default_ask_at_boot(
                api_settings.default_ask_generation
            )
            pin = DefaultAskActivationPin(
                wire_generation=api_settings.default_ask_generation,
                config_hash=platform.config_hash,
                default_agent_id=definition.agent_id,
                default_agent_version=definition.version,
                producer_snapshot_hash=definition.definition_hash,
                submission_ready=True,
                worker_execution_ready=True,
                finalizer_ready=True,
                legacy_attachment_writer="retired",
                legacy_terminal_writer="retired",
                legacy_cross_turn_loader="retired",
            )
            default_ask_submission = compose_default_ask_submission_service(
                run_submissions=run_submission_service,
                transactions=platform.agent_run_store,
                activation=pin,
            )

    # BD-15: one API-owned authority shared by Ask and agentic workflows. Private
    # availability comes from the SAME process-local serving observer exposed by
    # /models/serving; frontier availability comes from the verified audit chain.
    # The PDP uses the same settings/plugin registry and the same fail-closed sink
    # recipe as every other governed decision in this composition.
    agent_model_preflight = None
    if model_selection is not None:
        if route_provider is None:  # pragma: no cover - guarded at composition above
            raise ValueError("model preflight requires the governed live route store")
        agent_model_preflight = AgentModelPreflightService(
            agent_catalog=platform.agent_catalog,
            selection=model_selection,
            pdp=model_selection_pdp,
            route_provider=route_provider,
        )

    state = ApiState(
        settings=api_settings,
        config_hash=platform.config_hash,
        sink=platform.sink,
        gate=platform.gate,
        executor=platform.executor,
        retriever=platform.retriever,
        current_retriever=platform.current_retriever,
        entity_graph=platform.entity_graph,
        demo_dispatcher=platform.demo_dispatcher,
        workflow_dispatcher=platform.workflow_dispatcher,
        verified_router=platform.verified_router,
        grounding=platform.grounding,
        grounded_agent_name=platform.grounded_agent_name,
        gateway_settings=platform.gateway_settings,
        egress_settings=platform.egress_settings,
        egress_settings_provider=platform.egress_settings_provider,
        egress_store=platform.egress_store,
        route_store=platform.route_store,
        session_store=platform.session_store,
        agent_run_store=platform.agent_run_store,
        agent_transport_generation=platform.agent_transport_generation,
        default_ask_submission=default_ask_submission,
        agent_run_enqueuer=agent_run_enqueuer,
        # 015A0-AUTHORITY-CORE: the Reader ONLY. `platform.source_authority_continuation`
        # is deliberately not threaded here — request state never needs the codec, and
        # `platform.source_authority_writer` does not exist to be threaded by mistake.
        source_authority_reader=platform.source_authority_reader,
        authority_attempts=platform.authority_attempts,
        source_authority_evaluator=platform.source_authority_evaluator,
        source_guard=platform.source_guard,
        current_document_authorizer=platform.current_document_authorizer,
        protected_source_output_writer=platform.protected_source_output_writer,
        protected_source_execution_writer=platform.protected_source_execution_writer,
        gateway=platform.gateway,
        agent_catalog=platform.agent_catalog,
        agent_preflight=platform.agent_preflight,
        runtime_settings=platform.runtime_settings,
        govern_settings=platform.govern_settings,
        identity_settings=platform.identity_settings,
        audit_settings=platform.audit_settings,
        secret_store=platform.secret_store,
        perm_store=platform.perm_store,
        expander=platform.expander,
        perm_sync=platform.perm_sync,
        engines=engines,
        audit_engine=platform.audit_engine,
        document_lineage=platform.document_lineage,
        eval_store=eval_store,
        kpi_sources=kpi_sources,
        review_service=review_service,
        proposal_store=proposal_store,
        verified_curation=verified_curation,
        turn_enqueuer=turn_enqueuer,
        turn_reader=turn_reader,
        ontology=platform.ontology,
        connect_sync_settings=platform.connect_sync_settings,
        connect_sync_requests=platform.connect_sync_requests,
        signal_capture=signal_capture,
        skills_engine=platform.skills_engine,
        # NE-SERVING-STATUS: ONE prober per app (its cache + last-success
        # bookkeeping are the stale-vs-never-answered memory); the seam injects
        # a fake transport/clock in tests, production constructs the real one.
        serving_status=serving_status,
        model_selection=model_selection,
        chain_verifier=chain_verifier,
        inference_lifecycle=platform.inference_lifecycle,
    )

    # (12) Inner FastAPI app (all /v1 routers) → OIDC wrap → two-layer mount.
    registry = seams.plugin_registry
    inner = FastAPI()
    install_error_handlers(inner)
    inner.include_router(me.router)
    # Static cross-Agent collection route MUST precede /v1/agents/{agent_id}.
    inner.include_router(agent_run_updates.router)
    inner.include_router(agents.router)
    inner.include_router(agent_profiles.router)
    inner.include_router(conversations.router)
    inner.include_router(evidence.router)
    inner.include_router(runs.router)
    inner.include_router(principals.router)
    inner.include_router(chat.router)
    inner.include_router(context_ledger.router)
    inner.include_router(signals.router)
    inner.include_router(brain.router)
    inner.include_router(answer_ledger.router)
    inner.include_router(documents.router)
    inner.include_router(graph.router)
    inner.include_router(approvals.router)
    inner.include_router(audit.router)
    inner.include_router(trace.router)
    inner.include_router(evals.router)
    inner.include_router(kpi.router)
    inner.include_router(models.router)
    inner.include_router(posture.router)
    inner.include_router(network.router)
    inner.include_router(connect.router)
    inner.include_router(steward.router)
    inner.include_router(skills.router)
    inner.include_router(ontology.router)
    inner.include_router(flowise.router)
    inner.include_router(demo.router)
    inner.include_router(workflows.router)
    attach_state(inner.state, state)
    # PRE-010A: never synthesize accepted-submission atomicity from the older
    # AgentRunStore's separate calls. Composition returns ``None`` and every write
    # endpoint fails closed at 503 unless the exact runtime transaction is present.
    setattr(
        inner.state,
        RUN_SUBMISSION_STATE_KEY,
        run_submission_service,
    )
    # PRE-011E — the Agents preflight route resolves its service through a LOCAL
    # typed dependency off ``app.state`` (it shipped ahead of this composition).
    # Bound from ``state`` rather than from ``platform`` so there is exactly ONE
    # composed value behind both accessors: a second read of ``platform`` here
    # could silently drift from the typed ``ApiState`` field. ``None`` (no
    # composition at all) still resolves to the router's honest 503.
    setattr(inner.state, agents.CAPABILITY_PREFLIGHT_STATE_KEY, state.agent_preflight)
    # PRE-011D HTTP composition — the singular route joins this service with
    # PRE-011E's capability projection. It shares the exact catalog,
    # ModelSelectionService, PDP and live ModelRouteStore already composed above.
    setattr(inner.state, agents.MODEL_PREFLIGHT_STATE_KEY, agent_model_preflight)
    # PRE-012 workflow-profile projection: the request route reads the exact immutable runtime
    # profiles composed by ``build_platform`` beside the SAME typed catalog state
    # used by every Agent visibility decision.  No profile is reconstructed here.
    setattr(
        inner.state,
        agent_profiles.AGENT_PROFILE_PROJECTION_STATE_KEY,
        AgentProfileProjectionState(
            agent_catalog=state.agent_catalog,
            workflow_execution_profiles=platform.workflow_execution_profiles,
        ),
    )
    evidence_service = None
    source_bound_output_reader = None
    authority_evaluator = state.source_authority_evaluator
    if state.agent_run_store is not None and authority_evaluator is not None:
        # Viewing retained run evidence is a HISTORICAL read: a document being
        # superseded is history's normal state and must not withhold the record
        # of how a past answer was derived, while fabrication, source removal
        # and ACL revocation still withhold it (see
        # HistoricalSourceDisclosureEvaluator).  Serving-as-current keeps the
        # strict current-truth evaluator everywhere else (retrieval, answer
        # ledger stamping, document routes).
        historical_evaluator = (
            HistoricalSourceDisclosureEvaluator(
                state.source_authority_reader,
                platform.runtime_engine,
                group_expander=state.expander,
            )
            if state.source_authority_reader is not None
            else None
        )
        view_evaluator = historical_evaluator or authority_evaluator

        async def _evaluate_evidence_stamp(stamp: Any, context: Any) -> SourceAuthorityDecision:
            principal = Principal(
                id=context.principal_id,
                groups=tuple(context.authorization_groups),
                claims={"department": context.department} if context.department is not None else {},
            )
            return (
                await view_evaluator.evaluate(
                    stamp,
                    principal=principal,
                    access_context_hash=context.context_hash,
                )
            ).decision

        visible_agent = catalog_visibility(state.agent_catalog.get_visible)
        projection_reader = SqlAlchemyEvidenceProjectionReader(platform.runtime_engine)
        materializer = RunStoreCurrentEvidenceMaterializer(
            state.agent_run_store,
            _evaluate_evidence_stamp,
        )
        lineage_store = state.document_lineage

        async def _resolve_served_versions(
            keys: tuple[ServedVersionKey, ...],
        ) -> tuple[ServedDocumentVersionView, ...]:
            # Artifact keys come from the run's own retained contributor stamps;
            # the lineage join names the exact version each snapshot was
            # admitted as. Deduplicated per (document, version) in contributor
            # order; a stamp with no retained snapshot row is honestly absent.
            # The key is the stamp's immutable content/ACL snapshot identity and
            # NOT its receipt id, because bind_source_authority rebinds one row
            # per (document, version) on every re-admission — a receipt id stops
            # resolving as soon as the same version is re-admitted.
            bound = await anyio.to_thread.run_sync(lineage_store.versions_for_artifacts, keys)
            views: list[ServedDocumentVersionView] = []
            seen: set[tuple[str, int]] = set()
            for key in keys:
                version = bound.get(key)
                if version is None:
                    continue
                identity = (version.document_id, version.version_seq)
                if identity in seen:
                    continue
                seen.add(identity)
                views.append(
                    ServedDocumentVersionView(
                        document_id=version.document_id,
                        version_seq=version.version_seq,
                        content_hash=version.content_hash,
                        display_name=version.display_name,
                    )
                )
            return tuple(views)

        evidence_service = EvidenceProjector(
            visible_agent=visible_agent,
            reader=projection_reader,
            materializer=materializer,
            served_version_resolver=_resolve_served_versions,
        )
        # THE SAME visibility check, reader and reauthorizing materializer the
        # Evidence drawer uses -- one authorization path, two read surfaces. The
        # single-Run GET may now serve a source-derived answer inline instead of
        # sending the reader to a drawer to find out what the agent said.
        source_bound_output_reader = SourceBoundRunOutputReader(
            visible_agent=visible_agent,
            reader=projection_reader,
            materializer=materializer,
        )
    evidence.attach_evidence_service(inner.state, evidence_service)
    runs.attach_source_bound_output_reader(inner.state, source_bound_output_reader)

    healthz_settings = _force_healthz_exempt(platform.identity_settings)
    # NE-BEW-GROUPS-ZEROGROUPS: wire the ONE platform audit sink so a brokered
    # login that resolves zero mapped groups lands in the queryable audit log.
    wrapped = build_identity(healthz_settings, inner, registry=registry, audit_sink=platform.sink)

    # NE-CONSOLE-ISSUER: the PUBLIC runtime console config. issuer/audience come
    # from the SAME platform.identity_settings.oidc the OIDC verifier trusts (ONE
    # issuer source of truth); client_id from ApiSettings; api_base is the "/api"
    # mount-path literal three lines below. Settings are frozen, so computing the
    # payload ONCE at factory time is correct. NEVER a secret (C12: public PKCE
    # SPA — no client secret).
    console_config_payload: dict[str, str] = {
        "issuer": platform.identity_settings.oidc.issuer,
        "client_id": api_settings.console_client_id,
        "audience": platform.identity_settings.oidc.audience,
        "api_base": "/api",  # the literal /api mount path constant below
    }

    async def _console_config(_request: Request) -> JSONResponse:
        # Closure over the factory-time payload — issuer rotation can never serve
        # stale (no-store), and no secret is ever in the body (C12).
        return JSONResponse(console_config_payload, headers={"Cache-Control": "no-store"})

    # ORDER IS LOAD-BEARING (spec §5-row + NE-103b + NE-089 + NE-CONSOLE-ISSUER):
    # the exact-match /healthz Route FIRST, then the exact-match /config.json Route
    # (public runtime console config, OUTSIDE the OIDC wrap), then (iff configured)
    # the bearer-gated /scim/v2 Mount — it has its OWN auth, sits OUTSIDE the OIDC
    # wrap, and can never shadow /api or /healthz — then the OIDC-wrapped /api
    # Mount, and the console StaticFiles mount at / LAST. Starlette matches in list
    # order, so /config.json (a Route) always wins over any config.json inside the
    # console dist StaticFiles mount.
    routes: list[BaseRoute] = [
        Route("/healthz", _healthz, methods=["GET"]),
        Route("/config.json", _console_config, methods=["GET"]),
    ]
    if scim_app is not None:
        routes.append(Mount("/scim/v2", app=scim_app))
    if cdc_app is not None:
        # NE-084: the CDC webhook sink — its OWN Standard-Webhooks HMAC gate, also
        # OUTSIDE the OIDC wrap (Debezium is a machine caller, not an OIDC user);
        # placed after /scim/v2, before the OIDC-wrapped /api Mount.
        routes.append(Mount("/cdc/v1", app=cdc_app))
    routes.append(Mount("/api", app=wrapped))
    if api_settings.serve_console_dist:
        index = Path(api_settings.serve_console_dist) / "index.html"
        if not index.is_file():
            raise ValueError(
                f"ApiSettings.serve_console_dist is set but {index} does not exist "
                "— the console dist is missing (NE_API_SERVE_CONSOLE_DIST)"
            )
        routes.append(
            Mount(
                "/",
                app=_SpaStaticFiles(directory=api_settings.serve_console_dist, html=True),
                name="console",
            )
        )

    # NE-T2X-4: emit the graph.extraction_mode audit event once, at composition-root
    # startup, on the REAL sink — only when the extraction pathway is composed.
    extraction_on_startup: Callable[[], Awaitable[None]] | None = None
    if platform.extraction is not None:
        extraction_on_startup = platform.extraction.emit_mode_event

    run_dispatch_reconciler: Callable[[], Awaitable[None]] | None = None
    if platform.agent_run_store is not None and agent_run_enqueuer is not None:
        approval_checkpoints = getattr(platform.gate, "_run_approval_checkpoints", None)

        async def _reconcile_run_dispatches() -> None:
            await _run_agent_dispatch_reconciler(
                run_store=platform.agent_run_store,
                enqueuer=agent_run_enqueuer,
                approval_checkpoints=approval_checkpoints,
            )

        run_dispatch_reconciler = _reconcile_run_dispatches

    return Starlette(
        routes=routes,
        lifespan=_lifespan_factory(
            platform.config_hash,
            engines,
            platform.rebac_client,
            on_startup=extraction_on_startup,
            inference_lifecycle=platform.inference_lifecycle,
            approval_gate=platform.gate,
            run_dispatch_reconciler=run_dispatch_reconciler,
            frontier_evidence_refresher=(
                model_selection.run_frontier_evidence_refresher
                if model_selection is not None
                else None
            ),
            chain_anchor_refresher=(
                model_selection.run_chain_anchor_refresher if model_selection is not None else None
            ),
        ),
    )


def _build_relay(
    api_settings: ApiSettings, seams: AppSeams
) -> tuple[TurnEnqueuer | None, TurnFrameReader | None]:
    """Construct the enqueue-mode relay (None in inline mode; test seams override).

    In ``enqueue`` mode the ONE ``DBOSClient`` is constructed (lazily, inside the
    impl constructors — the ``_default_gateway`` precedent) and shared by the
    enqueuer + reader. An empty ``dbos_system_database_url`` in enqueue mode is a
    boot ValueError (see :class:`ApiSettings`).
    """
    if seams.turn_enqueuer is not None or seams.turn_reader is not None:
        return seams.turn_enqueuer, seams.turn_reader
    if api_settings.turn_execution != "enqueue":
        return None, None
    if not api_settings.dbos_system_database_url:
        raise ValueError(
            "NE_API_TURN_EXECUTION=enqueue requires NE_API_DBOS_SYSTEM_DATABASE_URL "
            "(the DBOS system database the worker launches its schema in)"
        )
    url = api_settings.dbos_system_database_url
    return (
        DbosTurnEnqueuer(system_database_url=url, queue_name="chat-turns"),
        DbosStreamTurnFrameReader(system_database_url=url),
    )


def app_factory() -> Starlette:
    """uvicorn factory entrypoint (``uvicorn.run('ne.api.app:app_factory', factory=True)``)."""
    # PRE-011F production gate: the worker must already have admitted the exact
    # mounted Skill pin into the shared verified registry. This verifier is
    # strictly read-only and runs before create_app/build_platform freezes the
    # catalog. Direct create_app calls remain the explicit test/embedding seam.
    from ne.api.skills_delivery import verify_required_agent_skills_from_environment

    verify_required_agent_skills_from_environment()
    return create_app(ApiSettings())  # type: ignore[call-arg]


# --- helpers ---------------------------------------------------------------


async def _healthz(_request: Request) -> JSONResponse:
    # Public exact-match route (spec §1.8). No DB ping in v1; no secrets/config.
    return JSONResponse({"status": "ok"})


def _force_healthz_exempt(identity_settings: IdentitySettings) -> IdentitySettings:
    """Belt-and-braces: also list ``/healthz`` in the middleware's exempt paths."""
    exempt = tuple({*identity_settings.oidc.exempt_paths, "/healthz"})
    oidc = identity_settings.oidc.model_copy(update={"exempt_paths": exempt})
    return identity_settings.model_copy(update={"oidc": oidc})


async def _dispatch_pending_agent_runs_once(
    *, run_store: Any, enqueuer: DbosAgentRunEnqueuer, approval_checkpoints: Any | None
) -> int:
    """Re-enqueue each exact unclaimed durable handoff once per scan."""

    dispatched = 0
    for request in await run_store.load_pending_workflow_requests():
        await enqueuer.enqueue(request)
        dispatched += 1
    if approval_checkpoints is not None:
        for request in await approval_checkpoints.load_pending_resume_requests():
            await enqueuer.enqueue_approval_resume(request)
            dispatched += 1
    return dispatched


async def _run_agent_dispatch_reconciler(
    *, run_store: Any, enqueuer: DbosAgentRunEnqueuer, approval_checkpoints: Any | None
) -> None:
    """Continuously close commit-before-DBOS-enqueue crash windows."""

    while True:
        try:
            await _dispatch_pending_agent_runs_once(
                run_store=run_store,
                enqueuer=enqueuer,
                approval_checkpoints=approval_checkpoints,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Content, arguments and identifiers are intentionally absent.
            _logger.error(
                "canonical Run dispatch reconciliation failed (%s)",
                type(exc).__name__,
            )
        await asyncio.sleep(1.0)


def _lifespan_factory(
    config_hash: str,
    engines: list[sa.Engine],
    rebac_client: Any | None = None,
    *,
    on_startup: Callable[[], Awaitable[None]] | None = None,
    inference_lifecycle: Any | None = None,
    approval_gate: Any | None = None,
    run_dispatch_reconciler: Callable[[], Awaitable[None]] | None = None,
    frontier_evidence_refresher: Callable[[], Awaitable[None]] | None = None,
    chain_anchor_refresher: Callable[[], Awaitable[None]] | None = None,
) -> Any:
    """``on_startup`` (NE-T2X-4) runs ONCE, after the config_hash assertion,
    before the app starts serving — the composition-root hook
    ``platform.extraction.emit_mode_event`` rides (only when extraction is
    composed). Default ``None`` keeps every existing call site byte-identical."""

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Startup: the composed hash was frozen eagerly at factory time; assert it.
        assert get_config_hash() == config_hash
        if on_startup is not None:
            await on_startup()
        lifecycle_task: asyncio.Task[None] | None = None
        if inference_lifecycle is not None and inference_lifecycle.config.targets:
            lifecycle_task = asyncio.create_task(
                inference_lifecycle.run_reconciler(approval_gate=approval_gate),
                name="inference-lifecycle-reconciler",
            )
        # NE-SELECT-LATENCY: re-measure frontier liveness on a timer so an
        # admitted turn finds CURRENT evidence already on the chain and never
        # pays a warm — or an O(chain) verify — between its own hops. Purely an
        # optimisation: if this task dies the next authorize re-probes inline
        # and, for a genuinely dark lane, still refuses by name.
        frontier_task: asyncio.Task[None] | None = None
        if frontier_evidence_refresher is not None:
            refresher = frontier_evidence_refresher

            async def _refresh_frontier_evidence() -> None:
                await refresher()

            frontier_task = asyncio.create_task(
                _refresh_frontier_evidence(),
                name="frontier-evidence-refresher",
            )
        # NE-SELECT-LATENCY: the ONLY place the full audit chain is walked once
        # evidence reads verify through the process's anchored verifier. Losing
        # this task costs LATENCY, never integrity: the anchor ages out and
        # ne.audit.AnchoredChainVerifier refuses it, putting the inline
        # O(chain) verify_chain back on the reading path — loudly, and exactly
        # as it behaved before the anchor existed.
        chain_anchor_task: asyncio.Task[None] | None = None
        if chain_anchor_refresher is not None:
            anchor_refresher = chain_anchor_refresher

            async def _refresh_chain_anchor() -> None:
                await anchor_refresher()

            chain_anchor_task = asyncio.create_task(
                _refresh_chain_anchor(),
                name="audit-chain-anchor-refresher",
            )
        run_dispatch_task: asyncio.Task[None] | None = None
        if run_dispatch_reconciler is not None:
            reconciler = run_dispatch_reconciler

            async def _run_dispatches() -> None:
                await reconciler()

            run_dispatch_task = asyncio.create_task(
                _run_dispatches(),
                name="agent-run-dispatch-reconciler",
            )
        try:
            yield
        finally:
            if frontier_task is not None:
                frontier_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await frontier_task
            if chain_anchor_task is not None:
                chain_anchor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await chain_anchor_task
            if run_dispatch_task is not None:
                run_dispatch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_dispatch_task
            if lifecycle_task is not None:
                lifecycle_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lifecycle_task
            if inference_lifecycle is not None:
                await inference_lifecycle.aclose()
            # NE-087 (O-4, FIX-2): close the retained OpenFGA client (its owned async
            # HTTP session) BEFORE engine disposal. Default-off (no client) is a no-op.
            try:
                if rebac_client is not None:
                    await rebac_client.close()
            finally:
                for engine in engines:
                    engine.dispose()

    return _lifespan
