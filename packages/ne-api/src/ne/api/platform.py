# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The shared composition root — ``build_platform`` (spec §3.2 steps 1-11).

D2 (NE-103c): the ne-api spec §3.2 lifespan steps 1-11 are IDENTICAL in both the
``ne-api`` process (``ne.api.app.create_app``) and the durable ``ne-worker``
process (``ne.worker.main``). Extracting them here — ONE ``build_platform`` called
by BOTH — is the strongest structural guarantee that the worker cannot silently
compose a WEAKER governed stack than the HTTP surface: there is exactly one recipe,
frozen once per process, and both roots run it.

``build_platform`` performs the normative order VERBATIM from ``create_app``:
secrets → ``freeze_platform_config`` (ONCE, before any emitter) → the ONE runtime
engine → the ONE audit sink → the ONE approval gate → the NE-070H shared guardrail
state → permissions/C7 acl filter → the ONE acl-trimmed retriever → gateway → the
ONE governed executor (NE-CKD1 recipe, sink+gate REQUIRED) → the session store. The
eval / flywheel / kpi READ stores stay in ``create_app`` (ne-api-only surfaces); the
worker needs none of them.

``AppSeams`` lives here (moved from ``app.py``, which re-exports it so
``from ne.api.app import AppSeams`` still works) because both roots inject the same
network-edge seams in tests.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import anyio.to_thread
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from ne.api.extraction import ExtractionRuntime
    from ne.api.inference_lifecycle import InferenceLifecycleService
    from ne.api.model_routing import ModelRouteStore
    from ne.api.network_egress import EgressPostureStore
    from ne.api.verified_curation import VerifiedCurationContext
    from ne.api.verified_tools import OntologyReadState
    from ne.brain_graph.config import BrainGraphSettings
    from ne.brain_graph.construct.runner import M365GraphSyncHost
    from ne.brain_graph.extraction.apply import ExtractionApplyStore
    from ne.brain_graph.extraction.job import ChunkSource, ExtractionScanStore
    from ne.brain_graph.read import EntityGraph, GraphReadStore
    from ne.brain_ontology.config import OntologySettings
    from ne.brain_ontology.gateway import ValidatingTripleStore
    from ne.brain_ontology.verified.answers import VerifiedReadStore
    from ne.brain_ontology.verified.selected_release import (
        SelectedOxigraphRelease,
        SelectedReleaseVerifiedReader,
    )
    from ne.brain_ontology.verified.templates import TemplateRegistry
    from ne.connect.authority_attempts import AuthorityAttemptReader
    from ne.connect.cdc import CdcSinkService
    from ne.connect.m365.config import M365ConnectorSettings
    from ne.connect.runner import ConnectSyncSettings
    from ne.connect.sql.config import SqlConnectorSettings
    from ne.contracts.inference import InferenceTargetOperator
    from ne.gateway.egress import EgressSettings
    from ne.permissions_rebac.config import RebacSettings
    from ne.permissions_rebac.ports import OpenFgaTupleClient
    from ne.runtime.dispatch import GovernedSubAgentDispatcher
    from ne.runtime.verified_router import VerifiedRouter

from ne.api.agent_catalog import (
    AgentCatalog,
    AgentDependencySnapshot,
    effective_agent_tool_grants,
    reconcile_agent_catalog,
)
from ne.api.agent_execution_caps import enforced_agentic_caps
from ne.api.agent_manifest import (
    AgentManifestDefinition,
    LoadedAgentManifest,
    load_agent_manifest,
)
from ne.api.agent_preflight import (
    AgentCapabilityPreflightService,
    AgentSkillDetail,
    AgentSkillDetailProvider,
)
from ne.api.agent_skills import AgentSkillService
from ne.api.connect_status import configured_sync_resources, sync_source_read_health
from ne.api.connect_sync import (
    build_connect_sync_request_bindings,
    sync_request_tool_name,
)
from ne.api.graph_tools import (
    GRAPH_PATH_ARG_POLICY,
    GRAPH_PATH_TOOL_NAME,
    EntityGraphPathRead,
    build_graph_path_binding,
)
from ne.api.knowledge_source_evidence import (
    KnowledgeSourceEvidenceAuthor,
    current_existing_knowledge_read,
)
from ne.api.posture import effective_egress
from ne.api.serving import ServingStatusService
from ne.api.settings import ApiSettings
from ne.api.source_authority_descriptors import (
    GovernedLocalT3SourceAuthorityDescriptor,
    M365SourceAuthorityDescriptor,
    project_governed_local_t3_source_authority,
    project_m365_source_authority,
)
from ne.api.tool_transit import ToolTransitObservingSink
from ne.api.tools import (
    KNOWLEDGE_SEARCH_ARG_POLICY,
    SKILLS_SEARCH_ARG_POLICY,
    SKILLS_VIEW_ARG_POLICY,
    TOOL_NAME,
    TRACE_ARG_POLICY,
    TRACE_TOOL_NAME,
    CurrentSourceAuthorizedKnowledgeRead,
    KnowledgeSearchEvidence,
    build_chat_bindings,
    build_skill_bindings,
    build_trace_bindings,
)
from ne.api.turn_relay import TurnEnqueuer, TurnFrameReader
from ne.api.turn_wire import PROTECTED_TRANSPORT_GENERATION
from ne.api.workflows import (
    MERIDIAN_CUSTOMERS_TOOL,
    MERIDIAN_DISASTER_DECLARATIONS_TOOL,
    MERIDIAN_DISPUTES_DIRECTORY_TOOL,
    MERIDIAN_LOANS_TOOL,
    MERIDIAN_NOTES_TOOL,
    MERIDIAN_OPEN_DISPUTES_TOOL,
    MERIDIAN_OVERDUE_CREDIT_TOOL,
    MERIDIAN_PAYMENTS_TOOL,
)
from ne.audit import (
    create_engine_with_timeouts,
    ensure_audit_schema,
    ensure_transactional_sqlite,
)
from ne.audit.config import AuditSettings
from ne.audit.factory import create_audit_sink
from ne.brain.lineage import SqlAlchemyDocumentLineageStore
from ne.brain.pipeline import RetrievalPipeline
from ne.brain.retriever import HybridRetriever, QuerySparseEncoder
from ne.brain.source_health import DegradedSourcesProvider, SourceHealthSnapshot
from ne.brain.trimming import PrincipalAclFilter
from ne.contracts.frozen import FrozenDict
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import ChunkStore, Embedder, KnowledgeRead, Reranker
from ne.contracts.models import CompletionRequest, ModelGateway
from ne.contracts.plugins import PluginRegistry
from ne.contracts.runtime import ToolSpec
from ne.contracts.secrets import SecretStore
from ne.contracts.source_authority import (
    AuthorizedContinuationTokenCodec,
    CurrentSourceAuthorityReader,
    CurrentSourceAuthorityReceipt,
    ProtectedSourceExecutionWriter,
    ProtectedSourceOutputWriter,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    stamp_from_receipt,
)
from ne.contracts.types import ChatMessage, Lane, Principal
from ne.eval.config import EvalSettings
from ne.flywheel.config import FlywheelSettings
from ne.gateway.config import GatewaySettings, TaskRoute
from ne.govern import is_valid_email
from ne.govern.config import GovernSettings, ToolArgPolicy
from ne.govern.guardrails.rate import SlidingWindowCounter
from ne.govern.guardrails.spend import SpendLedger
from ne.identity.agents.config import AgentIdentitySettings
from ne.identity.agents.factory import create_agent_credential_service
from ne.identity.agents.guardrail import AgentScopeGuardrail
from ne.identity.agents.service import AgentCredentialService
from ne.identity.config import IdentitySettings
from ne.obs.config import ObsReportSettings
from ne.permissions.config import PermissionsSettings
from ne.permissions.expand import PrincipalExpander
from ne.permissions.factory import build_permissions_stack
from ne.permissions.store import PrincipalStore
from ne.permissions.sync import PrincipalSyncService
from ne.runtime.agent import AgentExecutionBinding
from ne.runtime.agent_capabilities import (
    AgentCapabilityInventory,
    AgentSourceGrant,
    AgentToolGrant,
    ConnectorCapabilityBinding,
    StaticCapabilityVisibility,
    ToolCapabilityBinding,
    project_agent_capabilities,
)
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import (
    current_run_principal_id,
    current_run_requested_model,
    current_run_requested_model_lane,
    current_run_trace_id,
)
from ne.runtime.factory import (
    create_agent_execution_binding,
    create_approval_gate,
    create_shared_guardrail_state,
    create_subagent_dispatcher,
    create_tool_executor,
    freeze_platform_config,
)
from ne.runtime.grounding import GroundingPolicy
from ne.runtime.middleware import (
    GovernedToolExecutor,
    current_tool_approval,
    current_tool_principal,
)
from ne.runtime.private_payloads import (
    RunPrivatePayloadCipher,
    StaticPrivatePayloadKeys,
    decode_private_payload_b64,
)
from ne.runtime.reference_actions import (
    CrmContactLookupClient,
    CrmLookupClient,
    CrmNoteClient,
    DraftMailClient,
    SendMailClient,
    TicketWriteClient,
)
from ne.runtime.run_store import AgentRunStore, ensure_run_schema
from ne.runtime.runner import BoundedAgenticAdapter
from ne.runtime.scheduler import ManualJobRequestStore
from ne.runtime.source_results import (
    MissingAuthenticatedContextError,
    ProtectedSourceResultGuard,
    RunStoreProtectedSourceExecutionWriter,
    RunStoreProtectedSourceOutputWriter,
    RunStoreSourceBoundResultResolver,
    UnavailableSourceBoundResultResolver,
    require_authenticated_context,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.runtime.tools import ToolBinding
from ne.runtime.workflow_execution_profile import (
    MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
    MERIDIAN_B2_CURRENT_SOP_PROFILE,
    MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
    MERIDIAN_S9_OVERDUE_CREDIT_PROFILE,
    WorkflowExecutionProfile,
)
from ne.secrets.config import SecretsSettings
from ne.secrets.factory import create_secret_store
from ne.skills.config import SkillsSettings
from ne.skills.registry import SkillPin, SqlAlchemySkillRegistry, ensure_skills_schema
from ne.skills.substrate import PydanticAiSkillSubstrate
from ne.skills.tools import SEARCH_TOOL_NAME as SKILLS_SEARCH_TOOL_NAME
from ne.skills.tools import VIEW_TOOL_NAME as SKILLS_VIEW_TOOL_NAME

_LOG = logging.getLogger(__name__)

#: NE-RERANK-FANOUT: the hard ceiling on how many candidates ONE search may send
#: through the reranker, whatever the caller's ``top_k`` and whatever over-fetch
#: any wrapper above the pipeline has already applied. 40 is the
#: documented pool; ``ne.brain.reranker``'s own table was measured at
#: 20 (``retrieval_top_k=5 x candidate_multiplier=4``). Cost and latency are
#: LINEAR in this number for every rerank backend — CPU TEI, GPU vLLM or a
#: gateway LLM — so it is the one knob that bounds the stage.
RERANK_CANDIDATE_CEILING = 40

#: NE-RERANK-FANOUT reconciled the two stacked over-fetches to bound RERANK
#: work (authorization headroom 2 x rerank pool 4 = the documented 40-candidate
#: pool on the default Ask). When the rerank stage is NOT composed (empty
#: ``tei_rerank_url`` — the live configuration once CPU reranking is removed)
#: there is no rerank work to bound, and the smaller window has a measured
#: behavioural cost on a source-bearing corpus: at ``top_k=5`` a 75%-denial
#: evaluator fills 3 rows from a 10-row scan where the pre-fanout 40-row scan
#: filled all 5. So the bare-retriever path keeps the pre-NE-RERANK-FANOUT
#: authorization headroom instead of inheriting the reranker's reconciled 2.
RERANKED_SCAN_MULTIPLIER = 2
BARE_RETRIEVER_SCAN_MULTIPLIER = 8

# NE-ACTION-WIRE: the 3 NE-050 reference write-action tool names (mirrors the
# *_definition factories in ne.runtime.reference_actions — module-level
# constants here so the guardrail-policy merge and the composition block below
# never risk a typo'd literal diverging between the two sites).
_CRM_NOTE_TOOL = "write.internal.crm.note"
_TICKET_UPDATE_TOOL = "write.external.ticket.update"
_EMAIL_DRAFT_TOOL = "draft.email.create"
# NE-CS-SEND: the REAL governed Tier-2 send of an existing draft.
_EMAIL_SEND_TOOL = "write.external.email.send"
_CRM_LOOKUP_TOOL = "read.external.crm.account"
# NE-CS-LOOKUP: the Tier-0 contact read that turns a customer NAME served by
# governed graph evidence into the outreach recipient (recorded email) and the
# CRM note target (parent Account id) — both READ from the system of record,
# never carried in the prompt.
_CRM_CONTACT_TOOL = "read.external.crm.contact"
#: NE-S5-INTERNAL-CRM: the three CRM tools share ONE connection identity, and
#: it now honestly names the INTERNAL mirror (the synced connector documents in
#: the chunk store + the platform's own note ledger) — no live Salesforce leg
#: exists behind them any more. These bind only when ``_CRM_NOTE_TOOL`` is
#: selected and the internal clients composed, so their registration IS the
#: staging proof and no M365 authority gate applies to them.
_INTERNAL_CRM_TOOLS = frozenset({_CRM_NOTE_TOOL, _CRM_LOOKUP_TOOL, _CRM_CONTACT_TOOL})
_INTERNAL_CRM_CONNECTION = "internal:crm-mirror"
#: The SQL write-action allowlist (SqlWriteClient's ``allowed_writes``) — matches
#: ``ticket_update_definition``'s ``perform`` exactly: ``table='tickets'``, key
#: ``'id'``, fields ``status``/``public_reply``. The key column MUST be present
#: in the per-table allowlist (SqlWriteClient rejects a key outside it).
_TICKET_ALLOWED_WRITES: dict[str, tuple[str, ...]] = {"tickets": ("id", "status", "public_reply")}
_MERIDIAN_SOURCE_ID = "meridian-servicing"
_M365_SOURCE_SYSTEM = "m365-sharepoint"
_MERIDIAN_AGENT_ID = "agt_01k3meridiananalyst000000000"
_MERIDIAN_LOCAL_T3_SOURCE_ID = "meridian-governed-local-t3"
_MERIDIAN_WORKFLOW_TOOLS = frozenset(
    {
        MERIDIAN_CUSTOMERS_TOOL,
        MERIDIAN_LOANS_TOOL,
        MERIDIAN_PAYMENTS_TOOL,
        MERIDIAN_NOTES_TOOL,
        MERIDIAN_DISPUTES_DIRECTORY_TOOL,
        MERIDIAN_OPEN_DISPUTES_TOOL,
        MERIDIAN_OVERDUE_CREDIT_TOOL,
        MERIDIAN_DISASTER_DECLARATIONS_TOOL,
    }
)
#: The Keycloak group that holds the Meridian servicing-operations capability
#: surface. Already the narrowing used by the local-T3 workflow tools and by the
#: two deterministic workflow Agents in ``deploy/agents/registry.toml``
#: (``groups = ["ops"]``); the pilot ``ops`` persona carries it and the
#: ``meridian-frontline`` persona does not (see
#: ``ne.cli.demo.live._MERIDIAN_OPS_GROUPS`` / ``_MERIDIAN_RESTRICTED_GROUPS``
#: and the committed ``ne-pilot`` realm).
_MERIDIAN_OPS_GROUP = "ops"
#: Tools whose composed static visibility is NARROWED to
#: ``_MERIDIAN_OPS_GROUP`` rather than admitted to every authenticated
#: principal. ``draft.email.create`` is a Tier-0 action -- ``tiers.rego``'s
#: ``draft.`` class, which ``ne.actions.email_draft`` states in its own module
#: docstring and declares at its binding -- so it is AUTONOMOUS: no approval
#: gate fires on it at all, and it nonetheless writes a REAL unsent M365 draft.
#: That is exactly why the persona gate has to be this rule: the Rego PDP
#: classifies on the action NAME and never reads principal groups, so for the
#: Tier-2 send approval gates EXECUTION and nothing else gates who may hold the
#: capability -- while for this Tier-0 draft nothing gates it downstream at all.
#:
#: The Meridian ``approver`` group is deliberately NOT admitted here. The
#: restricted operational-graph ACL (``ne.meridian_seed.slice._operational_acl``)
#: admits ``["ops", "approver"]``, but that is a knowledge READ ACL, not a write
#: grant. Separation of duties is now enforced on the decide path itself
#: (``ne.runtime.approvals.refuse_self_approval`` -- the requester on an approval
#: row can never be its approver), so this exclusion is defence in depth: it
#: keeps the approver persona from ever ORIGINATING the external write it would
#: then be refused from deciding.
_MERIDIAN_OPS_ONLY_TOOLS = frozenset(
    _MERIDIAN_WORKFLOW_TOOLS | {_EMAIL_DRAFT_TOOL, _EMAIL_SEND_TOOL}
)
_MERIDIAN_TOOL_LABELS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        SKILLS_VIEW_TOOL_NAME: ("View an activated Skill", "read"),
        TOOL_NAME: ("Search Meridian servicing knowledge", "read"),
        GRAPH_PATH_TOOL_NAME: ("Read governed Meridian graph paths", "read"),
        _EMAIL_DRAFT_TOOL: ("Create a reviewable email draft", "write"),
        _EMAIL_SEND_TOOL: ("Send an approved outreach email", "write"),
        _CRM_CONTACT_TOOL: ("Look up a CRM contact by name", "read"),
        # NE-SF-STAGING: the other two Salesforce grants were absent from this
        # map, so even a fully staged Salesforce leg projected them as "granted,
        # not eligible" while the executor ran them fine — the console lying in
        # the honest direction is still lying.
        _CRM_LOOKUP_TOOL: ("Look up a CRM account by name", "read"),
        _CRM_NOTE_TOOL: ("Record a CRM contact note", "write"),
        MERIDIAN_CUSTOMERS_TOOL: ("Read governed Meridian customers by county", "read"),
        MERIDIAN_LOANS_TOOL: ("Read governed Meridian auto loans by customer", "read"),
        MERIDIAN_PAYMENTS_TOOL: ("Read governed Meridian scheduled payments", "read"),
        MERIDIAN_NOTES_TOOL: ("Read governed Meridian servicing notes", "read"),
        MERIDIAN_DISPUTES_DIRECTORY_TOOL: (
            "Read the governed Meridian dispute directory",
            "read",
        ),
        MERIDIAN_OPEN_DISPUTES_TOOL: ("Read governed Meridian open disputes", "read"),
        MERIDIAN_DISASTER_DECLARATIONS_TOOL: (
            "Read governed FEMA disaster declarations",
            "read",
        ),
        # S9: granted to both Meridian Overdue Credit Review agents. A granted
        # tool absent from this map renders "Granted, not currently eligible"
        # on the console while executing fine (the NE-SF-STAGING lesson above)
        # — for the agentic S9 agent that unlabelled grant would be its ONLY
        # tool.
        MERIDIAN_OVERDUE_CREDIT_TOOL: (
            "Read governed Meridian overdue provisional-credit disputes",
            "read",
        ),
    }
)


@dataclass(frozen=True)
class WriteActionClients:
    """The 3 injected write-action connector clients (NE-ACTION-WIRE).

    Narrow structural Protocols from ``ne.runtime.reference_actions`` — tests
    inject fakes; production is built by :func:`_build_write_action_clients`.
    """

    crm: CrmNoteClient
    ticket: TicketWriteClient
    draft: DraftMailClient
    #: NE-CS-SEND: the send leg. Defaulted to ``None`` (same rationale as ``lookup``)
    #: so every existing 3-fake test construction stays valid; ``None`` means the
    #: governed send tool is simply not registered.
    send: SendMailClient | None = None
    #: NE-WORKFLOWS: the CRM READ leg that resolves a write target by name. Defaulted
    #: to ``None`` so every existing ``WriteActionClients(...)`` construction (tests
    #: injecting the 3 write fakes) stays valid; ``None`` simply means the Tier-0
    #: lookup tool is not registered, and a workflow then withholds every write for
    #: want of a resolved target rather than inventing one.
    lookup: CrmLookupClient | None = None
    #: NE-CS-LOOKUP: the Tier-0 CONTACT read (name -> recorded email + parent
    #: Account id). Same rationale and default as ``lookup``; production binds
    #: the same ``InternalCrmLookupClient`` instance to both seams
    #: (NE-S5-INTERNAL-CRM).
    contact_lookup: CrmContactLookupClient | None = None


@dataclass(frozen=True)
class _WriteActionEnvSettings:
    """The 3 write-action connector settings, resolved from env BEFORE freeze.

    NE-ACTION-WIRE / Codex C1: resolved (and stamped into ``extra_modules``) by
    :func:`_load_write_action_env_settings` *before* ``freeze_platform_config``
    runs, so the frozen ``config_hash`` pins the actual write DESTINATIONS — the
    SQL connection URL, the Salesforce instance/api_version, the M365 tenant —
    not merely whether ``write_actions_enabled`` is set. Without this, two
    deployments that differ only in WHERE writes land (a different SQL DSN, a
    different Salesforce org, a different M365 tenant) would freeze to the
    IDENTICAL config_hash, so an audit / configuration-drift check could not
    tell them apart. Consumed post-freeze by :func:`_build_write_action_clients`
    to construct the real clients — settings resolution and client/engine
    construction are deliberately split so the hash-affecting part runs before
    the freeze and the I/O-performing part runs after it (the same ordering
    every other conditionally-stamped module in this file already follows).
    """

    sql: SqlConnectorSettings
    m365: M365ConnectorSettings


@dataclass(frozen=True)
class _SelectedWriteActionEnvSettings:
    """Connector settings for only the explicitly selected reference actions.

    NE-S5-INTERNAL-CRM: there is no Salesforce leg any more — the CRM tools are
    served by the internal clients over the platform's own stores and need no
    connector settings of their own.
    """

    sql: SqlConnectorSettings | None = None
    m365: M365ConnectorSettings | None = None


@dataclass(frozen=True)
class _SelectedWriteActionClients:
    """Connector clients built only for explicitly selected reference actions."""

    crm: CrmNoteClient | None = None
    ticket: TicketWriteClient | None = None
    draft: DraftMailClient | None = None
    send: SendMailClient | None = None
    lookup: CrmLookupClient | None = None
    contact_lookup: CrmContactLookupClient | None = None


def _sole_configured_mailbox(api_settings: ApiSettings) -> str | None:
    """The deployment default mailbox for the email actions, or ``None``.

    Exactly one configured allowlisted mailbox IS the default (the model omits
    ``mailbox`` and the action uses it — NE-162 keeps mailbox addresses out of
    frontier-visible text, so requiring the model to quote one is a trap). Zero
    or several configured: no default, an omitted mailbox fails closed in the
    action itself.
    """
    mailboxes = api_settings.email_draft_allowed_mailboxes
    return mailboxes[0] if len(mailboxes) == 1 else None


class _AllowedDraftMailClient:
    """Fail closed unless a draft targets an exact configured mailbox."""

    def __init__(self, inner: DraftMailClient, allowed_mailboxes: Sequence[str]) -> None:
        self._inner = inner
        self._allowed = frozenset(allowed_mailboxes)

    async def create_draft(self, *, mailbox: str, subject: str, body: str, to: str) -> str:
        normalized = mailbox.strip().casefold()
        if normalized not in self._allowed:
            raise ValueError("refusing to create a draft outside the configured mailbox allowlist")
        return await self._inner.create_draft(
            mailbox=normalized,
            subject=subject,
            body=body,
            to=to,
        )


