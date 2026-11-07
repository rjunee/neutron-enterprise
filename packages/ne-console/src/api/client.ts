// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The ONE typed client over the governed backend HTTP API. Every byte the
// console renders arrives through this module — there is no other network seam.
// Each request carries the OIDC bearer access token (RFC 6750 Authorization
// header only; never a query-param token). A 401 invokes onUnauthorized (which
// re-triggers the OIDC redirect) and throws a typed ApiError — this holds on
// BOTH the JSON request() path and the streaming (SSE) sendMessage path. All
// URLs are built from the single API_BASE — no endpoint literals live outside
// here.

import { API_BASE } from "./config";
import { SseError, streamSse } from "./sse";
import { transportUserMessage } from "./userMessage";
import type {
  AcceptedAgentRunSubmissionWire,
  AgentCapabilityPreflightWire,
  AgentCatalogItemWire,
  AgentCatalogPageWire,
  AgentConversationPageWire,
  AgentConversationTurnPageWire,
  AgentRunUpdatePageWire,
  AgentRunUpdateViewWire,
  AgentWorkflowProfileWire,
  AgentConversationWire,
  AgentRunPageWire,
  AgentRunDetailWire,
  AgentRunEventPageWire,
  AgentEvidenceEnvelopeWire,
  ControlAgentRunRequestWire,
  StartAgentRunRequestWire,
  ApprovalActionResult,
  ApprovalPayloadDisclosureView,
  AuthorityAttemptsView,
  AuthorityAttemptView,
  AuthorizedRetrievalEnvelopeWire,
  AuditEventsPage,
  ChatCapabilitiesView,
  ChatSession,
  Citation,
  ConnectQuarantineView,
  ConnectSyncRequestView,
  ConnectStatusView,
  DocumentGraphEntityWire,
  DocumentGraphEvidenceWire,
  DocumentGraphRelationshipWire,
  DocumentJourneyReadWire,
  DocumentListQuery,
  DocumentListWire,
  DocumentSemanticChunksWire,
  DocumentStageArtifactWire,
  DocumentStageEvidenceWire,
  DocumentStageNameWire,
  DelegateRunView,
  EvalRunView,
  GraphEntityFacetsWire,
  GraphEntityListQuery,
  GraphEntityListWire,
  GraphPathsWire,
  KpiSummaryView,
  ModelChoiceCatalogView,
  ModelChoiceSurface,
  InferenceLifecycleView,
  InferenceStopConfirmationView,
  ModelRoutingView,
  OntologyClassesWire,
  OntologyShapesWire,
  OntologyViolationsWire,
  PendingApprovalView,
  PostureView,
  GovernedChangeResult,
  PrincipalView,
  PrincipalReferenceScope,
  ResolvedPrincipalsView,
  RejectionCodeWire,
  ReplayResultView,
  RetrievalResult,
  ReviewEventView,
  ServingStatusView,
  SkillApplyView,
  SkillEditFields,
  SkillLibraryRow,
  StewardDigestView,
  StewardLatencyView,
  ServedStewardProposalView,
  StreamEvent,
  SubgraphWire,
  TraceAssemblyView,
  VerifiedInvokeWire,
  VerifiedTemplatesWire,
  WorkflowRunView,
  WorkflowView,
  AgenticWorkflowRunView,
  AgenticWorkflowRunStatusView,
} from "./types";

/**
 * Thrown on any non-2xx governed-API response.
 *
 * THE RAW STRING IS NOT ON `message`. `developerMessage` holds the
 * client-composed "request to {path} failed with HTTP {status}" for the console
 * and for telemetry, where the path and the code are the whole value. `message`
 * — and its named twin `userMessage` — carry §2.4's sentence instead, so a
 * screen that reaches for either gets something a reader can read. That is the
 * point of the split: the previous shape put the developer string on the one
 * property every render site reaches by habit, and ten of them rendered it.
 *
 * `detail`, when present, is the server's own human-readable reason parsed from
 * the error body's `detail` field — an approval 403 ("not in the Tier-3
 * named-approver list") surfaces through it, and it is what `userMessage`
 * becomes whenever it exists.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail?: string;
  /** The server's sentence, or §2.4's for this status class. ALWAYS populated,
   * never the endpoint path, never a status code. Render this. */
  readonly userMessage: string;
  /** The client-composed transport string. Console and telemetry only; a
   * guardian fails the build if a shipped component so much as names it. */
  readonly developerMessage: string;
  constructor(developerMessage: string, status: number, detail?: string) {
    super(transportUserMessage(status, detail));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.developerMessage = developerMessage;
    this.userMessage = this.message;
  }
}

export interface ApiClientOptions {
  /** Returns the current OIDC access token, or undefined if not signed in. */
  getAccessToken: () => string | undefined;
  /** Called exactly once when the API returns 401 (token invalid/expired). */
  onUnauthorized: () => void;
  /** Renew the token now unless it has at least this much verified lifetime
   * left (see SessionBoundClientOptions). Called before RUN-STARTING requests
   * only: a Run freezes the submitting bearer's expiry into its authorization
   * snapshot, and the worker fail-closed re-checks that snapshot before every
   * protected disclosure — so a run admitted on a nearly-expired token dies
   * mid-flight (AuthorizationExpiredError). Must not throw; the client guards
   * anyway and always proceeds with the current token on failure. */
  ensureFreshAccessToken?: (minRemainingSeconds: number) => Promise<void>;
}

/**
 * Minimum verified credential lifetime, in seconds, for starting a Run.
 *
 * The agentic run budget caps execution at 400s (the composer's "Caps ...
 * 400s"), and the run's frozen authorization snapshot must outlive the run —
 * expiry mid-run is a fail-closed AuthorizationExpiredError, indistinguishable
 * on stage from the product breaking. 420 > 400 guarantees any admitted run's
 * snapshot covers its whole window once the realm's access-token lifespan is
 * >= 420s (ne-pilot: 900s). Against a shorter realm lifespan the renew simply
 * always fires and the run starts with the freshest credential obtainable.
 */
export const RUN_CREDENTIAL_MIN_REMAINING_SECONDS = 420;

export interface ApiClient {
  me(): Promise<PrincipalView>;
  /** Principal-scoped Agent catalog and immutable Conversation/Run reads. */
  listAgents(cursor?: string): Promise<AgentCatalogPageWire>;
  getAgent(agentId: string): Promise<AgentCatalogItemWire>;
  getAgentPreflight(
    agentId: string,
    capabilityHash?: string,
  ): Promise<AgentCapabilityPreflightWire>;
  listAgentConversations(agentId: string, cursor?: string): Promise<AgentConversationPageWire>;
  createAgentConversation(agentId: string): Promise<AgentConversationWire>;
  getConversation(agentId: string, conversationId: string): Promise<AgentConversationWire>;
  listConversationTurns(
    agentId: string,
    conversationId: string,
    cursor?: string,
  ): Promise<AgentConversationTurnPageWire>;
  getAgentWorkflowProfile(agentId: string): Promise<AgentWorkflowProfileWire>;
  listAgentRunUpdates(
    view: AgentRunUpdateViewWire,
    windowStart: string,
    windowEnd: string,
    cursor?: string,
  ): Promise<AgentRunUpdatePageWire>;
  listConversationRuns(
    agentId: string,
    conversationId: string,
    cursor?: string,
  ): Promise<AgentRunPageWire>;
  listAgentRuns(agentId: string, cursor?: string): Promise<AgentRunPageWire>;
  getAgentRun(agentId: string, runId: string): Promise<AgentRunDetailWire>;
  listAgentRunEvents(
    agentId: string,
    runId: string,
    cursor?: string,
    limit?: number,
  ): Promise<AgentRunEventPageWire>;
  getAgentRunEvidence(agentId: string, runId: string): Promise<AgentEvidenceEnvelopeWire>;
  exportAgentRunEvidence(agentId: string, runId: string): Promise<AgentEvidenceEnvelopeWire>;
  cancelAgentRun(
    agentId: string,
    runId: string,
    body: ControlAgentRunRequestWire,
    idempotencyKey: string,
  ): Promise<AcceptedAgentRunSubmissionWire>;
  retryAgentRun(
    agentId: string,
    runId: string,
    body: ControlAgentRunRequestWire,
    idempotencyKey: string,
  ): Promise<AcceptedAgentRunSubmissionWire>;
  /** Start one canonical Run in an existing Conversation.  The caller owns the
   * stable idempotency key; principal identity remains bearer-derived. */
  startAgentRun(
    agentId: string,
    body: StartAgentRunRequestWire,
    idempotencyKey: string,
  ): Promise<AcceptedAgentRunSubmissionWire>;
  /** Resolve only principals attached to server-owned records already visible
   * in the named governed surface. Principal ids are never accepted as input. */
  resolvePrincipals(
    scope: PrincipalReferenceScope,
    references: string[],
  ): Promise<ResolvedPrincipalsView>;
  /** Server-advertised optional chat modes. The client never guesses an agent
   * name from deployment convention. */
  chatCapabilities(): Promise<ChatCapabilitiesView>;
  createSession(agentName: string): Promise<ChatSession>;
  sendMessage(
    sessionId: string,
    prompt: string,
    onEvent: (event: StreamEvent) => void,
    signal?: AbortSignal,
    /** Fired on every server keep-alive comment — not data, but proof the
     * stream is alive. The turn's stall clock feeds on frames AND these, so a
     * legitimately long turn (thinking, a parked approval) is distinguishable
     * from a dead stream. */
    onHeartbeat?: () => void,
    /** Exact server-catalog alias for this turn. Omitted preserves the legacy
     * deployment route; the client never sends a lane or fallback. */
    requestedModel?: string,
    /**
     * THE TURN'S IDEMPOTENCY KEY — what stops one question becoming six runs.
     *
     * The chat POST used to send none, alone among the write calls here
     * (`startAgentRun`, `cancelAgentRun` and `retryAgentRun` all require one).
     * The server mints its turn id from this key, so without it every resend of
     * the SAME question minted a FRESH run: one owner's question produced six
     * distinct run ids, each of which the server ran to success while the
     * console had already walked away from all of them.
     *
     * Frozen for the life of a turn by the caller — a retry of that turn sends
     * the key it was born with, exactly as DeterministicRunLauncher freezes its
     * key across retries, so the resend RESOLVES to the run already running
     * rather than starting another one beside it.
     */
    idempotencyKey?: string,
  ): Promise<void>;
  /** `signal` lets the caller put a DEADLINE and a cancel on the query. /search
   * hung indefinitely on a slow backend with one word of grey text and no way
   * out; no request in this console may be able to do that. */
  search(query: string, topK?: number, signal?: AbortSignal): Promise<RetrievalResult>;
  retrieve(chunkIds: string[]): Promise<RetrievalResult>;
  /** ACL-trimmed document directory and per-version journey. Historical
   * content/diffs are authorized by the server against that version's ACL. */
  listDocuments(query?: DocumentListQuery, signal?: AbortSignal): Promise<DocumentListWire>;
  documentJourney(
    documentId: string,
    selector?: { version?: number; as_of?: string },
    signal?: AbortSignal,
  ): Promise<DocumentJourneyReadWire>;
  documentStageEvidence(
    documentId: string,
    version: number,
    stage: DocumentStageNameWire,
    signal?: AbortSignal,
  ): Promise<DocumentStageEvidenceWire>;

  // --- NE-137 flywheel intake (chat feedback signals) ----------------------
  /** Emit a thumbs-up/down CorrectionSignal on an answer. The body carries NO
   * identity field — the server builds provenance (principal, surface=console,
   * verified chat session) from the bearer. Returns the built signal's event_id. */
  signalThumbs(
    sessionId: string,
    body: { up: boolean; target_ref: string; comment?: string; message_id?: string },
  ): Promise<{ event_id: string }>;
  /** Flag a citation on an answer as wrong/irrelevant (CorrectionSignal). As
   * signalThumbs: identity is server-derived, never client-supplied. */
  signalWrongSource(
    sessionId: string,
    body: { answer_ref: string; citation_ref: string; comment?: string; message_id?: string },
  ): Promise<{ event_id: string }>;

  // --- NE-062 ops surfaces -------------------------------------------------
  listPendingApprovals(): Promise<{ approvals: PendingApprovalView[] }>;
  /** GET the governed, per-request payload disclosure (owner ruling
   * 2026-07-31, NE-APPROVAL-PAYLOAD) — served ONLY to the named approver(s)
   * for this request; every read is server-audited. A non-named approver's
   * request rejects with a typed `ApiError` (403) whose `detail` names the
   * policy — render it verbatim, never fabricate a reason. */
  readApprovalPayload(requestId: string): Promise<ApprovalPayloadDisclosureView>;
  /** Submit an APPROVE onto the NE-035 gate. The body carries NO approver /
   * principal / actor field — the SERVER derives the approver from the verified
   * OIDC bearer. This is the security crux: identity is never client-supplied. */
  approve(requestId: string): Promise<ApprovalActionResult>;
  /** Submit a DENY (UI verb) — mirrors DurableApprovalGate.reject. Like approve,
   * the approver identity is server-derived; only an optional reason is sent. */
  reject(requestId: string, reason?: string): Promise<ApprovalActionResult>;
  /** GET /v1/audit/events. `anchor_seq` asks for the page CONTAINING that
   * entry instead of the most recent page (the `?seq=` deep link's fetch); the
   * response's `anchor` field then carries the SERVER's reachability verdict,
   * rendered verbatim — the console never infers it from row counts (C7). */
  listAuditEvents(filters: {
    actor?: string;
    action?: string;
    from?: string;
    to?: string;
    limit?: number;
    anchor_seq?: number;
    /** Ask the server for the REAL unbounded `verify_chain` instead of the
     * anchored extension proof it answers a poll with. Changes the VERDICT
     * only — same rows, same projection, same ops gate — and costs an O(chain)
     * walk, so it is the explicit Re-verify sweep's flag and never the poll's.
     * It exists because a break is only ever confirmed off a full-chain walk
     * and a weaker proof may not withdraw a stronger proof's finding
     * (`audit/chainVerdict.ts`, I4): without it the one control an operator has
     * to clear a standing break cannot produce a verdict strong enough to clear
     * it. */
    full_verify?: boolean;
  }): Promise<AuditEventsPage>;
  /** GET /v1/audit/export — the governed CSV export of the SAME window
   * `listAuditEvents` would page (C6: ops-gated, same refusal as the ledger
   * read). The file is assembled 100% SERVER-SIDE from the audit_record_view
   * wire projection (C7) — this client never re-filters, re-sorts, or
   * reassembles rows; it hands back the server's bytes verbatim as a Blob for
   * the caller to save. */
  exportAuditCsv(filters: {
    actor?: string;
    action?: string;
    from?: string;
    to?: string;
    limit?: number;
  }): Promise<Blob>;
  replayAudit(selector: { trace_id?: string; event_id?: string }): Promise<ReplayResultView>;
  listEvalRuns(opts?: { suite?: string; limit?: number }): Promise<{ runs: EvalRunView[] }>;
  kpiSummary(): Promise<KpiSummaryView>;
  /** The sovereignty-posture surface (NE-131): C1-C12 attestation, effective
   * AIR_GAP/egress, model lanes, and the frozen config_hash + its history. */
  posture(): Promise<PostureView>;
  /** POST /v1/network/air-gap (NE-AIRGAP-TOGGLE, §18 B3's network write): ask
   * the GOVERNED path to change the air-gap posture. The server assigns the
   * tier (engaging applies immediately; disengaging parks for approval) and
   * answers applied | pending; a policy refusal is an `ApiError` 403 whose
   * `detail` is rendered verbatim. The body carries the TARGET posture only —
   * identity is server-derived from the bearer, never client-supplied. */
  setAirGap(airGap: boolean): Promise<GovernedChangeResult>;
  /** GET /v1/models/serving (NE-SERVING-STATUS): the server's LIVE observation
   * of each privately-served lane — reachability, the model the endpoint
   * itself reports, its serving window/engine version, and the observation's
   * timestamps. Ops-gated like /v1/audit/* (a non-ops principal gets a 403).
   * C6/C7 crux: the SERVER probes; this console renders the verdict and has no
   * route of its own to any model endpoint. */
  modelsServing(): Promise<ServingStatusView>;
  /** GET /v1/models/routing (NE-MODEL-ROUTING): which model the RUNNING gateway
   * routes each task to, what the deployment shipped, and every target the
   * SERVER would accept — each with the governed action and tier a repoint
   * would ride. Ops-gated. C7 crux: `selectable` is the server's judgement;
   * this console renders it and computes no part of it. */
  modelRouting(): Promise<ModelRoutingView>;
  /** POST /v1/models/routing (NE-MODEL-ROUTING): ask the GOVERNED path to
   * repoint a task at a different REGISTERED model. The body names a task and a
   * model ALIAS — never an endpoint and never a credential — and identity is
   * server-derived from the bearer. The server derives the action from the
   * target's lane, assigns the tier, and answers applied | pending; a refusal
   * is an `ApiError` 403/409 whose `detail` is rendered verbatim. */
  setModelRoute(task: string, model: string): Promise<GovernedChangeResult>;
  /** GET /v1/models/lifecycle: generic, server-ordered target state and the
   * exact actions currently admissible. No host executable detail crosses. */
  modelLifecycle(): Promise<InferenceLifecycleView>;
  /** Mint the server-bound, short-lived STOP confirmation. The caller keeps
   * the returned token only in component memory for the immediate submit. */
  confirmModelStop(
    targetId: string,
    expectedVersion: number,
  ): Promise<InferenceStopConfirmationView>;
  /** Submit one server-advertised transition. Identity, target semantics and
   * approval tier remain server-owned; the client supplies no host command. */
  transitionModelTarget(
    targetId: string,
    desiredState: "ready" | "stopped",
    expectedVersion: number,
    idempotencyKey: string,
    confirmationToken?: string,
  ): Promise<GovernedChangeResult>;
  /** The connector-sync status surface (NE-132): per-connector freshness,
   * fail-closed ACL-mirror state, last-sync audit seq, and quarantine counts +
   * the sanitized reason-category ledger. */
  connectStatus(): Promise<ConnectStatusView>;
  /** Queue a zero-body governed sync request. Identity and policy come from the server. */
  requestConnectSync(source: string): Promise<ConnectSyncRequestView>;
  /** Poll counts/metadata only for a durable sync request. */
  readConnectSync(requestId: string): Promise<ConnectSyncRequestView>;
  /** The ops-gated quarantine drill-down (NE-CONNECT-CSV): the parked rows
   * themselves — raw validator reason, record identity, bounded payload. A
   * non-ops principal gets the server's 403; the console renders the refusal,
   * never a locally-fabricated empty quarantine. */
  connectQuarantine(): Promise<ConnectQuarantineView>;
  /** Ops-only metadata queue. Currency and source membership are server-owned. */
  authorityAttempts(): Promise<AuthorityAttemptsView>;
  /** Read one current attempt; all non-current/missing ids are the same 404. */
  authorityAttempt(attemptId: string): Promise<AuthorityAttemptView>;
  /** Zero-body governed retry; source routing is derived by the server. */
  retryAuthorityAttempt(attemptId: string): Promise<AuthorityAttemptView>;