@dataclass(frozen=True)
class AgentCapabilityMetadata:
    """The ONE producer of the safe capability facts PRE-011A cannot derive.

    ``project_agent_capabilities`` intersects; it invents nothing. Four of its
    inputs are therefore facts only a composition root can author, and none of
    them exists anywhere in this repository today:

    * a Tool's safe display label — ``ToolSpec`` carries a ``description`` written
      for the MODEL, not a label written for a person, and ``ToolBinding`` carries
      no label at all;
    * a Tool's read/write class — ``tier_for_action`` classifies an action into an
      approval TIER, which is not the same question (``draft.`` is tier 0 and is
      plainly not a read);
    * the connection identity a Tool or Connector acts through, and its sanitized
      credential-owner label;
    * per-Tool and per-Connector STATIC visibility, which is deny-default and is a
      different mechanism from the runtime PDP.

    Composing this object ABSENT (``None`` on :class:`AppSeams`) is the honest
    state for a deployment that cannot answer. Composing it PRESENT-BUT-EMPTY
    would be a different and false claim -- "this principal is eligible for
    nothing" -- because 011A drops any Tool whose binding or visibility is
    missing. That distinction is the whole point of the seam, so the two are not
    collapsed into one default.

    ``tool_health`` is the exception and is deliberately a plain mapping: 011A
    already maps an absent entry to the ``"unknown"`` health value, so an empty
    mapping here is a producer ANSWERING "not known", not a producer declining.

    The composed ``_degraded_sources_provider`` is deliberately NOT reused as that
    health signal. It answers with a set of degraded stamps, so "absent from the
    set" collapses healthy and never-synced into one value (see
    ``connect_status.degraded_sync_sources``); reading absence as ``True`` would
    fabricate green for a lane that has never run, which is precisely the failure
    ``agent_capabilities._health``'s three-valued answer exists to prevent.
    """

    tool_bindings: tuple[ToolCapabilityBinding, ...] = ()
    static_tool_visibility: Mapping[str, StaticCapabilityVisibility] = field(default_factory=dict)
    connector_bindings: tuple[ConnectorCapabilityBinding, ...] = ()
    static_connector_visibility: Mapping[str, StaticCapabilityVisibility] = field(
        default_factory=dict
    )
    tool_health: Mapping[str, bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Snapshot the caller-owned mappings; ``frozen=True`` does not.

        A frozen dataclass freezes the ATTRIBUTE BINDINGS, not what they point
        at.  Composition hands these three mappings in as ordinary dicts, and a
        caller that keeps its reference can keep editing them after the provider
        is built -- flipping a Tool's static visibility from deny-default to
        ``all_authenticated``, or a health row from absent to ``True`` -- and the
        already-composed provider would project the new values.  That is an
        authorization surface mutating after composition, so the snapshot is a
        real control and not tidiness.  ``MappingProxyType`` over a COPY: the
        copy defeats the retained reference, the proxy defeats anyone who later
        reaches through this object.
        """
        for name in ("static_tool_visibility", "static_connector_visibility", "tool_health"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True)
class _ComposedAgentCapabilityInventory:
    """The production PRE-011A read, bound to one frozen composition.

    Holds the REAL registered Tool specs and the REAL Agent definitions, and runs
    the REAL ``project_agent_capabilities``. It answers ``None`` -- "this
    deployment cannot answer" -- in exactly three cases, none of which is
    "the answer is empty":

    * no :class:`AgentCapabilityMetadata` producer is composed at all;
    * the requested Agent is not in the composed definitions;
    * the requested version/definition hash is not the composed one, so the
      grants this object could build would describe a DIFFERENT immutable Agent.

    ``config_hash`` is the frozen platform hash this inventory was composed under.
    It is captured after ``freeze_platform_config`` and carried so a composition
    test can prove there is ONE composed hash rather than a second one frozen at a
    different point in ``build_platform`` -- the ordering bug 009A's key parsing
    comment names, in the one other seam composed this late.
    """

    definitions: Mapping[str, AgentManifestDefinition]
    tool_specs: tuple[ToolSpec, ...]
    metadata: AgentCapabilityMetadata | None
    config_hash: str
    effective_tool_grants: Mapping[str, tuple[str, ...]]

    async def __call__(
        self,
        *,
        agent_id: str,
        agent_version: int,
        agent_definition_hash: str,
        principal: Principal,
    ) -> AgentCapabilityInventory | None:
        metadata = self.metadata
        if metadata is None:
            return None
        definition = self.definitions.get(agent_id)
        if definition is None:
            return None
        if (definition.version, definition.definition_hash) != (
            agent_version,
            agent_definition_hash,
        ):
            return None
        identity = {
            "agent_id": agent_id,
            "agent_version": agent_version,
            "agent_definition_hash": agent_definition_hash,
        }
        return project_agent_capabilities(
            **identity,
            principal=principal,
            # The EXACT frozen executor catalog the governed loop runs, never a
            # second list: a Tool this deployment cannot execute must not appear
            # as an eligible capability.
            registered_specs=self.tool_specs,
            tool_bindings=metadata.tool_bindings,
            # The Agent's OWN declared grants, read from the authoritative
            # manifest definition and bound to this exact identity.
            agent_tool_grants=tuple(
                AgentToolGrant(**identity, tool_name=name)
                for name in self.effective_tool_grants[agent_id]
            ),
            static_tool_visibility=metadata.static_tool_visibility,
            composed_tool_health=metadata.tool_health,
            connector_bindings=metadata.connector_bindings,
            agent_source_grants=tuple(
                AgentSourceGrant(**identity, source_id=grant.source_id, tiers=grant.tiers)
                for grant in definition.knowledge_sources
            ),
            static_connector_visibility=metadata.static_connector_visibility,
        )


@dataclass(frozen=True)
class AppSeams:
    """Injectable seams for tests/embedding — every field defaults to ``None``.

    Injecting the model/embedder/qdrant edges keeps the governed spine REAL while
    faking only the network edges. A ``gateway`` seam suppresses ``LiteLLMGateway``
    construction (avoiding the heavy litellm import) — but ``gateway_settings``
    still freezes into the hash.
    """

    #: PRE-011E -- the composition-authored safe capability metadata producer.
    #: ``None`` (the default) means this deployment has no producer for the safe
    #: labels, read/write classes, connection identities and static visibility
    #: PRE-011A cannot derive, so the Agents preflight answers "capabilities
    #: unavailable" rather than inventing them or reporting an empty grant.
    agent_capability_metadata: AgentCapabilityMetadata | None = None
    #: PRE-009A A3 -- an explicit "this deployment runs Agent Run private payloads"
    #: signal, independent of the environment. False by default so a composition
    #: that never had a deployment stays dormant; 009C sets it when it activates
    #: the routes. Setting it makes both key variables MANDATORY.
    agent_private_payload_configured: bool = False
    identity_settings: IdentitySettings | None = None
    runtime_settings: RuntimeSettings | None = None
    govern_settings: GovernSettings | None = None
    audit_settings: AuditSettings | None = None
    permissions_settings: PermissionsSettings | None = None
    gateway_settings: GatewaySettings | None = None
    eval_settings: EvalSettings | None = None
    flywheel_settings: FlywheelSettings | None = None
    secrets_settings: SecretsSettings | None = None
    obs_settings: ObsReportSettings | None = None
    gateway: ModelGateway | None = None
    embedder: Embedder | None = None
    sparse_encoder: QuerySparseEncoder | None = None
    chunk_store: ChunkStore | None = None
    #: The rerank edge (SPEC §3.1). Tests inject a fake so the REAL
    #: ``RetrievalPipeline`` composition runs with only the network edge faked;
    #: production defaults to ``None`` → build_platform constructs the real
    #: ``TEIReranker`` from ``api_settings.tei_rerank_url`` (empty URL ⇒ bare
    #: retriever + logged warning).
    reranker: Reranker | None = None
    plugin_registry: PluginRegistry | None = None
    #: Extra governed tool bindings appended to the chat menu (tests exercise the
    #: HITL path with a tier-2 tool). Production is byte-identical when defaulted.
    extra_tool_bindings: tuple[ToolBinding, ...] = ()
    #: Extra tool arg policies merged into ``guardrails.tools`` BEFORE the hash
    #: freezes (so a tier-2 test tool is not fail-closed-rejected). Defaults to none.
    extra_tool_policies: Mapping[str, ToolArgPolicy] | None = None
    #: NE-103c relay injection (tests exercise enqueue mode WITHOUT real DBOS by
    #: injecting a fake enqueuer/reader). Either being set forces the relay path.
    turn_enqueuer: TurnEnqueuer | None = None
    turn_reader: TurnFrameReader | None = None
    #: NE-085c Brain-T3 verified-answers config. Defaults to ``None`` → build_platform
    #: resolves ``OntologySettings()`` (env-driven); verified answers stay OFF unless
    #: ``verified_answers_enabled`` is set, keeping the config_hash + tool menu
    #: byte-identical to pre-085c by default.
    ontology_settings: OntologySettings | None = None
    #: NE-087 (O-4) optional OpenFGA ReBAC module. Defaults to ``None`` → build_platform
    #: resolves ``RebacSettings()`` (env-driven, ``NE_REBAC_*``); ReBAC stays OFF unless
    #: ``enabled`` is set, keeping the config_hash + C7 filter byte-identical by default.
    rebac_settings: RebacSettings | None = None
    #: An injected confined OpenFGA client (the in-memory fake in tests). ``None`` →
    #: ``build_platform`` constructs the real ``OpenFgaHttpClient`` when ReBAC is
    #: enabled. Ignored entirely when ReBAC is disabled (byte-identical default-off).
    rebac_client: OpenFgaTupleClient | None = None
    #: NE-BRAIN-T2-WIRING (Tier-2 graph retrieval). Defaults to ``None`` →
    #: build_platform resolves env-driven ``BrainGraphSettings()``; the graph tier
    #: stays OFF unless ``enabled``, keeping the config_hash + composed retriever
    #: byte-identical to pre-unit by default (the 085c/087 conditional-stamp pattern).
    brain_graph_settings: BrainGraphSettings | None = None
    #: NE-BRAIN-T2-WIRING: an injected trimming-faithful graph read store (the
    #: ``InMemoryGraphStore`` in tests). ``None`` ⇒ the real ``AgeGraphStore`` when
    #: the tier is enabled; ignored entirely when the tier is disabled.
    graph_store: GraphReadStore | None = None
    #: NE-ACTION-WIRE — the injected :class:`WriteActionClients` (tests use fakes
    #: so the full inline HITL round-trip runs over the otherwise-real governed
    #: spine). ``None`` ⇒ ``build_platform`` constructs the real clients from the
    #: connector env settings (the ``rebac_client`` precedent). IGNORED ENTIRELY
    #: when ``ApiSettings.write_actions_enabled`` is ``False`` — byte-identical
    #: default-off (never a ``ValueError`` just because a seam is set while the
    #: legacy flag and ``ApiSettings.write_action_tools`` are both off).
    write_action_clients: WriteActionClients | None = None
    #: NE-T2X-4 (NE-BRAIN-T2-WIRING's extraction pathway). Host-provided chunk
    #: enumeration for the extraction job — the ``ChunkSource`` protocol from
    #: ``ne.brain_graph.extraction.job`` (the ``compose_extraction_job`` CLI
    #: precedent, ``ne.brain_graph.cli``). ``ChunkStore`` (contracts
    #: ``knowledge.py``) has no scan-by-source surface, so a production/live
    #: composition MUST inject one (NE-T2X-6's demo harness) — ``None`` (the
    #: default) is a fail-loud boot ValueError whenever extraction is enabled
    #: (the B-P1-4 no-silently-dead-feature discipline), never a silent no-op.
    extraction_chunk_source: ChunkSource | None = None
    #: NE-SERVING-STATUS — test seam for the live serving-status prober
    #: (GET /v1/models/serving): inject a service with a fake transport/clock so
    #: only the network edge is faked. ``None`` (production) ⇒ ``create_app``
    #: constructs the real :class:`ne.api.serving.ServingStatusService`. NOT
    #: hashed (a probe cache is runtime state, not configuration), and NOT on
    #: :class:`Platform` — ne-worker serves no console read.
    serving_probe: ServingStatusService | None = None
    #: Confined ADR-0021 operator seam. Tests inject a closed fake; production
    #: constructs the strict UDS client only when the boot registry names a socket.
    inference_operator: InferenceTargetOperator | None = None


@dataclass(frozen=True)
class Platform:
    """The governed singletons produced by ``build_platform`` — shared by BOTH roots.

    One recipe, two processes (ne-api + ne-worker). The frozen ``config_hash`` is
    stamped on every ``AuditEvent`` in the process; cross-process ``config_hash``
    parity (ADR-0003 one-platform-hash) is achieved by both roots running this same
    ``build_platform`` over identical composed settings (SPEC §6.2/Q5).
    """

    config_hash: str
    sink: AuditSink
    gate: DurableApprovalGate
    executor: GovernedToolExecutor
    #: The ONE composed retriever shared by BOTH process roots (chat tool binding +
    #: /v1/brain routes). ``KnowledgeRead``-typed: a ``RetrievalPipeline`` (rerank
    #: wrapping the acl-trimmed ``HybridRetriever``) when the rerank URL is set —
    #: the default — else the bare ``HybridRetriever`` (empty-URL degrade).
    retriever: KnowledgeRead
    current_retriever: KnowledgeRead
    session_store: SqlAlchemySessionStore
    gateway: ModelGateway
    runtime_settings: RuntimeSettings
    govern_settings: GovernSettings
    identity_settings: IdentitySettings
    audit_settings: AuditSettings
    api_settings: ApiSettings
    secret_store: SecretStore
    perm_store: PrincipalStore
    expander: PrincipalExpander
    perm_sync: PrincipalSyncService
    #: NE-FIX-SYNC-INVOKERS (P2-20): the resolved permissions config, exposed so the
    #: ne-worker principal-sync cron + `ne permissions sync` build the source via
    #: ``ne.permissions.factory.build_principal_source`` at the SAME interval/mode.
    #: Already unconditionally in ``extra_modules['ne-permissions']`` (line 313), so
    #: surfacing it here is additive and leaves the frozen config_hash byte-identical.
    permissions_settings: PermissionsSettings
    engines: list[sa.Engine]
    runtime_engine: sa.Engine
    audit_engine: sa.Engine
    #: BD-3: the runtime-DB document lineage ledger shared by API reads and
    #: polling/CLI/CDC KnowledgeWriter compositions.
    document_lineage: SqlAlchemyDocumentLineageStore
    ledger: SpendLedger
    counter: SlidingWindowCounter
    #: The NE-036 ``AgentScopeGuardrail`` on ``extra_guardrails`` of the executor —
    #: the worker's cron scheduler rides the SAME rail (NE-CKD1, factory.py:32-33).
    scope_rail: AgentScopeGuardrail
    #: NE-IDENTITY-ROTATION-CRON — the ONE composed NE-036 service, the SAME
    #: instance inside ``scope_rail``, exposed so the ne-worker rotation cron
    #: sweeps the credentials the guardrail authorizes against; previously
    #: reachable only via scope_rail's private attr — the chunk_store
    #: reportPrivateUsage precedent.
    credential_service: AgentCredentialService
    #: NE-IDENTITY-ROTATION-CRON — the resolved NE_AGENT_IDENTITY_* settings; the
    #: rotation cron derives its check cadence from
    #: credentials.rotate_after_seconds. This FIELD is NOT stamped into
    #: extra_modules — AgentIdentitySettings was never hashed and adding this
    #: accessor alone would be byte-identical. The frozen config_hash DOES still
    #: move for this unit, but only because the new ALWAYS-ON
    #: ``write.internal.identity.credential_rotation`` tool's ``ToolArgPolicy``
    #: joins ``guardrails.tools`` in ``_merge_tool_policy`` (the same
    #: unconditional pattern as ``read.trace``/``read.knowledge.search``) — a
    #: real, expected, additive registry change, not a settings-surface one.
    agent_identity_settings: AgentIdentitySettings
    #: PRE-008 metadata-only declarative registry. This has no execution seam;
    #: both service roots compose the same immutable boot snapshot.
    agent_catalog: AgentCatalog
    #: PRE-010D — boot-validated deterministic profiles and their exact Agent-version
    #: bindings. Both API and worker inherit these immutable mappings from this one
    #: composition recipe; no request path constructs or weakens a profile.
    workflow_execution_profiles: Mapping[str, WorkflowExecutionProfile]
    agent_execution_bindings: Mapping[str, AgentExecutionBinding]
    effective_agent_tool_grants: Mapping[str, tuple[str, ...]]
    #: NE-INGEST (P1-4): the ONE composed chunk store, exposed so the ne-worker
    #: connector-sync cron + the ``ne connect sync`` one-shot build the governed
    #: KnowledgeWriter over the SAME store instance (previously reachable only via the
    #: private ``retriever`` attr — reportPrivateUsage/Argus bait + drift-prone).
    chunk_store: ChunkStore
    #: NE-INGEST (P1-4): the resolved connector-sync config (env-driven,
    #: ``NE_CONNECT_SYNC_*``). ``sources=()`` (default) ⇒ no cron jobs, no-op CLI.
    connect_sync_settings: ConnectSyncSettings
    #: Durable, short-lived manual requests consumed by the existing worker cron tick.
    connect_sync_requests: ManualJobRequestStore
    #: NE-084 CDC sink service — ``None`` (byte-identical) unless
    #: ``ApiSettings.cdc_webhook_secret`` is configured. Governs Debezium change
    #: events onto the SAME KnowledgeWriter path as the polling connectors.
    cdc_service: CdcSinkService | None = None
    #: NE-087 (O-4) — the confined OpenFGA client retained for lifecycle. ``None``
    #: (byte-identical default) unless ``RebacSettings.enabled``. Both process roots
    #: (ne-api lifespan + ne-worker shutdown) ``await close()`` it on teardown so the
    #: owned async HTTP session is never leaked (FIX-2).
    rebac_client: OpenFgaTupleClient | None = None
    #: NE-133 — the ONE composed T2 EntityGraph (the SAME instance inside the
    #: GraphRetriever, group_expander=the platform expander). None (byte-identical
    #: default) unless BrainGraphSettings.enabled; /v1/graph/* gate on it.
    entity_graph: EntityGraph | None = None
    #: NE-ONTOLOGY-FLYWHEEL — the governed HITL verified-answer-template curation
    #: seam. ``None`` (byte-identical default) unless verified answers are enabled;
    #: carries the boot dir + the ``config_hash`` projection closure the steward
    #: apply route uses to persist + re-fingerprint + audit a governed template.
    verified_curation: VerifiedCurationContext | None = None
    #: NE-135 (O-5) — the precomputed T3 ontology-read surface (compiled shapes +
    #: quarantine lane + verified setup, over the ONE C12-gated gateway). ``None``
    #: (byte-identical default) unless an explicit T3 store is configured; the ne-api
    #: ontology routes answer 503 ``ontology-disabled`` when it is ``None``. Defaulted
    #: so the ne-worker ``build_platform`` call site is untouched.
    ontology: OntologyReadState | None = None
    #: NE-131 — the COMPOSED gateway settings (already frozen into config_hash under
    #: 'ne-gateway'), surfaced so /v1/posture can report the model-registry lanes.
    #: Trailing defaulted field (the entity_graph precedent) — leaves ne-worker
    #: construction untouched and the frozen config_hash byte-identical.
    gateway_settings: GatewaySettings | None = None
    #: NE-UI-P2-CLEANUP — the SAME boot-frozen EgressSettings instance the live
    #: gateway guard enforces (``build_egress_guard(egress_settings)`` in
    #: ``_default_gateway``). Threaded so /v1/posture reports the ENFORCED egress
    #: policy, never a per-request re-read (§0 law 5 reported==enforced), mirroring
    #: the frozen config_hash semantics. Trailing-defaulted (the gateway_settings
    #: NE-131 precedent) so ne-worker/test construction is untouched; NOT hashed.
    egress_settings: EgressSettings | None = None
    #: NE-AIRGAP-TOGGLE — the durable runtime egress-posture register (audit
    #: engine) and the per-call settings provider the live guard reads. The
    #: provider is the ONE read surface for the EFFECTIVE posture: the guard
    #: enforces it and /v1/posture reports it (reported==enforced). Trailing-
    #: defaulted; NOT hashed — the runtime posture's timeline lives on the audit
    #: chain (``network.egress.changed``), not in config_hash (see the
    #: ne.api.network_egress module docstring for the full justification).
    egress_store: EgressPostureStore | None = None
    egress_settings_provider: Callable[[], EgressSettings] | None = None
    #: NE-MODEL-ROUTING — the durable runtime model-route register (audit engine,
    #: beside the egress posture and for the same reasons). The per-call route
    #: provider built over it is handed to the gateway; this handle is what the
    #: console read and the governed bindings share. NOT hashed — the route's
    #: timeline lives on the audit chain (``model.routing.changed``), not in
    #: config_hash (see the ne.api.model_routing module docstring).
    route_store: ModelRouteStore | None = None
    inference_lifecycle: InferenceLifecycleService | None = None
    #: NE-168 — the demo governed multi-lane delegation dispatcher (3
    #: AgentDefinitions: triage/reason -> private-vllm, adjudicate -> frontier-api
    #: with the NE-161 runtime gate left CLOSED). ``None`` (byte-identical default)
    #: unless ``ApiSettings.demo_delegation`` — the demo_writeback_tool/entity_graph
    #: conditional-stamp precedent. Trailing-defaulted so ne-worker construction is
    #: untouched.
    demo_dispatcher: GovernedSubAgentDispatcher | None = None
    #: NE-WORKFLOWS — the drafting dispatcher for governed workflow runs. ``None``
    #: exactly when ``ApiSettings.workflows_enabled`` is off (the routes 404).
    workflow_dispatcher: GovernedSubAgentDispatcher | None = None
    #: NE-VAF-U4 — the deterministic verified-answer turn pre-step router.
    #: ``None`` (byte-identical default) unless verified answers are enabled
    #: AND >=1 loaded template carries ``[routing]``; built HERE because
    #: ``build_platform`` is the only composition root legally importing BOTH
    #: ``ne.brain_ontology`` and ``ne.runtime`` (C3) — ne-worker inherits it via
    #: this same recipe (one recipe two processes, see line ~177). Deliberately
    #: NOT a config_hash input: the routing table is a pure function of the
    #: registry bytes already fingerprinted under 'ne-brain-ontology-templates'
    #: (line ~458).
    verified_router: VerifiedRouter[Any] | None = None
    #: NE-GA-G5 — the grounded-agent GroundingPolicy, built next to
    #: ``verified_router`` (same root, same C3 rationale). ``None``
    #: (byte-identical default) unless ``OntologySettings.grounded_agent_enabled``;
    #: None ⇒ no session is ever built grounded — ne-worker inherits it via this
    #: same recipe.
    grounding: GroundingPolicy | None = None
    #: NE-GA-G5 — the ONE agent_name the policy above applies to; ``None`` exactly
    #: when ``grounding`` is ``None``. Call sites compare the request agent_name
    #: against this before threading ``grounding=``.
    grounded_agent_name: str | None = None
    #: NE-GA-RUN1 — the canonical-Run Agent ids whose agentic executions get the
    #: grounding gate (``OntologySettings.grounded_run_agent_ids``). Empty ⇒ no
    #: Run is gated (byte-identical default). Non-empty REQUIRES ``grounding``
    #: above to be composed — ``build_profile_bound_run_execution_adapter``'s
    #: consumer fails loud at construction rather than run an agent silently
    #: ungated.
    grounded_run_agent_ids: tuple[str, ...] = ()
    #: NE-GA-RUN4 — the Run-path grounding strictness tier
    #: (``OntologySettings.grounding_run_strictness``; env
    #: ``NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS``). The Run adapter re-tiers the
    #: ONE shared ``grounding`` policy with this value (assist already
    #: stripped), so the chat knob above never silently sets the Run gate's
    #: enforcement tier. ``"strict"`` default matches the settings default;
    #: meaningless (and harmless) when ``grounding`` is ``None``.
    grounded_run_strictness: str = "strict"
    #: NE-SKILL-1a — the Skill Forge registry engine (over the flywheel signal DB, the
    #: same-engine requirement). ``None`` (byte-identical default) unless the flywheel
    #: signal database is configured; the steward /v1/steward/skills route + the boot
    #: reconcile gate on it. Trailing-defaulted so ne-worker/test construction is
    #: untouched (the entity_graph precedent).
    skills_engine: sa.Engine | None = None
    #: NE-SKILL-1a — the resolved SkillsSettings (env-driven, ``NE_SKILLS_*``), stamped
    #: into the frozen config_hash ONLY when skills are enabled. Surfaced so the
    #: ne-worker boot reconcile reads ``bundle_dir`` from the SAME composed recipe.
    skills_settings: SkillsSettings | None = None
    #: PRE-009A A3 — the ONE canonical Agent Run store (engine/schema/store/cipher),
    #: or ``None`` when the deployment is genuinely unconfigured and holds no
    #: retained private rows. Dormant: nothing routes to it until 009C.
    agent_run_store: AgentRunStore | None = None
    #: PRE-009C wire fence shared by API and worker composition. ``None`` is the
    #: pre-cutover image; the protected generation is admitted only with the
    #: canonical private-payload store composed.
    agent_transport_generation: str | None = None
    default_ask_generation: str | None = None
    #: 015A0-AUTHORITY-CORE — the READ-ONLY current-source authority evaluator over
    #: the runtime engine. Request state receives this and only this: the ne-brain
    #: reconciliation Writer is producer capability and is composed by the pull
    #: path, never by the API composition root.
    source_authority_reader: CurrentSourceAuthorityReader | None = None
    #: PRE-DEMO-AUTHORITY-TAIL — the metadata-only current-attempt reader.
    #: Its scope set is built once from server configuration plus durable
    #: provider-owned discovery state. It carries no authority Writer and no
    #: operation that can advance an attempt directly.
    authority_attempts: AuthorityAttemptReader | None = None
    source_authority_evaluator: Any | None = None
    source_guard: ProtectedSourceResultGuard | None = None
    current_document_authorizer: Any | None = None
    protected_source_output_writer: ProtectedSourceOutputWriter | None = None
    protected_source_execution_writer: ProtectedSourceExecutionWriter | None = None
    #: 015A0-AUTHORITY-CORE — the validated deterministic continuation codec BOTH
    #: roots inherit from this one recipe. ``None`` when the continuation key is
    #: missing, short or malformed: continuation is not ready on this replica, and
    #: nothing about which variable was wrong is logged.
    source_authority_continuation: AuthorizedContinuationTokenCodec | None = None
    #: PRE-011E — the composed Agents capability preflight. ALWAYS composed: a
    #: deployment that cannot answer says so INSIDE the answer (``capabilities``
    #: null), so the absence of the service is never how unavailability is
    #: reported. ``None`` here would put the Phase-1 503 back into production.
    agent_preflight: AgentCapabilityPreflightService | None = None
    #: PRE-011B — the Agent→Skill activation service, over the SAME registry object
    #: whose exact-pin verification produced ``AgentDependencySnapshot.skills`` and
    #: the SAME PrincipalExpander the retrieval trimming uses (F2 one-expander).
    #: ``None`` (byte-identical default) unless skills are enabled. Trailing-
    #: defaulted and NOT hashed: a composed service over already-stamped settings.
    agent_skills: AgentSkillService | None = None
    #: NE-T2X-4 — the composed extraction pathway (mode 1 proposals-only, or
    #: modes 2/3's proposals-plus-immediate-write). ``None`` (byte-identical
    #: default) unless ``BrainGraphSettings.extraction_enabled`` — the
    #: entity_graph/skills_engine conditional-stamp precedent. Trailing-defaulted
    #: so ne-worker construction is untouched; the ne-api lifespan calls
    #: ``extraction.emit_mode_event()`` once at startup when composed.
    extraction: ExtractionRuntime | None = None
    #: PRE-DEMO-REHEARSAL-001 E2A — deliberately M365-only T2 producer.  The
    #: worker sees an opaque host and never imports ne-brain-graph directly.
    #: None unless both graph and scheduled M365 sync are enabled.
    m365_graph_sync_host: M365GraphSyncHost | None = None
    #: NE-INGEST/015A0-AUTHORITY-M365 — the ONE composed embedder, exposed so the
    #: connector-sync roots build the immutable-artifact store over the SAME
    #: embedding profile the retriever uses. Reading it through the private
    #: retriever attribute is the drift-prone shape ``chunk_store`` already fixed.
    embedder: Embedder | None = None
    #: 015A0-AUTHORITY-M365 — the CREDENTIAL-FREE M365 contract descriptor and its
    #: current epoch. ``None`` when M365 authority is not configured. This is a
    #: projection, never a producer: it carries no Writer, no claim and no
    #: credential, and request state never receives it.
    m365_source_authority: M365SourceAuthorityDescriptor | None = None
    #: 015A0-AUTHORITY-T3 — the CREDENTIAL-FREE governed local T3 contract descriptor.
    #: ``None`` unless the governed local dataset is enabled. Like the M365 field it
    #: is a projection: no Writer, no claim, no engine.
    governed_local_t3_source_authority: GovernedLocalT3SourceAuthorityDescriptor | None = None
    #: 015A0-AUTHORITY-T3 — the atomically selected, READ-ONLY immutable Oxigraph
    #: release the governed verified-answer path reads. ``None`` unless governed local
    #: T3 is enabled. It exposes no mutation method at all, so holding it on the
    #: platform cannot become a write capability.
    governed_local_t3_release: SelectedOxigraphRelease | None = None


def _verified_agent_skill_pins(
    registry: SqlAlchemySkillRegistry, loaded: LoadedAgentManifest
) -> frozenset[tuple[str, int, str]]:
    """Resolve EVERY declared Agent Skill pin at its EXACT version + declared hash.

    The availability projection compares ``(skill_id, version, content_hash)``
    triples (``agent_catalog._skill_reason``), so this returns exactly the triples
    that a hash-verified registry row actually satisfies.

    ``verified_pinned_pack`` is mandatory here and ``get_current`` is FORBIDDEN.
    ``get_current`` folds to ``max(version)``, so an Agent pinned to v1 would be
    judged against whatever version the registry most recently admitted: a rollover
    to v2 would either wrongly satisfy the v1 pin (if only the name were compared)
    or wrongly fail it (if the triple were compared against the newer row), and a
    Run frozen on v1 would silently drift. The exact read never folds, and it
    refuses on retirement, absence, wrong version, a stale declared hash, and an
    R-28 row-integrity mismatch — so every one of those leaves the pin out of the
    snapshot and the Agent unavailable, which is the fail-closed direction.

    PRE-011F's ``skills_delivery.verify_required_agent_skills`` deliberately uses
    ``get_current`` for its readiness probe; that is a different question (is the
    mounted bundle the admitted current row?) and its answer must NOT be reused
    here.
    """
    verified: set[tuple[str, int, str]] = set()
    for agent in loaded.manifest.agents:
        for grant in agent.skills:
            pin = SkillPin(
                name=grant.skill_id, version=grant.version, content_hash=grant.content_hash
            )
            if registry.verified_pinned_pack(pin) is not None:
                verified.add((pin.name, pin.version, pin.content_hash))
    return frozenset(verified)


def _ensure_api_claims(identity_settings: IdentitySettings) -> IdentitySettings:
    """Union ``'department'`` AND ``'sid'`` into the JIT claim allowlist.

    ne-api needs BOTH: chat sessions derive ``department`` from
    ``principal.claims['department']`` (AMENDMENT A1), and the steward provenance
    ``session_id`` is the verified token's ``sid`` claim when present (spec §7-R4).
    The JIT provisioner copies ONLY allowlisted, str-valued claims from the
    JWKS-verified token — never request input — so this stays fail-closed. Applied
    ALWAYS and grep-provably here; byte-identical when both are already present.
    """
    claims = identity_settings.principal_claims
    needed = tuple(name for name in ("department", "sid") if name not in claims)
    if not needed:
        return identity_settings
    return identity_settings.model_copy(update={"principal_claims": (*claims, *needed)})


def _merge_tool_policy(
    govern_settings: GovernSettings,
    extra_policies: Mapping[str, ToolArgPolicy] | None = None,
) -> GovernSettings:
    """Merge the always-on core tool arg policies (+ any extra) into ``guardrails.tools``.

    A tool absent from ``guardrails.tools`` is rejected fail-closed, so the policy
    MUST be present; and the frozen hash must reflect the real config, so the
    merge happens before ``freeze_platform_config``. ``extra_policies`` lets a
    test add a tier-2 tool's policy (production passes none). ``read.trace``
    (NE-167a) is merged here as an always-on core tool, identically to
    ``read.knowledge.search`` — both ride the SAME frozen executor in both
    ``ne-api`` and ``ne-worker`` (``build_platform`` parity).

    NE-IDENTITY-ROTATION-CRON: ``write.internal.identity.credential_rotation``
    joins this ALWAYS-ON set (empty args — the ne-worker rotation cron's
    no-injectable-surface handler, mirroring the zero-arg pattern). Unlike the
    conditionally-stamped ``connect-sync``/``principal-sync``/skills policies
    below, this tool has NO config gate (the seam it wires,
    ``AgentCredentialService.rotation_due_agents()``, always exists and
    validates), so its policy belongs here rather than in a conditional merge.
    ne-api MUST NOT import ne-worker (the dependency points the other way), so
    the tool name is duplicated here as a literal, mirroring
    ``principal_sync_policies`` below and ``ne.worker.credential_rotation._TOOL``.
    """
    # NE-AIRGAP-TOGGLE: the two governed egress actions join the ALWAYS-ON set —
    # the toggle has no config gate (the posture register always exists). Lazy
    # import mirrors the reference_actions pattern. The frozen config_hash moves
    # once for this addition — the same real, expected, additive registry change
    # the credential_rotation policy above records.
    from ne.api.inference_lifecycle import LIFECYCLE_ACTION, LIFECYCLE_ARG_POLICY
    from ne.api.model_routing import (
        MODEL_ROUTING_ARG_POLICY,
        ROUTE_EXTERNAL_ACTION,
        ROUTE_INTERNAL_ACTION,
    )
    from ne.api.network_egress import (
        EGRESS_EXPAND_ACTION,
        EGRESS_RESTRICT_ACTION,
        NETWORK_EGRESS_ARG_POLICY,
    )

    guardrails = govern_settings.guardrails
    tools = {
        **guardrails.tools,
        TOOL_NAME: KNOWLEDGE_SEARCH_ARG_POLICY,
        GRAPH_PATH_TOOL_NAME: GRAPH_PATH_ARG_POLICY,
        TRACE_TOOL_NAME: TRACE_ARG_POLICY,
        "write.internal.identity.credential_rotation": ToolArgPolicy(),
        EGRESS_RESTRICT_ACTION: NETWORK_EGRESS_ARG_POLICY,
        EGRESS_EXPAND_ACTION: NETWORK_EGRESS_ARG_POLICY,
        # NE-MODEL-ROUTING: the two governed routing actions join the same
        # ALWAYS-ON set, for the same reason — the route register always exists.
        ROUTE_INTERNAL_ACTION: MODEL_ROUTING_ARG_POLICY,
        ROUTE_EXTERNAL_ACTION: MODEL_ROUTING_ARG_POLICY,
        LIFECYCLE_ACTION: LIFECYCLE_ARG_POLICY,
    }
    if extra_policies:
        tools.update(extra_policies)
    return govern_settings.model_copy(
        update={"guardrails": guardrails.model_copy(update={"tools": tools})}
    )


def _first_error_line(exc: ValidationError) -> str:
    """Render the FIRST validation error as ``'loc: msg'`` — never the input value.

    Mirrors ``ne.api.errors._flatten_validation`` (same discipline): ``loc``/``msg``
    only, so a rejected secret value can never surface in a boot error (C12).
    """
    errors = exc.errors(include_url=False, include_input=False)
    if not errors:
        return "validation error"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "invalid"))
    return f"{loc}: {msg}" if loc else msg


def _load_write_action_env_settings() -> _WriteActionEnvSettings:
    """Resolve the 3 write-action connector *settings* from env, pre-freeze.

    NE-ACTION-WIRE / Codex C1: this is the hash-affecting half of what used to be
    a single "construct everything" function — split out so it can run BEFORE
    ``freeze_platform_config`` and have its result stamped into ``extra_modules``
    (see :func:`_WriteActionEnvSettings`). No engines, no clients, no I/O: purely
    ``NeSettings`` construction/validation, safe to call before anything freezes.

    The two remaining CONNECTOR legs (sql -> m365, in order) each construct their
    own ``NeSettings`` subclass; a ``pydantic.ValidationError`` there means that
    leg is NOT configured. BOTH legs are attempted regardless of an earlier miss
    (so one boot cycle names every gap at once) and collected into ONE
    ``ValueError`` naming every missing leg + its env prefix (never a
    silently-absent tool). No secrets in any message (C12): only leg names / env
    prefixes / field locations.

    NE-S5-INTERNAL-CRM (owner decision 2026-08-28): the CRM legs
    (``write.internal.crm.note`` + both ``read.*.crm.*`` lookups) no longer have
    a Salesforce connector requirement AT ALL — they are served by the internal
    clients over the platform's own stores (``ne.api.internal_crm``), so no
    ``NE_SALESFORCE_*`` env is resolved, stamped, or required here.
    """
    from ne.connect.m365.config import M365ConnectorSettings
    from ne.connect.sql.config import SqlConnectorSettings

    missing: list[str] = []

    sql_settings: SqlConnectorSettings | None = None
    try:
        sql_settings = SqlConnectorSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing.append(f"sql (NE_SQL_*): {_first_error_line(exc)}")

    m365_settings: M365ConnectorSettings | None = None
    try:
        m365_settings = M365ConnectorSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing.append(f"m365 (NE_M365_*): {_first_error_line(exc)}")

    if missing:
        raise ValueError(
            "NE_API_WRITE_ACTIONS_ENABLED=true but the following write-action "
            "legs are not configured: " + "; ".join(missing)
        )
    assert sql_settings is not None
    assert m365_settings is not None

    return _WriteActionEnvSettings(sql=sql_settings, m365=m365_settings)


def _load_selected_write_action_env_settings(
    selected_tools: Sequence[str],
) -> _SelectedWriteActionEnvSettings:
    """Resolve only the connector legs required by ``selected_tools``.

    Selection is already closed by :class:`ApiSettings` to the reference action
    names.  Each selected leg is attempted so one boot reports every relevant
    configuration gap without inspecting or validating unrelated ambient
    connector variables.  NE-S5-INTERNAL-CRM: ``write.internal.crm.note``
    requires NO connector leg — its clients read/write the platform's own
    stores.
    """
    from ne.connect.m365.config import M365ConnectorSettings
    from ne.connect.sql.config import SqlConnectorSettings

    selected = frozenset(selected_tools)
    missing: list[str] = []
    sql_settings: SqlConnectorSettings | None = None
    m365_settings: M365ConnectorSettings | None = None

    if _TICKET_UPDATE_TOOL in selected:
        try:
            sql_settings = SqlConnectorSettings()  # type: ignore[call-arg]
        except ValidationError as exc:
            missing.append(f"sql (NE_SQL_*): {_first_error_line(exc)}")
    if _EMAIL_DRAFT_TOOL in selected or _EMAIL_SEND_TOOL in selected:
        try:
            m365_settings = M365ConnectorSettings()  # type: ignore[call-arg]
        except ValidationError as exc:
            missing.append(f"m365 (NE_M365_*): {_first_error_line(exc)}")

    if missing:
        raise ValueError(
            "NE_API_WRITE_ACTION_TOOLS selected connector legs are not configured: "
            + "; ".join(missing)
        )
    return _SelectedWriteActionEnvSettings(
        sql=sql_settings,
        m365=m365_settings,
    )


def _build_write_action_clients(
    env_settings: _WriteActionEnvSettings, engines: list[sa.Engine]
) -> _SelectedWriteActionClients:
    """Construct the CONNECTOR write-action clients from already-resolved settings.

    NE-ACTION-WIRE / Codex C1: the I/O-performing half — split from
    :func:`_load_write_action_env_settings` so this runs AFTER
    ``freeze_platform_config`` (matching every other engine/client construction in
    this file), while the settings it consumes were already resolved (and stamped
    into the frozen hash) before the freeze. A configured-but-INVALID leg (e.g.
    ``SqlSyncError`` from ``create_validated_engine``, a C2 ``UnsafeEndpointError``
    at client construction) is already loud and named, and propagates as-is,
    unfiltered by this function.
    """
    from ne.connect.m365.graph import GraphClient
    from ne.connect.m365.write import GraphWriteClient
    from ne.connect.sql.engine import create_validated_engine
    from ne.connect.sql.write import SqlWriteClient

    # sql — disposal parity: every OTHER platform engine is appended to the SAME
    # ``engines`` list (build_platform disposes them all on teardown).
    sql_engine = create_validated_engine(env_settings.sql)
    engines.append(sql_engine)
    ticket_client = SqlWriteClient(sql_engine, allowed_writes=_TICKET_ALLOWED_WRITES)

    graph_write = GraphWriteClient(GraphClient(env_settings.m365))

    # NE-S5-INTERNAL-CRM: the crm/lookup/contact_lookup seams are filled by the
    # INTERNAL clients at the build_platform call site (they need the composed
    # chunk store and runtime engine, which this connector-settings-scoped
    # helper deliberately does not see).
    return _SelectedWriteActionClients(
        ticket=ticket_client,
        draft=graph_write,
        send=graph_write,
    )


def _build_selected_write_action_clients(
    env_settings: _SelectedWriteActionEnvSettings,
    engines: list[sa.Engine],
) -> _SelectedWriteActionClients:
    """Construct only the clients whose settings were selected pre-freeze.

    NE-S5-INTERNAL-CRM: the crm/lookup/contact_lookup seams are no longer built
    here — when ``write.internal.crm.note`` is selected the build_platform call
    site fills them with the INTERNAL clients over the platform's own stores.
    """
    ticket_client: TicketWriteClient | None = None
    draft_client: DraftMailClient | None = None
    send_client: SendMailClient | None = None

    if env_settings.sql is not None:
        from ne.connect.sql.engine import create_validated_engine
        from ne.connect.sql.write import SqlWriteClient

        sql_engine = create_validated_engine(env_settings.sql)
        engines.append(sql_engine)
        ticket_client = SqlWriteClient(sql_engine, allowed_writes=_TICKET_ALLOWED_WRITES)
    if env_settings.m365 is not None:
        from ne.connect.m365.graph import GraphClient
        from ne.connect.m365.write import GraphWriteClient

        # ONE GraphWriteClient serves both the draft and send legs — it satisfies
        # both narrow Protocols, and one client means one throttle/token flow.
        graph_write = GraphWriteClient(GraphClient(env_settings.m365))
        draft_client = graph_write
        send_client = graph_write

    return _SelectedWriteActionClients(
        ticket=ticket_client,
        draft=draft_client,
        send=send_client,
    )


#: NE-TEMPLATE-ENUM (SPEC §12 item 4): the sentence the withheld-notice fallback
#: appends when EVERY verified query this turn returned zero rows. It claims exactly
#: what the gate verified — the queries' results — and explicitly disclaims the
#: store-level reading ("the tables contain 0 rows") that the owner caught as false
#: against 18 real rows during the live walkthrough. Emitted whenever all fetches
#: are empty (no data-dependent signal — C7-safe).
_GROUNDED_ALL_QUERIES_EMPTY_NOTE = (
    "Every verified query run this turn returned no rows. That describes these "
    "query results only — it is not a statement that the underlying tables or "
    "records are empty, and nothing about the store's contents was verified."
)

#: The G4 assist prompts (NE-HGATE-3), module-level so the copy is testable. The
#: FAIL-shape system prompt scopes the no-answer claim to the QUERIES (SPEC §12
#: item 4): the model must never assert the underlying tables/store are empty —
#: the gate verified what the queries returned, nothing more.
_GROUNDED_ASSIST_POLISH_SYSTEM = (
    "You polish an already-verified draft answer. Use "
    "ONLY facts present in the draft and the verified "
    "tables below. Never add a new fact, number, or name."
)
_GROUNDED_ASSIST_FAIL_SYSTEM = (
    "You answer strictly and only from the verified "
    "tables provided. Every name and number in your "
    "answer must appear in the tables. If the tables do "
    "not answer the question, say that the queries run "
    "this turn returned no matching rows — never state or "
    "imply that the underlying tables, store, or systems "
    "are empty or lack the data; you know only what these "
    "queries returned."
)


def _render_grounded_fallback(fetched: Sequence[Mapping[str, Any]]) -> str:
    """The deterministic §2.4 withheld-notice + verified-table fallback.

    Empty ``fetched`` ⇒ the notice alone (no verified data was fetched this
    turn); else the notice preamble followed by one rendered table section per
    fetched answer, each with a provenance footer. When every fetched answer is
    ZERO-row, the notice also carries the query-scoped honesty sentence
    (:data:`_GROUNDED_ALL_QUERIES_EMPTY_NOTE`) — the copy states what the gate
    verified (the queries returned no rows), never a claim about the store
    (SPEC §12 item 4). The mappings are full ``VerifiedAnswer.model_dump(mode=
    "json")`` payloads recorded DIRECTLY by the verified-read handler via the
    recorder's structured-record API (§4.2) — they carry the NE-CTXB-5 envelope
    keys (``returned_rows``/``total_rows``/``truncation_reason``), so
    ``model_validate`` reconstructs the full answer and ``render_answer_text``
    emits the compact envelope header in the fallback tables too.
    """
    from ne.brain_ontology.verified.answers import VerifiedAnswer, render_answer_text

    notice = (
        "The drafted narration contained content I could not verify "
        "against this turn's fetched data, so it was withheld "
        "(audited)."
    )
    if not fetched:
        return f"{notice} No verified data was fetched this turn."
    answers = [VerifiedAnswer.model_validate(dict(mapping)) for mapping in fetched]
    if all(answer.returned_rows == 0 for answer in answers):
        notice = f"{notice} {_GROUNDED_ALL_QUERIES_EMPTY_NOTE}"
    sections = [notice]
    for answer in answers:
        table = render_answer_text(answer)
        truncated_note = ", truncated" if answer.truncated else ""
        footer = (
            f"[{answer.template_id}@v{answer.template_version} — "
            f"{len(answer.rows)} rows{truncated_note}]"
        )
        sections.append(f"{table}\n{footer}")
    return "\n\n".join(sections)


def _degraded_sources_provider(
    audit_engine: sa.Engine, resources: Sequence[str]
) -> DegradedSourcesProvider:
    """Build the uncached per-read health provider over the authoritative audit DB.

    Returns the full two-way ``SourceHealthSnapshot`` (withheld ``unavailable``
    stamps + served-with-caveat ``stale`` stamps — see
    ``ne.api.connect_status.sync_source_read_health``). T1 consumes both legs;
    T2 graph reads take only the unavailable set via
    ``evaluate_degraded_sources``.
    """
    bounded_resources = tuple(resources)

    async def provide() -> SourceHealthSnapshot:
        health = await asyncio.to_thread(sync_source_read_health, audit_engine, bounded_resources)
        return SourceHealthSnapshot(unavailable=health.unavailable, stale=health.stale)

    return provide


#: PRE-009A A3 -- the ephemeral private-payload key, delivered by A2 as a REQUIRED
#: deployment secret. Parsed AFTER ``freeze_platform_config`` so neither the key id
#: nor the material can ever enter the frozen ``config_hash`` (C12).
AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV = "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID"
AGENT_PRIVATE_PAYLOAD_KEK_ENV = "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64"


def _private_payload_tables() -> list[sa.Table]:
    """Every envelope-bearing table, DERIVED from the schema rather than listed.

    A hand-written list of the seven private domains is the classic
    under-approximation: the day an eighth domain is added, an enumerated
    inventory silently stops seeing its rows and the readiness check below starts
    reporting "no retained rows" for a database that has them. Selecting on the
    envelope columns themselves cannot drift.
    """
    from ne.runtime.store import metadata

    return [
        table
        for table in metadata.sorted_tables
        if "key_id" in table.c and "encrypted_dek" in table.c
    ]


def _retained_private_payload_key_ids(engine: sa.Engine) -> set[str]:
    """Key ids referenced by retained private rows -- WITHOUT constructing a cipher.

    This runs on the DORMANT path too, which is the whole point: "no key configured"
    must never be a way to walk past a database that still holds rows wrapped under
    some other key.
    """
    referenced: set[str] = set()
    with engine.connect() as conn:
        for table in _private_payload_tables():
            for value in conn.execute(sa.select(table.c.key_id).distinct()).scalars():
                if value:
                    referenced.add(str(value))
    return referenced


def _compose_agent_run_store(
    runtime_engine: sa.Engine, *, deployment_configured: bool
) -> AgentRunStore | None:
    """Compose the ONE canonical Agent Run store, or stay dormant -- fail-closed.

    The discriminator between dormant and activated is itself security-relevant: if
    it can be made to read "not activated" on a deployment that genuinely is, the
    fail-closed property inverts into fail-open. It is therefore a union of three
    INDEPENDENT signals, any one of which activates:

    * an explicit deployment signal (``deployment_configured``);
    * EITHER key environment variable being present -- deliberately either, so a
      half-configured deployment activates and then fails, rather than reading as
      unconfigured;
    * ANY retained private-payload row, whatever key it references.

    Once activated, both variables are mandatory, canonical and mutually
    consistent, and every retained row must already be under the current key.
    Nothing here echoes the key material, and the key never enters the config hash.
    """
    ensure_run_schema(runtime_engine)
    key_id = os.environ.get(AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV, "").strip()
    kek_b64 = os.environ.get(AGENT_PRIVATE_PAYLOAD_KEK_ENV, "").strip()
    retained = _retained_private_payload_key_ids(runtime_engine)
    activated = deployment_configured or bool(key_id) or bool(kek_b64) or bool(retained)
    if not activated:
        # Genuinely unconfigured AND no retained rows: dormant. 009C activates.
        return None

    missing = [
        name
        for name, value in (
            (AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV, key_id),
            (AGENT_PRIVATE_PAYLOAD_KEK_ENV, kek_b64),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Agent Run private payloads are active for this deployment but "
            f"{', '.join(missing)} is not set. Provision the key with "
            "`ne private-payload-keys provision` and redeploy; a root that cannot "
            "open its own private rows must not become ready."
        )
    try:
        kek = decode_private_payload_b64(kek_b64)
    except ValueError as exc:
        # The message names the VARIABLE, never the value.
        raise ValueError(
            f"{AGENT_PRIVATE_PAYLOAD_KEK_ENV} is not a canonical unpadded base64url 32-byte key"
        ) from exc
    try:
        keys = StaticPrivatePayloadKeys(current_key_id=key_id, keys={key_id: kek})
    except ValueError as exc:
        raise ValueError(f"{AGENT_PRIVATE_PAYLOAD_KEK_ENV} must decode to a 256-bit key") from exc
    store = AgentRunStore(runtime_engine, RunPrivatePayloadCipher(keys))
    readiness = store.current_key_readiness()
    if not readiness.ready:
        raise ValueError(
            f"{readiness.rows_under_other_keys} retained private-payload rows are "
            f"wrapped under a key other than {readiness.current_key_id!r}. Complete the "
            "rotation with `ne private-payload-keys rotate` before starting this root."
        )
    return store


def _compose_source_authority(
    runtime_engine: sa.Engine,
) -> tuple[CurrentSourceAuthorityReader, AuthorizedContinuationTokenCodec | None]:
    """Compose 015A0-AUTHORITY-CORE's Reader and continuation codec, in that order.

    Two failure modes are deliberately different, because the operator response is
    different:

    * **Foreign construction history fails composition.** A runtime database
      holding receipts admitted under another deployment's construction id cannot
      be revalidated here, and pre-demo has no import-as-new operation. Starting
      anyway would join two histories.
    * **An unusable continuation key does NOT fail composition.** It fails
      continuation readiness on this replica only, silently — see
      :mod:`ne.api.source_authority_secrets` for why nothing is logged.

    Both run AFTER ``freeze_platform_config`` so no key material can reach the
    frozen ``config_hash``; the non-secret codec/schema version is frozen in
    ne-brain and bound into every token preimage instead.
    """
    from ne.api.source_authority_secrets import load_source_authority_continuation_key
    from ne.brain.current_source_authority import (
        HmacAuthorizedContinuationTokenCodec,
        SqlAlchemyCurrentSourceAuthorityReader,
        bind_deployment_construction_id,
        ensure_source_authority_schema,
    )
    from ne.brain.write.immutable_artifacts import ensure_immutable_artifact_schema

    ensure_source_authority_schema(runtime_engine)
    ensure_immutable_artifact_schema(runtime_engine)
    construction_id = bind_deployment_construction_id(runtime_engine)
    reader = SqlAlchemyCurrentSourceAuthorityReader(runtime_engine, construction_id)
    key = load_source_authority_continuation_key()
    if key is None:
        return reader, None
    try:
        codec = HmacAuthorizedContinuationTokenCodec(key.key_id, key.material)
    except ValueError:
        # The loader already rejects everything the codec would; this is the
        # belt for a future divergence, and it stays as silent as the loader.
        return reader, None
    return reader, codec


class _RetainedM365GraphSourceAuthorityProvider:
    """Project only the exact current T1 admission retained for an M365 record."""

    def __init__(
        self,
        lineage: SqlAlchemyDocumentLineageStore,
        reader: CurrentSourceAuthorityReader,
    ) -> None:
        self._lineage = lineage
        self._reader = reader

    async def stamps_for(
        self, record: Any, *, acl: tuple[str, ...]
    ) -> tuple[SourceAuthorityStamp, ...]:
        from ne.connect.identity import source_acl_hash, source_ref_for, source_snapshot_hash

        source_system = "m365-sharepoint"
        snapshot_hash = source_snapshot_hash(source_system, record)
        acl_hash = source_acl_hash(acl)
        retained = self._lineage.retained_source_authority_for_snapshot(
            source_system=source_system,
            source_ref=source_ref_for(record),
            source_snapshot_hash=snapshot_hash,
            source_acl_hash=acl_hash,
        )
        if retained is None:
            return ()
        load_receipt = getattr(self._reader, "load_receipt", None)
        if not callable(load_receipt):
            return ()
        receipt = cast(CurrentSourceAuthorityReceipt | None, load_receipt(retained.receipt_id))
        if receipt is None:
            return ()
        artifact = receipt.artifact_identity
        if (
            receipt.state is not SourceAuthorityState.PRESENT
            or retained.state != SourceAuthorityState.PRESENT.value
            or artifact is None
            or receipt.receipt_hash != retained.receipt_hash
            or receipt.source_identity.identity_hash != retained.source_identity_hash
            or receipt.source_identity.source_kind != source_system
            or receipt.source_contract.source_kind != source_system
            or artifact.artifact_id != retained.artifact_id
            or artifact.source_snapshot_hash != snapshot_hash
            or artifact.acl_snapshot_hash != acl_hash
            or receipt.acl_snapshot_hash != acl_hash
            or receipt.contract_epoch != retained.contract_epoch
        ):
            return ()
        # The cross-checks above are what this caller must prove; the stamp
        # itself comes from the ONE canonical derivation (ne.contracts).
        stamp = stamp_from_receipt(receipt)
        if stamp is None:  # pragma: no cover - guarded by the checks above
            return ()
        try:
            current = await self._reader.resolve(
                stamp,
                principal_id="ne.graph-sync",
                access_context_hash=acl_hash,
            )
        except Exception:
            return ()
        if current.decision is not SourceAuthorityDecision.AUTHORIZED:
            return ()
        return (stamp,)


def _compose_m365_source_authority(
    runtime_engine: sa.Engine, *, connect_sync_settings: ConnectSyncSettings
) -> M365SourceAuthorityDescriptor | None:
    """Claim-or-load the M365 contract head and project its credential-free view.

    Runs in BOTH roots because there is one ``build_platform``. The claim-or-load
    is idempotent, so the API's call and the producer's call converge on one head
    rather than racing to create two.

    A misconfigured or unavailable M365 connector returns ``None``: the scope
    stays Unknown and is not advertised, which is the fail-closed outcome. It is
    never an exception, because an unrelated route must not 500 because one
    connector's contract cannot be computed.
    """
    if "m365" not in connect_sync_settings.sources:
        return None
    from ne.connect.authority_admission import (
        AuthorityAdmissionError,
        SqlAlchemySourceContractHeadStore,
        claim_or_load_connector_instance,
        connector_instance_scope,
        ensure_admission_schema,
    )
    from ne.connect.m365.config import M365ConnectorSettings
    from ne.connect.m365.contract import (
        m365_admitted_target_digest,
        m365_contract_identity,
        m365_contract_sla_seconds,
    )

    try:
        m365_settings = M365ConnectorSettings()  # type: ignore[call-arg]  # env-driven
    except ValidationError:
        return None
    if not m365_settings.authority_enabled:
        return None
    from ne.brain.current_source_authority import bind_deployment_construction_id

    ensure_admission_schema(runtime_engine)
    construction_id = bind_deployment_construction_id(runtime_engine)
    try:
        instance = claim_or_load_connector_instance(
            runtime_engine,
            deployment_id=construction_id,
            source_name=m365_settings.source_name,
            admitted_target_digest=m365_admitted_target_digest(m365_settings),
        )
        store = SqlAlchemySourceContractHeadStore(runtime_engine)
        scope = connector_instance_scope(
            deployment_id=construction_id,
            connector_instance_id=instance.connector_instance_id,
            graph_tenant_id=m365_settings.tenant_id,
        )
        head = store.claim_or_load(
            scope,
            source_generation=instance.source_generation,
            contract=m365_contract_identity(m365_settings),
        )
    except AuthorityAdmissionError:
        return None
    return project_m365_source_authority(
        m365_settings,
        head=head,
        freshness_sla_seconds=m365_contract_sla_seconds(m365_settings),
        resolved=store.is_resolved(scope),
    )


def _compose_authority_attempt_reader(
    runtime_engine: sa.Engine,
    *,
    connect_sync_settings: ConnectSyncSettings,
    connect_source_settings: Mapping[str, Any],
    m365_source_authority: M365SourceAuthorityDescriptor | None,
) -> Any:
    """Compose one metadata-only reader over exact configured child scopes.

    M365 direct drives come from the boot-frozen connector configuration; drives
    discovered beneath an explicitly configured site come from the durable
    hierarchy ledger. Salesforce's organization id is provider-issued, so it is
    accepted only from a current durable connector head whose credential-free
    target digest and contract identity still match the boot configuration.

    This function reads producer state. It never constructs an authority Writer,
    claims an instance, advances a stage, or treats a path prefix as membership.
    """
    from ne.brain.current_source_authority import bind_deployment_construction_id
    from ne.connect.authority_admission import (
        SqlAlchemyAdmissionAttemptStore,
        SqlAlchemyHierarchicalAdmission,
        drive_scope,
        ensure_admission_schema,
        ne_source_connector_instances,
        ne_source_contract_heads,
        site_scope,
    )
    from ne.connect.authority_attempts import AuthorityAttemptReader
    from ne.connect.salesforce.authority import salesforce_object_scope
    from ne.connect.salesforce.contract import (
        salesforce_admitted_target_digest,
        salesforce_contract_identity,
    )
    from ne.connect.salesforce.identity import canonical_salesforce_organization_id
    from ne.contracts.source_authority import SourceReconciliationScope

    ensure_admission_schema(runtime_engine)
    deployment_id = bind_deployment_construction_id(runtime_engine)
    scopes: list[SourceReconciliationScope] = []

    m365_settings = connect_source_settings.get("m365")
    if (
        "m365" in connect_sync_settings.sources
        and m365_source_authority is not None
        and m365_settings is not None
        and bool(getattr(m365_settings, "authority_enabled", False))
    ):
        instance_id = m365_source_authority.connector_instance_id
        tenant_id = str(m365_settings.tenant_id)
        drive_ids = set(str(value) for value in m365_settings.drive_ids)
        hierarchy = SqlAlchemyHierarchicalAdmission(runtime_engine)
        for site_id in m365_settings.site_ids:
            parent = site_scope(
                deployment_id=deployment_id,
                connector_instance_id=instance_id,
                graph_tenant_id=tenant_id,
                site_id=str(site_id),
            )
            drive_ids.update(hierarchy.known_descendant_keys(parent))
        scopes.extend(
            drive_scope(
                deployment_id=deployment_id,
                connector_instance_id=instance_id,
                graph_tenant_id=tenant_id,
                drive_id=drive_id,
            )
            for drive_id in sorted(drive_ids)
        )

    salesforce_settings = connect_source_settings.get("salesforce")
    if (
        "salesforce" in connect_sync_settings.sources
        and salesforce_settings is not None
        and bool(getattr(salesforce_settings, "authority_enabled", False))
    ):
        # The provider-issued org id is not configuration and must not be guessed
        # from the instance URL. Read candidate parent scopes, then retain only
        # the one whose durable instance target and current contract match the
        # exact configured Salesforce connector.
        with runtime_engine.begin() as conn:
            heads = conn.execute(
                sa.select(ne_source_contract_heads).where(
                    ne_source_contract_heads.c.source_kind == "salesforce"
                )
            ).all()
            instances = {
                str(row.connector_instance_id): str(row.admitted_target_digest)
                for row in conn.execute(sa.select(ne_source_connector_instances)).all()
            }
        for head in heads:
            path = tuple(str(part) for part in head.scope_path)
            if len(path) != 4 or path[0] != "salesforce" or path[1] != deployment_id:
                continue
            try:
                organization_id = canonical_salesforce_organization_id(path[3])
                expected_target = salesforce_admitted_target_digest(
                    salesforce_settings, organization_id
                )
                expected_contract = salesforce_contract_identity(
                    salesforce_settings, organization_id
                )
            except (TypeError, ValueError):
                continue
            if (
                instances.get(path[2]) != expected_target
                or str(head.contract_identity_hash) != expected_contract.identity_hash
            ):
                continue
            scopes.extend(
                salesforce_object_scope(
                    deployment_id=deployment_id,
                    connector_instance_id=path[2],
                    organization_id=organization_id,
                    object_name=config.name,
                )
                for config in salesforce_settings.objects
            )

    return AuthorityAttemptReader(SqlAlchemyAdmissionAttemptStore(runtime_engine), scopes)


@dataclass(frozen=True)
class _GovernedLocalT3Composition:
    """What the governed local T3 composition hands the rest of ``build_platform``."""

    release: SelectedOxigraphRelease
    reader: SelectedReleaseVerifiedReader
    descriptor: GovernedLocalT3SourceAuthorityDescriptor
    authority_index: _GovernedLocalT3AuthorityIndex


class _GovernedLocalT3AuthorityIndex:
    """Immutable contributor stamps and release graphs for one admitted T3 head."""

    def __init__(
        self,
        stamps: Mapping[str, SourceAuthorityStamp],
        graphs: Sequence[str],
        graph_stamps: Mapping[str, Sequence[SourceAuthorityStamp]],
        *,
        head_store: Any,
        head: Any,
    ) -> None:
        self._stamps = MappingProxyType(dict(stamps))
        self._graphs = tuple(sorted(set(graphs)))
        self._graph_stamps = MappingProxyType(
            {
                graph: tuple(sorted(values, key=lambda stamp: stamp.authority_receipt_hash))
                for graph, values in graph_stamps.items()
            }
        )
        self._head_store = head_store
        self._head = head
        # Supersession states already shouted about — one ERROR per DISTINCT observed
        # durable head, not one per request (the state, not the traffic, is the news).
        self._reported_stale: set[tuple[int, str, bool] | None] = set()

    def _is_current(self) -> bool:
        current = self._head_store.load(self._head.connector_scope)
        resolved = self._head_store.is_resolved(self._head.connector_scope)
        if current == self._head and resolved:
            return True
        self._report_superseded(current, resolved)
        return False

    def _report_superseded(self, current: Any, resolved: bool) -> None:
        """LOUDLY name why every source-bearing read is about to refuse.

        2026-08-27 outage: an `ne` CLI run from an OLDER code checkout, pointed at
        the live database, recomposed this scope under its own (different) contract
        identity — advancing the durable epoch out from under both running
        containers. Every ``read.kg.*`` tool then refused with the honest but
        OPAQUE ``source_authority_unavailable`` scope error, and nothing anywhere
        said why. This diagnosis turns that silent degradation into one ERROR line
        naming the composed head, the durable head that displaced it, and the
        remedy (restart this process so it recomposes under the current contract).
        """
        observed = (
            None
            if current is None
            else (int(current.contract_epoch), str(current.source_contract.identity_hash), resolved)
        )
        if observed in self._reported_stale:
            return
        self._reported_stale.add(observed)
        durable = (
            "MISSING (no head row for this scope)"
            if current is None
            else (
                f"epoch={current.contract_epoch} "
                f"contract={current.source_contract.identity_hash} resolved={resolved}"
            )
        )
        _LOG.error(
            "governed local T3 head SUPERSEDED: this process composed "
            "epoch=%d contract=%s at boot, but the durable head for scope %s is now %s. "
            "Another composition claimed this scope after boot — a second api/worker "
            "boot against the same database, or an out-of-container `ne` CLI run from "
            "a DIFFERENT code checkout (a changed loader schema/fixture changes the "
            "contract identity and advances the epoch). Every source-bearing verified "
            "read in THIS process now refuses with 'source_authority_unavailable' "
            "until the process restarts and recomposes under the current contract.",
            self._head.contract_epoch,
            self._head.source_contract.identity_hash,
            self._head.connector_scope.scope_hash,
            durable,
        )

    async def resolve(self, contributor: str) -> SourceAuthorityStamp | None:
        return self._stamps.get(contributor) if self._is_current() else None

    def authorized_graphs(self) -> tuple[str, ...]:
        return self._graphs if self._is_current() else ()

    async def snapshot_stamps(self, graphs: tuple[str, ...]) -> tuple[SourceAuthorityStamp, ...]:
        if not self._is_current() or any(graph not in self._graphs for graph in graphs):
            return ()
        unique = {
            stamp.authority_receipt_hash: stamp
            for graph in graphs
            for stamp in self._graph_stamps.get(graph, ())
        }
        return tuple(unique[key] for key in sorted(unique))


def _compose_governed_local_t3_source_authority(
    runtime_engine: sa.Engine,
    *,
    api_settings: ApiSettings,
    ontology_settings: OntologySettings,
    t3_store: ValidatingTripleStore,
    config_hash: str,
) -> _GovernedLocalT3Composition | None:
    """Claim, build, publish and register the governed local T3 release head.

    The production order is the invariant, not a convention:

    1. Build the release from the CHECKED-IN generator bytes with the real loader's
       own builder. No test seed, no request-local fake, no legacy unstamped path.
    2. Claim-or-load the server-issued dataset-instance id from the DURABLE registry,
       keyed by this deployment's construction id and the credential-free configured
       target digest. Nothing here mints a deployment id or an instance id locally.
    3. Stage the release into its OWN Store and CAS-select it, which is what makes a
       read-only :class:`SelectedOxigraphRelease` exist at all.
    4. CAS-register the SourceContract head/epoch through the SAME store the worker's
       producer uses, so API and worker report one head rather than two agreeing
       derivations; a value-changed contract advances the epoch and clears
       ``resolved`` before readiness.

    Failure is LOUD, deliberately unlike the M365 projector. M365 unavailability
    leaves an unrelated route unaffected; here a silent ``None`` would hand the
    verified-answer bindings the ordinary mutable gateway instead of the pinned
    release — a source-bearing surface downgraded to a non-source-bearing one
    without anyone being told. A deployment that asked for governed local T3 and
    cannot have it must not become ready.

    There is no scheduler, cadence, interval or heartbeat anywhere in this function:
    it runs once, at composition, and freshness afterwards is the selected head.
    """
    if not ontology_settings.governed_local_t3_enabled:
        return None
    from ne.brain_ontology import (
        SELECTED_RELEASE_ALGORITHM_VERSION,
        GovernedLocalT3Instance,
        SelectedReleaseRegistry,
        build_meridian_release,
        contributor_artifact_identity,
        contributor_source_identity,
        governed_local_t3_admitted_target_digest,
        governed_local_t3_scope,
        graph_iri_for_label,
        meridian_source_contract_identity,
        publish_meridian_release,
        require_source_bearing_backend,
    )
    from ne.brain_ontology.verified.selected_release import SelectedReleaseVerifiedReader

    fixture_path = api_settings.demo_meridian_fixture_path
    if not fixture_path:
        raise ValueError(
            "NE_ONTOLOGY_GOVERNED_LOCAL_T3_ENABLED requires a configured Meridian "
            "fixture directory (NE_API_DEMO_MERIDIAN_FIXTURE_PATH): the governed "
            "local T3 producer loads one checked-in dataset and has no other source"
        )
    # The belt behind OntologySettings' fail-loud backend validator: a registry-
    # supplied store could still be a backend that cannot prove the snapshot, and a
    # source-bearing read on such a backend is unavailable, not degraded.
    require_source_bearing_backend(t3_store)

    from ne.api.demo_seams import run_boot_coroutine
    from ne.brain.current_source_authority import (
        SqlAlchemyCurrentSourceAuthorityWriter,
        bind_deployment_construction_id,
        ensure_source_authority_schema,
        ne_source_reconciliation_claims,
        persist_current_acl_snapshot,
    )
    from ne.connect.authority_admission import (
        SqlAlchemySourceContractHeadStore,
        claim_or_load_connector_instance,
        ensure_admission_schema,
    )

    build = build_meridian_release(Path(fixture_path))
    dataset_name = build.descriptor.dataset_name

    ensure_source_authority_schema(runtime_engine)
    ensure_admission_schema(runtime_engine)
    construction_id = bind_deployment_construction_id(runtime_engine)
    # The generic per-deployment source-instance registry, reused verbatim: the id it
    # issues is durable and server-side, and re-pointing the deployment at a different
    # governed dataset is REFUSED rather than silently re-admitted. Reusing it mints a
    # ``governed_local_t3`` instance only — no M365 identity, point or lineage is
    # produced here, and none is claimed.
    claimed = claim_or_load_connector_instance(
        runtime_engine,
        deployment_id=construction_id,
        source_name=f"governed-local-t3:{dataset_name}",
        admitted_target_digest=governed_local_t3_admitted_target_digest(dataset_name),
    )
    instance = GovernedLocalT3Instance(
        deployment_id=construction_id,
        dataset_instance_id=claimed.connector_instance_id,
        generation=claimed.source_generation,
    )

    scope = governed_local_t3_scope(instance, dataset_name)
    registry = SelectedReleaseRegistry(scope)
    release = run_boot_coroutine(
        publish_meridian_release(
            registry, build, settings=ontology_settings, config_hash=config_hash
        )
    )

    head_store = SqlAlchemySourceContractHeadStore(runtime_engine)
    head = head_store.claim_or_load(
        scope,
        source_generation=instance.generation,
        contract=meridian_source_contract_identity(build.descriptor),
    )

    async def _admit_release_contributors() -> _GovernedLocalT3AuthorityIndex:
        """Publish every contributor receipt before resolving the contract head."""
        writer = SqlAlchemyCurrentSourceAuthorityWriter(runtime_engine, construction_id)
        with runtime_engine.connect() as conn:
            expected_revision = conn.execute(
                sa.select(ne_source_reconciliation_claims.c.head_revision).where(
                    ne_source_reconciliation_claims.c.scope_hash == scope.scope_hash
                )
            ).scalar_one_or_none()
        claim = await writer.claim(
            scope,
            expected_head_revision=(None if expected_revision is None else str(expected_revision)),
            lease_seconds=60,
        )
        stamps: dict[str, SourceAuthorityStamp] = {}
        try:
            with runtime_engine.connect() as conn:
                observed_at = conn.execute(sa.select(sa.func.now())).scalar_one()
            if isinstance(observed_at, str):
                observed_at = datetime.fromisoformat(observed_at)
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            else:
                observed_at = observed_at.astimezone(UTC)

            contract = meridian_source_contract_identity(build.descriptor)
            for contributor in build.contributors:
                labels = (contributor.graph_label,)
                acl_payload = json.dumps(
                    {"acl": list(labels)},
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                acl_snapshot_hash = hashlib.sha256(acl_payload.encode("utf-8")).hexdigest()
                identity = contributor_source_identity(
                    instance, build.descriptor, contributor.contributor_key
                )
                artifact = contributor_artifact_identity(
                    instance,
                    build.descriptor,
                    contributor.contributor_key,
                    content_snapshot_hash=contributor.content_hash,
                    acl_snapshot_hash=acl_snapshot_hash,
                )
                persist_current_acl_snapshot(
                    runtime_engine,
                    identity_hash=identity.identity_hash,
                    artifact_id=artifact.artifact_id,
                    acl_snapshot_hash=acl_snapshot_hash,
                    labels=labels,
                )
                candidate = SourceAuthorityCandidate(
                    source_identity=identity,
                    artifact_identity=artifact,
                    source_revision=release.release_id,
                    source_contract=contract,
                    contract_epoch=head.contract_epoch,
                    acl_snapshot_hash=acl_snapshot_hash,
                    state=SourceAuthorityState.PRESENT,
                    authority_as_of=observed_at,
                )
                fence = SourceAdmissionFence(
                    deployment_id=instance.deployment_id,
                    connector_instance_id=instance.dataset_instance_id,
                    source_generation=instance.generation,
                    external_source_revision=release.release_id,
                    contract_epoch=head.contract_epoch,
                    reconciliation_scope=claim.scope,
                    reconciliation_epoch=claim.reconciliation_epoch,
                    fencing_token=claim.fencing_token,
                )
                receipt = await writer.finalize(claim, fence, candidate)
                contributor_stamp = stamp_from_receipt(receipt)
                if contributor_stamp is None:
                    raise ValueError(
                        "a governed local T3 admission returned no disclosable artifact"
                    )
                stamps[contributor.contributor_key] = contributor_stamp
        finally:
            await writer.release(claim)

        if len(stamps) != len(build.contributors):
            raise RuntimeError("governed local T3 contributor publication was incomplete")
        if not head_store.mark_resolved(head):
            raise RuntimeError("governed local T3 contract head could not be resolved")
        graph_stamps: dict[str, list[SourceAuthorityStamp]] = {}
        for contributor in build.contributors:
            graph_stamps.setdefault(graph_iri_for_label(contributor.graph_label), []).append(
                stamps[contributor.contributor_key]
            )
        return _GovernedLocalT3AuthorityIndex(
            stamps,
            tuple(graph.graph_iri for graph in release.contributor_graphs),
            graph_stamps,
            head_store=head_store,
            head=head,
        )

    authority_index = run_boot_coroutine(_admit_release_contributors())
    descriptor = project_governed_local_t3_source_authority(
        dataset_instance_id=instance.dataset_instance_id,
        source_generation=instance.generation,
        head=head,
        selected_release_id=release.release_id,
        selected_release_algorithm_version=SELECTED_RELEASE_ALGORITHM_VERSION,
        dataset_name=dataset_name,
        resolved=head_store.is_resolved(scope),
    )
    reader = SelectedReleaseVerifiedReader(release, t3_store.ontology_version())
    # The one boot-time line saying what this process actually composed. Without it a
    # post-boot supersession (see _report_superseded) left NOTHING in the log to diff
    # against — cost: a full deploy cycle spent guessing which of three preconditions
    # was false at request time.
    _LOG.info(
        "governed local T3 composed: dataset=%s release=%s loader_schema_version=%d "
        "contract=%s epoch=%d resolved=%s contributors=%d graphs=%d",
        dataset_name,
        release.release_id,
        build.descriptor.loader_schema_version,
        head.source_contract.identity_hash,
        head.contract_epoch,
        descriptor.resolved,
        len(build.contributors),
        len(release.contributor_graphs),
    )
    return _GovernedLocalT3Composition(
        release=release,
        reader=reader,
        descriptor=descriptor,
        authority_index=authority_index,
    )


def _compose_workflow_execution_bindings(
    loaded: LoadedAgentManifest,
) -> tuple[
    Mapping[str, WorkflowExecutionProfile],
    Mapping[str, AgentExecutionBinding],
]:
    """Bind every executable manifest Agent to its exact strategy implementation.

    Unknown profile ids remain ordinary unavailable catalog dependencies. A manifest
    that names a bundled profile, however, must agree with every execution-bearing
    value; treating a mismatched pin as merely unavailable would let API and worker
    boot with contradictory definitions of the same profile identity.
    """
    bundled = {
        profile.workflow_id: profile
        for profile in (
            MERIDIAN_B1_DISASTER_HARDSHIP_PROFILE,
            MERIDIAN_B2_CURRENT_SOP_PROFILE,
            MERIDIAN_B3_FABRICATION_REFUSAL_PROFILE,
            MERIDIAN_S9_OVERDUE_CREDIT_PROFILE,
        )
    }
    bindings: dict[str, AgentExecutionBinding] = {}
    for definition in loaded.manifest.agents:
        declared = definition.workflow
        if declared is None:
            if definition.default_strategy != "agentic":
                continue
            budgets = definition.budgets
            if budgets.max_tool_calls < 1:
                raise ValueError("agentic Agent must allow at least one governed Tool call")
            # ONE derivation, shared with the catalog wire. The console prints
            # these exact numbers, so a displayed cap cannot drift from the cap
            # the adapter below is built with — they are the same object.
            caps = enforced_agentic_caps(budgets, definition.tool_grants)
            bindings[definition.agent_id] = create_agent_execution_binding(
                agent_id=definition.agent_id,
                agent_version=definition.version,
                strategy=definition.default_strategy,
                agentic_adapter=BoundedAgenticAdapter(
                    # The worker replaces this sentinel with the Agent's own manifest
                    # instructions plus the exact bounded Skill index after persisting
                    # its activation receipt (at least one of which is non-empty, by
                    # manifest contract). It is deliberately unusable as useful model
                    # guidance on its own.
                    instructions="Run Skill activation is required before execution.",
                    max_tool_calls=caps.max_tool_calls,
                    total_tokens_limit=caps.max_total_tokens,
                    wall_clock_seconds=caps.wall_clock_seconds,
                    max_parked_writes=caps.max_parked_writes,
                ),
            )
            continue
        if declared.profile_id not in bundled:
            continue
        profile = bundled[declared.profile_id]
        frozen_inputs = declared.frozen_inputs.canonical_values()
        if frozen_inputs != dict(profile.fixed_inputs):
            raise ValueError("Agent workflow frozen inputs do not match the executable profile")
        profile.validate_input({"max_subjects": declared.max_subjects})
        bindings[definition.agent_id] = create_agent_execution_binding(
            agent_id=definition.agent_id,
            agent_version=definition.version,
            strategy=definition.default_strategy,
            workflow_profile=profile,
            declared_profile_id=declared.profile_id,
            declared_profile_version=declared.version,
            declared_profile_config_hash=declared.config_hash,
        )
    return MappingProxyType(bundled), MappingProxyType(bindings)


def _has_admitted_m365_graph_population(engine: sa.Engine) -> bool:
    """Read the durable, content-free receipt left only after a graph write.

    The M365 graph runner writes AGE first and publishes its exact T1-bound graph
    evidence afterward. Requiring a nonempty entity receipt is therefore a
    conservative population fence: an empty/new AGE database, a host that never
    ran, or an old graph population without current T1 authority cannot advertise
    Tier 2. Unknown/missing tables are ordinary not-ready states.
    """
    inspector = sa.inspect(engine)
    required = {
        "ne_document_graph_evidence_artifacts",
        "ne_document_source_authority_receipts",
        "ne_document_source_snapshots",
    }
    if not all(inspector.has_table(name) for name in required):
        return False
    query = sa.text(
        """
        SELECT 1
        FROM ne_document_graph_evidence_artifacts AS graph_artifact
        JOIN ne_document_source_authority_receipts AS authority
          ON authority.document_id = graph_artifact.document_id
         AND authority.version_seq = graph_artifact.version_seq
        JOIN ne_document_source_snapshots AS snapshot
          ON snapshot.document_id = graph_artifact.document_id
         AND snapshot.version_seq = graph_artifact.version_seq
        WHERE graph_artifact.kind = 'entity'
          AND authority.state = 'present'
          AND snapshot.source_system = :source_system
        LIMIT 1
        """
    )
    try:
        with engine.connect() as conn:
            return conn.execute(query, {"source_system": _M365_SOURCE_SYSTEM}).first() is not None
    except SQLAlchemyError:
        return False


def _admitted_meridian_knowledge_sources(
    *,
    source_authority: M365SourceAuthorityDescriptor | None,
    current_search_evidence_bound: bool,
    graph_host_bound: bool,
    graph_authority_provider_bound: bool,
    graph_population_admitted: bool,
    graph_path_bound: bool,
) -> frozenset[tuple[str, str]]:
    """Admit T1/T2 only from their complete production evidence chains."""
    t1 = bool(
        source_authority is not None and source_authority.resolved and current_search_evidence_bound
    )
    admitted: set[tuple[str, str]] = set()
    if t1:
        admitted.add((_MERIDIAN_SOURCE_ID, "t1"))
    if (
        t1
        and graph_host_bound
        and graph_authority_provider_bound
        and graph_population_admitted
        and graph_path_bound
    ):
        admitted.add((_MERIDIAN_SOURCE_ID, "t2"))
    return frozenset(admitted)


def _production_agent_capability_metadata(
    *,
    registered_tools: frozenset[str],
    admitted_sources: frozenset[tuple[str, str]],
    source_authority: M365SourceAuthorityDescriptor | None,
    governed_local_t3_source_authority: GovernedLocalT3SourceAuthorityDescriptor | None,
    skill_ready: bool,
    graph_host_bound: bool,
    m365_snapshot: bool = False,
    verified_answer_columns: Mapping[str, tuple[str, ...]] | None = None,
) -> AgentCapabilityMetadata | None:
    """Build the closed safe Meridian inventory from exact composed bindings.

    ``verified_answer_columns`` (NE-CAPTAB) maps a verified-answer tool NAME to
    the column names its template's ``[answer]`` block declares — the one place
    composition can read that contract (the registry), so the capability
    projection can state what a template returns without re-deriving templates.
    Absent/None keeps every binding's ``answer_columns`` at None ("not stated").
    """
    local_t3_admitted = bool(
        governed_local_t3_source_authority is not None
        and governed_local_t3_source_authority.resolved
        and (_MERIDIAN_LOCAL_T3_SOURCE_ID, "t3") in admitted_sources
    )
    if source_authority is None and not local_t3_admitted:
        return None
    m365_connection = "m365:meridian-servicing"
    local_t3_connection = (
        "governed-local-t3:selected-release:"
        + governed_local_t3_source_authority.selected_release_id
        if local_t3_admitted and governed_local_t3_source_authority is not None
        else None
    )
    tool_bindings: list[ToolCapabilityBinding] = []
    for name, (safe_label, read_write_class) in _MERIDIAN_TOOL_LABELS.items():
        if name not in registered_tools:
            continue
        if name in _MERIDIAN_WORKFLOW_TOOLS:
            if local_t3_connection is None:
                continue
            connection = local_t3_connection
        elif name in _INTERNAL_CRM_TOOLS:
            # NE-S5-INTERNAL-CRM: registration is the staging proof (the
            # platform composes these bindings only with the internal clients
            # constructed), and the connection identity honestly names the
            # internal mirror — never the M365 label of the branch below.
            connection = _INTERNAL_CRM_CONNECTION
        else:
            # Preserve the original generic Analyst projection exactly: it is an
            # M365 capability surface and remains absent without M365 authority.
            if source_authority is None:
                continue
            connection = (
                "ne-skills:run-surface" if name == SKILLS_VIEW_TOOL_NAME else m365_connection
            )
        tool_bindings.append(
            ToolCapabilityBinding(
                tool_name=name,
                safe_label=safe_label,
                read_write_class=cast(Any, read_write_class),
                connection_identity_label=connection,
                answer_columns=(
                    None if verified_answer_columns is None else verified_answer_columns.get(name)
                ),
            )
        )
    t1 = (_MERIDIAN_SOURCE_ID, "t1") in admitted_sources
    t2 = (_MERIDIAN_SOURCE_ID, "t2") in admitted_sources
    tool_health: dict[str, bool | None] = {
        SKILLS_VIEW_TOOL_NAME: bool(skill_ready),
        # A frozen source retains its admitted catalog and historical evidence,
        # but boot/composition cannot be presented as current provider health.
        TOOL_NAME: None if m365_snapshot else bool(t1),
        GRAPH_PATH_TOOL_NAME: (
            None if m365_snapshot else (True if t2 else (False if graph_host_bound else None))
        ),
        # No connector runtime-health signal exists for the draft mailbox. Merely
        # constructing its handler cannot be laundered into a green status.
        _EMAIL_DRAFT_TOOL: None,
        _EMAIL_SEND_TOOL: None,
    }
    tiers = tuple(tier for tier in ("t1", "t2") if (_MERIDIAN_SOURCE_ID, tier) in admitted_sources)
    connector_bindings: list[ConnectorCapabilityBinding] = []
    connector_visibility: dict[str, StaticCapabilityVisibility] = {}
    if tiers:
        connector_bindings.append(
            ConnectorCapabilityBinding(
                source_id=_MERIDIAN_SOURCE_ID,
                connection_identity_label=m365_connection,
                safe_label=(
                    "Meridian servicing Microsoft 365 (frozen snapshot)"
                    if m365_snapshot
                    else "Meridian servicing Microsoft 365"
                ),
                kind="microsoft-365",
                sanitized_credential_owner_label="Configured Microsoft 365 application",
                composed_tiers=cast(Any, tiers),
                composed_health=None if m365_snapshot else bool(t1),
            )
        )
        connector_visibility[m365_connection] = StaticCapabilityVisibility(all_authenticated=True)
    if local_t3_connection is not None:
        connector_bindings.append(
            ConnectorCapabilityBinding(
                source_id=_MERIDIAN_LOCAL_T3_SOURCE_ID,
                connection_identity_label=local_t3_connection,
                safe_label="Meridian governed local T3",
                kind="governed-local-t3",
                sanitized_credential_owner_label=(
                    "Credential-free selected governed local T3 release"
                ),
                composed_tiers=("t3",),
                # A resolved immutable head is admission, not an ongoing backend
                # health monitor. Do not turn boot success into permanent green.
                composed_health=None,
            )
        )
        connector_visibility[local_t3_connection] = StaticCapabilityVisibility(
            groups=(_MERIDIAN_OPS_GROUP,)
        )
    return AgentCapabilityMetadata(
        tool_bindings=tuple(tool_bindings),
        static_tool_visibility={
            binding.tool_name: (
                StaticCapabilityVisibility(groups=(_MERIDIAN_OPS_GROUP,))
                if binding.tool_name in _MERIDIAN_OPS_ONLY_TOOLS
                else StaticCapabilityVisibility(all_authenticated=True)
            )
            for binding in tool_bindings
        },
        connector_bindings=tuple(connector_bindings),
        static_connector_visibility=connector_visibility,
        tool_health=tool_health,
    )


def _production_agent_dependency_snapshot(
    *,
    registered_tools: frozenset[str],
    knowledge_sources: frozenset[tuple[str, str]],
    skill_pins: frozenset[tuple[str, int, str]],
    workflow_profiles: Mapping[str, WorkflowExecutionProfile],
    model_policies: frozenset[str],
    agent_execution_bindings: Mapping[str, AgentExecutionBinding],
    agent_transport_ready: bool,
) -> AgentDependencySnapshot:
    """Gate executable dependencies on the complete protected-Run composition."""
    meridian = agent_execution_bindings.get(_MERIDIAN_AGENT_ID)
    execution_ready = bool(
        agent_transport_ready and meridian is not None and meridian.agentic_adapter is not None
    )
    return AgentDependencySnapshot(
        tools=registered_tools if execution_ready else frozenset(),
        knowledge_sources=knowledge_sources,
        skills=skill_pins,
        workflow_profiles=frozenset(
            (profile.workflow_id, profile.version, profile.config_hash)
            for profile in workflow_profiles.values()
        ),
        model_policies=model_policies,
    )


def build_platform(api_settings: ApiSettings, seams: AppSeams) -> Platform:
    """Compose the governed platform ONCE (spec §3.2 steps 1-11); freeze the hash.

    The VERBATIM ``create_app`` steps (1)-(11 session store). Both ne-api and
    ne-worker call this, so the composed ``config_hash`` and the governed spine are
    guaranteed identical across processes when the composed settings match.
    """
    registry = seams.plugin_registry if seams.plugin_registry is not None else PluginRegistry()

    # (1) Settings + secrets + the A1 claim union + the merged tool policy.
    identity_settings = _ensure_api_claims(
        seams.identity_settings if seams.identity_settings is not None else IdentitySettings()  # type: ignore[call-arg]
    )
    runtime_settings = seams.runtime_settings or RuntimeSettings()
    # NE-085c (additive): resolve verified-answer setup (None when disabled — default).
    # Its arg policies merge FIRST so a caller-supplied extra_tool_policies still wins.
    from ne.api.verified_tools import (
        VerifiedKnowledgeEvidence,
        build_verified_answer_bindings,
        load_verified_answer_setup,
    )
    from ne.brain_ontology.config import OntologySettings

    # Resolve the flywheel settings HERE (moved above the verified-setup block,
    # NE-ONTOLOGY-FLYWHEEL): the governed HITL template store rides the SAME
    # flywheel signal database, so its URL is the overlay source for the enabled
    # verified-answer registry. Default-off (verified disabled OR empty store URL)
    # leaves the enabled/default hash byte-identical.
    flywheel_settings = seams.flywheel_settings or FlywheelSettings()
    # NE-SKILL-1a (additive): the Skill Forge read side is ENABLED iff the flywheel
    # signal database is configured — the registry runs on the SAME engine as the
    # flywheel review service (the same-engine requirement, plan §2.2). Default-off
    # (empty signal URL) resolves the settings but stamps NOTHING, so the composed
    # config_hash + tool menu stay BYTE-IDENTICAL to pre-unit (the 085c/NE-INGEST
    # conditional-stamp pattern). Env-driven ``NE_SKILLS_*``; no AppSeams field.
    skills_settings = SkillsSettings()  # type: ignore[call-arg]
    skills_enabled = bool(flywheel_settings.signal_database_url)
    skills_policies: dict[str, ToolArgPolicy] = (
        {
            SKILLS_SEARCH_TOOL_NAME: SKILLS_SEARCH_ARG_POLICY,
            SKILLS_VIEW_TOOL_NAME: SKILLS_VIEW_ARG_POLICY,
            # NE-SKILL-1b: the tier-1 forge cron tool takes ZERO model args; its
            # EMPTY ToolArgPolicy MUST be in guardrails.tools BEFORE freeze or the
            # cron executor rejects it fail-closed (mirrors principal_sync_policies).
            "write.internal.skills.forge_run": ToolArgPolicy(),
            # NE-SKILL-4: the tier-1 monthly Gardener cron tool — same zero-arg
            # policy (a tool absent from guardrails.tools is fail-closed-rejected by
            # the worker's governed executor). ONE dict reused by the projected
            # demo-seam config-hash cross-check below, so this covers both sites.
            "write.internal.skills.gardener_run": ToolArgPolicy(),
        }
        if skills_enabled
        else {}
    )
    ontology_settings = seams.ontology_settings or OntologySettings()
    verified_setup = load_verified_answer_setup(
        ontology_settings, governed_store_url=flywheel_settings.signal_database_url or None
    )
    # RED-ALERT Meridian demo wiring (additive, default-off): a banking fixture
    # may never be loaded behind an incomplete/disabled verified registry. Check
    # before config freeze and before resolving the T3 store so a deployment
    # fails at boot instead of advertising workflows whose tools cannot exist.
    if api_settings.demo_meridian_fixture_path:
        if verified_setup is None:
            raise ValueError(
                "NE_API_DEMO_MERIDIAN_FIXTURE_PATH requires verified answers enabled "
                "(NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED + verified_templates_dir)"
            )
        from ne.brain_ontology import MERIDIAN_TEMPLATE_IDS
        from ne.brain_ontology.verified import MERIDIAN_GRAPH_LABELS

        available_template_ids = set(verified_setup.registry.fingerprint.template_ids)
        missing_template_ids = sorted(set(MERIDIAN_TEMPLATE_IDS) - available_template_ids)
        if missing_template_ids:
            raise ValueError(
                "NE_API_DEMO_MERIDIAN_FIXTURE_PATH requires every packaged Meridian "
                f"verified templates; missing: {missing_template_ids}"
            )
        # ...and the OTHER direction. A SURPLUS template is equally a "workflow whose
        # tools cannot exist" whenever governed local T3 pins the reader: below, every
        # verified binding is handed the read-only SelectedOxigraphRelease, whose
        # ``confine`` refuses any graph outside the release's own set — and a Meridian
        # release holds exactly MERIDIAN_GRAPH_LABELS. So a BUNDLED template declaring
        # a ``[scope] graph_classes`` label the release provably does not hold can only
        # ever be advertised on the tool menu and then fail at invoke with a 502
        # ReleaseScopeError. Refuse to boot instead, exactly as the guard above does
        # for the missing direction.
        #
        # PROVENANCE — the check runs over EVERY template in the BOOT-DIRECTORY bundle,
        # canonical Meridian ids included, and pointedly NOT over
        # ``verified_setup.registry.templates``. That registry is the bundle
        # OVERLAID with the governed steward/HITL template store whenever a flywheel
        # signal database is configured (``verified_tools._load_registry_with_governed_
        # store``), which the pilot compose does. A steward APPLIES templates into that
        # store at runtime (/v1/steward/proposals/apply-verified-template), so if a
        # stored row could trip this guard the next restart would raise HERE and ne-api
        # AND ne-worker would both refuse to start, with no in-product recovery — a
        # human would have to delete the row from ``ne_verified_templates`` by hand.
        # Turning a recoverable runtime refusal into an unbootable box is strictly worse
        # than the 502 it replaces. So a steward-applied unservable template keeps its
        # PRE-EXISTING behaviour (advertised, then 502 at invoke), and only bundle
        # content can stop the box. ONE MISS IS DELIBERATE AND WORTH NAMING: a store
        # row may SUPERSEDE a bundle slot at a higher version, so a servable bundle
        # template can be replaced at runtime by an unservable one and this check will
        # not see it. That is the same trade -- a runtime action must never brick boot --
        # but it means bundle SLOTS are not fully covered either, only bundle CONTENT.
        #
        # What IS covered: bundle content — build-time content we package, where a
        # packaging mistake SHOULD stop
        # the box — fails loudly at boot. The registry carries no provenance field, so
        # the bundle set is obtained by re-loading the boot directory: a one-shot,
        # boot-only, demo-fixture-only re-read of files the same boot already parsed.
        # SURPLUS IS NOT THE ONLY WAY BUNDLE CONTENT GOES UNSERVABLE, and an earlier
        # version of this check exempted every id in ``MERIDIAN_TEMPLATE_IDS`` — so a
        # template we ship OURSELVES could keep its canonical id, drift its ``[scope]
        # graph_classes`` outside the release, satisfy the missing-id check above, be
        # advertised, and 502 at invoke: exactly the failure this guard exists to
        # prevent, on exactly the content we control. Membership was never the
        # question; servability is. So the predicate runs over the WHOLE bundle.
        #
        # PREDICATE — SUBSET, deliberately STRICTER than what ``confine`` refuses at
        # runtime; do NOT lower it to match. Runtime servability is an INTERSECTION
        # predicate: ``scoped_dataset`` builds the dataset from ``principal labels ∩
        # template graph_classes``, so a template scoped
        # ["bank.customer.read", "all-employees"] invoked by a principal holding only
        # "bank.customer.read" yields a dataset ``confine`` ACCEPTS. This guard refuses
        # that template anyway, because for PACKAGED content naming any label outside
        # the release is a packaging mistake: the menu entry would answer for some
        # principals and 502 for whoever actually holds "all-employees". A bundle
        # template whose labels are ALL inside the release stays permitted.
        if ontology_settings.governed_local_t3_enabled:
            from ne.brain_ontology.verified.templates import load_template_registry

            assert ontology_settings.verified_templates_dir is not None  # load validated this.
            bundle_templates = load_template_registry(
                ontology_settings.verified_templates_dir
            ).templates
            unservable_template_ids = sorted(
                template.template_id
                for template in bundle_templates
                if not set(template.graph_classes) <= set(MERIDIAN_GRAPH_LABELS)
            )
            if unservable_template_ids:
                raise ValueError(
                    "NE_ONTOLOGY_GOVERNED_LOCAL_T3_ENABLED pins every verified read to "
                    f"the Meridian release graphs {sorted(MERIDIAN_GRAPH_LABELS)}; these "
                    "packaged bundle templates address graphs outside it and could only "
                    f"fail at invoke: {unservable_template_ids}"
                )
    # NE-135 (additive): the T3 ONTOLOGY-READ surface (governed HTTP routes) is enabled
    # whenever ANY explicit T3 store is configured — verified answers on, OR an embedded
    # oxigraph path, OR a Fuseki URL. Default ``OntologySettings()`` sets none of these,
    # so the predicate is False and the platform stays byte-identical to pre-NE-135
    # (no store resolved, no stamp, routes answer 503). This is a SUPERSET of the 085c
    # verified-only condition (see the ne-brain-ontology stamp below).
    ontology_read_enabled = (
        verified_setup is not None
        or ontology_settings.oxigraph_path is not None
        or ontology_settings.fuseki_url is not None
    )
    # NE-INGEST (P1-4, additive): resolve the connector-sync config (env-driven,
    # NE_CONNECT_SYNC_*; no AppSeams field — tests drive it via monkeypatch.setenv)
    # and build one EMPTY ToolArgPolicy per configured source. Empty args = the
    # tier-1 ``write.internal.connect.sync_<source>`` tool accepts ZERO model-supplied
    # arguments (the flywheel no-injectable-surface pattern). These policies MUST be
    # merged into guardrails.tools BEFORE freeze_platform_config: the toolargs
    # guardrail fail-closes any tool absent from that map, and the frozen config_hash
    # must reflect the real config with ne-api/ne-worker cross-process parity (§6.2).
    from ne.connect.runner import (
        ConnectSyncSettings,
        SyncNotConfiguredError,
        build_sync_settings,
        configured_source_name,
        sync_tool_name,
    )

    connect_sync_settings = ConnectSyncSettings()  # type: ignore[call-arg]
    # A configured connector's authoritative source-link hosts/origin influence
    # both mapper output and durable dedup. Resolve those exact connector settings
    # before the shared API/worker freeze so identical executable deployments
    # share one hash and authority changes move it. Listed-but-unconfigured sources
    # retain their existing read-only API posture; the worker still fails loud when
    # it later builds the executable sync surface.
    connect_source_settings: dict[str, Any] = {}
    for source in connect_sync_settings.sources:
        if source not in {"m365", "salesforce"}:
            continue
        try:
            connect_source_settings[source] = build_sync_settings(source)
        except SyncNotConfiguredError:
            continue
    connect_policies = {
        sync_tool_name(source): ToolArgPolicy() for source in connect_sync_settings.sources_to_run
    }
    connect_request_policies = {
        sync_request_tool_name(source): ToolArgPolicy()
        for source in connect_sync_settings.sources_to_run
    }
    permissions_settings = seams.permissions_settings or PermissionsSettings()
    # NE-FIX-SYNC-INVOKERS (P2-20, additive): the governed principal-sync cron tool
    # takes ZERO model-supplied args; register its EMPTY ToolArgPolicy ONLY when a
    # pull-capable principal source (mode entra/fixture) is configured, so the toolargs
    # guardrail (reject-by-default) ADMITS the cron tool and the default-off
    # (oidc-claims) config_hash stays BYTE-IDENTICAL (the NE-INGEST default-off
    # pattern). The tool name mirrors ``ne.worker.principal_sync._TOOL`` verbatim —
    # ne-api MUST NOT import ne-worker (the dependency points the other way), so the
    # string is duplicated here with this cross-reference.
    principal_sync_policies = (
        {"write.internal.permissions.principal_sync": ToolArgPolicy()}
        if permissions_settings.mode in ("entra", "fixture")
        else {}
    )
    # NE-DEMO-LIVE-SEAMS (additive, default-off): the governed TIER-2 demo write-back
    # tool's arg policy MUST be in guardrails.tools BEFORE freeze (a tool absent from
    # that map is fail-closed-rejected). Lazy import — ne.api.demo_seams drags no heavy
    # dependency. Empty dict when the seam is off ⇒ byte-identical default.
    from ne.api.demo_seams import DEMO_WRITEBACK_ACTION, DEMO_WRITEBACK_POLICY

    demo_policies: dict[str, ToolArgPolicy] = (
        {DEMO_WRITEBACK_ACTION: DEMO_WRITEBACK_POLICY} if api_settings.demo_writeback_tool else {}
    )
    # NE-ACTION-WIRE (additive, default-off): the 3 NE-050 reference write actions'
    # arg policies MUST be in guardrails.tools BEFORE freeze (an unmerged action is
    # fail-closed-rejected as a toolargs violation — the SAME discipline as every
    # other conditional-stamp policy above). Lazy import (the demo_seams precedent
    # immediately above). Empty dict when the flag is off ⇒ byte-identical default.
    from ne.runtime.reference_actions import (
        CRM_NOTE_ARG_POLICY,
        EMAIL_DRAFT_ARG_POLICY,
        EMAIL_SEND_ARG_POLICY,
        TICKET_UPDATE_ARG_POLICY,
    )

    selected_write_actions = (
        frozenset({_CRM_NOTE_TOOL, _TICKET_UPDATE_TOOL, _EMAIL_DRAFT_TOOL, _EMAIL_SEND_TOOL})
        if api_settings.write_actions_enabled
        else frozenset(api_settings.write_action_tools)
    )
    reference_action_policies = {
        _CRM_NOTE_TOOL: CRM_NOTE_ARG_POLICY,
        _TICKET_UPDATE_TOOL: TICKET_UPDATE_ARG_POLICY,
        _EMAIL_DRAFT_TOOL: EMAIL_DRAFT_ARG_POLICY,
        _EMAIL_SEND_TOOL: EMAIL_SEND_ARG_POLICY,
    }
    write_action_policies: dict[str, ToolArgPolicy] = {
        tool: reference_action_policies[tool] for tool in selected_write_actions
    }
    # NE-WORKFLOWS: the Tier-0 CRM account LOOKUP read tool. It rides the write-action
    # flag deliberately — it is built over the SAME Salesforce connector settings the
    # write client is built from, so there is nothing for it to read against unless
    # those legs resolved. Its arg policy has the same fail-closed pre-freeze
    # requirement as every policy above.
    from ne.runtime.reference_actions import (
        CRM_ACCOUNT_LOOKUP_ARG_POLICY,
        CRM_CONTACT_LOOKUP_ARG_POLICY,
    )

    if _CRM_NOTE_TOOL in selected_write_actions:
        write_action_policies[_CRM_LOOKUP_TOOL] = CRM_ACCOUNT_LOOKUP_ARG_POLICY
        write_action_policies[_CRM_CONTACT_TOOL] = CRM_CONTACT_LOOKUP_ARG_POLICY
    # NE-ACTION-WIRE / Codex C1: resolve the write-action connector *settings*
    # (not just the flag) BEFORE freeze — production only, and only when a seam
    # hasn't already injected fake clients (tests: no real destination to pin;
    # flag-off: nothing to resolve). Missing/invalid legs raise ValueError HERE,
    # before ANYTHING is frozen — fail loud, fail early, and the resolved
    # settings get stamped into extra_modules below so the frozen config_hash
    # pins the actual write DESTINATIONS, not merely "write actions are on".
    write_action_env_settings: _WriteActionEnvSettings | None = None
    if api_settings.write_actions_enabled and seams.write_action_clients is None:
        write_action_env_settings = _load_write_action_env_settings()
    selected_write_action_env_settings: _SelectedWriteActionEnvSettings | None = None
    if api_settings.write_action_tools and seams.write_action_clients is None:
        selected_write_action_env_settings = _load_selected_write_action_env_settings(
            api_settings.write_action_tools
        )
    merged_policies: Mapping[str, ToolArgPolicy] | None = seams.extra_tool_policies
    if (
        connect_policies
        or connect_request_policies
        or principal_sync_policies
        or verified_setup is not None
        or demo_policies
        or skills_policies
        or write_action_policies
    ):
        # Connect policies merge FIRST, then the principal-sync policy, then verified
        # answers, then the demo write-back, then the skill tools, then the write
        # actions, then seams.extra — so a caller-supplied policy still wins.
        # Default-off (no sources, oidc-claims, no verified setup, no demo tool, no
        # flywheel signal DB, no write actions) leaves merged_policies ==
        # seams.extra_tool_policies.
        merged: dict[str, ToolArgPolicy] = dict(connect_policies)
        merged.update(connect_request_policies)
        merged.update(principal_sync_policies)
        if verified_setup is not None:
            merged.update(verified_setup.policies)
        merged.update(demo_policies)
        merged.update(skills_policies)
        merged.update(write_action_policies)
        merged.update(seams.extra_tool_policies or {})
        merged_policies = merged
    govern_settings = _merge_tool_policy(seams.govern_settings or GovernSettings(), merged_policies)
    audit_settings = seams.audit_settings or AuditSettings()
    gateway_settings = seams.gateway_settings or GatewaySettings()
    # NE-CTXB-3: derive the DEFAULT runtime context budget from the composed
    # GatewaySettings — the SINGLE owner of the estimator params + budget (design
    # §2.3). The runtime task's route names its PRIMARY model; the trim guard
    # targets THAT model's input budget (design §2.2: NOT min-over-candidates — the
    # gateway per-candidate pre-flight is the belt for fallback rolls).
    #
    # NE-CTX-IN-FORCE — READ THIS BEFORE TREATING THE VALUE BELOW AS THE ENFORCED
    # ONE. It is the composed DEFAULT and the trim guard's on/off switch, not the
    # number every turn is measured against. ``GatewayModel`` re-derives the budget
    # per REQUEST from the model actually in force (``TurnContextBudget`` on the
    # gateway: the governed per-run selection if one is pinned, else the task's
    # primary under the CURRENT route table) and falls back to this value only when
    # the injected gateway cannot name that model. That is what lets each lane
    # declare its OWN serving window instead of every lane being capped at the
    # narrowest one.
    #
    # CONFIG_HASH, stated honestly because the hash exists so two processes can
    # PROVE they agree: this field stays in the frozen preimage exactly as before,
    # so the hash is byte-identical and the drift tripwire is untouched. What the
    # hash pins is not weakened either — the per-turn budget is a pure function of
    # (a) ``gateway_settings``, already frozen under 'ne-gateway' with every model's
    # window and output reserve, and (b) the in-force model NAME, which is governed
    # and audited (``model.routing.changed`` / ``model.selection``) rather than
    # hashed, on the same standing as the egress posture and the route register
    # themselves. So the hash pins the FUNCTION and its inputs' definitions; the
    # audit chain pins which evaluation was in force when. ne-api and ne-worker
    # agree because they run this same recipe over the same env (§6.2/Q5) AND read
    # the same route register on the shared audit database.
    #
    # ALWAYS model_copy so BOTH process roots (ne-api + ne-worker run this same recipe)
    # freeze and thread the identical updated instance; the _project_config_hash
    # closure and Platform(runtime_settings=...) below pick up the rebind. This new
    # settings state moves the ne-runtime module-subset config_hash — EXPECTED (the
    # DispatchSettings precedent, SPEC-row DoD e) — and holds cross-process
    # §6.2/Q5 parity because ne-worker composes the same env. It happens BEFORE the
    # freeze block and the projection closure below, so the drift tripwire holds.
    runtime_route = gateway_settings.tasks.get(runtime_settings.task)
    if runtime_route is not None:
        runtime_primary = next(m for m in gateway_settings.models if m.name == runtime_route.model)
        runtime_context_budget = gateway_settings.context_budget_for(runtime_primary)
    else:
        # An unrouted runtime task ⇒ trim guard DISABLED (budget=None). Nothing
        # silently downgrades: the gateway raises UnknownTaskError on the first
        # complete() for this task anyway, so the composition is honest either way.
        _LOG.info(
            "runtime task %r is not routed in gateway_settings.tasks; "
            "context-budget trim guard disabled (budget=None)",
            runtime_settings.task,
        )
        runtime_context_budget = None
    runtime_settings = runtime_settings.model_copy(
        update={
            "context_budget": runtime_context_budget,
            "bytes_per_token": gateway_settings.bytes_per_token,
            "per_call_overhead": gateway_settings.per_call_overhead,
        }
    )
    eval_settings = seams.eval_settings or EvalSettings()
    secrets_settings = seams.secrets_settings or SecretsSettings()
    obs_settings = seams.obs_settings or ObsReportSettings()
    # NE-087 (O-4, additive): resolve the optional OpenFGA ReBAC settings (LOCAL
    # import — the verified-tools precedent above). Default-disabled unless
    # NE_REBAC_ENABLED (and a pinned store+model) are set; enabled-without-ids
    # raises at construction (fail-closed boot).
    from ne.permissions_rebac.config import RebacSettings

    rebac_settings = seams.rebac_settings if seams.rebac_settings is not None else RebacSettings()
    # NE-BRAIN-T2-WIRING (additive): resolve the Tier-2 graph settings (LOCAL import —
    # the verified-tools/ReBAC precedent). Env-driven; BrainGraphSettings reads the
    # NE_BRAIN_GRAPH_ env prefix (e.g. NE_BRAIN_GRAPH_ENABLED / NE_BRAIN_GRAPH_DATABASE_URL /
    # NE_BRAIN_GRAPH_GRAPH_NAME). The graph tier stays OFF unless ``enabled`` — default-off
    # keeps everything below
    # byte-identical to pre-unit.
    from ne.brain_graph.config import BrainGraphSettings

    brain_graph_settings = (
        seams.brain_graph_settings
        if seams.brain_graph_settings is not None
        else BrainGraphSettings()
    )
    secret_store = create_secret_store(secrets_settings, registry)

    # PRE-008: parse exact registry bytes before freeze. A configured malformed
    # or missing file fails the API and worker identically; empty is default-off.
    loaded_agent_manifest = load_agent_manifest(api_settings.agent_manifest_path)
    workflow_execution_profiles, agent_execution_bindings = _compose_workflow_execution_bindings(
        loaded_agent_manifest
    )

    # (2) Freeze the COMPOSED platform config_hash ONCE, before any emitter. The
    # extra_modules mapping is LOAD-BEARING: cross-process hash parity depends on
    # ne-api and ne-worker freezing the IDENTICAL mapping (SPEC §6.2/Q5).
    extra_modules: dict[str, Any] = {
        "ne-identity": identity_settings,
        "ne-gateway": gateway_settings,
        "ne-audit": audit_settings,
        "ne-eval": eval_settings,
        "ne-flywheel": flywheel_settings,
        "ne-api": api_settings,
        "ne-permissions": permissions_settings,
        "ne-secrets": secrets_settings,
        "ne-obs": obs_settings,
    }
    # NE-085c/NE-135 (additive): stamp the ontology settings into the frozen hash when
    # the T3 read surface is enabled (a SUPERSET of the 085c verified-only condition —
    # see below), and the template-registry fingerprint ONLY when verified answers are
    # enabled. The default-off config_hash stays BYTE-IDENTICAL to pre-085c (§11.2
    # config_hash input), AND a verified-enabled deployment hashes IDENTICALLY to today
    # (verified-on ⇒ ontology_read_enabled, so the same two stamps land). The ONLY
    # config state whose hash MOVES is oxigraph_path/fuseki_url-set-but-verified-OFF — a
    # previously-inert state now expressed as an enabled read surface (NE-135 PR note,
    # the 085c/087 conditional-stamp precedent).
    if ontology_read_enabled:
        extra_modules["ne-brain-ontology"] = ontology_settings
    if verified_setup is not None:
        extra_modules["ne-brain-ontology-templates"] = verified_setup.fingerprint
    # NE-087 (O-4, additive): stamp the ReBAC settings into the frozen hash ONLY when
    # the module is enabled — so the default-off config_hash stays BYTE-IDENTICAL to
    # pre-087 (the 085c conditional-stamp pattern).
    if rebac_settings.enabled:
        extra_modules["ne-permissions-rebac"] = rebac_settings
    # NE-INGEST (P1-4, additive): stamp the connector-sync settings into the frozen
    # hash ONLY when at least one source is configured (the 085c/087 conditional-stamp
    # pattern) — so the default-off config_hash stays BYTE-IDENTICAL to pre-NE-INGEST.
    if connect_sync_settings.sources:
        extra_modules["ne-connect-sync"] = connect_sync_settings
    for source, connector_settings in sorted(connect_source_settings.items()):
        # Hashing the typed settings model uses the platform's standard SecretStr
        # masking. No credential bytes enter the preimage, while source-link
        # authority and every other executable connector decision remain pinned.
        extra_modules[f"ne-connect-source-{source}"] = connector_settings
    # NE-BRAIN-T2-WIRING (F1, additive): stamp the Tier-2 graph settings into the
    # frozen hash ONLY when the graph tier is enabled (the 085c/087/NE-INGEST
    # conditional-stamp pattern) — so the default-off config_hash stays BYTE-IDENTICAL
    # to pre-unit. When enabled, the frozen hash PINS the ER/graph config
    # (er_proposal_threshold, er_classes, max_hops, graph_expansion_backend,
    # retrieval_expansion_hops) that a governed SAME_AS merge stamps via
    # get_config_hash() (audit-integrity F1). Cross-process §6.2/Q5 parity holds
    # automatically because ne-worker runs this same recipe over the same env.
    if brain_graph_settings.enabled:
        extra_modules["ne-brain-graph"] = brain_graph_settings
    # NE-SKILL-1a (additive): stamp the SkillsSettings into the frozen hash ONLY when
    # skills are enabled (⇔ flywheel signal DB configured) — the default-off config_hash
    # stays BYTE-IDENTICAL to pre-unit (the 085c/087/NE-INGEST conditional-stamp pattern).
    if skills_enabled:
        extra_modules["ne-skills"] = skills_settings
    if loaded_agent_manifest.configured:
        assert loaded_agent_manifest.fingerprint is not None
        extra_modules["ne-agent-registry"] = loaded_agent_manifest.fingerprint
    # NE-ACTION-WIRE (additive, default-off) / Codex C1: stamp the RESOLVED
    # write-action connector settings into the frozen hash — not just the flag —
    # whenever production resolved them above (write_actions_enabled AND no
    # seam-injected fake clients). ``None`` (flag off, or a seam injected fakes
    # with no real destination) leaves the config_hash BYTE-IDENTICAL to
    # pre-unit (the 085c/087/NE-INGEST conditional-stamp pattern). When present,
    # changing the SQL DSN / Salesforce instance-api_version / M365 tenant now
    # MOVES the platform config_hash — the previous behaviour silently let two
    # deployments with different write destinations freeze to the identical
    # hash, which config-drift/audit tooling could not distinguish.
    # NE-S5-INTERNAL-CRM: there is no ``ne-action-salesforce`` stamp any more —
    # the CRM legs write the platform's OWN stores, whose identities are already
    # frozen (runtime settings / vector-store settings). Dropping the stamp is
    # an EXPECTED release-level config_hash change, PR-flagged.
    if write_action_env_settings is not None:
        extra_modules["ne-action-sql"] = write_action_env_settings.sql
        extra_modules["ne-action-m365"] = write_action_env_settings.m365
    if selected_write_action_env_settings is not None:
        if selected_write_action_env_settings.sql is not None:
            extra_modules["ne-action-sql"] = selected_write_action_env_settings.sql
        if selected_write_action_env_settings.m365 is not None:
            extra_modules["ne-action-m365"] = selected_write_action_env_settings.m365
    config_hash = freeze_platform_config(
        settings=runtime_settings,
        govern_settings=govern_settings,
        extra_modules=extra_modules,
    )

    # NE-ONTOLOGY-FLYWHEEL: the governed-curation seam. The projection closure
    # recomputes the platform config_hash for a HYPOTHETICAL projected template
    # REGISTRY — every OTHER frozen module held fixed — so a steward apply can audit
    # the PROJECTED config_hash delta, and a fresh restart provably freezes to
    # exactly that hash (the re-fingerprint provability test).
    #
    # CRITICAL: a HITL-added template mutates TWO hashed inputs at the next boot, not
    # one — (i) the ne-brain-ontology-templates registry fingerprint AND (ii) the
    # ne-govern guardrail tool-policy map (build_platform merges one ToolArgPolicy per
    # template into guardrails.tools BEFORE freezing). The projection therefore
    # re-derives BOTH from the projected registry, mirroring this function's OWN merge
    # (connect sync → connect request → verified → seams.extra) VERBATIM. The
    # self-consistency check below
    # (projection of the BOOT registry == the frozen hash) is the drift tripwire that
    # fails loud at boot if this composition ever diverges from freeze_platform_config.
    verified_curation: VerifiedCurationContext | None = None
    if verified_setup is not None:
        from ne.api.verified_curation import VerifiedCurationContext
        from ne.api.verified_tools import build_tool_policies
        from ne.contracts.config import compute_platform_config_hash

        _govern_base = seams.govern_settings or GovernSettings()
        _extra_wo_templates = {
            k: v for k, v in extra_modules.items() if k != "ne-brain-ontology-templates"
        }

        def _project_config_hash(registry: TemplateRegistry) -> str:
            projected_policies: dict[str, ToolArgPolicy] = dict(connect_policies)
            projected_policies.update(connect_request_policies)
            projected_policies.update(build_tool_policies(registry))
            # NE-DEMO-LIVE-SEAMS / NE-ACTION-WIRE: mirror the boot merge order
            # (verified → demo → skills → write actions → extra) so the projection
            # accounts for the demo tool's + skill tools' + write actions' policies;
            # otherwise the drift tripwire fails when demo_writeback_tool / the skill
            # tools / write_actions_enabled AND verified answers are all on.
            projected_policies.update(demo_policies)
            projected_policies.update(skills_policies)
            projected_policies.update(write_action_policies)
            projected_policies.update(seams.extra_tool_policies or {})
            projected_govern = _merge_tool_policy(_govern_base, projected_policies)
            return compute_platform_config_hash(
                {
                    "ne-runtime": runtime_settings,
                    "ne-govern": projected_govern,
                    **_extra_wo_templates,
                    "ne-brain-ontology-templates": registry.fingerprint,
                }
            )

        if _project_config_hash(verified_setup.registry) != config_hash:
            raise RuntimeError(
                "verified-answer config_hash projection drifted from the frozen platform "
                "hash — the projection composition no longer mirrors freeze_platform_config "
                "(NE-ONTOLOGY-FLYWHEEL drift tripwire)"
            )
        assert ontology_settings.verified_templates_dir is not None  # load validated this.
        verified_curation = VerifiedCurationContext(
            ontology_settings=ontology_settings,
            boot_dir=ontology_settings.verified_templates_dir,
            project_config_hash=_project_config_hash,
        )

    engines: list[sa.Engine] = []

    # (3) ONE runtime engine per URL (session store + approval gate + outbox ride it).
    if not runtime_settings.session_database_url:
        raise ValueError(
            "RuntimeSettings.session_database_url is empty; set "
            "NE_RUNTIME_SESSION_DATABASE_URL for the process"
        )
    runtime_engine = create_engine_with_timeouts(
        runtime_settings.session_database_url, timeouts=runtime_settings.engine_timeouts
    )
    ensure_transactional_sqlite(runtime_engine)
    ensure_runtime_schema(runtime_engine)
    connect_sync_requests = ManualJobRequestStore(runtime_engine)
    document_lineage = SqlAlchemyDocumentLineageStore(runtime_engine)
    engines.append(runtime_engine)

    # (4) The ONE audit sink shared by every governed component below.
    sink = create_audit_sink(audit_settings, registry)

    # (4b) The audit READ engine for /v1/audit/* (list_records + verify_chain +
    # replay). It is the SAME database the sink writes; schema-ensure is idempotent.
    if not audit_settings.database_url:
        raise ValueError(
            "AuditSettings.database_url is empty; the /v1/audit read surface "
            "requires NE_AUDIT_DATABASE_URL"
        )
    audit_engine = create_engine_with_timeouts(
        audit_settings.database_url, timeouts=audit_settings.engine_timeouts
    )
    ensure_transactional_sqlite(audit_engine)
    ensure_audit_schema(audit_engine)
    engines.append(audit_engine)
    health = _degraded_sources_provider(
        audit_engine, configured_sync_resources(connect_sync_settings, api_settings)
    )

    # (4c) NE-137: wrap the ONE audit sink so a chat turn can OBSERVE its own governed
    # tool-call transits (allow/deny/pending + REAL audit seq) into the inline PDP
    # lane. create_tool_executor builds the PDP + guardrails over THIS sink and the
    # gate shares it (factory.py), so ALL pdp.decide / approval.requested /
    # tool.approval / tool.execute events flow through the observer. It delegates
    # append FIRST (governance fail-closed untouched) and is a pure pass-through
    # unless a chat turn set the TURN_TOOL_TRANSITS ContextVar — ne-worker and every
    # non-chat route see byte-identical behavior.
    sink = ToolTransitObservingSink(sink, audit_engine=audit_engine)

    # (4d) The ONE boot-frozen egress config + the durable runtime egress-posture
    # register, and the per-call provider the live guard reads. LOCAL imports (the
    # RebacSettings line-372 precedent). On the AUDIT engine so a governed flip and
    # its chain record commit atomically (C10), and so BOTH process roots (ne-api +
    # ne-worker share the audit database) enforce the same posture. Constructed
    # UNCONDITIONALLY — even when seams.gateway is injected — so a malformed
    # NE_EGRESS_* is a LOUD boot error in ALL compositions (the documented
    # fail-at-factory norm) rather than a request-time posture error, and so
    # /v1/posture and POST /v1/network/air-gap work in every composition.
    #
    # NE-SEARCH-LATENCY moved this block UP from step (9): the rerank adapter at
    # step (8) is an egress client whose air-gap posture must be the SAME live
    # register the gateway enforces, so it has to exist before the retriever is
    # composed. Nothing between here and (9) reads it; (9) consumes these names.
    from ne.gateway.egress import EgressSettings

    egress_settings = EgressSettings()  # type: ignore[call-arg]
    from ne.api.network_egress import (
        EgressPostureStore,
        build_egress_settings_provider,
        ensure_egress_posture_schema,
    )

    ensure_egress_posture_schema(audit_engine)
    route_store: ModelRouteStore

    def _frontier_routed_tasks() -> tuple[str, ...]:
        # The air-gap engage guard's evidence: every effective route whose
        # PRIMARY is on the frontier lane, as "task -> model" strings — NAMED,
        # not a bare bool, so the refusal (FrontierRoutedTasksError -> HTTP 409)
        # tells the operator exactly which routes to repoint. A route naming an
        # unregistered model KeyErrors here and the store fails CLOSED
        # ("routes could not be checked"), same as before.
        by_name = route_store.models_by_name()
        return tuple(
            sorted(
                f"{task} -> {route.model}"
                for task, route in route_store.effective_routes().items()
                if by_name[route.model].lane is Lane.FRONTIER_API
            )
        )

    egress_store = EgressPostureStore(
        audit_engine,
        egress_settings,
        _frontier_routed_tasks,
    )
    egress_settings_provider = build_egress_settings_provider(egress_store)

    # ADR-0021: durable API-owned lifecycle state over a closed, local-only
    # operator seam. Missing transport is an intentional read-only partial
    # rollout: reads report UNKNOWN and every mutation fails closed before HITL.
    from ne.api.inference_lifecycle import (
        InferenceLifecycleService,
        UdsInferenceTargetOperator,
        build_inference_lifecycle_binding,
        ensure_inference_lifecycle_schema,
    )

    ensure_inference_lifecycle_schema(audit_engine)
    inference_operator = seams.inference_operator
    if inference_operator is None and api_settings.inference_lifecycle.operator_socket:
        inference_operator = UdsInferenceTargetOperator(
            api_settings.inference_lifecycle.operator_socket,
            expected_uid=api_settings.inference_lifecycle.operator_uid,
            expected_gid=api_settings.inference_lifecycle.operator_gid,
            expected_parent_mode=api_settings.inference_lifecycle.operator_parent_mode,
            expected_socket_mode=api_settings.inference_lifecycle.operator_socket_mode,
        )
    inference_lifecycle = InferenceLifecycleService(
        engine=audit_engine,
        config=api_settings.inference_lifecycle,
        operator=inference_operator,
        air_gap_provider=lambda: effective_egress(egress_settings_provider())[0],
        config_hash=config_hash,
        model_lanes={model.name: model.lane for model in gateway_settings.models},
    )

    def _egress_air_gap_engaged() -> bool:
        """The LIVE air-gap posture, read per call — the gateway's own term.

        ``EgressPolicy`` gates its allowlist on ``settings.air_gap or
        _air_gap_env()``; this is the first half of that expression, resolved
        through the same fail-closed provider (an unreadable posture store reports
        air-gapped). The bare-global ``AIR_GAP`` half is read inside
        ``ne.net.ssrf.allowlist_revoked_by_air_gap`` on every check, so a caller
        can only ever ADD restriction here. Passed to the rerank adapters so a
        governed ``write.internal.network.egress.restrict`` flip stops an
        ALREADY-CONSTRUCTED reranker from POSTing to an allowlisted external host.
        """
        return bool(egress_settings_provider().air_gap)

    # (6) The NE-070H durable shared spend ledger + rate counter pair.
    ledger, counter = create_shared_guardrail_state(runtime_settings.session_database_url)

    # (7) Permissions / C7 acl filter (never None).
    perm_store, expander, perm_sync = build_permissions_stack(permissions_settings, sink)

    # PRE-015A0-ENFORCEMENT: compose the source authority and protected-result
    # capabilities once, before any consumer factory exists.  The private-payload
    # key is never a config-hash input and the API surface receives read/write
    # capabilities separately (request state gets no Writer).
    agent_run_store = _compose_agent_run_store(
        runtime_engine, deployment_configured=seams.agent_private_payload_configured
    )
    configured_transport_generation = os.environ.get("NE_AGENT_TRANSPORT_GENERATION", "").strip()
    if configured_transport_generation and (
        configured_transport_generation != PROTECTED_TRANSPORT_GENERATION or agent_run_store is None
    ):
        raise ValueError("protected Agent transport generation is not ready")
    agent_transport_generation = configured_transport_generation or None
    agent_transport_ready = bool(
        agent_run_store is not None
        and agent_transport_generation == PROTECTED_TRANSPORT_GENERATION
        and api_settings.default_ask_generation == "default-ask/canonical-run/v1"
    )

    # NE-MODEL-ROUTING: the durable runtime ROUTE register, beside the egress
    # posture and on the same AUDIT engine for the same reasons — a change and
    # its chain record commit atomically (C10), and both process roots share the
    # audit database so a change reaches EVERY gateway, not just this process.
    # Constructed UNCONDITIONALLY (the egress precedent) so /v1/models/routing
    # works in every composition; the per-call provider it feeds is passed to
    # _default_gateway below, and an INJECTED gateway seam simply does not read
    # it (a test double routes however it likes). Constructed AFTER the Run
    # store because the routing gate reads it: a repoint target is measured
    # against the LARGEST live conversation snapshot for the task (SPEC
    # PRE-DEMO-014 — no governed compaction exists, no silent truncation), read
    # per judgement from the same durable boundary receipts the per-run
    # selection gate reads. No Run store ⇒ no conversations ⇒ honestly no
    # snapshot to fit (None), never a fabricated zero.
    from ne.api.model_routing import (
        ModelRouteStore,
        build_route_provider,
        ensure_model_route_schema,
    )

    ensure_model_route_schema(audit_engine)
    route_store = ModelRouteStore(
        audit_engine,
        gateway_settings,
        lambda: bool(egress_settings_provider().air_gap),
        snapshot_tokens_provider=(
            None
            if agent_run_store is None
            else agent_run_store.largest_live_context_snapshot_estimate_sync
        ),
    )
    route_provider = build_route_provider(route_store)
    source_authority_reader, source_authority_continuation = _compose_source_authority(
        runtime_engine
    )
    from ne.brain.current_source_filter import (
        CurrentSourceDisclosureEvaluator,
        SqlAlchemyCurrentAclSnapshotResolver,
    )

    source_authority_evaluator = CurrentSourceDisclosureEvaluator(
        source_authority_reader,
        SqlAlchemyCurrentAclSnapshotResolver(runtime_engine),
        group_expander=expander,
    )

    async def _authorize_current_document(
        document_id: str,
        version_seq: int,
        principal: Principal,
        access_context_hash: str,
    ) -> bool:
        retained = document_lineage.retained_source_authority(document_id, version_seq)
        if retained is None:
            return False
        load_receipt = getattr(source_authority_reader, "load_receipt", None)
        if not callable(load_receipt):
            return False
        receipt = cast(CurrentSourceAuthorityReceipt | None, load_receipt(retained.receipt_id))
        if receipt is None:
            return False
        artifact = receipt.artifact_identity
        if (
            receipt.state is not SourceAuthorityState.PRESENT
            or artifact is None
            or receipt.receipt_hash != retained.receipt_hash
            or receipt.source_identity.identity_hash != retained.source_identity_hash
            or artifact.artifact_id != retained.artifact_id
            or receipt.contract_epoch != retained.contract_epoch
        ):
            return False
        # The cross-checks above are what this caller must prove; the stamp
        # itself comes from the ONE canonical derivation (ne.contracts).
        stamp = stamp_from_receipt(receipt)
        if stamp is None:  # pragma: no cover - guarded by the checks above
            return False
        try:
            decision = await source_authority_evaluator.evaluate(
                stamp,
                principal=principal,
                access_context_hash=access_context_hash,
            )
        except Exception:
            return False
        return decision.authorized

    async def _evaluate_source_stamp(stamp: Any, context: Any) -> SourceAuthorityDecision:
        principal = Principal(
            id=context.principal_id,
            groups=tuple(context.authorization_groups),
            claims={"department": context.department} if context.department is not None else {},
        )
        return (
            await source_authority_evaluator.evaluate(
                stamp,
                principal=principal,
                access_context_hash=context.context_hash,
            )
        ).decision

    source_resolver = (
        RunStoreSourceBoundResultResolver(agent_run_store, _evaluate_source_stamp)
        if agent_run_store is not None
        else UnavailableSourceBoundResultResolver()
    )
    source_guard = ProtectedSourceResultGuard(source_resolver)
    protected_source_output_writer = (
        None if agent_run_store is None else RunStoreProtectedSourceOutputWriter(agent_run_store)
    )
    protected_source_execution_writer = (
        None if agent_run_store is None else RunStoreProtectedSourceExecutionWriter(agent_run_store)
    )
    knowledge_evidence_author = (
        None if agent_run_store is None else KnowledgeSourceEvidenceAuthor(agent_run_store)
    )

    def _access_context_hash(principal: Principal) -> str:
        context = require_authenticated_context()
        if context.principal_id != principal.id:
            raise ValueError("graph principal does not match authenticated context")
        if tuple(context.authorization_groups) != tuple(sorted(set(principal.groups))):
            raise ValueError("graph groups do not match authenticated context")
        return context.context_hash

    def _authority_context_hash_or_unavailable() -> str:
        try:
            return require_authenticated_context().context_hash
        except MissingAuthenticatedContextError:
            return ""

    # (5) The ONE durable approval gate (over the runtime engine, sharing the sink).
    gate = create_approval_gate(
        sink=sink,
        settings=runtime_settings,
        engine=runtime_engine,
        source_guard=source_guard,
        run_store=agent_run_store,
    )
    # NE-087 (O-4, additive): when ReBAC is enabled, inject the shared-document
    # resolver as the SINGLE C7 union seam — PrincipalAclFilter(shared_docs=...).
    # Default-off constructs the plain filter, BYTE-IDENTICAL to pre-087. The
    # retained client is closed by both process roots on shutdown (FIX-2).
    rebac_client: OpenFgaTupleClient | None = None
    if rebac_settings.enabled:
        from ne.permissions_rebac.factory import create_openfga_client
        from ne.permissions_rebac.resolver import OpenFgaSharedDocumentResolver

        rebac_client = (
            seams.rebac_client
            if seams.rebac_client is not None
            else create_openfga_client(rebac_settings)
        )
        resolver = OpenFgaSharedDocumentResolver(rebac_client, expander, perm_store, rebac_settings)
        acl = PrincipalAclFilter(expander, shared_docs=resolver)
    else:
        acl = PrincipalAclFilter(expander)

    # (8) Brain — the ONE acl-trimmed retriever (acl_filters ALWAYS the real filter).
    embedder = (
        seams.embedder
        if seams.embedder is not None
        else _default_embedder(api_settings, air_gap=_egress_air_gap_engaged)
    )
    sparse_encoder = seams.sparse_encoder if seams.sparse_encoder is not None else _default_sparse()
    chunk_store = (
        seams.chunk_store
        if seams.chunk_store is not None
        else _default_chunk_store(api_settings, embedder, sparse_encoder, expander, registry)
    )
    # NE-164 read-path audit rides the same (4c) ToolTransitObservingSink-wrapped
    # sink as every governed component; one knowledge.retrieve event per call
    # plus a counts-only knowledge.withhold event on a source-health trim (C7).
    # Store/embedder/sparse_encoder are bound as `brain_edges` below
    # purely so the sole retriever construction line stays under the 100-col
    # limit while still carrying its acl_filters= keyword (the test_no_bypass.py
    # C7 guardian's grep target) on that one line.
    brain_edges = (chunk_store, embedder, sparse_encoder)
    base = HybridRetriever(*brain_edges, acl_filters=acl, audit_sink=sink, source_health=health)
    # SPEC §3.1 rerank stage: wrap the ONE acl-trimmed retriever in the
    # RetrievalPipeline (rerank-on by default). Rerank rides ABOVE the acl-trimmed
    # retriever — it adds NO second permission path; C7 trimming happens exactly
    # once, BELOW the pipeline (ne.brain.pipeline docstring). An empty rerank URL
    # EXPLICITLY disables the stage (degrade to the bare retriever, logged).
    reranker = (
        seams.reranker
        if seams.reranker is not None
        else (
            _default_reranker(api_settings, air_gap=_egress_air_gap_engaged)
            if api_settings.tei_rerank_url
            else None
        )
    )
    retriever: KnowledgeRead
    if reranker is not None:
        # NE-RERANK-FANOUT: candidate_multiplier is the reranker's selection pool;
        # max_candidate_k is the ceiling that keeps it from compounding with the
        # over-fetch CurrentSourceAuthorizedKnowledgeRead applies ABOVE this
        # pipeline (its window arrives here as top_k). Default Ask:
        # retrieval_top_k=5 -> wrapper window 10 -> 40 reranked candidates, the
        # documented figure. /v1/brain/search accepts top_k up to 50,
        # where the ceiling — not the multiplier — is what bounds rerank cost.
        retriever = RetrievalPipeline(
            base, reranker, candidate_multiplier=4, max_candidate_k=RERANK_CANDIDATE_CEILING
        )
    else:
        _LOG.warning(
            "NE_API_TEI_RERANK_URL is empty — rerank stage DISABLED; shipped "
            "retrieval diverges from the Checkpoint-C eval-gated pipeline (SPEC §3.1)"
        )
        retriever = base

    # (8a) Brain Tier-2 graph retrieval (NE-BRAIN-T2-WIRING) — OFF by default. When
    # the graph tier is enabled, wrap the reranked T1 pipeline in a GraphRetriever so
    # the full brain ladder (T1 hybrid → T2 graph → T3 ontology) composes end-to-end.
    entity_graph: EntityGraph | None = None
    # NE-DEMO-LIVE-SEAMS: the composed graph read store, captured so the optional boot
    # ontology lift can scan it (None unless the graph tier is enabled).
    composed_graph_store: GraphReadStore | None = None
    if brain_graph_settings.enabled:
        # Fail LOUD at boot on the LightRAG backend: ne-api composes the NATIVE T2
        # backend only. The LightRAG sidecar (blocking venv build, out-of-process C4
        # lane) needs a sidecar-host wiring this unit does not ship — refuse here so
        # it is never a silent runtime RuntimeError on the first query (fail-closed).
        if brain_graph_settings.graph_expansion_backend != "native":
            raise ValueError(
                "NE-BRAIN-T2-WIRING: ne-api composes the NATIVE Tier-2 graph backend "
                "only; graph_expansion_backend="
                f"{brain_graph_settings.graph_expansion_backend!r} requires a LightRAG "
                "sidecar host this unit does not ship (fail-closed at boot)."
            )
        from ne.brain_graph.lightrag.retrieval import GraphRetriever
        from ne.brain_graph.read import EntityGraph

        if seams.graph_store is not None:
            graph_store = seams.graph_store
        else:
            graph_store = _default_graph_store(brain_graph_settings)
        composed_graph_store = graph_store
        # F2/C7 (LOAD-BEARING): ``expander`` is the IDENTICAL PrincipalExpander
        # instance behind the chunk retriever's PrincipalAclFilter (step 7). Graph
        # reads and chunk reads MUST resolve the SAME label set for a principal
        # (audit F2) — an independently-injected expander would let the graph tier
        # over-return relative to the caller's ACLs.
        # NE-164b read-path audit rides the SAME (4c) ToolTransitObservingSink-
        # wrapped sink as every other governed component; one `knowledge.graph_read`
        # event per completed public read, counts + adapter-side trim delta only
        # (C7) — mirrors the NE-164 HybridRetriever audit_sink= wiring above.
        entity_graph = EntityGraph(
            graph_store,
            group_expander=expander,
            settings=brain_graph_settings,
            audit_sink=sink,
            source_health=health,
            source_authority=source_authority_reader,
            access_context_hash=_access_context_hash,
        )
        # The graph wraps the RERANKED pipeline (T1+rerank BELOW, T2 ABOVE) — NOT the
        # reverse: RetrievalPipeline.search constructs RetrievalResult(chunks=,
        # citations=) and would DROP graph_paths if it wrapped a GraphRetriever (the
        # exact F3 silent-drop hazard). Graph-above-rerank keeps graph_paths intact.
        retriever = GraphRetriever(entity_graph, retriever, brain_graph_settings)
        # NEVER log database_url (the DSN may carry credentials — C2); graph_name only.
        _LOG.info(
            "graph tier (T2) ENABLED — GraphRetriever composed over the reranked "
            "retrieval path (graph=%s)",
            brain_graph_settings.graph_name,
        )
    else:
        _LOG.info(
            "graph tier (T2) disabled — retrieval is T1 hybrid(+T3 when configured); "
            "set NE_BRAIN_GRAPH_ENABLED (BrainGraphSettings) to compose the graph tier"
        )

    # The reduced scan multiplier exists solely to bound rerank work; select it
    # only when the rerank pipeline is actually composed. With rerank disabled
    # the bare retriever keeps the full pre-NE-RERANK-FANOUT authorization
    # headroom (see BARE_RETRIEVER_SCAN_MULTIPLIER) — otherwise a top_k=5 read
    # scans 10 rows and can return a short page where 40 would have filled it,
    # bounding rerank work that is not being performed.
    current_retriever: KnowledgeRead = CurrentSourceAuthorizedKnowledgeRead(
        retriever,
        source_authority_evaluator,
        scan_multiplier=(
            RERANKED_SCAN_MULTIPLIER if reranker is not None else BARE_RETRIEVER_SCAN_MULTIPLIER
        ),
    )

    m365_graph_sync_host: M365GraphSyncHost | None = None
    m365_graph_authority_provider_bound = False
    #: 015A0-AUTHORITY-M365 — the CREDENTIAL-FREE M365 contract descriptor and its
    #: current epoch. ``None`` when M365 authority is not configured. This is a
    #: projection, never a producer: it carries no Writer, no claim and no
    #: credential, and request state never receives it.
    m365_source_authority: M365SourceAuthorityDescriptor | None = None
    if brain_graph_settings.enabled and "m365" in connect_sync_settings.sources:
        if not brain_graph_settings.sources:
            raise ValueError(
                "scheduled M365 sync with Tier-2 enabled requires at least one "
                "NE_BRAIN_GRAPH_SOURCES mapping; the worker validates the exact "
                "configured M365 source identity before seeding"
            )
        assert composed_graph_store is not None
        required_writes = (
            "upsert_nodes",
            "upsert_edges",
            "upsert_edges_confirmed",
            "update_acl",
            "delete_where_stale",
            "delete_edges_from_source",
        )
        missing_writes = [
            method for method in required_writes if not hasattr(composed_graph_store, method)
        ]
        if missing_writes:
            raise ValueError(
                "scheduled M365 graph sync requires a writable graph store; missing "
                f"capabilities {missing_writes!r}"
            )
        from ne.brain_graph.construct.ledger import GraphSyncLedger
        from ne.brain_graph.construct.runner import GraphWriteStore, M365GraphSyncHost

        graph_ledger_engine = create_engine_with_timeouts(
            brain_graph_settings.ledger_url or brain_graph_settings.database_url,
            timeouts=runtime_settings.engine_timeouts,
        )
        ensure_transactional_sqlite(graph_ledger_engine)
        engines.append(graph_ledger_engine)
        graph_authority_provider = _RetainedM365GraphSourceAuthorityProvider(
            document_lineage,
            source_authority_reader,
        )
        m365_graph_sync_host = M365GraphSyncHost(
            store=cast("GraphWriteStore", composed_graph_store),
            ledger=GraphSyncLedger(graph_ledger_engine),
            mappings=brain_graph_settings.sources,
            evidence_recorder=document_lineage,
            source_authority_provider=graph_authority_provider,
            audit_sink=sink,
            config_hash=config_hash,
        )
        m365_graph_authority_provider_bound = True

    # (8b) CDC sink (NE-084) — OFF by default. Constructed ONLY when a webhook
    # secret is configured (fail-closed default: unset ⇒ no service ⇒ no /cdc
    # mount ⇒ 404). Governs Debezium change events onto the SAME chunk_store via
    # the governed KnowledgeWriter (no new write path). The ne.connect.cdc import
    # is LOCAL to keep the module import cheap and to keep dlt out of ne-api's
    # eager import graph (ne.connect.cdc is dlt-free by construction).
    cdc_service: CdcSinkService | None = None
    if api_settings.cdc_webhook_secret.get_secret_value():
        cdc_service = _build_cdc_service(
            api_settings, chunk_store, sink, runtime_engine, document_lineage
        )

    # (9) Gateway — inject a seam in tests; construct the real guarded gateway in prod.
    # NE-UI-P2-CLEANUP: the ONE boot-frozen egress config, the runtime posture
    # register, and the per-call provider are all constructed at step (4d) above
    # (moved there by NE-SEARCH-LATENCY so the rerank adapter at step (8) can share
    # the live posture). The live guard is built OVER that same instance and
    # /v1/posture reports THAT instance (§0 law 5 reported==enforced). It is NOT
    # stamped into extra_modules (no new frozen-contract field; default config_hash
    # byte-identical).
    from ne.api.model_routing import build_model_routing_bindings
    from ne.api.network_egress import build_network_egress_bindings

    gateway = (
        seams.gateway
        if seams.gateway is not None
        # NE-162: the fail-closed pre-egress redactor (C2/C12) is wired in
        # UNCONDITIONALLY at this composition root, alongside the egress guard.
        else _default_gateway(
            gateway_settings,
            runtime_engine,
            sink,
            egress_settings_provider,
            govern_settings,
            route_provider,
            redaction_exempt_addresses=_redaction_exempt_addresses(api_settings, govern_settings),
        )
    )
    # NE-GA-G7 (demo-only, default-off): wraps the composed `gateway` (from
    # EITHER branch above — an injected test/demo seam OR the real
    # `_default_gateway`) so a LIVE `ne demo up` run can prove the grounding
    # gate refuses a fabricated draft against the real pod model, not only in
    # the CI fixture arc. MUST wrap here, immediately after gateway resolution
    # and BEFORE the executor/assist-closure/dispatcher constructions below
    # capture `gateway` — wrapping any later would leave the G4 assist path
    # unintercepted and the live refuse leg could be "redeemed" by a real-model
    # assist re-draft, erasing the withheld notice the demo arc asserts.
    if api_settings.demo_grounded_hallucination:
        if not ontology_settings.grounded_agent_enabled:
            raise ValueError(
                "NE_API_DEMO_GROUNDED_HALLUCINATION requires the grounded agent "
                "enabled (NE_ONTOLOGY_GROUNDED_AGENT_ENABLED): the seam only makes "
                "sense over the grounded-analyst exhibit"
            )
        from ne.api.demo_seams import DemoGroundedHallucinationGateway

        gateway = DemoGroundedHallucinationGateway(gateway)

    # (9b) NE-T2X-4: the extraction pathway boot checks + composition.
    #
    # Boot fail-loud #1 (B-P1-4, the NE-HGATE-3 shape verbatim): an unrouted
    # 'graph-extract' task would leave the job's every gateway call raising
    # UnknownTaskError at RUN time — refuse to boot such a deployment instead.
    # Deliberately a NO-OP when tasks == {} (the ne-api conftest / injected-seam
    # default), mirroring the HGATE-3 no-op-when-unrouted semantics above.
    from ne.brain_graph.extraction import EXTRACTION_TASK

    if (
        brain_graph_settings.extraction_enabled
        and gateway_settings.tasks
        and EXTRACTION_TASK not in gateway_settings.tasks
    ):
        raise ValueError(
            f"NE_BRAIN_GRAPH_EXTRACTION_ENABLED requires a {EXTRACTION_TASK!r} route in "
            "NE_GATEWAY_TASKS: every extraction completion rides task="
            f"{EXTRACTION_TASK!r} and an unrouted task raises UnknownTaskError at call "
            "time, failing every extraction run. Add the route or set "
            "NE_BRAIN_GRAPH_EXTRACTION_ENABLED=false."
        )
    # Checks #2 (YOLO-without-consent) and #3 (extraction-without-graph) are
    # ALREADY enforced UNCONDITIONALLY at settings construction
    # (BrainGraphSettings._validate_extraction, config.py) — a config artifact
    # can never even express either invalid combo, so no second, potentially-
    # drifting platform-layer check is added here (pinned by
    # test_extraction_wiring.py, not duplicated).
    extraction: ExtractionRuntime | None = None
    if brain_graph_settings.extraction_enabled:
        if not flywheel_settings.signal_database_url:
            raise ValueError(
                "NE_BRAIN_GRAPH_EXTRACTION_ENABLED requires a flywheel signal database "
                "(NE_FLYWHEEL_SIGNAL_DATABASE_URL): every extraction mode writes "
                "graph_extraction_candidate proposal rows (design F5) — extraction "
                "without the proposal ledger is unrepresentable"
            )
        if seams.extraction_chunk_source is None:
            raise ValueError(
                "NE_BRAIN_GRAPH_EXTRACTION_ENABLED requires AppSeams.extraction_chunk_source: "
                "ChunkStore has no scan-by-source surface, so a host composition must "
                "inject a ChunkSource — an extraction composition that cannot enumerate "
                "chunks must never boot silently-dead"
            )
        # entity_graph is None only when brain_graph_settings.enabled is False, which
        # BrainGraphSettings._validate_extraction already refuses alongside
        # extraction_enabled=True — composed_graph_store is therefore always set here.
        assert composed_graph_store is not None
        from ne.api.extraction import (
            ChunkStoreAclResolver,
            ExtractionRuntime,
            ImmediateWriteContext,
        )
        from ne.brain_graph.extraction.job import ExtractionJob
        from ne.flywheel.proposals import ensure_proposal_schema
        from ne.flywheel.review import ProposalReviewService, ensure_review_schema
        from ne.schema import SCHEMA_VERSION

        extraction_engine = create_engine_with_timeouts(
            flywheel_settings.signal_database_url, timeouts=runtime_settings.engine_timeouts
        )
        ensure_transactional_sqlite(extraction_engine)
        ensure_review_schema(extraction_engine)
        ensure_proposal_schema(extraction_engine)
        engines.append(extraction_engine)

        def extraction_clock() -> datetime:
            return datetime.now(UTC)

        extraction_authority_principal = "system:graph-extraction"
        extraction_authority_context_hash = hashlib.sha256(
            f"graph-extraction:{config_hash}".encode()
        ).hexdigest()

        # Fail-loud AT BOOT (not AttributeError at first candidate-scan call) if
        # the composed graph store cannot support the read-only scan every mode
        # needs — mirrors the modes-2/3 hasattr fail-loud below, but unconditional
        # because ExtractionJob's candidate scan runs regardless of mode.
        if not hasattr(composed_graph_store, "scan_nodes"):
            raise ValueError(
                "NE_BRAIN_GRAPH_EXTRACTION_ENABLED requires a graph store with "
                "scan_nodes (ExtractionScanStore): the job's candidate scan needs it "
                "in every extraction mode; the composed store is missing it"
            )
        # Legitimate: this cast sits directly behind the hasattr fail-loud above,
        # so the type assertion is backed by a runtime boot check, not an assumption.
        scan_store = cast("ExtractionScanStore", composed_graph_store)
        job = ExtractionJob(
            scan_store,
            extraction_engine,
            brain_graph_settings,
            gateway,
            seams.extraction_chunk_source,
            audit_sink=sink,
            config_hash=config_hash,
            clock=extraction_clock,
            source_authority=source_authority_reader,
            authority_principal_id=extraction_authority_principal,
            authority_access_context_hash=extraction_authority_context_hash,
        )

        write_ctx: ImmediateWriteContext | None = None
        if brain_graph_settings.extraction_mode != "ingest_review":
            # Modes 2/3 write immediately — fail-loud AT BOOT if the composed
            # graph store cannot support it, never AttributeError at runtime.
            required_methods = (
                "fetch_nodes",
                "upsert_nodes",
                "upsert_edges",
                "scan_nodes",
                "scan_edges",
                "purge_llm_extracted",
            )
            missing = [m for m in required_methods if not hasattr(composed_graph_store, m)]
            if missing:
                raise ValueError(
                    f"NE_BRAIN_GRAPH_EXTRACTION_MODE={brain_graph_settings.extraction_mode!r} "
                    "requires an immediate-write-capable graph store; the composed store is "
                    f"missing: {sorted(missing)}"
                )
            # Legitimate: this cast sits directly behind the required_methods
            # hasattr fail-loud immediately above, so the type assertion is
            # backed by a runtime boot check, not an assumption.
            apply_store = cast("ExtractionApplyStore", composed_graph_store)
            write_ctx = ImmediateWriteContext(
                engine=extraction_engine,
                review_service=ProposalReviewService(extraction_engine),
                store=apply_store,
                acl_resolver=ChunkStoreAclResolver(chunk_store),
                schema_version=SCHEMA_VERSION,
                clock=extraction_clock,
                source_authority=source_authority_reader,
                authority_access_context_hash=extraction_authority_context_hash,
            )

        extraction = ExtractionRuntime(
            job=job,
            mode=brain_graph_settings.extraction_mode,
            sink=sink,
            config_hash=config_hash,
            write_ctx=write_ctx,
        )

    # (10) The ONE governed executor (NE-CKD1 recipe; sink+gate REQUIRED kwargs).
    agent_identity_settings = AgentIdentitySettings()
    credential_service = create_agent_credential_service(
        agent_identity_settings, store=secret_store, sink=sink
    )
    scope_rail = AgentScopeGuardrail(credential_service)
    # NE-085c/NE-135 (additive): the verified-answer tools become standard bindings in
    # the SAME frozen executor — no ungoverned query path. Empty tuple when disabled.
    # NE-135: the T3 gateway is resolved EXACTLY ONCE here (an embedded oxigraph path is
    # never opened twice) and shared by BOTH the verified bindings and the read routes.
    verified_bindings: tuple[ToolBinding, ...] = ()
    graph_path_bindings: tuple[ToolBinding, ...] = (
        ()
        if entity_graph is None or knowledge_evidence_author is None
        else (
            build_graph_path_binding(
                EntityGraphPathRead(entity_graph),
                authority_reader=source_authority_reader,
                access_context_hash_provider=_access_context_hash,
                principal_provider=current_tool_principal,
                evidence_author=knowledge_evidence_author,
                evidence_context_provider=current_existing_knowledge_read,
            ),
        )
    )
    ontology_state: OntologyReadState | None = None
    governed_local_t3: _GovernedLocalT3Composition | None = None
    t3_authority_index: Any | None = None
    if ontology_read_enabled:
        from ne.api.verified_tools import build_ontology_read_state, resolve_triple_store

        t3_store = resolve_triple_store(registry, ontology_settings, config_hash)
        # 015A0-AUTHORITY-T3: when the governed local dataset is enabled, the release
        # head is selected HERE, before any binding exists, and the bindings receive
        # the pinned read-only release reader instead of the mutable gateway. The
        # gateway keeps serving the steward/browse routes below, which are not
        # source-bearing. Default-off deployments take the identical `t3_store`
        # branch they took before this unit, so their composition is byte-identical.
        governed_local_t3 = _compose_governed_local_t3_source_authority(
            runtime_engine,
            api_settings=api_settings,
            ontology_settings=ontology_settings,
            t3_store=t3_store,
            config_hash=config_hash,
        )
        if governed_local_t3 is not None:
            t3_authority_index = governed_local_t3.authority_index
        verified_store: VerifiedReadStore = (
            t3_store if governed_local_t3 is None else governed_local_t3.reader
        )
        if verified_setup is not None:

            async def _t3_contributor_stamp(contributor: str) -> Any | None:
                if t3_authority_index is None:
                    return None
                return await t3_authority_index.resolve(contributor)

            async def _t3_contributor_snapshot(graphs: tuple[str, ...]) -> Any:
                if t3_authority_index is None:
                    return ()
                return await t3_authority_index.snapshot_stamps(graphs)

            verified_bindings = build_verified_answer_bindings(
                verified_setup,
                plugin_registry=registry,
                ontology_settings=ontology_settings,
                store=verified_store,
                expander=expander,
                config_hash=config_hash,
                audit_sink=sink,
                contributor_stamp_provider=_t3_contributor_stamp,
                contributor_snapshot_provider=_t3_contributor_snapshot,
                source_authority=source_authority_reader,
                authority_access_context_hash_provider=_authority_context_hash_or_unavailable,
                authorized_contributor_graphs_provider=lambda: (
                    t3_authority_index.authorized_graphs()
                    if t3_authority_index is not None
                    else ()
                    if governed_local_t3 is None
                    else tuple(
                        graph.graph_iri for graph in governed_local_t3.release.contributor_graphs
                    )
                ),
                evidence=(
                    None
                    if knowledge_evidence_author is None
                    else VerifiedKnowledgeEvidence(
                        author=knowledge_evidence_author,
                        context_provider=current_existing_knowledge_read,
                    )
                ),
            )
        ontology_state = build_ontology_read_state(ontology_settings, t3_store, verified_setup)
        # NE-DEMO-LIVE-SEAMS (additive, default-off): boot-lift the composed graph store
        # into the T3 store so a verified-answer template returns a real table. Requires
        # BOTH the ontology read surface (this branch) AND the graph tier enabled — else
        # a LOUD boot ValueError (never a silently empty verified answer).
        if api_settings.demo_ontology_lift:
            if composed_graph_store is None:
                raise ValueError(
                    "NE_API_DEMO_ONTOLOGY_LIFT requires the graph tier enabled "
                    "(NE_BRAIN_GRAPH_ENABLED=true) so there is a graph store to lift from"
                )
            from ne.api.demo_seams import run_demo_ontology_lift

            lifted_authority = run_demo_ontology_lift(composed_graph_store, t3_store)
            if governed_local_t3 is None:
                t3_authority_index = lifted_authority
        # The generic graph lift is a COMPLETE desired-state reconciliation: it
        # deliberately vacates ACL graphs absent from T2. Meridian is an independent
        # manifest-bound T3 projection, so load it LAST or the Acme-only reconcile
        # correctly purges the three bank.* graphs that B1/B2/B3 read.
        if api_settings.demo_meridian_fixture_path:
            from ne.api.demo_seams import run_demo_meridian_fixture_load

            meridian_report = run_demo_meridian_fixture_load(
                Path(api_settings.demo_meridian_fixture_path), t3_store
            )
            # 015A0-AUTHORITY-T3: the advertised contract must describe the release
            # this boot actually loaded. The selected release and the serving
            # materialization are two loads of the same checked-in bytes, so a
            # divergence means one of them read something else — fail loud rather
            # than advertise a descriptor no load produced.
            if (
                governed_local_t3 is not None
                and meridian_report.release != governed_local_t3.release.descriptor
            ):
                raise ValueError(
                    "the Meridian release materialized into the serving store is not the "
                    "release selected as the governed local T3 head "
                    f"({meridian_report.release.descriptor_hash} != "
                    f"{governed_local_t3.release.descriptor.descriptor_hash})"
                )
    elif api_settings.demo_ontology_lift:
        raise ValueError(
            "NE_API_DEMO_ONTOLOGY_LIFT requires the T3 ontology read surface enabled "
            "(NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED / NE_ONTOLOGY_OXIGRAPH_PATH)"
        )
    # NE-DEMO-LIVE-SEAMS (additive, default-off): the governed TIER-2 demo write-back
    # binding is a STANDARD ToolBinding appended into the SAME frozen executor tuple —
    # it runs ONLY through the C6 GovernedToolExecutor (no bypass). Empty when disabled.
    demo_bindings: tuple[ToolBinding, ...] = ()
    if api_settings.demo_writeback_tool:
        from ne.api.demo_seams import demo_writeback_binding

        demo_bindings = (demo_writeback_binding(),)
    # NE-SKILL-1a (additive, default-off): the two governed read.skills.* bindings are
    # STANDARD ToolBindings in the SAME frozen executor (no ungoverned discovery path).
    # The registry runs on the flywheel signal DB (same-engine requirement); `expander`
    # is the IDENTICAL PrincipalExpander behind the retrieval trimming (F2 one-expander).
    skills_engine: sa.Engine | None = None
    skill_bindings: tuple[ToolBinding, ...] = ()
    # PRE-011B: the exact hash-verified Agent Skill pins, and the activation service
    # composed over the SAME registry object that verified them. Empty/None when
    # skills are disabled — byte-identical to the pre-unit default-off composition.
    agent_skills: AgentSkillService | None = None
    verified_skill_pins: frozenset[tuple[str, int, str]] = frozenset()
    if skills_enabled:
        skills_engine = create_engine_with_timeouts(
            flywheel_settings.signal_database_url,
            timeouts=runtime_settings.engine_timeouts,
        )
        ensure_transactional_sqlite(skills_engine)
        ensure_skills_schema(skills_engine)
        engines.append(skills_engine)
        # ONE registry instance is the single source: the pin verification below and
        # the ActiveSet service both read through THIS object, so the availability
        # projection and what an Agent can actually be served can never be answered
        # by two different lookups that disagree. The substrate binds the SAME
        # engine (it owns its own registry internally, by its frozen constructor).
        skills_registry = SqlAlchemySkillRegistry(skills_engine)
        verified_skill_pins = _verified_agent_skill_pins(skills_registry, loaded_agent_manifest)
        agent_skills = AgentSkillService(
            skills_registry,
            expander,
            skills_settings,
            PydanticAiSkillSubstrate(skills_engine, audit_engine=audit_engine),
        )
        skill_bindings = build_skill_bindings(
            skills_engine,
            expander,
            skills_settings,
            run_pinned=agent_transport_ready,
        )
    # NE-ACTION-WIRE (additive, default-off): the 3 NE-050 reference write actions
    # become STANDARD ToolBindings in the SAME frozen executor tuple (no second
    # executor — C6) via ``build_action_bindings``, the U3-blessed composition seam
    # (``create_write_actions`` no longer exists). The dedup store rides the SAME
    # runtime engine the DurableApprovalGate already uses above (the demo_server.py
    # root's same-engine precedent). An injected ``seams.write_action_clients`` wins
    # in tests; production builds the 3 clients from ``write_action_env_settings``
    # (already resolved — and stamped into the frozen config_hash, Codex C1 —
    # BEFORE freeze, above), failing loud (naming every missing leg) rather than
    # silently omitting a tool.
    action_bindings: tuple[ToolBinding, ...] = ()
    if selected_write_actions:
        from ne.runtime.action_tools import (
            ActionDedupStore,
            build_action_bindings,
            ensure_action_dedup_schema,
        )
        from ne.runtime.reference_actions import (
            crm_note_definition,
            email_draft_definition,
            email_send_definition,
            ticket_update_definition,
        )

        ensure_action_dedup_schema(runtime_engine)
        dedup = ActionDedupStore(runtime_engine)
        if seams.write_action_clients is not None:
            selected_clients = _SelectedWriteActionClients(
                crm=seams.write_action_clients.crm,
                ticket=seams.write_action_clients.ticket,
                draft=seams.write_action_clients.draft,
                send=seams.write_action_clients.send,
                lookup=seams.write_action_clients.lookup,
                contact_lookup=seams.write_action_clients.contact_lookup,
            )
        elif api_settings.write_actions_enabled:
            assert write_action_env_settings is not None
            selected_clients = _build_write_action_clients(write_action_env_settings, engines)
        else:
            assert selected_write_action_env_settings is not None
            selected_clients = _build_selected_write_action_clients(
                selected_write_action_env_settings,
                engines,
            )
        # NE-S5-INTERNAL-CRM (owner decision 2026-08-28): the CRM legs are served
        # by the INTERNAL clients over the platform's own stores — the contact /
        # account lookups read the already-synced connector documents in the
        # chunk store, and the CRM note lands durably in the runtime database.
        # No live Salesforce call exists on this path any more (the demo org's
        # REST quota is a rolling-window hard 403). Seam-injected clients above
        # still win — tests keep full control of every leg.
        if seams.write_action_clients is None and _CRM_NOTE_TOOL in selected_write_actions:
            from dataclasses import replace as _dc_replace

            from ne.api.internal_crm import (
                CRM_SOURCE_ALIAS,
                DEFAULT_CRM_CONNECTOR,
                FilteredChunkScan,
                InternalCrmLookupClient,
                InternalCrmNoteClient,
                assert_crm_connector_is_unshared,
                ensure_internal_crm_schema,
            )

            ensure_internal_crm_schema(runtime_engine)
            if not isinstance(chunk_store, FilteredChunkScan):
                # Fail loud at BOOT, not at the first lookup: a store that
                # cannot scan by stamped metadata cannot serve the internal
                # CRM reads, and a silently-missing tool is forbidden.
                raise ValueError(
                    "write.internal.crm.note is selected but the composed chunk "
                    "store does not support scan_filtered; the internal CRM "
                    "lookups cannot be served"
                )
            # The scan pins metadata.connector as well as metadata.object (the
            # metadata namespace is shared by every connector, so the sObject
            # alone would let a SQL/SharePoint row into a governed CRM read).
            #
            # The pinned value is resolved from the SAME typed connector settings
            # ingestion uses and the config_hash freezes (``connect_source_settings``
            # above, stamped as ``ne-connect-source-salesforce``) — NOT from raw
            # environment text read after the freeze. That matters three ways:
            # ``SalesforceConnectorSettings`` also reads ``ne.toml``, so a TOML-only
            # ``source_name`` is honoured; ``configured_source_name`` returns the
            # name VERBATIM (unstripped), exactly as the mapper stamps it; and the
            # served corpus can no longer be changed by configuration that never
            # entered the frozen preimage. When the Salesforce sync is not a
            # configured source there is nothing resolved OR hashed to read, so the
            # pin falls back to the code constant — which no env var can move. A
            # deployment that synced under a non-default source_name must therefore
            # keep "salesforce" in NE_CONNECT_SYNC_SOURCES for the lookup to see the
            # same identity; if it does not, the scan finds nothing and the lookup
            # reports a miss (fail-safe) rather than serving a foreign record.
            resolved_crm_source = connect_source_settings.get(CRM_SOURCE_ALIAS)
            if resolved_crm_source is not None:
                crm_connector = configured_source_name(
                    CRM_SOURCE_ALIAS, settings=resolved_crm_source
                )
            else:
                crm_connector = DEFAULT_CRM_CONNECTOR
            # metadata.connector carries the operator-chosen source NAME, never the
            # connector KIND, so the pin only attributes a record if that name is
            # unique across configured connectors. Enforce it, fail loud.
            configured_source_names: dict[str, str] = {}
            for _source in connect_sync_settings.sources:
                try:
                    configured_source_names[_source] = configured_source_name(_source)
                except (SyncNotConfiguredError, ValueError):
                    continue  # unconfigured/invalid sources cannot collide
            assert_crm_connector_is_unshared(
                crm_connector, configured_source_names=configured_source_names
            )
            internal_lookup = InternalCrmLookupClient(chunk_store, connector=crm_connector)
            selected_clients = _dc_replace(
                selected_clients,
                crm=InternalCrmNoteClient(runtime_engine),
                lookup=internal_lookup,
                contact_lookup=internal_lookup,
            )

        definitions = []
        if _CRM_NOTE_TOOL in selected_write_actions:
            assert selected_clients.crm is not None
            definitions.append(
                crm_note_definition(
                    selected_clients.crm,
                    approval_provider=current_tool_approval,
                    principal_provider=current_tool_principal,
                    source_system="internal",
                )
            )
        if _TICKET_UPDATE_TOOL in selected_write_actions:
            assert selected_clients.ticket is not None
            definitions.append(ticket_update_definition(selected_clients.ticket))
        if _EMAIL_DRAFT_TOOL in selected_write_actions:
            assert selected_clients.draft is not None
            draft_client: DraftMailClient = selected_clients.draft
            if api_settings.email_draft_allowed_mailboxes:
                draft_client = _AllowedDraftMailClient(
                    draft_client,
                    api_settings.email_draft_allowed_mailboxes,
                )
            definitions.append(
                email_draft_definition(
                    draft_client,
                    default_mailbox=_sole_configured_mailbox(api_settings),
                )
            )
        if (
            _EMAIL_SEND_TOOL in selected_write_actions
            and selected_clients.send is not None
            # Deny-by-default made STRUCTURAL: with no explicit recipient opt-in
            # the send tool is not registered at all (its absence is a missing
            # tool the executor refuses by name) — never a registered tool whose
            # every call would fail on an empty allowlist.
            and api_settings.email_send_allowed_recipients
        ):
            # NE-CS-SEND: the REAL Tier-2 send. Both allowlists are handed to the
            # definition itself (ApiSettings validates them non-empty whenever the
            # tool is selected), and the definition enforces them AFTER the durable
            # human approval and BEFORE any egress, against the draft's Graph-read
            # recipient set — never a caller claim. The client-presence guard is
            # the `lookup` precedent: a seam that injected no send double simply
            # does not register the tool, and its absence is a missing tool the
            # executor refuses by name — never a silently-succeeding stub.
            definitions.append(
                email_send_definition(
                    selected_clients.send,
                    allowed_mailboxes=api_settings.email_draft_allowed_mailboxes,
                    allowed_recipients=api_settings.email_send_allowed_recipients,
                    default_mailbox=_sole_configured_mailbox(api_settings),
                )
            )
        action_bindings = build_action_bindings(definitions, dedup=dedup)
        # NE-WORKFLOWS: the Tier-0 CRM lookup is a STANDARD ToolBinding appended to
        # the SAME tuple — the read that resolves a write target rides the identical
        # PDP + guardrails + audit path as the write it feeds. Omitted (not faked)
        # when no lookup client was composed, so its absence is a missing tool the
        # executor refuses by name, never a silently-succeeding stub.
        if _CRM_NOTE_TOOL in selected_write_actions and selected_clients.lookup is not None:
            from ne.runtime.reference_actions import crm_account_lookup_binding

            action_bindings = (
                *action_bindings,
                crm_account_lookup_binding(selected_clients.lookup, source_system="internal"),
            )
        # NE-CS-LOOKUP: the contact read rides the same internal-store staging
        # and the same omitted-not-faked discipline as the account lookup above.
        # NE-S5-INTERNAL-CRM: both lookups are served by the internal client
        # over the synced mirror, and the audit detail says so honestly.
        if _CRM_NOTE_TOOL in selected_write_actions and selected_clients.contact_lookup is not None:
            from ne.runtime.reference_actions import crm_contact_lookup_binding

            action_bindings = (
                *action_bindings,
                crm_contact_lookup_binding(
                    selected_clients.contact_lookup, source_system="internal"
                ),
            )
    connect_request_bindings = build_connect_sync_request_bindings(
        connect_sync_settings,
        connect_sync_requests,
        config_hash=config_hash,
        principal_provider=current_tool_principal,
    )
    executor = create_tool_executor(
        (
            # NE-T2X-5-RECORDER-WIRE: extraction_mode is DEPLOYMENT config
            # (BrainGraphSettings.extraction_mode), never request data.
            *build_chat_bindings(
                current_retriever,
                top_k=api_settings.retrieval_top_k,
                extraction_mode=brain_graph_settings.extraction_mode,
                evidence=(
                    None
                    if knowledge_evidence_author is None
                    else KnowledgeSearchEvidence(
                        author=knowledge_evidence_author,
                        context_provider=current_existing_knowledge_read,
                    )
                ),
            ),
            *build_trace_bindings(audit_engine, api_settings.ops_roles),
            # NE-AIRGAP-TOGGLE: the two governed egress actions (Tier-1 restrict /
            # Tier-2 expand) over the durable posture register. The principal
            # reaches the handler through the executor's sanctioned ambient
            # carrier (current_tool_principal — set by _run_handler from its
            # VERIFIED principal argument), per the skill-bindings discipline.
            *build_network_egress_bindings(egress_store, current_tool_principal),
            # NE-MODEL-ROUTING: the two governed routing actions over the durable
            # route register. BOTH ambient carriers are injected as seams (never
            # imported by the module): current_tool_principal names the requester
            # for the chain record, current_tool_approval names the human who
            # granted it — and the handler refuses when they are the same person
            # (separation of duties, which the Tier-2 gate does not itself check).
            *build_model_routing_bindings(
                route_store, current_tool_principal, current_tool_approval
            ),
            build_inference_lifecycle_binding(inference_lifecycle, current_tool_principal),
            *connect_request_bindings,
            *seams.extra_tool_bindings,
            *graph_path_bindings,
            *verified_bindings,
            *demo_bindings,
            *skill_bindings,
            *action_bindings,
        ),
        sink=sink,
        approval_gate=gate,
        govern_settings=govern_settings,
        registry=registry,
        ledger=ledger,
        counter=counter,
        extra_guardrails=(scope_rail,),
    )

    # NE-CTXB-3: the §2.2 fail-loud boot check the merged ne.gateway.config
    # docstring assigns to THIS unit — every routed task's tool catalog + a 512
    # prompt-headroom floor must fit its PRIMARY model's input budget, or a
    # small-window rig silently blows the window at call time. The catalog text is
    # derived from the SAME governed tool specs the live loop renders in-band
    # (byte-parity pinned by test_context_budget_platform), so the boot estimate
    # matches what GatewayModel actually appends. No-op when tasks=={} (the ne-api
    # conftest default), so the existing suite stays green.
    from ne.gateway.config import validate_tool_catalog_budgets
    from ne.runtime.model import catalog_text_for_tool_specs

    catalog_text = catalog_text_for_tool_specs(executor.tool_specs)
    validate_tool_catalog_budgets(
        gateway_settings,
        {
            task: catalog_text
            for task in {
                runtime_settings.task,
                *(
                    definition.model_policy_id
                    for definition in loaded_agent_manifest.manifest.agents
                    if definition.model_policy_id in gateway_settings.tasks
                ),
            }
        },
    )

    # 015A0-AUTHORITY-M365's credential-free descriptor is read before catalog
    # reconciliation because PRE-010G's T1 admission is a dependency fact, not a
    # later display projection.
    m365_source_authority = _compose_m365_source_authority(
        runtime_engine, connect_sync_settings=connect_sync_settings
    )
    authority_attempts = _compose_authority_attempt_reader(
        runtime_engine,
        connect_sync_settings=connect_sync_settings,
        connect_source_settings=connect_source_settings,
        m365_source_authority=m365_source_authority,
    )
    registered_tools = frozenset(spec.name for spec in executor.tool_specs)
    m365_knowledge_sources = _admitted_meridian_knowledge_sources(
        source_authority=m365_source_authority,
        current_search_evidence_bound=(
            knowledge_evidence_author is not None and TOOL_NAME in registered_tools
        ),
        graph_host_bound=m365_graph_sync_host is not None,
        graph_authority_provider_bound=m365_graph_authority_provider_bound,
        graph_population_admitted=_has_admitted_m365_graph_population(runtime_engine),
        graph_path_bound=GRAPH_PATH_TOOL_NAME in registered_tools,
    )
    governed_local_t3_source_authority = (
        None if governed_local_t3 is None else governed_local_t3.descriptor
    )
    local_t3_knowledge_sources = (
        frozenset({(_MERIDIAN_LOCAL_T3_SOURCE_ID, "t3")})
        if governed_local_t3_source_authority is not None
        and governed_local_t3_source_authority.resolved
        else frozenset()
    )
    knowledge_sources = m365_knowledge_sources | local_t3_knowledge_sources
    capability_metadata = seams.agent_capability_metadata
    if capability_metadata is None:
        capability_metadata = _production_agent_capability_metadata(
            registered_tools=registered_tools,
            admitted_sources=knowledge_sources,
            source_authority=m365_source_authority,
            governed_local_t3_source_authority=governed_local_t3_source_authority,
            skill_ready=bool(verified_skill_pins and agent_skills is not None),
            graph_host_bound=m365_graph_sync_host is not None,
            m365_snapshot=(
                "m365" in connect_sync_settings.sources
                and "m365" not in connect_sync_settings.sources_to_run
            ),
            verified_answer_columns=(
                None
                if verified_setup is None
                else {
                    template.tool_name: tuple(template.answer.columns)
                    for template in verified_setup.registry.templates
                }
            ),
        )

    # Catalog availability is a conjunction over the complete production path.
    # A missing Run store/generation or agentic binding removes the Tool side of
    # the conjunction without hiding the independently truthful status of the
    # source, Skill, model, and deterministic-profile dependency classes.
    agent_dependencies = _production_agent_dependency_snapshot(
        registered_tools=registered_tools,
        knowledge_sources=knowledge_sources,
        skill_pins=verified_skill_pins,
        workflow_profiles=workflow_execution_profiles,
        model_policies=frozenset(gateway_settings.tasks),
        agent_execution_bindings=agent_execution_bindings,
        agent_transport_ready=agent_transport_ready,
    )
    agent_catalog = reconcile_agent_catalog(
        runtime_engine,
        loaded_agent_manifest,
        agent_dependencies,
    )
    effective_tool_grants = MappingProxyType(
        {
            row.definition.agent_id: effective_agent_tool_grants(row.definition, agent_dependencies)
            for row in agent_catalog.agents
        }
    )
    # NE-VAF-U4: the deterministic verified-answer turn PRE-STEP router, built
    # HERE (the ONE root legally importing both ne.brain_ontology and ne.runtime
    # — C3). ``rows`` empty (verified disabled OR zero [routing] templates)
    # leaves ``verified_router`` None — byte-identical default-off. NOT a
    # config_hash input: rows are a pure function of the registry bytes already
    # fingerprinted under 'ne-brain-ontology-templates' above.
    verified_router: VerifiedRouter[Any] | None = None
    # NE-GA-G5: the grounding-agent GroundingPolicy — built HERE, the ONE root
    # legally importing both lanes (C3); None ⇒ byte-identical default-off.
    grounding: GroundingPolicy | None = None
    grounded_agent_name: str | None = None
    if verified_setup is not None:
        from ne.brain_ontology.verified.routing import (
            build_routing_table,
            match_selection,
            match_template,
        )
        from ne.govern.guardrails.engine import GuardrailEngine
        from ne.govern.guardrails.verified import VerifiedDomainGuardrail
        from ne.runtime.verified_router import VerifiedRouter as _VerifiedRouter

        routing_rows = build_routing_table(verified_setup.registry)
        if routing_rows:

            def _render_verified_answer(raw: str) -> str:
                # NE-CTXB-5: the verified-read handler's return IS the extended
                # render_answer_text (the compact envelope + table) now — the router
                # emits it verbatim. No JSON parse: the structured artifact reaches
                # the router via the GroundingRecorder's direct record API (§4.2),
                # not this string.
                return raw

            # The SAME (4c) ToolTransitObservingSink-wrapped sink the executor's
            # own guardrail engine rides — router refusals ride the one audit
            # spine (C6/C7: no second, unaudited path).
            verified_router = _VerifiedRouter(
                rows=routing_rows,
                match=match_template,
                match_selection=match_selection,
                render_answer=_render_verified_answer,
                executor=executor,
                guardrail_engine=GuardrailEngine((VerifiedDomainGuardrail(),), sink),
                source_guard=source_guard,
            )

        # NE-GA-G5: the grounded-agent GroundingPolicy — built HERE, the ONE
        # root legally importing both lanes (C3); None ⇒ byte-identical
        # default-off. Reuses this block's already-imported GuardrailEngine
        # (avoids ruff redefinition noise).
        # NE-GA-RUN1: the SAME policy also gates configured canonical-Run
        # agents (`grounded_run_agent_ids`), so it is built when EITHER surface
        # asks for it; the chat-only `grounded_agent_name` stays tied to
        # `grounded_agent_enabled` below.
        if ontology_settings.grounded_agent_enabled or ontology_settings.grounded_run_agent_ids:
            from ne.govern import create_pdp
            from ne.govern.grounding_policy import (
                PROFILE_NG1_FROZEN,
                PROFILE_NG2,
                StrictnessTier,
            )
            from ne.govern.guardrails.grounding import GroundingGuardrail

            granted = tuple(t.tool_name for t in verified_setup.registry.templates)

            assist: Callable[..., Awaitable[str]] | None = None
            if ontology_settings.grounded_assist_enabled:

                async def _grounding_assist(
                    *,
                    question: str,
                    tables: str,
                    draft: str | None = None,
                    reasons: Sequence[str] = (),
                ) -> str:
                    """A bare 2-message gateway completion — NO tool catalog, NO
                    params (C2: same gateway, no new egress/fact source; mirrors
                    ``GatewayModel``'s request shape, model.py:112-119). Runs the
                    PASS-polish shape (``draft`` set) or the FAIL-re-draft shape
                    (``reasons`` set) — the withheld draft is NEVER passed on the
                    FAIL shape (``run_grounding_gate`` never calls it that way;
                    this wrapper adds no such kwarg).
                    """
                    if draft is not None:
                        system = _GROUNDED_ASSIST_POLISH_SYSTEM
                        instruction = (
                            "Rewrite the draft for clarity; keep every fact exactly as stated."
                        )
                        user = (
                            f"Question: {question}\n\nVerified data:\n{tables}\n\n"
                            f"Draft:\n{draft}\n\n{instruction}"
                        )
                    else:
                        system = _GROUNDED_ASSIST_FAIL_SYSTEM
                        reason_text = ", ".join(reasons) or "ungrounded content"
                        instruction = (
                            f"A prior draft was rejected by a grounding check "
                            f"for: {reason_text}. Write a grounded answer using "
                            f"only the verified data."
                        )
                        user = f"Question: {question}\n\nVerified data:\n{tables}\n\n{instruction}"
                    requested_lane = current_run_requested_model_lane()
                    completion = await gateway.complete(
                        CompletionRequest(
                            messages=(
                                ChatMessage(role="system", content=system),
                                ChatMessage(role="user", content=user),
                            ),
                            task="grounding-assist",
                            lane=(
                                requested_lane
                                if requested_lane is not None
                                else Lane(runtime_settings.lane)
                                if runtime_settings.lane
                                else None
                            ),
                            # C8: this call is part of the run's spend, so it
                            # carries the deployment's declared outcome exactly as
                            # GatewayModel's does. Omitting it here would leave the
                            # grounding gate's polish/re-draft spend unattributed
                            # on a deployment that HAS declared an outcome.
                            params=(
                                FrozenDict({"kpi_tag": runtime_settings.kpi_tag})
                                if runtime_settings.kpi_tag is not None
                                else FrozenDict()
                            ),
                            trace_id=current_run_trace_id(),
                            principal_id=current_run_principal_id(),
                            requested_model=current_run_requested_model(),
                        )
                    )
                    return completion.text

                assist = _grounding_assist

            # NE-HGATE-3 (design §2/§3.2, B-P0-1) — the deployment tier + gate
            # profile threaded as frozen plain data; NEVER request-selectable. The
            # invalid ng1-frozen+standard combo already failed loud at
            # OntologySettings construction (config.py combo validator), so both
            # values here are a legal pair.
            tier = StrictnessTier(ontology_settings.grounding_strictness)
            profile = (
                PROFILE_NG1_FROZEN
                if ontology_settings.grounding_gate_profile == "ng1-frozen"
                else PROFILE_NG2
            )

            grounding = GroundingPolicy(
                granted_tools=granted,
                render_fallback=_render_grounded_fallback,
                guardrail_engine=GuardrailEngine((GroundingGuardrail(),), sink),
                sink=sink,
                # A fresh GovernedPdp over the SAME sink+settings+registry — the
                # exact recipe create_tool_executor uses internally (factory.py)
                # and the create_subagent_dispatcher own-pdp precedent above;
                # GovernedToolExecutor does not expose its private _pdp.
                pdp=create_pdp(govern_settings, sink=sink, registry=registry),
                assist=assist,
                strictness=tier,
                profile=profile,
            )
            if ontology_settings.grounded_agent_enabled:
                grounded_agent_name = ontology_settings.grounded_agent_name

    # NE-GA-G5 (fail-loud config, owner rule: never silently downgrade): a
    # grounded-analyst that silently isn't grounded is forbidden — mirrors the
    # demo_ontology_lift fail-loud precedent above.
    if (
        ontology_settings.grounded_agent_enabled or ontology_settings.grounded_run_agent_ids
    ) and verified_setup is None:
        raise ValueError(
            "NE_ONTOLOGY_GROUNDED_AGENT_ENABLED / NE_ONTOLOGY_GROUNDED_RUN_AGENT_IDS require "
            "verified answers enabled "
            "(NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED + verified_templates_dir): the "
            "grounding gate's corpus is built only from the governed verified reads"
        )

    # NE-HGATE-3 (design §6.2-2, B-P1-3; owner rule: never silently downgrade a
    # critical stage) — the boot BELT for the dead-assist failure class (the
    # TaskRoutingGateway regression tests are the runtime SUSPENDERS). The G4
    # assist issues task='grounding-assist' completions; an UNROUTED task raises
    # UnknownTaskError at call time, which _call_assist swallows to assist_used=
    # False — silently degrading every grounded turn. Refuse to boot such a
    # deployment. Deliberately a NO-OP when tasks == {} (the ne-api conftest /
    # injected-seam-gateway default), mirroring validate_tool_catalog_budgets'
    # no-op-when-unrouted semantics (platform.py:966-980).
    if (
        ontology_settings.grounded_agent_enabled
        and ontology_settings.grounded_assist_enabled
        and gateway_settings.tasks
        and "grounding-assist" not in gateway_settings.tasks
    ):
        raise ValueError(
            "NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED requires a 'grounding-assist' route in "
            "NE_GATEWAY_TASKS: the G4 assist issues task='grounding-assist' completions and "
            "an unrouted task raises UnknownTaskError at call time, silently degrading every "
            "grounded turn to assist_used=false. Add the route or set "
            "NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED=false."
        )

    # (11) Session store over the ONE runtime engine.
    session_store = SqlAlchemySessionStore(runtime_engine)

    # (11c) PRE-011E — the ONE composed Agents capability preflight, over the
    # AUTHORITATIVE catalog built above and the EXACT frozen executor catalog.
    # Composed unconditionally: this seam's unavailability is reported INSIDE the
    # answer, so leaving the service absent would put the Phase-1 "not configured"
    # 503 back into production and claim the deployment has no preflight at all.
    #
    # It reads NOTHING from the Agent Run store and starts NOTHING: 009A's store
    # stays dormant and 009C alone activates execution. Composed AFTER
    # freeze_platform_config so the hash it carries is the frozen one.
    # NE-CAPTAB-SKILL: the principal-scoped pinned-Skill detail provider. It runs
    # the SAME governed selection the runtime activates (AgentSkillService.select:
    # exact pin → ACL → required-tools → platform), so the Capabilities panel can
    # never show a Skill the principal could not activate, and the content_hash it
    # displays is the one a Run would freeze against. Absent Skill substrate leaves
    # the provider None and the projection's ``skills`` field None. ``select`` is
    # blocking SQL, bridged off the event loop exactly as AgentSkillService.activate
    # does. The manifest definitions are the catalog's own, keyed by agent id.
    _preflight_definitions = {
        row.definition.agent_id: row.definition for row in agent_catalog.agents
    }
    _skill_detail_provider: AgentSkillDetailProvider | None = None
    if agent_skills is not None:
        _skill_service = agent_skills

        async def _skill_detail_provider_impl(
            *, agent_id: str, principal: Principal
        ) -> tuple[AgentSkillDetail, ...] | None:
            definition = _preflight_definitions.get(agent_id)
            if definition is None:
                return None
            packs = await anyio.to_thread.run_sync(
                functools.partial(_skill_service.select, definition, principal)
            )
            return tuple(
                AgentSkillDetail(
                    skill_id=pack.name,
                    version=pack.version,
                    content_hash=pack.content_hash,
                    description=pack.description,
                    requires_tools=pack.requires_tools,
                    acl_labels=pack.acl_labels,
                    triggers=pack.triggers,
                    category=pack.category,
                    body=pack.body,
                )
                for pack in packs
            )

        _skill_detail_provider = _skill_detail_provider_impl

    agent_preflight = AgentCapabilityPreflightService(
        agent_catalog=agent_catalog,
        inventory_provider=_ComposedAgentCapabilityInventory(
            definitions=_preflight_definitions,
            tool_specs=tuple(executor.tool_specs),
            metadata=capability_metadata,
            config_hash=config_hash,
            effective_tool_grants=effective_tool_grants,
        ),
        skill_detail_provider=_skill_detail_provider,
    )

    # NE-168 (additive, default-off): the demo governed multi-lane delegation
    # dispatcher. Shares the SAME ledger/counter/scope_rail as the executor above
    # (the NE-CKD1 normative recipe, factory.py:19-47) — NEVER a private guardrail
    # state. allow_frontier_subagent_routing is NEVER passed (default False stays
    # the frontier deny exhibit).
    demo_dispatcher: GovernedSubAgentDispatcher | None = None
    if api_settings.demo_delegation:
        from ne.api.demo_seams import demo_agent_definitions

        demo_dispatcher, _ = create_subagent_dispatcher(
            bindings=(),
            definitions=demo_agent_definitions(),
            sink=sink,
            approval_gate=gate,
            gateway=gateway,
            principal=Principal(id="agent:demo-delegate"),
            settings=runtime_settings,
            govern_settings=govern_settings,
            ledger=ledger,
            counter=counter,
            extra_guardrails=(scope_rail,),
            plugin_registry=registry,
            source_guard=source_guard,
        )

    # NE-WORKFLOWS (additive, default-off): the workflow drafting dispatcher. It
    # shares the SAME ledger/counter/scope_rail as the executor above (the NE-CKD1
    # normative recipe) and registers ONE empty-toolset definition, so the drafting
    # leg is a governed model call and nothing more. The workflow's reads and its
    # write go through the executor, never through this dispatcher.
    workflow_dispatcher: GovernedSubAgentDispatcher | None = None
    if api_settings.workflows_enabled:
        from ne.api.workflows import workflow_agent_definitions

        workflow_dispatcher, _ = create_subagent_dispatcher(
            bindings=(),
            definitions=workflow_agent_definitions(),
            sink=sink,
            approval_gate=gate,
            gateway=gateway,
            principal=Principal(id="agent:workflow-drafter"),
            settings=runtime_settings,
            govern_settings=govern_settings,
            ledger=ledger,
            counter=counter,
            extra_guardrails=(scope_rail,),
            plugin_registry=registry,
            source_guard=source_guard,
        )

    return Platform(
        config_hash=config_hash,
        sink=sink,
        gate=gate,
        executor=executor,
        retriever=retriever,
        current_retriever=current_retriever,
        session_store=session_store,
        agent_run_store=agent_run_store,
        agent_transport_generation=agent_transport_generation,
        default_ask_generation=api_settings.default_ask_generation or None,
        source_authority_reader=source_authority_reader,
        authority_attempts=authority_attempts,
        source_authority_evaluator=source_authority_evaluator,
        source_guard=source_guard,
        current_document_authorizer=_authorize_current_document,
        protected_source_output_writer=protected_source_output_writer,
        protected_source_execution_writer=protected_source_execution_writer,
        source_authority_continuation=source_authority_continuation,
        m365_source_authority=m365_source_authority,
        governed_local_t3_source_authority=(
            None if governed_local_t3 is None else governed_local_t3.descriptor
        ),
        governed_local_t3_release=(
            None if governed_local_t3 is None else governed_local_t3.release
        ),
        embedder=embedder,
        agent_preflight=agent_preflight,
        gateway=gateway,
        runtime_settings=runtime_settings,
        govern_settings=govern_settings,
        identity_settings=identity_settings,
        audit_settings=audit_settings,
        api_settings=api_settings,
        secret_store=secret_store,
        perm_store=perm_store,
        expander=expander,
        perm_sync=perm_sync,
        permissions_settings=permissions_settings,
        engines=engines,
        runtime_engine=runtime_engine,
        audit_engine=audit_engine,
        document_lineage=document_lineage,
        ledger=ledger,
        counter=counter,
        scope_rail=scope_rail,
        credential_service=credential_service,
        agent_identity_settings=agent_identity_settings,
        agent_catalog=agent_catalog,
        workflow_execution_profiles=workflow_execution_profiles,
        agent_execution_bindings=agent_execution_bindings,
        effective_agent_tool_grants=effective_tool_grants,
        chunk_store=chunk_store,
        connect_sync_settings=connect_sync_settings,
        connect_sync_requests=connect_sync_requests,
        cdc_service=cdc_service,
        rebac_client=rebac_client,
        entity_graph=entity_graph,
        verified_curation=verified_curation,
        ontology=ontology_state,
        gateway_settings=gateway_settings,
        egress_settings=egress_settings,
        egress_store=egress_store,
        egress_settings_provider=egress_settings_provider,
        route_store=route_store,
        inference_lifecycle=inference_lifecycle,
        demo_dispatcher=demo_dispatcher,
        workflow_dispatcher=workflow_dispatcher,
        verified_router=verified_router,
        grounding=grounding,
        grounded_agent_name=grounded_agent_name,
        grounded_run_agent_ids=tuple(ontology_settings.grounded_run_agent_ids),
        grounded_run_strictness=ontology_settings.grounding_run_strictness,
        skills_engine=skills_engine,
        skills_settings=skills_settings if skills_enabled else None,
        agent_skills=agent_skills,
        extraction=extraction,
        m365_graph_sync_host=m365_graph_sync_host,
    )


# --- lazy network-edge helpers (moved verbatim from app.py) ----------------


def _build_cdc_service(
    api_settings: ApiSettings,
    chunk_store: ChunkStore,
    sink: AuditSink,
    runtime_engine: sa.Engine,
    document_lineage: SqlAlchemyDocumentLineageStore,
) -> CdcSinkService:
    """Construct the NE-084 CDC sink over the governed KnowledgeWriter.

    Fail-closed at FACTORY time (never a runtime 401 on a config typo): the
    webhook secret must decode, the table map must be present + valid, and the
    quarantine path must be set. ``ne.connect.cdc`` is imported LOCALLY so
    ne-api's eager import graph stays cheap and dlt-free.
    """
    from pathlib import Path

    from ne.brain.write import (
        KnowledgeWriter,
        OutboxShapeViolationSink,
        ProvenanceClass,
        SqliteWriteQuarantine,
    )
    from ne.connect import CONNECTOR_RECORD_SHAPE
    from ne.connect.cdc import CdcSinkConfig, CdcSinkService, decode_webhook_secret
    from ne.events.outbox import ensure_outbox_schema

    # Validate the secret LOUD here (a weak/garbled secret is a boot ValueError,
    # not a silent always-401 at request time).
    decode_webhook_secret(api_settings.cdc_webhook_secret.get_secret_value())
    if not api_settings.cdc_tables.strip():
        raise ValueError(
            "NE_API_CDC_TABLES is required when NE_API_CDC_WEBHOOK_SECRET is set "
            "(a CDC lane with no governed table map would quarantine every event "
            "as unknown-table)"
        )
    config = CdcSinkConfig.from_json(api_settings.cdc_tables)
    if not api_settings.cdc_quarantine_path.strip():
        raise ValueError(
            "NE_API_CDC_QUARANTINE_PATH is required when NE_API_CDC_WEBHOOK_SECRET is set"
        )
    quarantine = SqliteWriteQuarantine(Path(api_settings.cdc_quarantine_path))
    ensure_outbox_schema(runtime_engine)  # idempotent; OutboxShapeViolationSink needs it
    writer = KnowledgeWriter(
        chunk_store,
        quarantine,
        {CONNECTOR_RECORD_SHAPE.name: CONNECTOR_RECORD_SHAPE},
        default_shape=CONNECTOR_RECORD_SHAPE.name,
        actor="cdc:debezium",
        default_provenance_class=ProvenanceClass.INGESTED,
        audit_sink=sink,
        violation_sink=OutboxShapeViolationSink(runtime_engine),
        lineage=document_lineage,
    )
    return CdcSinkService(writer, quarantine, config, audit_sink=sink)


def _default_embedder(
    api_settings: ApiSettings, *, air_gap: Callable[[], bool] | None = None
) -> Embedder:
    import os

    from ne.brain.embedder import TEIEmbedder

    # Escape hatch for slow/CPU TEI backends (e.g. the demo box): the embedder's 30s
    # default read-timeout is too short when a corpus-ingest batch is embedded on a
    # CPU-only TEI server (each forward pass is seconds, and batches queue). Read via
    # os.environ (deliberately NOT an ApiSettings field, so it never enters the frozen
    # config_hash preimage); unset ⇒ the shipped 30s default, prod behaviour unchanged.
    timeout = float(os.environ.get("NE_TEI_EMBED_TIMEOUT_S", "30"))
    # No `allowed_hosts`: the embed endpoint must satisfy the in-VPC shape rules on
    # its own, so a public IP literal is refused at construction and the air-gap
    # dominance rule has nothing to revoke here TODAY. The provider is still passed
    # so the guarantee does not depend on that argument staying absent — the
    # reranker's live defect began as exactly this kind of dormant parameter.
    return TEIEmbedder(api_settings.tei_url, timeout=timeout, air_gap=air_gap)


def _default_reranker(
    api_settings: ApiSettings, *, air_gap: Callable[[], bool] | None = None
) -> Reranker:
    import os

    # LOCAL import (the _default_embedder precedent). Construction C2-validates the
    # URL fail-closed (UnsafeEndpointError on an exfil-shaped endpoint) and performs
    # NO I/O — a bad NE_API_TEI_RERANK_URL is a boot ValueError, never a runtime POST.
    # NE-SEARCH-LATENCY: `rerank_backend` selects the SERVER adapter (TEI default,
    # vLLM /rerank for a GPU-served instance of the SAME model); `rerank_allowed_hosts`
    # is threaded into BOTH adapters' C2 validation (mirrors ne-gateway's
    # allowed_private_vllm_hosts) so an external GPU rerank host is an explicit,
    # per-deployment allowlist decision — never reachable by default.
    #
    # `air_gap` is the LIVE runtime posture (build_platform's
    # `_egress_air_gap_engaged`, over the same governed register the gateway reads).
    # It is threaded into BOTH adapters so an engaged air gap dominates the
    # allowlist for a public IP literal at construction AND before every request —
    # the ne-gateway allowed_private_vllm_hosts precedent, completed. Omitted (None)
    # ⇒ only the process-global AIR_GAP flag applies, which ne.net.ssrf reads
    # unconditionally, so a caller can never make this MORE permissive.
    from ne.brain.reranker import TEIReranker, VllmReranker

    # Escape hatch mirroring NE_TEI_EMBED_TIMEOUT_S (NE-DEMO-FIX1): read via
    # os.environ, deliberately outside the frozen config_hash preimage; unset ⇒
    # the shipped 30s default, prod behaviour unchanged.
    timeout = float(os.environ.get("NE_TEI_RERANK_TIMEOUT_S", "30"))
    # NE-RERANK-CPU: `rerank_scoring_char_limit` is threaded into BOTH adapters —
    # it is a property of the rerank STAGE, not of one server, so a GPU-lane flip
    # cannot silently change what is scored. Default None ⇒ full chunk text, and
    # the returned ScoredChunk carries the full Chunk either way.
    if api_settings.rerank_backend == "vllm":
        return VllmReranker(
            api_settings.tei_rerank_url,
            served_model_name=api_settings.rerank_served_model_name,
            timeout=timeout,
            scoring_char_limit=api_settings.rerank_scoring_char_limit,
            allowed_hosts=api_settings.rerank_allowed_hosts,
            air_gap=air_gap,
        )
    return TEIReranker(
        api_settings.tei_rerank_url,
        timeout=timeout,
        scoring_char_limit=api_settings.rerank_scoring_char_limit,
        allowed_hosts=api_settings.rerank_allowed_hosts,
        air_gap=air_gap,
    )


def _default_graph_store(settings: BrainGraphSettings) -> GraphReadStore:
    # NE-BRAIN-T2-WIRING: the real Tier-2 read store (Apache AGE over the psycopg
    # `_pg` chokepoint). LOCAL import (the _default_reranker precedent). The
    # constructor is I/O-free / lazy-connect (age/store.py) so a bad DSN surfaces at
    # the first query (a 502), never as a boot-time liveness failure.
    from ne.brain_graph.age.store import AgeGraphStore

    return AgeGraphStore(settings.database_url, settings.graph_name)


def _default_sparse() -> QuerySparseEncoder:
    from ne.brain.sparse import FastEmbedBM25Encoder

    return FastEmbedBM25Encoder()


def _default_chunk_store(
    api_settings: ApiSettings,
    embedder: Embedder,
    sparse_encoder: QuerySparseEncoder,
    expander: object,
    registry: PluginRegistry,
) -> ChunkStore:
    # NE-091: the pgvector alternate rides the EXISTING ne.plugins.vector_store
    # entry-point seam (the ne-govern create_pdp precedent; app.py untouched — the
    # 085c app-untouched discipline). Qdrant stays the byte-identical default.
    if api_settings.vector_store == "pgvector":
        if not api_settings.pgvector_dsn:
            raise ValueError("ApiSettings.vector_store='pgvector' requires NE_API_PGVECTOR_DSN")
        # Fail LOUD at platform construction if the optional [pgvector] extra is
        # absent — importing the psycopg chokepoint (`_pg`, store.py's lazy dep)
        # here surfaces a clear install hint instead of a cryptic ModuleNotFoundError
        # deferred to the first ingest/retrieval (the platform's fail-at-boot norm).
        try:
            import ne.brain.pgvector._pg  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ApiSettings.vector_store='pgvector' requires the psycopg driver — "
                "install the ne-brain '[pgvector]' extra (e.g. `uv sync --extra pgvector` "
                "or `pip install 'ne-brain[pgvector]'`)."
            ) from exc
        cls = registry.resolve("vector_store", "pgvector")
        return cls(
            api_settings.pgvector_dsn,
            api_settings.pgvector_table,
            embedder,
            sparse_encoder,
            group_expander=expander,
        )

    from qdrant_client import AsyncQdrantClient

    from ne.brain.qdrant import QdrantChunkStore

    # A FACTORY, not an instance (2026-08-28 cron-offload incident): the
    # AsyncQdrantClient is event-loop-affine, and ne-worker uses this ONE store
    # from two loops (DBOS turn loop + the persistent cron-tick loop). The store
    # builds one client per running loop from this factory, so a connector-sync
    # tick can never poison the turn path's read.knowledge.search client.
    qdrant_url = api_settings.qdrant_url
    return QdrantChunkStore(
        lambda: AsyncQdrantClient(qdrant_url),
        api_settings.qdrant_collection,
        embedder,
        sparse_encoder,
        group_expander=expander,  # type: ignore[arg-type]
    )


def _redaction_exempt_addresses(
    api_settings: ApiSettings, govern_settings: GovernSettings
) -> tuple[str, ...]:
    """The exact addresses pre-egress redaction must NOT mask, from BOTH sources.

    Sources, both deployment configuration and neither of them customer PII:

    * ``govern_settings.guardrails.recipients.allowed_recipients`` — every entry
      already GUARANTEED to be a full bare email address by
      :meth:`~ne.govern.config.RecipientSettings._validate_allowed_recipients`
      (a non-address entry there is inert in its own guardrail, so rejecting it
      at boot breaks nothing and catches the operator slip at its origin); and
    * ``api_settings.email_draft_allowed_mailboxes`` — which CANNOT be tightened
      the same way. That field is a Graph MAILBOX IDENTITY: Graph addresses a
      mailbox by userPrincipalName OR by object id, ``_AllowedDraftMailClient``
      compares it as an opaque casefolded string, and the config-hash test in
      ``test_beat3_governed_draft_email_chain`` configures a bare GUID. A GUID is
      a legal value there and an illegal exemption, so the split happens HERE.

    A mailbox identity that is not an address is DROPPED (loudly, at WARNING)
    rather than exempted: it can never be what pre-egress redaction masked in the
    first place — the redactor emits ``<REDACTED:EMAIL_ADDRESS>`` for addresses,
    and no EMAIL_ADDRESS entity is ever a bare GUID — so exempting it could only
    ever widen the un-mask, never restore a usable address. Dropping loses
    nothing; passing it through would hand
    :class:`~ne.govern.redaction.exempt.AllowlistExemptRedactor` a substring
    wildcard. That redactor refuses such an entry outright (fail closed at
    construction), so this filter is what keeps a legitimate object-id mailbox
    deployment bootable — it is NOT the security boundary, only the split.

    De-duplicated case-insensitively (the two sources overlap whenever the
    deployment mails from and to the same address) and sorted, so a
    semantically identical deployment always builds the identical pattern set.
    """
    addresses = [*govern_settings.guardrails.recipients.allowed_recipients]
    dropped = 0
    for mailbox in api_settings.email_draft_allowed_mailboxes:
        if is_valid_email(mailbox):
            addresses.append(mailbox)
        else:
            dropped += 1
    if dropped:
        _LOG.warning(
            "redaction exemption: dropped %d of %d email_draft_allowed_mailboxes "
            "entr%s that %s not a bare email address (a non-address mailbox identity, "
            "e.g. a Graph object id, is a legal mailbox but can never be an exemption: "
            "pre-egress redaction never masked it). Values omitted deliberately.",
            dropped,
            len(api_settings.email_draft_allowed_mailboxes),
            "y" if dropped == 1 else "ies",
            "is" if dropped == 1 else "are",
        )
    seen: dict[str, str] = {}
    for address in addresses:
        seen.setdefault(address.casefold(), address)
    return tuple(seen[key] for key in sorted(seen))


def _default_gateway(
    gateway_settings: GatewaySettings,
    runtime_engine: sa.Engine,
    sink: object,
    egress_settings_provider: Callable[[], EgressSettings],
    govern_settings: GovernSettings,
    route_provider: Callable[[], Mapping[str, TaskRoute]] | None = None,
    redaction_exempt_addresses: Sequence[str] = (),
) -> ModelGateway:
    from ne.events.outbox import ensure_outbox_schema
    from ne.gateway.cost import OutboxCostSink
    from ne.gateway.egress import build_egress_guard
    from ne.gateway.gateway import LiteLLMGateway
    from ne.govern import create_redactor

    ensure_outbox_schema(runtime_engine)
    # NE-AIRGAP-TOGGLE: the live guard reads the egress settings PER CALL from
    # the composition root's provider (boot-frozen EgressSettings overlaid with
    # the durable governed posture register; fail-closed to air-gap-engaged on a
    # store failure) — the SAME provider /v1/posture reports from (§0 law 5
    # reported==enforced). A governed flip therefore takes effect on the very
    # next model call, in ne-api AND ne-worker.
    # NE-162: wire the real fail-closed RedactionStage unconditionally (C2/C12) —
    # BOTH the isolation venv build AND the env-dir resolution (uv.lock repo-root
    # walk-up / NE_GOVERN_PRESIDIO_* env var reads) are deferred to the first
    # redact() call, so this never touches the venv OR requires a repo checkout
    # at boot — the fix for the CI air-gap boot regression where a production/
    # air-gap install (no uv.lock to walk up to) crashed ne-api at construction.
    return LiteLLMGateway(
        gateway_settings,
        OutboxCostSink(runtime_engine),
        audit_sink=sink,  # type: ignore[arg-type]
        egress_guard=build_egress_guard(settings_provider=egress_settings_provider),
        # NE-CS-USABLE-ADDRESS: deployment-owned addresses (the send allowlist +
        # the configured draft mailboxes) are exempt from pre-egress masking so a
        # governed CRM read serves the model an address draft.email.create can
        # actually use. Every other address still redacts (see
        # ne.govern.redaction.exempt for the policy + exposure statement).
        # SCOPE, stated accurately: this is the gateway's ONE pre-egress
        # redactor, and _prepare_egress_body applies it to EVERY FRONTIER_API
        # completion in ne-api AND ne-worker. It is not scoped to the outreach
        # draft, to a principal, to an agent, or to a field — it cannot be: it
        # sees a string, not a caller. What is narrow is the SET of un-masked
        # addresses, not the set of callers that can carry them.
        redactor=create_redactor(govern_settings, exempt_addresses=redaction_exempt_addresses),
        # NE-MODEL-ROUTING: the live route seam, the SAME per-call-provider shape
        # as the egress guard above. A governed repoint takes effect on the very
        # next model call, in ne-api AND ne-worker. The provider can only ever
        # re-select among `gateway_settings.models` — the boot-composed,
        # SSRF-validated set this Router was built from — so no runtime path can
        # introduce an endpoint (ne.api.model_routing §2.1).
        route_provider=route_provider,
    )