  // --- NE-067 steward surfaces ----------------------------------------------
  /** Non-terminal (proposed|approved) flywheel fix proposals, server-scoped and
   * ordered — no client-side filtering. */
  listStewardProposals(): Promise<{ proposals: ServedStewardProposalView[] }>;
  /** Submit an APPROVE onto the REAL NE-066 ProposalReviewService. The body
   * carries NO actor/principal/surface/session field — the SERVER derives the
   * steward identity from the verified OIDC bearer. This is the security crux:
   * identity is never client-supplied. */
  approveProposal(proposalId: string, note?: string): Promise<ReviewEventView>;
  /** Submit a REJECT with a typed rejection code (feeds NE-066's
   * rejection->signal loop server-side). Like approve, identity is
   * server-derived; only `code` and an optional `note` are sent. */
  rejectProposal(
    proposalId: string,
    code: RejectionCodeWire,
    note?: string,
  ): Promise<ReviewEventView>;
  /** RECORD a human-performed apply (WHO/WHEN are server-derived/stamped; the
   * console supplies only `applied_ref` + an optional note). The console NEVER
   * performs an apply — this only records that a human applied the change. */
  recordApply(proposalId: string, appliedRef: string, note?: string): Promise<ReviewEventView>;
  /** Apply an APPROVED forged skill candidate (NE-SKILL-1b). The steward's
   * ACL-label decision (acl_labels XOR global_visibility) is the assignment
   * moment; identity is server-derived. Registers the skill in ONE transaction. */
  applySkillProposal(
    proposalId: string,
    body: {
      acl_labels: string[];
      global_visibility: boolean;
      note: string;
      /** NE-SKILL-3 edit-before-approve — present ONLY when the steward changed a
       * field; omitted entirely for an unedited apply (no spurious server re-eval). */
      edits?: SkillEditFields;
    },
  ): Promise<SkillApplyView>;
  /** Server-computed weekly steward-activity digest (per-lane counts). */
  stewardDigest(weekStart?: string): Promise<StewardDigestView>;
  /** Server-computed steward-latency health metric (median/p90 wait, backlog). */
  stewardLatency(): Promise<StewardLatencyView>;
  /** Server-computed skill-library list (NE-SKILL-1a): folded rows INCLUDING
   * quarantined + pending-forge names. Steward-gated; read-only. */
  stewardSkills(): Promise<{ skills: SkillLibraryRow[] }>;
  /** Retire an ACTIVE skill (NE-SKILL-4). A HUMAN steward transition — identity
   * is server-derived; the body carries ONLY the reason. Content is retained
   * (never silent-delete); restore is one tap. */
  retireSkill(name: string, reason: string): Promise<{ name: string; decision: string }>;
  /** Restore a RETIRED skill (NE-SKILL-4) in one lifecycle transition. Identity
   * is server-derived; the body carries only the reason. */
  restoreSkill(name: string, reason: string): Promise<{ name: string; decision: string }>;

  // --- NE-134 graph surfaces ------------------------------------------------
  // These methods wrap the governed,
  // ACL-trimmed NE-133 read routes; the console renders exactly what they return
  // (C7). Client-supplied caps never exceed the server caps documented below.

  /** GET /v1/graph/entities — principal-visible entity discovery. The server
   * owns stable ordering, ACL/source-health trimming, cursor binding and the
   * default 25 / maximum 100 page size. */
  graphEntities(query?: GraphEntityListQuery, signal?: AbortSignal): Promise<GraphEntityListWire>;

  /** GET /v1/graph/facets — the entity classes and source systems present in
   * THIS principal's visible slice, for the entity finder's Class and Source
   * controls. Takes no filters: the vocabulary is a property of what the
   * principal can see, not of the filters currently applied, so this is one
   * request per Graph screen load — never one per keystroke or filter change.
   * The server owns derivation, ACL trimming and ordering; the console renders
   * the served lists verbatim and never substitutes a built-in list. */
  graphFacets(signal?: AbortSignal): Promise<GraphEntityFacetsWire>;

  /** POST /v1/graph/query — an anchored subgraph. At least one of anchor_keys /
   * anchor_citations must be non-empty (the server 422s an empty anchor set);
   * server caps: hops ≤ 2, limit_nodes ≤ 500, limit_edges ≤ 2000. */
  graphQuery(body: {
    anchor_keys?: string[];
    anchor_citations?: string[];
    hops?: number;
    limit_nodes?: number;
    limit_edges?: number;
  }): Promise<SubgraphWire>;

  /** GET /v1/graph/neighborhood?entity=…&hops=… — one entity's neighborhood
   * (BLOOM). Server caps: hops ≤ 4, limit_nodes ≤ 500, limit_edges ≤ 2000. */
  graphNeighborhood(
    entity: string,
    opts?: { hops?: number; limit_nodes?: number; limit_edges?: number },
  ): Promise<SubgraphWire>;

  /** POST /v1/graph/paths {from,to,max_hops,limit} — paths between two entities
   * (TRACE). `from`/`to` are the wire aliases (GraphPathsRequest). Server caps:
   * max_hops ≤ 4, limit ≤ 20. */
  graphPaths(
    fromKey: string,
    toKey: string,
    opts?: { max_hops?: number; limit?: number },
  ): Promise<GraphPathsWire>;

  // --- NE-136 T3 drawer surfaces -------------------------------------------
  // The ontology + verified-answer read/invoke seams for the /knowledge bottom
  // drawer. All wrap the governed NE-135 routes; the console renders EXACTLY
  // what they return (redacted server-side — no `delta_nt`, no violation
  // messages, no `acl`). C6 crux: `verifiedInvoke` is the ONLY invoke path and
  // it routes SERVER-SIDE through the frozen GovernedToolExecutor (read.kg.<id>,
  // tier-0) — the client constructs NO SPARQL and names NO store endpoint.

  /** GET /v1/ontology/classes — the BFO-2020/CCO-2.1/neop:* class browser tree
   * (iri + label + parents + source) with the sources version map + shapes_hash. */
  ontologyClasses(): Promise<OntologyClassesWire>;

  /** GET /v1/ontology/shapes — the compiled SHACL node-shape list + shapes_hash. */
  ontologyShapes(): Promise<OntologyShapesWire>;

  /** GET /v1/ontology/violations — the redacted ShapeViolation quarantine feed
   * (counts / ids / hashes only, C12); `configured:false` when unset. */
  ontologyViolations(): Promise<OntologyViolationsWire>;

  /** GET /v1/verified/templates — the verified-answer template catalog with each
   * template's strict generated `args_schema`; `enabled:false` when off. */
  verifiedTemplates(): Promise<VerifiedTemplatesWire>;

  /** POST /v1/verified/invoke {template_id, version?, args} — run a verified
   * answer. The body carries EXACTLY these keys (server `extra='forbid'`) and NO
   * principal/actor field — identity is the OIDC bearer, server-derived. C6: the
   * route executes ONLY through the frozen GovernedToolExecutor (read.kg.<id>,
   * tier-0); there is no raw SPARQL/store path from the client by construction.
   * The response's `replay_match` is the SERVER's double-invoke verdict. */
  verifiedInvoke(body: {
    template_id: string;
    version?: number;
    args: Record<string, unknown>;
  }): Promise<VerifiedInvokeWire>;

  // --- NE-167b Logfire-style trace view -------------------------------------

  /** GET /v1/trace/{trace_id} — the Logfire-style delegation trace assembly
   * (NE-167a). The response is 100% SERVER-COMPUTED (nesting, per-node
   * durations, per-model aggregates, the run rollup, and the chain-
   * verification verdict) and the console renders it VERBATIM — thin-client
   * C6/C7, no client-side recomputation of any cost/timing/verdict. Ops-gated
   * like /v1/audit/* (a non-ops principal gets a 403). */
  trace(traceId: string): Promise<TraceAssemblyView>;

  // --- NE-168 governed delegation demo trigger ------------------------------

  /** POST /v1/demo/delegate — dispatch the three registered sub-agents inside
   * ONE run_trace_scope and return the shared trace_id. Ops-gated exactly like
   * the trace read it feeds; a 404 means the demo seam is off in this
   * deployment (the honest default-off, never a silently-degraded endpoint).
   * This is the REAL router proof; there is deliberately no lane-switch method
   * on this client, because the backend has no such endpoint. */
  delegate(): Promise<DelegateRunView>;

  // --- NE-WORKFLOWS governed multi-step workflows ---------------------------

  /** Names-only live choices for one run surface. Availability and evidence
   * freshness are entirely server-owned. */
  modelChoices(surface: ModelChoiceSurface): Promise<ModelChoiceCatalogView>;

  /** GET /v1/workflows — what this deployment offers, each with the governed
   * tool names it READS and WRITES. Ops-gated; a 404 means the feature is off
   * in this deployment (the honest default-off). */
  listWorkflows(): Promise<WorkflowView[]>;
  /** POST /v1/workflows/{id}/run — run one end to end under ONE trace id.
   * BLOCKS for roughly half a minute per subject: the run reads, drafts, and
   * parks a real Tier-2 approval before it answers. `maxSubjects` is bounded
   * SERVER-side (`MAX_SUBJECTS_CEILING`); an over-asking caller gets the
   * server's own 422, which this console renders rather than pre-judging. */
  runWorkflow(workflowId: string, maxSubjects: number): Promise<WorkflowRunView>;
  /** POST /v1/agentic-workflows/{id}/run — a genuinely model-planned run under
   * fixed server caps. No deterministic fallback is used on this endpoint. */
  runAgenticWorkflow(workflowId: string, requestedModel?: string): Promise<AgenticWorkflowRunView>;
  /** Start without holding the browser request open; the opaque server ticket
   * supplies the trace id needed for live polling. */
  startAgenticWorkflow(
    workflowId: string,
    requestedModel?: string,
  ): Promise<AgenticWorkflowRunStatusView>;
  /** Read server-owned run state. Polling failures do not alter that state. */
  agenticWorkflowRun(runId: string): Promise<AgenticWorkflowRunStatusView>;
}

const DOCUMENT_ARTIFACT_FIELDS = [
  "kind",
  "label",
  "state",
  "ordinal",
  "identifier",
  "size_bytes",
  "artifact_count",
  "dimensions",
  "model",
  "index_reference",
  "fingerprint",
  "numeric_preview",
  "reason",
] as const;

const UNBOUND_AUDIT_DETAIL = "evidence unavailable: no bound audit receipt";
const UNBOUND_AUDIT_REASON = "unbound_audit_receipt";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOwn(value: object, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

// ===========================================================================
// 015A0-ENFORCEMENT — the two server-authored disclosure seams
//
// Both functions read a discriminant the SERVER wrote and neither derives one.
// Nothing here consults chunk counts, field presence, a cached earlier answer
// or an identity to decide what a caller may see: an availability the server
// did not state is an availability this console does not have.
// ===========================================================================

/**
 * The server's `AuthorizedRetrievalEnvelope`, preserved exactly, or nothing.
 *
 * ABSENT is absent: a pre-ENFORCEMENT server sends no envelope and the console
 * reports no retrieval status for it rather than manufacturing `complete`.
 * PRESENT AND MALFORMED FAILS THE READ: a response that carries an envelope
 * this console cannot read is a response whose completeness is unknown, and an
 * unknown completeness may not be rendered as a result set. It is never
 * repaired, defaulted or dropped to reveal the rows underneath.
 */
export function authorizedRetrievalEnvelope(
  value: unknown,
): AuthorizedRetrievalEnvelopeWire | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "object") return malformedAuthorizedRetrieval();
  const wire = value as Record<string, unknown>;
  const status = wire.status;
  if (status !== "complete" && status !== "partial" && status !== "exhausted") {
    return malformedAuthorizedRetrieval();
  }
  const counts = [wire.scanned_count, wire.returned_count, wire.scan_limit];
  if (!counts.every((count) => Number.isInteger(count) && Number(count) >= 0)) {
    return malformedAuthorizedRetrieval();
  }
  return {
    status,
    scanned_count: wire.scanned_count as number,
    returned_count: wire.returned_count as number,
    scan_limit: wire.scan_limit as number,
  };
}

function malformedAuthorizedRetrieval(): never {
  throw new TypeError("malformed authorized retrieval envelope");
}

/** One retrieval response, with its envelope validated and carried through
 * unchanged. The chunks are NOT trimmed, reordered or counted here: the
 * envelope describes the server's scan, not this console's render. */
function authorizedRetrieval(result: RetrievalResult): RetrievalResult {
  return {
    ...result,
    authorized_retrieval: authorizedRetrievalEnvelope(result.authorized_retrieval),
  };
}

/**
 * THE one place a served row's disclosure status is decided, for every
 * source-bearing surface in this console.
 *
 * It reads exactly one thing: the `disclosure_status` the SERVER wrote. It does
 * not look at which other fields arrived, at the caller, at a cached earlier
 * answer, or at anything this console happens to hold — deriving availability
 * from possession is the oracle ENFORCEMENT exists to close.
 *
 * FAIL CLOSED, in all three ways a row can fail to state one:
 *   * a row that is absent entirely,
 *   * a row that states NO status,
 *   * a row that states a status this console does not recognise,
 * all become `unresolved`: content-free and non-actionable. `unresolved` rather
 * than `withheld` because "the server did not tell me" is not the same fact as
 * "the server refused", and only the server may assert the second one.
 */
export type SourceDisclosure<T> =
  { kind: "available"; value: T } | { kind: "withheld" } | { kind: "unresolved" };

export function sourceDisclosure<T extends object>(row: T | null | undefined): SourceDisclosure<T> {
  if (row === null || row === undefined) return { kind: "unresolved" };
  const status = (row as { disclosure_status?: unknown }).disclosure_status;
  if (status === "available") return { kind: "available", value: row };
  if (status === "content_withheld") return { kind: "withheld" };
  return { kind: "unresolved" };
}

/**
 * The approval payload read, resolved through `sourceDisclosure`.
 *
 * Every non-available answer returns the SHELL AND ONLY THE SHELL — the
 * requested id and the status. Any protected field that arrived alongside a
 * withheld status is dropped here, at the seam, so no later consumer can render
 * bytes the gate said were no longer disclosable. The id is the one the caller
 * ASKED for, never one the response supplied.
 */
export function approvalPayloadDisclosure(
  requestId: string,
  body: ApprovalPayloadDisclosureView,
): ApprovalPayloadDisclosureView {
  const disclosure = sourceDisclosure(body);
  if (disclosure.kind === "available") return disclosure.value;
  return {
    request_id: requestId,
    disclosure_status: disclosure.kind === "withheld" ? "content_withheld" : "retryable_unknown",
  };
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || (Number.isInteger(value) && Number(value) >= 0);
}

function isNumericPreview(value: unknown, dimensions: unknown): value is number[] | null {
  return (
    value === null ||
    (Array.isArray(value) &&
      value.length > 0 &&
      value.length <= 8 &&
      Number.isInteger(dimensions) &&
      value.length <= Number(dimensions) &&
      value.every((component) => typeof component === "number" && Number.isFinite(component)))
  );
}

function isArtifactState(value: unknown): value is DocumentStageArtifactWire["state"] {
  return (
    value === "available" ||
    value === "empty" ||
    value === "skipped" ||
    value === "unavailable" ||
    value === "withheld"
  );
}

function malformedDocumentArtifacts(): never {
  throw new TypeError("malformed document stage artifact projection");
}

function malformedDocumentGraphEvidence(): never {
  throw new TypeError("malformed document graph evidence projection");
}

function malformedDocumentSemanticChunks(): never {
  throw new TypeError("malformed document semantic chunk projection");
}

function malformedDocumentStageEvidence(): never {
  throw new TypeError("malformed document stage evidence projection");
}

function legacyDocumentSemanticChunks(
  expectedStage: DocumentStageNameWire,
): DocumentSemanticChunksWire | null {
  return expectedStage === "chunk"
    ? {
        state: "unavailable",
        reason: "semantic_chunks_not_recorded",
        chunks: [],
        truncated: false,
      }
    : null;
}

function normalizeDocumentSemanticChunks(
  value: unknown,
  expectedStage: DocumentStageNameWire,
  unboundAudit: boolean,
): DocumentSemanticChunksWire | null {
  if (expectedStage !== "chunk") {
    if (value !== null) return malformedDocumentSemanticChunks();
    return null;
  }
  if (
    !isRecord(value) ||
    (value.state !== "available" && value.state !== "unavailable") ||
    !isNullableString(value.reason) ||
    !Array.isArray(value.chunks) ||
    value.chunks.length > 100 ||
    typeof value.truncated !== "boolean"
  ) {
    return malformedDocumentSemanticChunks();
  }
  const chunks = value.chunks.map((chunk) => {
    if (
      !isRecord(chunk) ||
      !isNonEmptyString(chunk.identifier) ||
      !Number.isInteger(chunk.size_bytes) ||
      Number(chunk.size_bytes) < 0
    ) {
      return malformedDocumentSemanticChunks();
    }
    return { identifier: chunk.identifier, size_bytes: Number(chunk.size_bytes) };
  });
  if (value.state === "available") {
    if (value.reason !== null || chunks.length === 0) return malformedDocumentSemanticChunks();
  } else if (
    value.reason !== (unboundAudit ? UNBOUND_AUDIT_REASON : "semantic_chunks_not_recorded") ||
    chunks.length !== 0 ||
    value.truncated
  ) {
    return malformedDocumentSemanticChunks();
  }
  return { state: value.state, reason: value.reason, chunks, truncated: value.truncated };
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value !== "";
}

function normalizeDocumentGraphEntity(value: unknown): DocumentGraphEntityWire {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.key) ||
    !isNonEmptyString(value.entity_class) ||
    !isNonEmptyString(value.display_name) ||
    !isNonEmptyString(value.source_system)
  ) {
    return malformedDocumentGraphEvidence();
  }
  // Rebuild the DTO so an unexpected internal receipt field can never cross
  // the thin-client render boundary.
  return {
    key: value.key,
    entity_class: value.entity_class,
    display_name: value.display_name,
    source_system: value.source_system,
  };
}

function normalizeDocumentGraphRelationship(value: unknown): DocumentGraphRelationshipWire {
  if (!isRecord(value) || !isNonEmptyString(value.key) || !isNonEmptyString(value.edge_kind)) {
    return malformedDocumentGraphEvidence();
  }
  return {
    key: value.key,
    edge_kind: value.edge_kind,
    subject: normalizeDocumentGraphEntity(value.subject),
    object: normalizeDocumentGraphEntity(value.object),
  };
}

function normalizeDocumentGraphEvidence(
  value: unknown,
  expectedStage: DocumentStageNameWire,
  unboundAudit: boolean,
): DocumentGraphEvidenceWire | null {
  if (expectedStage !== "graph") {
    if (value !== null) return malformedDocumentGraphEvidence();
    return null;
  }
  if (
    !isRecord(value) ||
    (value.state !== "available" &&
      value.state !== "empty" &&
      value.state !== "skipped" &&
      value.state !== "unavailable") ||
    !isNullableString(value.reason) ||
    !Array.isArray(value.entities) ||
    !Array.isArray(value.relationships) ||
    value.entities.length > 100 ||
    value.relationships.length > 100 ||
    typeof value.truncated !== "boolean"
  ) {
    return malformedDocumentGraphEvidence();
  }

  const entities = value.entities.map(normalizeDocumentGraphEntity);
  const relationships = value.relationships.map(normalizeDocumentGraphRelationship);
  if (value.state === "available") {
    if (value.reason !== null || (entities.length === 0 && relationships.length === 0)) {
      return malformedDocumentGraphEvidence();
    }
  } else {
    if (
      entities.length !== 0 ||
      relationships.length !== 0 ||
      value.truncated ||
      !isNonEmptyString(value.reason)
    ) {
      return malformedDocumentGraphEvidence();
    }
    if (value.state === "empty" && value.reason !== "no visible graph evidence") {
      return malformedDocumentGraphEvidence();
    }
    if (
      value.state === "unavailable" &&
      value.reason !==
        (unboundAudit ? UNBOUND_AUDIT_DETAIL : "version-bound graph evidence receipt unavailable")
    ) {
      return malformedDocumentGraphEvidence();
    }
  }
  return {
    state: value.state,
    reason: value.reason,
    entities,
    relationships,
    truncated: value.truncated,
  };
}

function normalizeDocumentArtifact(
  value: unknown,
  expectedStage: DocumentStageNameWire,
  index: number,
  unboundAudit: boolean,
): DocumentStageArtifactWire {
  if (!isRecord(value) || DOCUMENT_ARTIFACT_FIELDS.some((field) => !hasOwn(value, field))) {
    return malformedDocumentArtifacts();
  }

  const expectedKind =
    expectedStage === "chunk" ? "chunk" : expectedStage === "embed" ? "embedding" : null;
  const expectedLabel =
    expectedKind === "chunk"
      ? "Record chunk"
      : expectedKind === "embedding"
        ? "Vector artifact receipt"
        : null;
  if (
    expectedKind === null ||
    value.kind !== expectedKind ||
    value.label !== expectedLabel ||
    !isArtifactState(value.state) ||
    !Number.isInteger(value.ordinal) ||
    value.ordinal !== index ||
    !isNullableString(value.identifier) ||
    !isNullableNonNegativeInteger(value.size_bytes) ||
    !isNullableNonNegativeInteger(value.artifact_count) ||
    !isNullableNonNegativeInteger(value.dimensions) ||
    !isNullableString(value.model) ||
    !isNullableString(value.index_reference) ||
    !isNullableString(value.fingerprint) ||
    !isNumericPreview(value.numeric_preview, value.dimensions) ||
    !isNullableString(value.reason)
  ) {
    return malformedDocumentArtifacts();
  }

  if (value.state === "available") {
    if (value.reason !== null) return malformedDocumentArtifacts();
    if (expectedKind === "chunk") {
      if (
        value.identifier === null ||
        value.identifier === "" ||
        value.size_bytes === null ||
        value.artifact_count !== null ||
        value.dimensions !== null ||
        value.model !== null ||
        value.index_reference !== null ||
        value.fingerprint !== null ||
        value.numeric_preview !== null
      ) {
        return malformedDocumentArtifacts();
      }
    } else if (
      value.identifier !== null ||
      value.size_bytes !== null ||
      value.artifact_count === null ||
      value.artifact_count < 1 ||
      value.dimensions === null ||
      value.dimensions < 1 ||
      value.model === null ||
      value.model === "" ||
      value.fingerprint === null ||
      !/^[0-9a-f]{64}$/.test(value.fingerprint) ||
      value.index_reference !== `sha256:${value.fingerprint}`
    ) {
      return malformedDocumentArtifacts();
    }
  } else {
    if (
      value.identifier !== null ||
      value.size_bytes !== null ||
      value.artifact_count !== null ||
      value.dimensions !== null ||
      value.model !== null ||
      value.index_reference !== null ||
      value.fingerprint !== null ||
      value.numeric_preview !== null
    ) {
      return malformedDocumentArtifacts();
    }
    if (
      value.state === "unavailable" &&
      value.reason !== (unboundAudit ? UNBOUND_AUDIT_REASON : "not_recorded")
    ) {
      return malformedDocumentArtifacts();
    }
    if (value.state === "skipped" && (value.reason === null || value.reason === "")) {
      return malformedDocumentArtifacts();
    }
  }

  return value as unknown as DocumentStageArtifactWire;
}

function normalizeDocumentFacts(value: unknown, unboundAudit: boolean) {
  if (!Array.isArray(value)) return malformedDocumentStageEvidence();
  return value.map((fact) => {
    if (
      !isRecord(fact) ||
      !isNonEmptyString(fact.key) ||
      !isNonEmptyString(fact.label) ||
      typeof fact.available !== "boolean" ||
      (fact.value !== null &&
        typeof fact.value !== "string" &&
        typeof fact.value !== "number" &&
        typeof fact.value !== "boolean") ||
      (unboundAudit && (fact.available || fact.value !== null))
    ) {
      return malformedDocumentStageEvidence();
    }
    return {
      key: fact.key,
      label: fact.label,
      value: fact.value,
      available: fact.available,
    };
  });
}

function hasUnboundAuditProjection(value: Record<string, unknown>): boolean {
  const hasUnboundSignal =
    value.audit_seq === null ||
    value.status === "unavailable" ||
    value.detail === UNBOUND_AUDIT_DETAIL ||
    value.occurred_at === null;
  if (!hasUnboundSignal) return false;
  if (
    value.audit_seq !== null ||
    value.status !== "unavailable" ||
    value.detail !== UNBOUND_AUDIT_DETAIL ||
    value.occurred_at !== null
  ) {
    return malformedDocumentStageEvidence();
  }
  return true;
}

function legacyDocumentArtifacts(
  stage: DocumentStageNameWire,
  status: unknown,
  detail: unknown,
): DocumentStageArtifactWire[] {
  const kind = stage === "chunk" ? "chunk" : stage === "embed" ? "embedding" : null;
  if (kind === null) return [];
  const skipped = status === "skipped";
  if (skipped && (typeof detail !== "string" || detail === "")) {
    return malformedDocumentArtifacts();
  }
  return [
    {
      kind,
      label: kind === "chunk" ? "Record chunk" : "Vector artifact receipt",
      state: skipped ? "skipped" : "unavailable",
      ordinal: 0,
      identifier: null,
      size_bytes: null,
      artifact_count: null,
      dimensions: null,
      model: null,
      index_reference: null,
      fingerprint: null,
      numeric_preview: null,
      reason: skipped ? (detail as string) : "not_recorded",
    },
  ];
}

function normalizeDocumentStageEvidence(
  value: unknown,
  expectedStage: DocumentStageNameWire,
): DocumentStageEvidenceWire {
  if (!isRecord(value) || value.stage !== expectedStage) return malformedDocumentArtifacts();
  const unboundAudit = hasUnboundAuditProjection(value);
  if (!unboundAudit && !isNonEmptyString(value.occurred_at)) {
    return malformedDocumentStageEvidence();
  }
  const facts = normalizeDocumentFacts(value.facts, unboundAudit);
  if (!hasOwn(value, "graph_evidence")) return malformedDocumentGraphEvidence();
  const graphEvidence = normalizeDocumentGraphEvidence(
    value.graph_evidence,
    expectedStage,
    unboundAudit,
  );
  if (unboundAudit && expectedStage === "chunk" && !hasOwn(value, "semantic_chunks")) {
    return malformedDocumentSemanticChunks();
  }
  const semanticChunks = hasOwn(value, "semantic_chunks")
    ? normalizeDocumentSemanticChunks(value.semantic_chunks, expectedStage, unboundAudit)
    : legacyDocumentSemanticChunks(expectedStage);
  if (
    expectedStage === "graph" &&
    graphEvidence !== null &&
    ((graphEvidence.state === "skipped") !== (value.status === "skipped") ||
      (graphEvidence.state === "skipped" && graphEvidence.reason !== value.detail))
  ) {
    return malformedDocumentGraphEvidence();
  }
  if (unboundAudit && !hasOwn(value, "artifacts")) return malformedDocumentArtifacts();
  const artifacts = hasOwn(value, "artifacts")
    ? value.artifacts
    : legacyDocumentArtifacts(expectedStage, value.status, value.detail);
  if (!Array.isArray(artifacts)) return malformedDocumentArtifacts();
  if (
    ((expectedStage === "chunk" || expectedStage === "embed") && artifacts.length === 0) ||
    (expectedStage !== "chunk" && expectedStage !== "embed" && artifacts.length !== 0)
  ) {
    return malformedDocumentArtifacts();
  }
  return {
    ...value,
    facts,
    artifacts: artifacts.map((artifact, index) =>
      normalizeDocumentArtifact(artifact, expectedStage, index, unboundAudit),
    ),
    semantic_chunks: semanticChunks,
    graph_evidence: graphEvidence,
  } as unknown as DocumentStageEvidenceWire;
}

export function createApiClient(options: ApiClientOptions): ApiClient {
  const { getAccessToken, onUnauthorized, ensureFreshAccessToken } = options;

  /** Pre-submit credential freshness for run-starting requests. NEVER blocks
   * the submission: a missing hook or a failed renew proceeds on the current
   * token, and the server's expiry enforcement is untouched. */
  async function ensureRunCredential(): Promise<void> {
    if (ensureFreshAccessToken === undefined) return;
    try {
      await ensureFreshAccessToken(RUN_CREDENTIAL_MIN_REMAINING_SECONDS);
    } catch {
      // Fail-open to the current token; expiry stays server-enforced.
    }
  }

  function authHeaders(): Record<string, string> {
    const token = getAccessToken();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    return headers;
  }

  async function request<T>(path: string, init: RequestInit): Promise<T> {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { ...authHeaders(), ...(init.headers ?? {}) },
    });
    if (response.status === 401) {
      onUnauthorized();
      throw new ApiError("unauthorized", 401);
    }
    if (!response.ok) {
      // Best-effort: surface the governed API's `detail` reason (e.g. an
      // approval 403 "not in the Tier-3 named-approver list"). A body that is
      // absent or not JSON is ignored — the ApiError still carries the status.
      let detail: string | undefined;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // non-JSON / empty body — leave detail undefined.
      }
      throw new ApiError(
        `request to ${path} failed with HTTP ${response.status}`,
        response.status,
        detail,
      );
    }
    return (await response.json()) as T;
  }

  return {
    me() {
      return request<PrincipalView>("/v1/me", { method: "GET" });
    },

    listAgents(cursor?: string) {
      const params = new URLSearchParams();
      if (cursor !== undefined) params.set("cursor", cursor);
      const query = params.toString();
      return request<AgentCatalogPageWire>(`/v1/agents${query ? `?${query}` : ""}`, {
        method: "GET",
      });
    },

    getAgent(agentId: string) {
      return request<AgentCatalogItemWire>(`/v1/agents/${encodeURIComponent(agentId)}`, {
        method: "GET",
      });
    },

    getAgentPreflight(agentId: string, capabilityHash?: string) {
      const params = new URLSearchParams();
      if (capabilityHash !== undefined) params.set("capability_hash", capabilityHash);
      const query = params.toString();
      return request<AgentCapabilityPreflightWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/preflight${query ? `?${query}` : ""}`,
        { method: "GET" },
      );
    },

    listAgentConversations(agentId: string, cursor?: string) {
      const params = new URLSearchParams();
      if (cursor !== undefined) params.set("cursor", cursor);
      const query = params.toString();
      return request<AgentConversationPageWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/conversations${query ? `?${query}` : ""}`,
        { method: "GET" },
      );
    },

    createAgentConversation(agentId: string) {
      return request<AgentConversationWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/conversations`,
        { method: "POST" },
      );
    },

    getConversation(agentId: string, conversationId: string) {
      return request<AgentConversationWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/conversations/${encodeURIComponent(conversationId)}`,
        { method: "GET" },
      );
    },

    listConversationTurns(agentId: string, conversationId: string, cursor?: string) {
      const params = new URLSearchParams();
      if (cursor !== undefined) params.set("cursor", cursor);
      const query = params.toString();
      return request<AgentConversationTurnPageWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/conversations/${encodeURIComponent(conversationId)}/turns${query ? `?${query}` : ""}`,
        { method: "GET" },
      );
    },

    getAgentWorkflowProfile(agentId: string) {
      return request<AgentWorkflowProfileWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/workflow-profile`,
        { method: "GET" },
      );
    },

    listAgentRunUpdates(
      view: AgentRunUpdateViewWire,
      windowStart: string,
      windowEnd: string,
      cursor?: string,
    ) {
      const params = new URLSearchParams({
        view,
        window_start: windowStart,
        window_end: windowEnd,
      });
      if (cursor !== undefined) params.set("cursor", cursor);
      return request<AgentRunUpdatePageWire>(`/v1/agents/updates?${params.toString()}`, {
        method: "GET",
      });
    },

    listConversationRuns(agentId: string, conversationId: string, cursor?: string) {
      const params = new URLSearchParams();
      if (cursor !== undefined) params.set("cursor", cursor);
      const query = params.toString();
      return request<AgentRunPageWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/conversations/${encodeURIComponent(conversationId)}/runs${query ? `?${query}` : ""}`,
        { method: "GET" },
      );
    },

    listAgentRuns(agentId: string, cursor?: string) {
      const params = new URLSearchParams();
      if (cursor !== undefined) params.set("cursor", cursor);
      const query = params.toString();
      return request<AgentRunPageWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/runs${query ? `?${query}` : ""}`,
        { method: "GET" },
      );
    },

    getAgentRun(agentId: string, runId: string) {
      return request<AgentRunDetailWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(runId)}`,
        { method: "GET" },
      );
    },

    listAgentRunEvents(agentId: string, runId: string, cursor?: string, limit?: number) {
      const params = new URLSearchParams();
      if (cursor !== undefined) params.set("cursor", cursor);
      if (limit !== undefined) params.set("limit", String(limit));
      const query = params.toString();
      return request<AgentRunEventPageWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(runId)}/events${query ? `?${query}` : ""}`,
        { method: "GET" },
      );
    },

    getAgentRunEvidence(agentId: string, runId: string) {
      return request<AgentEvidenceEnvelopeWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(runId)}/evidence`,
        { method: "GET", cache: "no-store" },
      );
    },

    exportAgentRunEvidence(agentId: string, runId: string) {
      return request<AgentEvidenceEnvelopeWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(runId)}/evidence/export`,
        { method: "GET", cache: "no-store" },
      );
    },

    cancelAgentRun(
      agentId: string,
      runId: string,
      body: ControlAgentRunRequestWire,
      idempotencyKey: string,
    ) {
      return request<AcceptedAgentRunSubmissionWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(runId)}/cancel`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          body: JSON.stringify(body),
        },
      );
    },

    async retryAgentRun(
      agentId: string,
      runId: string,
      body: ControlAgentRunRequestWire,
      idempotencyKey: string,
    ) {
      await ensureRunCredential();
      return request<AcceptedAgentRunSubmissionWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(runId)}/retry`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          body: JSON.stringify(body),
        },
      );
    },

    async startAgentRun(agentId: string, body: StartAgentRunRequestWire, idempotencyKey: string) {
      await ensureRunCredential();
      return request<AcceptedAgentRunSubmissionWire>(
        `/v1/agents/${encodeURIComponent(agentId)}/runs`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          body: JSON.stringify(body),
        },
      );
    },

    chatCapabilities() {
      return request<ChatCapabilitiesView>("/v1/chat/capabilities", { method: "GET" });
    },

    createSession(agentName: string) {
      return request<ChatSession>("/v1/chat/sessions", {
        method: "POST",
        body: JSON.stringify({ agent_name: agentName }),
      });
    },

    async sendMessage(
      sessionId,
      prompt,
      onEvent,
      signal,
      onHeartbeat,
      requestedModel,
      idempotencyKey,
    ) {
      try {
        // A chat turn STARTS A RUN: the bearer this request presents is frozen
        // into the run's authorization snapshot at admission. Renew first so
        // the snapshot outlives the run's whole execution window.
        await ensureRunCredential();
        await streamSse({
          url: `${API_BASE}/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
          headers: {
            ...authHeaders(),
            ...(idempotencyKey === undefined ? {} : { "Idempotency-Key": idempotencyKey }),
          },
          body: JSON.stringify({
            prompt,
            ...(requestedModel === undefined ? {} : { requested_model: requestedModel }),
          }),
          signal,
          onComment: onHeartbeat,
          onFrame: (frame) => {
            // The `event:` field routes; the JSON payload carries the fields. We
            // trust the governed API's contract and coerce to the StreamEvent
            // union, defaulting the discriminant to the SSE event name.
            const parsed = JSON.parse(frame.data) as Record<string, unknown>;
            const type = (parsed.type as string | undefined) ?? frame.event;
            onEvent({ ...parsed, type } as StreamEvent);
          },
        });
      } catch (err) {
        // Exact parity with request(): a streaming 401 (token expired before the
        // chat stream) drives the SAME re-auth path — call onUnauthorized once
        // and surface a uniform ApiError(401), never a raw SseError. A typed
        // refusal/unavailable response for an EXPLICIT model selection keeps
        // its status and safe server detail as ApiError so the run surface can
        // invalidate that catalog evidence. Legacy non-selection failures and
        // AbortError still propagate unchanged.
        if (err instanceof SseError && err.status === 401) {
          onUnauthorized();
          throw new ApiError("unauthorized", 401);
        }
        if (
          requestedModel !== undefined &&
          err instanceof SseError &&
          (err.status === 403 || err.status === 409 || err.status === 503)
        ) {
          // `err.developerMessage`, not `err.message`: the raw transport
          // string is what this class's developer slot is for, and passing the
          // reader's sentence would put a sentence in both slots and lose the
          // only record of the wire failure. The re-thrown ApiError composes
          // its own `userMessage` from the same status and detail, so nothing
          // the reader sees changes.
          throw new ApiError(err.developerMessage, err.status, err.detail);
        }
        throw err;
      }
    },

    async search(query: string, topK = 10, signal?: AbortSignal) {
      const result = await request<RetrievalResult>("/v1/brain/search", {
        method: "POST",
        body: JSON.stringify({ query, top_k: topK }),
        signal,
      });
      return authorizedRetrieval(result);
    },

    async retrieve(chunkIds: string[]) {
      const result = await request<RetrievalResult>("/v1/brain/retrieve", {
        method: "POST",
        body: JSON.stringify({ chunk_ids: chunkIds }),
      });
      return authorizedRetrieval(result);
    },

    listDocuments(query = {}, signal) {
      const params = new URLSearchParams();
      if (query.q !== undefined) params.set("q", query.q);
      if (query.type !== undefined) params.set("type", query.type);
      if (query.source !== undefined) params.set("source", query.source);
      // INTERIM (PD-DOC-TITLES): structured-ness inferred from the
      // ".negraph.json" connector filename convention; REMOVE when the real
      // `structured` flag lands on the document wire. Omitted/false EXCLUDES
      // operational-graph records; true is the explicit opt-in.
      if (query.structured !== undefined) params.set("structured", String(query.structured));
      if (query.cursor !== undefined) params.set("cursor", query.cursor);
      if (query.limit !== undefined) params.set("limit", String(query.limit));
      const qs = params.toString();
      return request<DocumentListWire>(`/v1/documents${qs ? `?${qs}` : ""}`, {
        method: "GET",
        signal,
      });
    },

    documentJourney(documentId, selector, signal) {
      const params = new URLSearchParams();
      if (selector?.version !== undefined) params.set("version", String(selector.version));
      if (selector?.as_of !== undefined) params.set("as_of", selector.as_of);
      const qs = params.toString();
      return request<DocumentJourneyReadWire>(
        `/v1/documents/${encodeURIComponent(documentId)}/journey${qs ? `?${qs}` : ""}`,
        { method: "GET", signal },
      );
    },

    async documentStageEvidence(documentId, version, stage, signal) {
      const raw = await request<unknown>(
        `/v1/documents/${encodeURIComponent(documentId)}/versions/${version}/stages/${stage}`,
        { method: "GET", signal },
      );
      return normalizeDocumentStageEvidence(raw, stage);
    },

    // --- NE-137 flywheel intake (chat feedback signals) -------------------

    signalThumbs(sessionId, body) {
      // Identity is NEVER on the wire — only the payload fields; the server derives
      // provenance from the verified bearer + the verified chat session.
      return request<{ event_id: string }>(
        `/v1/chat/sessions/${encodeURIComponent(sessionId)}/signals/thumbs`,
        { method: "POST", body: JSON.stringify(body) },
      );
    },

    signalWrongSource(sessionId, body) {
      return request<{ event_id: string }>(
        `/v1/chat/sessions/${encodeURIComponent(sessionId)}/signals/wrong-source`,
        { method: "POST", body: JSON.stringify(body) },
      );
    },

    // --- NE-062 ops surfaces ----------------------------------------------

    listPendingApprovals() {
      return request<{ approvals: PendingApprovalView[] }>("/v1/approvals/pending", {
        method: "GET",
      });
    },

    resolvePrincipals(scope, references) {
      return request<ResolvedPrincipalsView>("/v1/principals/resolve", {
        method: "POST",
        body: JSON.stringify({ scope, references }),
      });
    },

    async readApprovalPayload(requestId: string) {
      // Mirrors approve()'s path-building exactly (encodeURIComponent on the id).
      const body = await request<ApprovalPayloadDisclosureView>(
        `/v1/approvals/${encodeURIComponent(requestId)}/payload`,
        { method: "GET" },
      );
      return approvalPayloadDisclosure(requestId, body);
    },

    approve(requestId: string) {
      // SECURITY CRUX: the body is empty — it carries NO approver/principal/actor
      // field. The governed backend derives the approver from the verified OIDC
      // bearer and routes the decision to the real NE-035 DurableApprovalGate.
      return request<ApprovalActionResult>(
        `/v1/approvals/${encodeURIComponent(requestId)}/approve`,
        { method: "POST", body: JSON.stringify({}) },
      );
    },

    reject(requestId: string, reason?: string) {
      // Mirrors DurableApprovalGate.reject. Only an optional reason is sent; the
      // approver identity is server-derived (never client-supplied), as above.
      return request<ApprovalActionResult>(
        `/v1/approvals/${encodeURIComponent(requestId)}/reject`,
        { method: "POST", body: JSON.stringify({ reason: reason ?? "" }) },
      );
    },

    listAuditEvents(filters) {
      const params = new URLSearchParams();
      if (filters.actor) params.set("actor", filters.actor);
      if (filters.action) params.set("action", filters.action);
      if (filters.from) params.set("from", filters.from);
      if (filters.to) params.set("to", filters.to);
      if (filters.limit !== undefined) params.set("limit", String(filters.limit));
      if (filters.anchor_seq !== undefined) params.set("anchor_seq", String(filters.anchor_seq));
      if (filters.full_verify) params.set("full_verify", "true");
      const qs = params.toString();
      return request<AuditEventsPage>(`/v1/audit/events${qs ? `?${qs}` : ""}`, { method: "GET" });
    },

    async exportAuditCsv(filters) {
      const params = new URLSearchParams();
      if (filters.actor) params.set("actor", filters.actor);
      if (filters.action) params.set("action", filters.action);
      if (filters.from) params.set("from", filters.from);
      if (filters.to) params.set("to", filters.to);
      if (filters.limit !== undefined) params.set("limit", String(filters.limit));
      const qs = params.toString();
      // Cannot reuse request<T>() — it always JSON-parses the body; the export
      // response is a CSV stream. Same auth header / 401 / error-detail
      // discipline as request(), applied to a raw fetch.
      const response = await fetch(`${API_BASE}/v1/audit/export${qs ? `?${qs}` : ""}`, {
        method: "GET",
        headers: authHeaders(),
      });
      if (response.status === 401) {
        onUnauthorized();
        throw new ApiError("unauthorized", 401);
      }
      if (!response.ok) {
        let detail: string | undefined;
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") detail = body.detail;
        } catch {
          // non-JSON / empty body — leave detail undefined.
        }
        throw new ApiError(
          `request to /v1/audit/export failed with HTTP ${response.status}`,
          response.status,
          detail,
        );
      }
      return await response.blob();
    },

    replayAudit(selector) {
      const params = new URLSearchParams();
      if (selector.trace_id) params.set("trace_id", selector.trace_id);
      if (selector.event_id) params.set("event_id", selector.event_id);
      const qs = params.toString();
      return request<ReplayResultView>(`/v1/audit/replay${qs ? `?${qs}` : ""}`, { method: "GET" });
    },

    listEvalRuns(opts) {
      const params = new URLSearchParams();
      if (opts?.suite) params.set("suite", opts.suite);
      if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
      const qs = params.toString();
      return request<{ runs: EvalRunView[] }>(`/v1/eval/runs${qs ? `?${qs}` : ""}`, {
        method: "GET",
      });
    },

    kpiSummary() {
      return request<KpiSummaryView>("/v1/kpi/summary", { method: "GET" });
    },

    posture() {
      return request<PostureView>("/v1/posture", { method: "GET" });
    },
    setAirGap(airGap: boolean) {
      // The body is the TARGET posture and nothing else — no identity field
      // (server-derived from the bearer, the approve() security crux).
      return request<GovernedChangeResult>("/v1/network/air-gap", {
        method: "POST",
        body: JSON.stringify({ air_gap: airGap }),
      });
    },
    modelsServing() {
      return request<ServingStatusView>("/v1/models/serving", { method: "GET" });
    },
    modelRouting() {
      return request<ModelRoutingView>("/v1/models/routing", { method: "GET" });
    },
    setModelRoute(task: string, model: string) {
      // Task + model ALIAS only — no identity field (server-derived from the
      // bearer) and no endpoint (the server re-selects among registered models).
      return request<GovernedChangeResult>("/v1/models/routing", {
        method: "POST",
        body: JSON.stringify({ task, model }),
      });
    },
    modelLifecycle() {
      return request<InferenceLifecycleView>("/v1/models/lifecycle", { method: "GET" });
    },
    confirmModelStop(targetId: string, expectedVersion: number) {
      return request<InferenceStopConfirmationView>(
        `/v1/models/lifecycle/${encodeURIComponent(targetId)}/stop-confirmation`,
        {
          method: "POST",
          body: JSON.stringify({ expected_version: expectedVersion }),
        },
      );
    },
    transitionModelTarget(
      targetId: string,
      desiredState: "ready" | "stopped",
      expectedVersion: number,
      idempotencyKey: string,
      confirmationToken?: string,
    ) {
      return request<GovernedChangeResult>(
        `/v1/models/lifecycle/${encodeURIComponent(targetId)}/transition`,
        {
          method: "POST",
          body: JSON.stringify({
            desired_state: desiredState,
            expected_version: expectedVersion,
            idempotency_key: idempotencyKey,
            ...(confirmationToken === undefined ? {} : { confirmation_token: confirmationToken }),
          }),
        },
      );
    },
    connectStatus() {
      return request<ConnectStatusView>("/v1/connect/status", { method: "GET" });
    },
    requestConnectSync(source: string) {
      return request<ConnectSyncRequestView>(`/v1/connect/${encodeURIComponent(source)}/sync`, {
        method: "POST",
      });
    },
    readConnectSync(requestId: string) {
      return request<ConnectSyncRequestView>(`/v1/connect/sync/${encodeURIComponent(requestId)}`, {
        method: "GET",
      });
    },
    connectQuarantine() {
      return request<ConnectQuarantineView>("/v1/connect/quarantine", { method: "GET" });
    },
    authorityAttempts() {
      return request<AuthorityAttemptsView>("/v1/connect/authority/attempts", { method: "GET" });
    },
    authorityAttempt(attemptId: string) {
      return request<AuthorityAttemptView>(
        `/v1/connect/authority/attempts/${encodeURIComponent(attemptId)}`,
        { method: "GET" },
      );
    },
    retryAuthorityAttempt(attemptId: string) {
      return request<AuthorityAttemptView>(
        `/v1/connect/authority/attempts/${encodeURIComponent(attemptId)}/retry`,
        { method: "POST" },
      );
    },

    // --- NE-067 steward surfaces --------------------------------------------

    listStewardProposals() {
      return request<{ proposals: ServedStewardProposalView[] }>("/v1/steward/proposals", {
        method: "GET",
      });
    },

    approveProposal(proposalId: string, note?: string) {
      // SECURITY CRUX: the body carries NO actor/principal/surface/session
      // field. The governed backend derives the steward identity from the
      // verified OIDC bearer and drives the REAL NE-066 ProposalReviewService.
      return request<ReviewEventView>(
        `/v1/steward/proposals/${encodeURIComponent(proposalId)}/approve`,
        { method: "POST", body: JSON.stringify({ note: note ?? "" }) },
      );
    },

    rejectProposal(proposalId: string, code: RejectionCodeWire, note?: string) {
      // SECURITY CRUX: as above — no client-supplied identity. `code` is a
      // typed RejectionCode that feeds NE-066's rejection->signal loop
      // server-side.
      return request<ReviewEventView>(
        `/v1/steward/proposals/${encodeURIComponent(proposalId)}/reject`,
        { method: "POST", body: JSON.stringify({ code, note: note ?? "" }) },
      );
    },

    recordApply(proposalId: string, appliedRef: string, note?: string) {
      // SECURITY CRUX: as above — no client-supplied identity. The console
      // NEVER performs an apply; this call only RECORDS that a human applied
      // the change (WHO/WHEN are server-derived/stamped; `applied_ref` is the
      // PR URL / commit / OPA-bundle version the human supplies).
      return request<ReviewEventView>(
        `/v1/steward/proposals/${encodeURIComponent(proposalId)}/apply`,
        { method: "POST", body: JSON.stringify({ applied_ref: appliedRef, note: note ?? "" }) },
      );
    },

    applySkillProposal(
      proposalId: string,
      body: {
        acl_labels: string[];
        global_visibility: boolean;
        note: string;
        edits?: SkillEditFields;
      },
    ) {
      // SECURITY CRUX: as above — no client-supplied identity. The steward's
      // ACL-label decision here is the ASSIGNMENT MOMENT (R-22): the body carries
      // EITHER a non-empty acl_labels set OR global_visibility=true (never both,
      // never neither — enforced server-side and client-side). The apply registers
      // the skill in ONE transaction bound to this approval on the audit chain.
      // NE-SKILL-3: `edits` is included ONLY when the steward changed a field, so
      // an unedited apply sends the exact old wire body (no spurious server re-eval).
      const payload: {
        acl_labels: string[];
        global_visibility: boolean;
        note: string;
        edits?: SkillEditFields;
      } = {
        acl_labels: body.acl_labels,
        global_visibility: body.global_visibility,
        note: body.note,
      };
      if (body.edits !== undefined) payload.edits = body.edits;
      return request<SkillApplyView>(
        `/v1/steward/proposals/${encodeURIComponent(proposalId)}/apply-skill`,
        { method: "POST", body: JSON.stringify(payload) },
      );
    },

    stewardDigest(weekStart?: string) {
      const params = new URLSearchParams();
      if (weekStart) params.set("week_start", weekStart);
      const qs = params.toString();
      return request<StewardDigestView>(`/v1/steward/digest${qs ? `?${qs}` : ""}`, {
        method: "GET",
      });
    },

    stewardLatency() {
      return request<StewardLatencyView>("/v1/steward/latency", { method: "GET" });
    },

    stewardSkills() {
      return request<{ skills: SkillLibraryRow[] }>("/v1/steward/skills", { method: "GET" });
    },

    retireSkill(name: string, reason: string) {
      return request<{ name: string; decision: string }>(
        `/v1/steward/skills/${encodeURIComponent(name)}/retire`,
        { method: "POST", body: JSON.stringify({ reason }) },
      );
    },

    restoreSkill(name: string, reason: string) {
      return request<{ name: string; decision: string }>(
        `/v1/steward/skills/${encodeURIComponent(name)}/restore`,
        { method: "POST", body: JSON.stringify({ reason }) },
      );
    },

    // --- NE-134 graph surfaces ---------------------------------------------

    graphEntities(query = {}, signal) {
      const params = new URLSearchParams();
      if (query.q !== undefined) params.set("q", query.q);
      if (query.entity_class !== undefined) params.set("entity_class", query.entity_class);
      if (query.source !== undefined) params.set("source", query.source);
      if (query.assurance !== undefined) params.set("assurance", query.assurance);
      if (query.cursor !== undefined) params.set("cursor", query.cursor);
      if (query.limit !== undefined) params.set("limit", String(query.limit));
      const qs = params.toString();
      return request<GraphEntityListWire>(`/v1/graph/entities${qs ? `?${qs}` : ""}`, {
        method: "GET",
        signal,
      });
    },

    graphFacets(signal) {
      return request<GraphEntityFacetsWire>("/v1/graph/facets", { method: "GET", signal });
    },

    graphQuery(body) {
      // Send only the fields the caller set (the server defaults the rest to its
      // caps). Caps we send never exceed the server caps (≤500/≤2000, hops ≤2).
      const payload: Record<string, unknown> = {};
      if (body.anchor_keys && body.anchor_keys.length > 0) payload.anchor_keys = body.anchor_keys;
      if (body.anchor_citations && body.anchor_citations.length > 0) {
        payload.anchor_citations = body.anchor_citations;
      }
      if (body.hops !== undefined) payload.hops = body.hops;
      if (body.limit_nodes !== undefined) payload.limit_nodes = body.limit_nodes;
      if (body.limit_edges !== undefined) payload.limit_edges = body.limit_edges;
      return request<SubgraphWire>("/v1/graph/query", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },

    graphNeighborhood(entity, opts) {
      const params = new URLSearchParams();
      params.set("entity", entity);
      if (opts?.hops !== undefined) params.set("hops", String(opts.hops));
      if (opts?.limit_nodes !== undefined) params.set("limit_nodes", String(opts.limit_nodes));
      if (opts?.limit_edges !== undefined) params.set("limit_edges", String(opts.limit_edges));
      return request<SubgraphWire>(`/v1/graph/neighborhood?${params.toString()}`, {
        method: "GET",
      });
    },

    graphPaths(fromKey, toKey, opts) {
      // 'from'/'to' are the wire aliases (GraphPathsRequest.from_key/to_key).
      const payload: Record<string, unknown> = { from: fromKey, to: toKey };
      payload.max_hops = opts?.max_hops ?? 4;
      payload.limit = opts?.limit ?? 20;
      return request<GraphPathsWire>("/v1/graph/paths", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },

    // --- NE-136 T3 drawer surfaces -----------------------------------------

    ontologyClasses() {
      return request<OntologyClassesWire>("/v1/ontology/classes", { method: "GET" });
    },

    ontologyShapes() {
      return request<OntologyShapesWire>("/v1/ontology/shapes", { method: "GET" });
    },

    ontologyViolations() {
      return request<OntologyViolationsWire>("/v1/ontology/violations", { method: "GET" });
    },

    verifiedTemplates() {
      return request<VerifiedTemplatesWire>("/v1/verified/templates", { method: "GET" });
    },

    verifiedInvoke(body) {
      // SECURITY / C6 CRUX: send EXACTLY {template_id, args, version?} — the
      // server forbids extra keys (`extra='forbid'`), so a smuggled
      // sparql/query/principal would be a LOUD 422, never a silent bypass. The
      // body carries NO actor/principal field: identity is the verified OIDC
      // bearer, server-derived. The route runs ONLY through the frozen
      // GovernedToolExecutor (read.kg.<id>, tier-0) — the client has no raw
      // SPARQL/store path by construction.
      const payload: Record<string, unknown> = { template_id: body.template_id, args: body.args };
      if (body.version !== undefined) payload.version = body.version;
      return request<VerifiedInvokeWire>("/v1/verified/invoke", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },

    // --- NE-167b Logfire-style trace view -----------------------------------

    trace(traceId: string) {
      return request<TraceAssemblyView>(`/v1/trace/${encodeURIComponent(traceId)}`, {
        method: "GET",
      });
    },

    // --- NE-168 governed delegation demo trigger ---------------------------

    delegate() {
      // Body-less: the three legs and their task texts are fixed SERVER-side
      // (deterministic, never model-chosen). The console supplies nothing.
      return request<DelegateRunView>("/v1/demo/delegate", {
        method: "POST",
        body: JSON.stringify({}),
      });
    },

    // --- NE-WORKFLOWS governed multi-step workflows ------------------------

    listWorkflows() {
      return request<WorkflowView[]>("/v1/workflows", { method: "GET" });
    },

    modelChoices(surface: ModelChoiceSurface) {
      return request<ModelChoiceCatalogView>(`/v1/models/choices/${encodeURIComponent(surface)}`, {
        method: "GET",
      });
    },

    runWorkflow(workflowId: string, maxSubjects: number) {
      // Body-less, like delegate(): every step, tool and subject is fixed
      // SERVER-side. The only thing the caller supplies is how many subjects to
      // sweep, and the server clamps that to its own ceiling.
      const params = new URLSearchParams({ max_subjects: String(maxSubjects) });
      return request<WorkflowRunView>(
        `/v1/workflows/${encodeURIComponent(workflowId)}/run?${params.toString()}`,
        { method: "POST", body: JSON.stringify({}) },
      );
    },

    runAgenticWorkflow(workflowId: string, requestedModel?: string) {
      return request<AgenticWorkflowRunView>(
        `/v1/agentic-workflows/${encodeURIComponent(workflowId)}/run`,
        {
          method: "POST",
          body: JSON.stringify(
            requestedModel === undefined ? {} : { requested_model: requestedModel },
          ),
        },
      );
    },

    startAgenticWorkflow(workflowId: string, requestedModel?: string) {
      return request<AgenticWorkflowRunStatusView>(
        `/v1/agentic-workflows/${encodeURIComponent(workflowId)}/start`,
        {
          method: "POST",
          body: JSON.stringify(
            requestedModel === undefined ? {} : { requested_model: requestedModel },
          ),
        },
      );
    },

    agenticWorkflowRun(runId: string) {
      return request<AgenticWorkflowRunStatusView>(
        `/v1/agentic-workflows/runs/${encodeURIComponent(runId)}`,
        { method: "GET" },
      );
    },
  };
}

export type { Citation };
