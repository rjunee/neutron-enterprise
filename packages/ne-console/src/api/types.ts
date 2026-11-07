// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Wire types for the governed backend HTTP API (ne-serve/ne-gateway). These
// MIRROR the frozen ne-contracts pydantic models (snake_case wire form) — see
// packages/ne-contracts/src/ne/contracts/types.py (Citation, ScoredChunk,
// RetrievalResult) and runtime.py (AgentRunResult). We do NOT modify the
// contracts; this file is the console-side typed reflection of them.
//
// SECURITY (C7): ChunkView deliberately OMITS the `acl` field present on the
// server-side Chunk. The console never receives, sees, or evaluates ACLs — the
// governed API trims results server-side and the client renders exactly what it
// is handed. There is intentionally no client-side ACL type or logic anywhere.

/** A pointer back to the source of record for an answer span. */
export interface Citation {
  chunk_id: string;
  document_id: string;
  source_system: string;
  source_ref: string;
  /** Server-side enrichment for click-through; absent when the API cannot
   * resolve a deep link. NEVER constructed client-side. */
  source_url?: string;
}

/** A retrievable unit of text with provenance — NO acl field (server trims). */
export interface ChunkView {
  chunk_id: string;
  document_id: string;
  text: string;
  metadata: Record<string, unknown>;
}

/** A chunk with a relevance score. */
export interface ScoredChunk {
  chunk: ChunkView;
  score: number;
}

/**
 * A Tier-2 graph entity pointer on the retrieval wire (mirrors
 * `ne.api.wire.GraphEntityRefView`). `assurance` (NE-T2X-4/§5.2) labels
 * WHETHER this entity is LLM-extracted-and-unconfirmed
 * (`isProvisionalAssurance`, `src/primitives/assurance.ts`) — required, no
 * default, the server's own anti-silent-drop stance.
 *
 * `source_ref` (NE-T2X-5-ENDPOINT-EVIDENCE-REF, ADR-0015) is this ENTITY's
 * own evidencing ref — required, no default, the SAME anti-silent-drop
 * stance: a fixture that omits it fails `tsc`, never silently renders the
 * EDGE's unrelated provenance for a provisional endpoint (the defect this
 * field closes).
 */
export interface GraphEntityRefWire {
  entity_class: string;
  entity_key: string;
  display: string;
  assurance: string;
  source_ref: string;
}

/** One traversed relation on the retrieval wire (mirrors
 * `ne.api.wire.GraphHopView`). `assurance` labels the ASSERTING relation
 * itself, independent of `subject.assurance`/`obj.assurance`. */
export interface RetrievalGraphHopWire {
  subject: GraphEntityRefWire;
  edge_kind: string;
  obj: GraphEntityRefWire;
  source_system: string;
  source_ref: string;
  assurance: string;
}

/** An ordered traversal path on the retrieval wire (mirrors
 * `ne.api.wire.GraphPathView`). */
export interface RetrievalGraphPathWire {
  hops: RetrievalGraphHopWire[];
}

/**
 * The result of a knowledge search: scored chunks plus citations plus Tier-2
 * graph paths.
 *
 * `graph_paths`/`provisional_source_count` (NE-T2X-4, design §6.1) are
 * REQUIRED, no default — mirroring the server's own no-default anti-
 * silent-drop stance (`ne.api.wire.RetrievalResultView`): the server has sent
 * both since NE-T2X-4, so a fixture that omits either fails `tsc` rather than
 * silently rendering zero graph facts.
 */
export interface RetrievalResult {
  chunks: ScoredChunk[];
  citations: Citation[];
  graph_paths: RetrievalGraphPathWire[];
  provisional_source_count: number;
  /**
   * 015A0-ENFORCEMENT's `AuthorizedRetrievalEnvelope`, when the server sends
   * one. OPTIONAL because it is additive on the wire — an older API omits the
   * key entirely, and the console must not manufacture a status for a server
   * that stated none. It NEVER computes one either: see
   * `search/results.ts::authorityLimitedLine`.
   */
  authorized_retrieval?: AuthorizedRetrievalEnvelopeWire;
}

/**
 * 015A0-ENFORCEMENT: whether the authorized result page is the WHOLE answer.
 *
 * `complete` — the requested authorized top-k was filled, or the backend end
 * was proven. Only an empty `complete` page is an ordinary no-result.
 * `partial` — at least one authorized result was returned, but the scan/cursor
 * bound fired before top-k or backend end.
 * `exhausted` — ZERO authorized results were returned and that bound fired
 * before backend end could be proven. It means authority-limited scan
 * exhaustion, NOT proven corpus emptiness.
 */
export type AuthorizedRetrievalStatusWire = "complete" | "partial" | "exhausted";

/**
 * The server's bounded scan accounting for one authorized retrieval.
 *
 * Every field is the SERVER's. The console preserves the status and the three
 * counts exactly as sent; it never recomputes a status from `chunks.length`,
 * never narrows `partial` to `complete`, and never derives a corpus total from
 * these bounded operational counts.
 */
export interface AuthorizedRetrievalEnvelopeWire {
  status: AuthorizedRetrievalStatusWire;
  scanned_count: number;
  returned_count: number;
  scan_limit: number;
}

/** A live agent chat session bound to a principal. */
export const DEFAULT_ASK_AGENT_ID = "agt_01k3meridiananalyst000000000";

export interface ChatSession {
  session_id: string;
  /** The exact persisted Agent identity. During canonical Ask activation this
   * is the configured opaque Agent id, not the retired `console-chat` adapter. */
  agent_name: string;
}

/** Opaque server mapping for the optional post-generation grounding mode. */
export interface ChatCapabilitiesView {
  grounded: { agent_name: string } | null;
}

/**
 * Structural provenance attached to a deterministic verified-answer result.
 * Mirrors `ne.contracts.runtime.VerifiedProvenance` exactly; none of these
 * fields is inferred from answer text in the browser.
 */
export interface VerifiedProvenanceWire {
  template_id: string;
  template_version: number;
  config_hash: string;
  row_count: number;
  truncated: boolean;
}

/**
 * Structural outcome of the server's grounding gate. `fallback` and
 * `assist_used` are the gate's own verdict bits, not client-side policy.
 */
export interface GroundedProvenanceWire {
  fetches: VerifiedProvenanceWire[];
  citation_count: number;
  gate: string;
  fallback: boolean;
  assist_used: boolean;
  prompt_echo_count: number;
  strictness: string;
}

/** The authenticated principal, as returned by GET /api/v1/me. */
export interface PrincipalView {
  id: string;
  groups: string[];
  /**
   * The SERVER'S OWN answer to "which surfaces would you refuse this key?" —
   * coarse names ("approvals", "steward", "ops") computed from the same
   * fail-closed role tuples every guarded route checks. Optional because an
   * older deployment does not send it; absent means "unknown", and the console
   * then behaves exactly as it did before (ask, and render the refusal).
   *
   * It authorizes NOTHING client-side. It is used only to (a) skip a governed
   * read this key is already told will be refused — the console's own polling
   * was writing ~100 audited denials per screen and inflating the KPI window —
   * and (b) mark a nav destination as locked instead of walking someone into a
   * full page of refusal prose.
   */
  entitlements?: string[];
}

// ===========================================================================
// PRE-012 — principal-owned Agent / Conversation / canonical Run wire types
//
// These mirror ne-api's closed PRE-012 wire projections field-for-field.
// ===========================================================================

export type AgentExecutionStrategyWire = "agentic" | "deterministic";
export type AgentLifecycleWire = "active" | "deprecated" | "retired";
export type AgentCapabilityHealthWire = "healthy" | "unhealthy" | "unknown";

export interface AgentBudgetsWire {
  max_steps: number;
  max_tool_calls: number;
  max_input_tokens: number;
  max_output_tokens: number;
}

export interface AgentKnowledgeGrantWire {
  source_id: string;
  tiers: ("t1" | "t2" | "t3")[];
}

export interface AgentSkillGrantWire {
  skill_id: string;
  version: number;
  content_hash: string;
}

export interface AgentWorkflowBindingWire {
  profile_id: string;
  version: number;
  config_hash: string;
  max_subjects: number;
  frozen_inputs: {
    counties: string[];
    payment_window_start: string;
    payment_window_end: string;
  };
}

/** The ceilings an agentic Agent's Runs are ACTUALLY measured against, per
 * attempt — served by ne-api's one derivation (agent_execution_caps.py) rather
 * than re-derived here, because a second copy of that arithmetic in TypeScript
 * is exactly how a displayed cap and an enforced cap come to disagree.
 *
 * `budgets` above stays as the operator DECLARED it. These are what the runtime
 * makes of that declaration, and they are what the console is allowed to print
 * under the word "Caps". `null` on a deterministic Agent: no bounded loop runs,
 * so there is no ceiling to show, and a row of zeroes would be a claim. */
export interface AgentEnforcedCapsWire {
  max_tool_calls: number;
  max_model_requests: number;
  wall_clock_seconds: number;
  max_total_tokens: number;
  max_parked_writes: number;
}

export interface AgentCatalogItemWire {
  agent_id: string;
  version: number;
  label: string;
  description: string;
  owner_label: string;
  lifecycle: AgentLifecycleWire;
  allowed_strategies: AgentExecutionStrategyWire[];
  default_strategy: AgentExecutionStrategyWire;
  model_policy_id: string;
  tool_grants: string[];
  knowledge_sources: AgentKnowledgeGrantWire[];
  skills: AgentSkillGrantWire[];
  budgets: AgentBudgetsWire;
  enforced_caps: AgentEnforcedCapsWire | null;
  workflow: AgentWorkflowBindingWire | null;
  available: boolean;
  availability_reasons: string[];
  /** The exact Agent definition hash.  This is not the platform config hash. */
  config_hash: string;
}

export interface AgentCatalogPageWire {
  items: AgentCatalogItemWire[];
  next_cursor: string | null;
}

/** One advertised Tool argument — sanitized schema metadata, never a value.
 * Mirrors ne-api's AgentToolArgumentItem. `null` = the schema does not state it. */
export interface AgentToolArgumentWire {
  name: string;
  type_label: string | null;
  required: boolean;
  description: string | null;
  enum: string[] | null;
  pattern: string | null;
  max_length: number | null;
  list_max: number | null;
}

export interface AgentToolCapabilityWire {
  name: string;
  label: string;
  read_write_class: "read" | "write";
  declared_tier: number;
  effective_tier: number;
  connection_identity_label: string;
  health: AgentCapabilityHealthWire;
  /** The tool's model-facing description; "" when the spec carries none. */
  description: string;
  /** `null` = the schema stated no argument surface (unknowable); `[]` = the
   * tool states it takes no arguments. Never collapse the two. */
  arguments: AgentToolArgumentWire[] | null;
  /** Declared answer columns (verified templates); `null` = not stated. */
  answer_columns: string[] | null;
}

export interface AgentConnectorCapabilityWire {
  connection_identity_label: string;
  label: string;
  kind: string;
  credential_owner_label: string;
  health: AgentCapabilityHealthWire;
  source_id: string;
  tiers: ("t1" | "t2" | "t3")[];
}

export interface AgentCapabilitySetWire {
  agent_version: number;
  agent_definition_hash: string;
  tools: AgentToolCapabilityWire[];
  connectors: AgentConnectorCapabilityWire[];
  capability_hash: string;
}

/** One pinned Skill this principal may see, with its authored procedure.
 * Mirrors ne-api's AgentSkillDetailItem. Skill-author text + manifest pins
 * only — never customer data. `body` is the authored procedure Markdown. */
export interface AgentSkillDetailWire {
  skill_id: string;
  version: number;
  content_hash: string;
  description: string;
  requires_tools: string[];
  acl_labels: string[];
  triggers: string[];
  category: string;
  body: string;
}

export interface AgentCapabilityPreflightWire {
  agent_id: string;
  version: number;
  definition_hash: string;
  available: boolean;
  availability_reasons: string[];
  /** null means the producer could not answer; it never means an empty grant. */
  capabilities: AgentCapabilitySetWire | null;
  /** Additive pinned-Skill detail, principal-scoped and never in the capability
   * hash. `null` = no Skill detail producer composed; `[]` = none visible. */
  skills: AgentSkillDetailWire[] | null;
  model: AgentModelPreflightWire;
}

export interface AgentModelCandidateWire {
  model: string;
  provider: string;
  lane: string;
  evidence: string;
  observed_at: string;
}

export interface AgentModelPreflightWire {
  task: string;
  allowed_strategies: AgentExecutionStrategyWire[];
  default_strategy: AgentExecutionStrategyWire;
  max_steps: number;
  max_tool_calls: number;
  max_input_tokens: number;
  max_output_tokens: number;
  candidates: AgentModelCandidateWire[];
  default_model: string | null;
}

export interface AgentWorkflowStepWire {
  ordinal: number;
  step_id: string;
  kind: "governed-tool" | "deterministic-analysis" | "model-judgment";
  action: string;
  model_role: string | null;
}

export interface AgentWorkflowProfileWire {
  agent_id: string;
  agent_version: number;
  agent_config_hash: string;
  strategy: "deterministic";
  workflow_id: string;
  workflow_version: number;
  workflow_config_hash: string;
  input_schema: Record<string, unknown>;
  fixed_inputs: Record<string, unknown>;
  fixed_plan: AgentWorkflowStepWire[];
}

export interface AgentConversationWire {
  conversation_id: string;
  agent_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_sequence: number;
}

export interface AgentConversationPageWire {
  items: AgentConversationWire[];
  next_cursor: string | null;
}

export type AgentRunStatusWire =
  | "submitted"
  | "queued"
  | "running"
  | "waiting_approval"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired"
  | "context_exhausted"
  | "budget_exhausted"
  | "unavailable";

export interface AgentRunWire {
  run_id: string;
  conversation_id: string;
  agent_id: string;
  agent_version: number;
  sequence: number;
  version: number;
  predecessor_run_id: string | null;
  root_run_id: string;
  retry_ordinal: number;
  display_label: string;
  strategy: AgentExecutionStrategyWire | "unavailable";
  status: AgentRunStatusWire;
  output_verdict: "verified" | "grounded" | "refused" | "unavailable" | null;
  model_policy_id: string | null;
  model_request_mode: "auto" | "explicit" | "unavailable";
  context_snapshot_hash: string | null;
  requested_model: string | null;
  requested_model_lane: string | null;
  served_model: string | null;
  /**
   * WHY `served_model` is that model, which the two names above cannot say.
   *
   * `"primary"` — nothing was attempted before the model that served. A
   * POSITIVE claim, which is what lets the receipt say "served as requested"
   * from the record rather than reading a null as reassurance.
   * `"fallback"` — something was attempted first and did not serve, i.e. an
   * INVOLUNTARY PROVIDER FALLBACK. `fallback_from` names what was passed over.
   * `"unavailable"` — the Run recorded no route position. Not "primary".
   *
   * NOT a `model.changed`: that is a deliberate policy change with an actor.
   * This is the provider chain moving under an unchanged policy.
   */
  route_position: "primary" | "fallback" | "unavailable";
  /** The candidates passed over, comma-separated, in attempt order. Declared
   * model NAMES only — the server never projects the provider's error text
   * here, and re-checks the name alphabet before serving it. */
  fallback_from: string | null;
  output_preview: string | null;
  config_hash: string;
  capability_snapshot_hash: string;
  created_at: string;
  updated_at: string;
  terminal_at: string | null;
}

/** Principal-safe detail fields served by GET on one canonical Run. */
export interface AgentRunDetailWire extends AgentRunWire {
  /**
   * Whether `output_preview` above is the answer, is a withheld answer, or
   * could not be resolved at all. DETAIL-ONLY: resolving it for a
   * source-derived answer costs one source-authority reauthorization per Run,
   * which the 25-row list pages must not spend, so `AgentRunWire` carries no
   * such field and a list row's null preview stays the weaker fact it is.
   *
   * `withheld` is the state a null preview could never express: the answer WAS
   * produced and current access to a source behind it changed.
   */
  output_state: "available" | "withheld" | "unavailable";
  approval_id: string | null;
  /**
   * WHY A FAILED RUN FAILED, in the server's own words — or `null`.
   *
   * A terminal non-completed Run used to carry only `failure_class`, an
   * OpaqueId this console never referenced, so a stated governance refusal and
   * an arbitrary crash reached the reader as the same generic line. This is the
   * OPT-IN safe reason the raiser marked publishable (ne-api
   * `run_projection._failure_reason`); the exception class is deliberately not
   * served.
   *
   * `null` means the Run published no marked reason. It renders as the status
   * word alone — never as a sentence this console wrote on the server's behalf.
   */
  failure_reason: string | null;
  agentic_plan: AgenticPlanStepWire[] | null;
  execution_profile_receipt: RunExecutionProfileReceiptWire | null;
  skill_activation_receipt: RunSkillActivationReceiptWire | null;
  claims: RunClaimWire[];
  claim_mapping_state: "mapping_pending" | "complete" | "partial" | "unavailable";
  claims_truncated: boolean;
}

/** A server-projected, content-safe plan step from the exact Run attempt. */
export interface AgenticPlanStepWire {
  ordinal: number;
  tool: string;
  step_intent: string;
  /**
   * The correlation id the Run's receipt gate minted for THIS step's governed
   * tool call — the same value the step's `run.tool.pre_effect` event carries
   * in its `tool_call_id` metadata. It is what lets an event row be joined to
   * the plan row that names the tool and its intent. `null` means the server
   * recorded no binding for the step, and the join simply does not happen —
   * never a positional guess.
   */
  tool_call_id: string | null;
}

/** Frozen deterministic profile and owner-visible input from one exact Run attempt. */
export interface RunExecutionProfileReceiptWire {
  profile_receipt_id: string;
  run_id: string;
  attempt_id: string;
  workflow_id: string;
  profile_version: number;
  profile_hash: string;
  input_schema: Record<string, unknown>;
  input_schema_hash: string;
  typed_input_state: "available" | "unavailable";
  typed_input: Record<string, unknown> | null;
  ordered_plan: AgentWorkflowStepWire[];
  ordered_plan_hash: string;
  receipt_hash: string;
  created_at: string;
}

export interface RunSkillPinWire {
  skill_id: string;
  version: number;
  content_hash: string;
}

/** Content-free Skill activation frozen for one exact Run attempt. */
export interface RunSkillActivationReceiptWire {
  activation_receipt_id: string;
  run_id: string;
  attempt_id: string;
  agent_id: string;
  agent_version: number;
  surface_key: string;
  pins: RunSkillPinWire[];
  index_hash: string;
  surface_hash: string;
  receipt_hash: string;
  created_at: string;
}

export interface RunClaimWire {
  claim_id: string;
  claim_verdict: "verified" | "grounded" | "unavailable";
  content_state: "available" | "content_withheld" | "content_unavailable";
  display: string | null;
  served_output_hash: string;
  claim_content_hash: string;
  association_status: "associated" | "association_unavailable";
  association_hash: string | null;
  evidence_id: string | null;
  producing_event_id: string | null;
  locator_status: "located" | "evidence_locator_unavailable" | "association_unavailable";
  locator_hash: string | null;
}

export interface AgentRunEventMetadataWire {
  key:
    | "claim_count"
    // POST_DEMO item 4: HOW a completed run reached its terminal —
    // "execution_complete" for one that finished its plan,
    // "approved_effect_complete" for one that resumed and executed the
    // approved effect without replaying the rest.
    | "outcome"
    | "actor"
    | "safe_boundary"
    | "decision"
    | "delivery_kind"
    | "model_policy_id"
    | "request_mode"
    | "requested_model"
    | "served_model"
    | "served_lane"
    | "authorization_receipt_hash"
    | "before_requested_model"
    | "before_served_model"
    | "after_requested_model"
    | "after_authorized_model"
    | "after_authorized_lane"
    | "context_snapshot_hash"
    | "context_fit"
    | "estimated_input_tokens"
    | "target_input_budget"
    | "compaction"
    | "cache_impact"
    | "before_config_hash"
    | "config_hash"
    | "protected_hash"
    | "stamp_set_hash"
    | "terminal_intent_hash"
    | "turn_id"
    // The Run-to-audit join (ne-api run_projection `_audit_metadata`): the
    // readable action NAME already committed to the governed audit chain, the
    // server's own decision class, and the tool call's correlation id. Names
    // and codes only — the server drops any action or class it did not
    // enumerate, so an absent key here means the server declined to name it.
    | "audit_action"
    | "decision_kind"
    | "tool_call_id"
    // The NAME of the governed act, from the chain's own pre-effect record:
    // the registered tool a `run.tool.pre_effect` receipt named. Served only
    // when the server could validate it as a tool identifier.
    | "tool_name"
    // WHAT A RETRIEVAL PRODUCED, as class and count only (ne-api
    // run_projection `_event_metadata` for `evidence.produced` /
    // `evidence.verified_row`): the read's kind, its locator status, its
    // assurance class, and how many contributor stamps supported the result.
    // The retrieved text never has a key here.
    | "read_kind"
    | "evidence_status"
    | "evidence_assurance"
    | "contributor_count";
  value: string | number | boolean;
}

export interface AgentRunEventWire {
  event_id: string;
  run_id: string;
  attempt_id: string;
  sequence: number;
  event_kind:
    | "claim.verifier"
    | "delivery.decided"
    | "evidence.produced"
    | "evidence.verified_row"
    | "model.changed"
    | "model.served"
    | "run.audit.intent"
    | "run.cancel.accepted"
    | "run.execution.claimed"
    | "run.execution.recovered"
    | "run.retry.accepted"
    | "run.start.accepted"
    | "run.terminal"
    | "event.unavailable";
  event_status:
    | AgentRunStatusWire
    | "preterminal"
    | "changed"
    | "served"
    | "withheld"
    | "receipt_pending"
    | "retry"
    | "uncertain"
    | "verified";
  parent_event_id: string | null;
  step_id: string | null;
  metadata: AgentRunEventMetadataWire[];
  detail_hash: string;
  created_at: string;
}

export interface AgentRunEventPageWire {
  items: AgentRunEventWire[];
  next_cursor: string | null;
}

export interface AgentEvidenceEnvelopeWire {
  schema_version: 1;
  agent_id: string;
  run_id: string;
  run_status: AgentRunStatusWire;
  mapping_state: "mapping_pending" | "complete" | "partial" | "unavailable";
  mapping_message: string;
  output: {
    verdict: "verified" | "grounded" | "refused" | "unavailable" | null;
    state: "available" | "withheld" | "unavailable";
    message: string;
    display: string | null;
  };
  claims: Array<{
    claim_id: string;
    claim_verdict: "verified" | "grounded" | "unavailable";
    state: "available" | "withheld" | "unavailable";
    message: string;
    display: string | null;
    evidence_id: string | null;
    association_hash: string | null;
    verifier_event_id: string | null;
    verifier_receipt_hash: string | null;
  }>;
  evidence: Array<{
    evidence_id: string;
    state: "available" | "withheld" | "locator_unavailable";
    message: string;
    assurance: "direct" | "inferred" | "extracted" | "unavailable";
    locator: AgentEvidenceLocatorWire | null;
  }>;
  governance: {
    status: "complete" | "partial" | "unavailable";
    delegated_user_label: string | null;
    agent_service_identity: string | null;
    connector_credential_owner_label: string | null;
    effective_scopes: string[];
    policy_environment: string | null;
    tool_tier: number | null;
    tool_decision: "allow" | "deny" | "approval_required" | null;
    unavailable_reasons: string[];
  };
}

/**
 * WHERE ONE EVIDENCE RECORD CAME FROM, as the server projects it.
 *
 * The locator arms are MUTUALLY EXCLUSIVE server-side (evidence_wire.py
 * `EvidenceLocatorView`): `kind: "verified_t3"` carries only `row_anchor` +
 * `receipt_hash`; `"verified_t3_empty"` carries only `receipt_hash`; and the
 * generic arm (`kind` absent) is the only one that can name a chunk, a page or
 * a title. Every field is therefore optional here, and a reader must branch on
 * what is actually present rather than on what it hoped for.
 *
 * `chunk_id` IS WHY THIS TYPE EXISTS. The server has always sent it; this
 * interface used to declare only `entity_id` and `graph_path`, so the one
 * identifier that resolves an evidence record back into the knowledge store was
 * invisible to TypeScript and Ask could not cite anything. Widening the mirror
 * is a type-file change only — no server field was added.
 */
export interface AgentEvidenceLocatorWire {
  kind?: "verified_t3" | "verified_t3_empty" | "knowledge_search" | null;
  row_anchor?: string | null;
  receipt_hash?: string | null;
  source_system?: string | null;
  title?: string | null;
  source_revision?: string | null;
  as_of?: string | null;
  content_hash?: string | null;
  /** The retrievable chunk. Together with `chunk_ids`, the only fields on this
   * wire that `retrieve()` can turn back into a `Citation`; absent on both
   * `verified_t3` arms. */
  chunk_id?: string | null;
  /** The `knowledge_search` arm: every chunk a search read served, in the
   * served order. These carry the SAME resolvable ids as `chunk_id` — a search
   * read rests on several passages, and citing only one of them would be an
   * invented selection the server never made. */
  chunk_ids?: string[] | null;
  /** The exact document versions this read's retained contributor stamps were
   * served from — resolved server-side through each stamp's immutable content
   * and ACL snapshot identity, never inferred from the corpus's CURRENT state,
   * so a later supersession cannot re-attribute a historical answer. Carried by
   * all three source-bearing arms: `knowledge_search`, the graph-path arm and
   * `verified_t3`. Empty means the server holds no binding; render absence,
   * never a guessed version. */
  served_document_versions?: AgentEvidenceServedDocumentVersionWire[];
  page?: number | null;
  row_id?: string | null;
  entity_id?: string | null;
  graph_path?: AgentEvidenceGraphHopWire[];
}

/** One `(document, version)` a knowledge-search read served, receipt-bound. */
export interface AgentEvidenceServedDocumentVersionWire {
  document_id: string;
  version_seq: number;
  content_hash: string;
  display_name: string;
}

export interface AgentEvidenceGraphEntityWire {
  entity_class: string;
  entity_key: string;
  display: string;
  assurance: "source" | "confirmed" | "extracted";
  source_ref: string;
}

/** One independently authorized canonical Graph hop from the evidence projection wire. */
export interface AgentEvidenceGraphHopWire {
  subject: AgentEvidenceGraphEntityWire;
  edge_kind: string;
  obj: AgentEvidenceGraphEntityWire;
  source_system: string;
  source_ref: string;
  assurance: "source" | "confirmed" | "extracted";
  review_state: "accepted" | "pending" | "rejected" | "not_applicable" | null;
}

/** Exact optimistic body accepted by canonical cancel/retry endpoints. */
export interface ControlAgentRunRequestWire {
  expected_version: number;
  agent_version: number;
  strategy: AgentExecutionStrategyWire;
  config_hash: string;
  capability_snapshot_hash: string;
}

export interface AgentConversationTurnWire {
  turn_id: string;
  turn_sequence: number;
  user_content_state: "available" | "unavailable";
  user_content: string | null;
  run: AgentRunWire;
}

export interface AgentConversationTurnPageWire {
  items: AgentConversationTurnWire[];
  next_cursor: string | null;
}

export type AgentRunUpdateViewWire = "attention" | "recent";
export interface AgentRunUpdateWire {
  run_id: string;
  agent_id: string;
  conversation_id: string;
  agent_label: string;
  conversation_title: string;
  status: "waiting_approval" | "failed" | "context_exhausted" | "budget_exhausted" | "completed";
  reason:
    | "Approval required"
    | "Run failed"
    | "Context limit exhausted"
    | "Budget limit exhausted"
    | "Run completed";
  updated_at: string;
}
export interface AgentRunUpdatePageWire {
  view: AgentRunUpdateViewWire;
  window_start: string;
  window_end: string;
  items: AgentRunUpdateWire[];
  next_cursor: string | null;
}

export interface AgentRunPageWire {
  items: AgentRunWire[];
  next_cursor: string | null;
}

/** Exact POST /v1/agents/{agent_id}/runs body. Identity stays bearer-derived. */
export interface StartAgentRunRequestWire {
  conversation_id: string;
  expected_version: number;
  agent_version: number;
  strategy: AgentExecutionStrategyWire;
  /** Frozen platform configuration hash from GET /v1/posture. */
  config_hash: string;
  capability_snapshot_hash: string;
  input: string;
  requested_model: string | null;
  requested_model_lane: string | null;
}

export interface AcceptedAgentRunSubmissionWire {
  action: "start" | "cancel" | "retry";
  run_id: string;
  attempt_id: string;
  version: number;
  status: "submitted" | "queued" | "cancelling";
  enqueue_id: string;
  replayed: boolean;
}

export type PrincipalReferenceScope = "approvals" | "audit";

export interface ResolvedPrincipalView {
  id: string;
  display_name: string;
  removed: boolean;
}

export interface ResolvedPrincipalsView {
  resolved: ResolvedPrincipalView[];
}

/**
 * The server-sent-event union streamed from the agent chat endpoint. Each SSE
 * frame's `data:` payload is a JSON object of one of these shapes, routed by
 * the SSE `event:` field (falling back to the payload's `type`).
 */
export type StreamEvent =
  | { type: "token"; delta: string }
  | { type: "citations"; citations: Citation[] }
  | ToolTransitEvent
  | {
      /**
       * Terminal handoff from default Ask into one admitted canonical Agent Run.
       * This settles the browser stream, not the Run: it deliberately carries no
       * output and makes no completion claim.
       */
      type: "accepted";
      agent_id: string;
      conversation_id: string;
      run_id: string;
      attempt_id: string;
      status: "submitted" | "queued";
    }
  | {
      type: "done";
      session_id: string;
      output: string;
      /** Opaque server-owned attachment identity for correction signals. */
      message_id?: string;
      /** Present only on the deterministic verified-router path. */
      verified?: VerifiedProvenanceWire;
      /** Present only when the grounding post-step actually ran. */
      grounded?: GroundedProvenanceWire;
      /**
       * Optional server correlation for the run and its exact gate event.
       * Older/current deployments may omit either additive field; the Ask
       * surface then renders a non-link chip instead of guessing from the
       * unrelated chat session id.
       */
      trace_id?: string;
      gate_event_id?: string;
    }
  | { type: "error"; detail: string };

/**
 * NE-137 additive SSE event: one governed tool call transiting the C6
 * GovernedToolExecutor. The `decision`/`tier`/`audit_seq` are SERVER-derived from
 * the REAL PDP/gate/executor audit events — the console renders them, never decides
 * them (C6). `tier`/`audit_seq` are `null` when the server could not resolve one
 * (rendered honestly as "—", never invented). `approval_id`, when present, is
 * the REAL DurableApprovalGate request id from THIS transition's audit
 * provenance: the pending request, the execution that consumed it, or its
 * terminal status or failed approved execution. It remains absent on ungated
 * allow/error and PDP/validation deny. Approval-terminal values are server-owned:
 * the console must never collapse expiry/escalation/integrity failure into a
 * human rejection.
 *
 * `reason` is the POLICY BUNDLE'S OWN sentence about the decision, served verbatim
 * off the audit record the PDP already wrote ("tier 2: external/PII action — per-item
 * human approval required"). Only the transit the server could read one from carries
 * it — today the PDP deny — so it is absent on every other decision, and the console
 * renders its existing "the server sent no reason" there. It is rendered VERBATIM and
 * never rewritten, prefixed or substituted: this is the one slot reserved for the
 * server's words about a governance verdict.
 */
export interface ToolTransitEvent {
  type: "tool";
  name: string;
  tier: number | null;
  decision:
    | "allow"
    | "deny"
    | "pending"
    | "rejected"
    | "expired"
    | "escalated"
    | "integrity-failed"
    | "approval-failed"
    | "failed";
  approval_id?: string;
  audit_seq: number | null;
  reason?: string | null;
}

// ===========================================================================
// NE-062 ops surfaces — wire types
//
// These MIRROR the merged backend models 1:1 in snake_case (datetimes are
// ISO-8601 UTC strings on the wire):
//   - approvals: ne-runtime approvals.PendingApproval (the read-model the
//     DurableApprovalGate exposes to the console queue)
//   - audit:     ne-audit store.AuditRecord, verify.ChainVerification,
//     replay.ReplayResult
//   - eval:      ne-eval store.EvalRunRecord
//   - kpi:       the ne-contracts KpiRecorder vocabulary (SCAFFOLD — NE-068
//     U-P-B reporting is not yet built)
// ===========================================================================

/**
 * A read-only view of a pending Tier-2/3 HITL approval — mirrors ne-runtime
 * approvals.PendingApproval. The LISTING stays deliberately REDACTED: it
 * carries NO payload/target (NE-035 keeps the queue read-model payload-free —
 * that redaction is a real, unweakened security property). In the UI,
 * "target" = action + request_id for this list surface. All timestamps are
 * server-authoritative; the console only does render-only arithmetic on them.
 *
 * Per-request disclosure (owner ruling 2026-07-31, NE-APPROVAL-PAYLOAD): an
 * approver who cannot see what they are approving is not approving anything,
 * so the NAMED approver(s) for a specific request may fetch
 * `GET /v1/approvals/{request_id}/payload` (see `ApprovalPayloadView`) — a
 * separate, governed, audited, per-request read. This list type is
 * deliberately UNCHANGED by that ruling.
 */
export interface PendingApprovalView {
  request_id: string;
  action: string;
  tier: number;
  principal_id: string;
  requested_at: string;
  sla_deadline: string;
  expires_at: string;
  escalated: boolean;
  /**
   * NE-T2X-5 (design §6.3): how many unconfirmed, LLM-extracted facts were in
   * this action's context when it escalated. OPTIONAL — LIVE-PARKABLE: the
   * ne-runtime read-model (`ne.runtime.approvals.PendingApproval`) carries
   * this today, but ne-api's `pending_approval_view` mapper (out of this
   * unit's lanes) does not yet thread it onto the wire — a one-line
   * follow-up. Undefined/absent renders no provisional placard.
   */
  provisional_source_count?: number;
  /** The opaque `llm:<sha16>:<ref>` evidence refs — see
   * `provisional_source_count`'s LIVE-PARKABLE note. */
  provisional_source_refs?: string[];
}

/**
 * GET /v1/approvals/{request_id}/payload response (owner ruling 2026-07-31,
 * NE-APPROVAL-PAYLOAD) — mirrors ne-runtime `ApprovalPayloadDisclosure` /
 * ne-api `ApprovalPayloadView` 1:1. Served ONLY to the named approver(s) for
 * THIS request: for Tier-3, membership in the configured
 * `tier3_named_approvers` list (both dual-flow approvers qualify); for Tier<3,
 * the server's `approver_roles` pre-check IS the named-decider gate. Every
 * other approver-role holder gets a 403 (the client surfaces `ApiError.detail`
 * verbatim — see `ApprovalsPage`'s "PAYLOAD WITHHELD" placard) whose body
 * carries NONE of these fields. `material_parameters` is `null` for a legacy
 * row that predates payload recording, never an empty object.
 */
export interface ApprovalRunContextView {
  approval_id: string;
  agent_id: string;
  run_id: string;
}

export interface ApprovalPayloadView {
  request_id: string;
  action: string;
  tier: number;
  principal_id: string;
  target_resource: string;
  description: string;
  material_parameters: Record<string, unknown> | null;
  reversibility: string;
  /** Present on an ENFORCEMENT-era server; absent on the pre-ENFORCEMENT wire.
   * A payload body carrying any OTHER status is not an available payload — see
   * `ApprovalPayloadDisclosureView`. */
  disclosure_status?: "available";
  /** Content-free canonical-Run shell, present only for an exact governed Run approval. */
  run_context?: ApprovalRunContextView;
}

/**
 * 015A0-ENFORCEMENT's shared disclosure vocabulary, identical on every
 * source-bearing surface.
 *
 * `available` — the server resolved this row under the caller's CURRENT
 * authority and is disclosing it.
 * `content_withheld` — it resolved it and will not disclose it. Permanent.
 * `retryable_unknown` — it could not resolve it (outage, contract drift). Not a
 * verdict about the caller, and it may become determinate later.
 *
 * A row that states NO status states no disclosure decision, and a row that
 * states an unrecognised one states a decision this console cannot read. Both
 * fail closed to a content-free, non-actionable shell — see
 * `client.ts::sourceDisclosure`, the one place any of this is decided.
 */
export type SourceDisclosureStatusWire = "available" | "content_withheld" | "retryable_unknown";

/**
 * 015A0-ENFORCEMENT: the safe shell the gate returns INSTEAD of a payload when
 * the request's source authority is no longer current.
 *
 * `content_withheld` is the permanent decision: the source-derived target,
 * parameters and consequence are gone and no decision control may exist.
 * `retryable_unknown` is an unresolved/outage/contract-drift decision: equally
 * non-actionable, equally content-free, and distinct because it is not a
 * verdict about this approver's access.
 *
 * The shell carries the request id and NOTHING derived from a source: no
 * target, no parameter, no description, no reversibility.
 */
export interface WithheldApprovalPayloadView {
  request_id: string;
  disclosure_status: "content_withheld" | "retryable_unknown";
}

/**
 * What `GET /v1/approvals/{request_id}/payload` may answer with, as a
 * status-discriminated union (SPEC 013C: `available|content_withheld|
 * retryable_unknown`).
 *
 * The console reads the discriminant the SERVER wrote. It never infers a
 * disclosure decision from which fields happen to be present, and it never
 * upgrades an unrecognised status to `available`.
 */
export type ApprovalPayloadDisclosureView = ApprovalPayloadView | WithheldApprovalPayloadView;

/**
 * The result of submitting an approve/deny action onto the gate. `status`
 * mirrors ne-runtime's ApprovalStatus plus `pending_second`: a Tier-3 first
 * approval where the gate returns None (awaiting the distinct second approver).
 * `approver` is the SERVER-derived identity (the verified OIDC principal) — the
 * console never supplies it.
 */
export interface ApprovalActionResult {
  status: "approved" | "rejected" | "expired" | "pending_second";
  approver?: string | null;
  decided_at?: string | null;
}

/**
 * One audit-chain row — mirrors ne-audit store.AuditRecord. The hash fields
 * (config_hash/prev_hash/record_hash) are DISPLAY-ONLY strings: the client
 * NEVER computes, compares, or verifies them. Chain integrity is a server
 * computation surfaced via ChainVerificationView.
 */
export interface AuditRecordView {
  id: number;
  seq: number;
  event_id: string;
  event_type: string;
  config_hash: string;
  actor: string;
  occurred_at: string;
  kpi_tag: string | null;
  trace_id: string | null;
  action: string | null;
  resource: string | null;
  outcome: string | null;
  payload: Record<string, unknown>;
  prev_hash: string;
  record_hash: string;
}

/**
 * WHICH PROOF PRODUCED A VERDICT — mirrors `ne.api.wire.WireVerificationBasis`.
 * Three DIFFERENT claims, and rendering one as another is the defect this type
 * exists to make impossible:
 *
 *   * `"full-chain"` — a real unbounded `verify_chain` walked the whole ledger
 *     on that request. The strongest verdict the server emits, and the ONLY one
 *     a screen may render as "the entire chain was verified".
 *   * `"anchored-extension"` — only the rows appended since a head a full
 *     verify blessed (up to CHAIN_ANCHOR_MAX_AGE_S ago) were re-hashed. The
 *     verdict COVERS the whole chain; the work on that call did not, and the
 *     prefix was not looked at.
 *   * `"windowed"` — the walk was bounded to a `from`/`to` window, so a row
 *     tampered outside it is invisible to the verdict by construction. Not a
 *     whole-chain reading at all.
 *
 * It is also an ORDERING (`audit/chainVerdict.ts`): a verdict may not withdraw
 * a standing break that a stronger proof confirmed.
 */
export type VerificationBasis = "full-chain" | "anchored-extension" | "windowed";

/**
 * The SERVER-computed verdict of walking the audit chain — mirrors
 * `ne.api.wire.ChainVerificationView`. The console renders it verbatim and
 * never recomputes or overrides it.
 *
 * `valid` ALONE IS NOT THE VERDICT. `basis` names the proof that produced it
 * and `contradiction` says whether a refusal is one the read-during-append race
 * could have manufactured. Both were missing from this shape while the server
 * had already stopped walking the whole chain on every page read, and the
 * console's verdict pipeline was written on the assumption every verdict is a
 * repeatable full walk — which is what let it print an anchored count under
 * "Entire audit chain … verified", let an anchored pass withdraw a break a full
 * walk had confirmed, and made the server's strongest signal unlatchable.
 */
export interface ChainVerificationView {
  valid: boolean;
  /** Which proof ran — see `VerificationBasis`. Decides what a screen may SAY
   * about this verdict, and what may withdraw a break another verdict latched. */
  basis: VerificationBasis;
  /** This `valid: false` is a refusal the read-during-append race cannot
   * produce AND will not repeat: the server proved the ledger no longer extends
   * a head IT had verified, then re-anchored, so the next read passes. The
   * console's flap suppression (`BREAK_CONFIRMATIONS`) would otherwise discard
   * the strongest signal the server has, every time, by construction. Mirrors
   * `ne.audit.AnchoredVerification.contradiction`. */
  contradiction: boolean;
  checked: number;
  first_break_seq: number | null;
  first_break_event_id: string | null;
  reason: string | null;
  detail: string;
}

/**
 * The SERVER's verdict on an anchored (`anchor_seq`) read — whether the named
 * entry was reachable, and where the chain currently ends. The console renders
 * this verdict verbatim (C7): it never decides reachability by counting rows.
 */
export interface AuditAnchorView {
  seq: number;
  status: "found" | "filtered_out" | "not_found";
  head_seq: number;
}

/** The audit list endpoint returns the server-computed verification WITH every
 * page of events, so the verdict travels with the data it describes. `anchor`
 * is present exactly when the request was anchored (`anchor_seq`); an
 * unanchored read never fabricates an anchor verdict. */
export interface AuditEventsPage {
  events: AuditRecordView[];
  verification: ChainVerificationView;
  anchor?: AuditAnchorView | null;
}

/** The provenance of a decision — mirrors ne-audit replay.ReplayResult. */
export interface ReplayResultView {
  events: AuditRecordView[];
  config_hashes: string[];
  context_ids: string[];
}

/** One eval-run row — mirrors ne-eval store.EvalRunRecord. The `passed` verdict
 * is SERVER-authoritative; the console never recomputes pass/fail from metrics. */
export interface EvalRunView {
  run_id: string;
  suite: string;
  config_hash: string;
  golden_set_hash: string;
  judge_model_id: string | null;
  judge_family: string | null;
  rubric_versions: Record<string, unknown>;
  metrics: Record<string, number>;
  thresholds: Record<string, number>;
  statuses: Record<string, string>;
  judge_error_rate: number;
  passed: boolean;
  created_at: string;
}

/** One KPI datapoint — aligned with the ne-contracts KpiRecorder vocabulary
 * (kpi_tag/value/dimensions). SCAFFOLD pending NE-068 U-P-B reporting. */
export interface KpiEntryView {
  kpi_tag: string;
  value: number | null;
  /** Server-authored measurement truth. Older servers omit this field and
   * only serve numeric, measured entries. */
  measurement_status?: "measured" | "undefined";
  unit: string | null;
  dimensions: Record<string, string>;
  window_start: string;
  window_end: string;
}

/** The governed /v1/kpi/summary response. SCAFFOLD pending NE-068. */
export interface KpiSummaryView {
  kpis: KpiEntryView[];
  generated_at: string;
}

// ===========================================================================
// NE-067 steward surfaces — wire types
//
// These MIRROR the merged ne-flywheel models 1:1 in snake_case:
//   - ne.flywheel.lanes.FixLane                 -> FixLaneWire
//   - ne.flywheel.review.ProposalStatus          -> ProposalStatusWire
//   - ne.flywheel.review.RejectionCode           -> RejectionCodeWire
//   - ne.flywheel.diffs.DiffArtifact             -> DiffArtifactView
//   - ne.flywheel.proposals.StoredFixProposal
//       + the review-status fold                -> StewardProposalView
//   - ne.flywheel.review.StoredReviewEvent        -> ReviewEventView
// Enum values are copied VERBATIM from the Python source — no invented states.
// ===========================================================================

/** Mirrors ne.flywheel.lanes.FixLane. The fifth value `entity_resolution` is the
 * JOB-EMITTED lane stamped by the ne-brain-graph Splink ER job on its
 * entity-merge-candidate proposals (never returned by route_cluster; it rides the
 * same NE-066 gate via the diff renderer's `_KNOWN_LANES` allowlist). ne-api
 * wire.py serializes `lane` as a plain str, so the value already crosses the wire —
 * this widening is additive display-side typing, not a contract change. */
export type FixLaneWire =
  "content" | "retrieval" | "schema" | "policy" | "entity_resolution" | "ontology" | "skills";

/** Mirrors ne.flywheel.review.ProposalStatus (the folded review-log status). */
export type ProposalStatusWire = "proposed" | "approved" | "rejected" | "applied";

/** Mirrors ne.flywheel.review.RejectionCode. */
export type RejectionCodeWire =
  "incorrect" | "unsafe" | "duplicate" | "stale" | "out_of_scope" | "other";

/**
 * Mirrors ne.flywheel.diffs.DiffArtifact — the SERVER-SANITIZED, deterministic
 * reviewable artifact produced by NE-066's `render_proposal_diff`. `patch` and
 * `pr_body` are UNTRUSTED proposal-carried text that has already been routed
 * through the renderer's inline/fenced/comment-line sanitizers server-side.
 * The console renders this as INERT preformatted text and NEVER re-renders,
 * re-sanitizes, or interprets it as markdown/HTML (no "dangerously-set-inner-
 * HTML" React prop, no markdown parser) — see flywheel/ProposalQueue.tsx and the
 * structural guardian.
 */
export interface DiffArtifactView {
  proposal_id: string;
  lane: FixLaneWire;
  kind: "git_unified_diff" | "opa_policy_diff";
  path: string;
  title: string;
  patch: string;
  pr_body: string;
}

/**
 * Mirrors ne.flywheel.proposals.StoredFixProposal, folded with its current
 * review status. DELIBERATELY OMITS the raw `summary` and `payload` fields —
 * proposal-carried free text reaches the console ONLY inside the already-
 * sanitized `artifact` (mirrors how ChunkView omits `acl`: the server trims
 * what the client is handed, by construction).
 */
export interface StewardProposalView {
  disclosure_status?: "available";
  proposal_id: string;
  lane: FixLaneWire;
  kind: string;
  status: ProposalStatusWire;
  cluster_id: string;
  confidence: number;
  priority: string;
  evidence_event_ids: string[];
  evidence_signal_ids: number[];
  config_hash: string;
  created_at: string;
  artifact: DiffArtifactView;
  /** Non-null ONLY for skill kinds (NE-SKILL-1b) — a field-by-field, server-
   * derived skill candidate. The raw payload NEVER crosses the wire. */
  skill?: SkillCandidateView | null;
}

/**
 * 015A0-ENFORCEMENT: the withheld branch of the steward queue wire.
 *
 * It carries the opaque proposal id, the kind-neutral safe status and time, and
 * the disclosure status. Lane, kind, cluster, confidence, priority, evidence
 * ids, config hash, `artifact` and `skill` are ABSENT — not empty, absent — so
 * there is no field on this shape a table, a search index, a CSV export or an
 * action handler could dereference.
 */
export interface WithheldStewardProposalView {
  disclosure_status: "content_withheld" | "retryable_unknown";
  proposal_id: string;
  status: ProposalStatusWire;
  created_at: string;
}

/** What `GET /v1/steward/proposals` may serve for one row. */
export type ServedStewardProposalView = StewardProposalView | WithheldStewardProposalView;

/**
 * Mirrors ne.api.wire.SkillCandidateView (NE-SKILL-1b). A forged skill candidate,
 * field-by-field server-derived. `body` is ATTACKER-INFLUENCED content injected
 * fleet-wide on approval — the console renders it INERT (a plain text node, never
 * HTML) with an explicit "untrusted — injected into every agent" framing so the
 * approver reviews it as adversarial input (R-23).
 */
export interface SkillCandidateView {
  name: string;
  version: number;
  description: string;
  triggers: string[];
  suggested_acl_labels: string[];
  provenance_line: string;
  body: string;
}

/**
 * Mirrors ne.api.wire.SkillEditFieldsRequest (NE-SKILL-3) — steward
 * edit-before-approve fields. NEVER `body`: a body edit is reject-and-re-forge,
 * not an in-place edit. Any supplied field re-runs the routing eval server-side
 * before the commit, fail-closed. Only fields the steward actually CHANGED are
 * sent (an unedited apply omits `edits` entirely).
 */
export interface SkillEditFields {
  name?: string;
  description?: string;
  triggers?: string[];
}

/** Mirrors ne.api.wire.SkillApplyView (NE-SKILL-1b) — the apply-skill response. */
export interface SkillApplyView {
  review: ReviewEventView;
  name: string;
  version: number;
  content_hash: string;
  acl_labels: string[];
}

// BD-3 / PRE-DEMO-REHEARSAL-001 — server-authoritative,
// historical-ACL-trimmed document lineage.
export type DocumentTypeWire =
  "pdf" | "document" | "presentation" | "spreadsheet" | "text" | "other";

export type DocumentStageNameWire = "parse" | "chunk" | "acl" | "embed" | "graph" | "supersede";

export type DocumentStageStatusWire = "waiting" | "active" | "complete" | "skipped" | "unavailable";

/** Legacy P1 summary shape retained for mounted callers during rolling cutover. */
export interface DocumentSummaryWire {
  document_id: string;
  display_name: string;
  source_system: string;
  version_seq: number;
  content_hash: string;
  ingested_at: string | null;
  indexing: boolean;
}

/** Full current /v1/documents summary projection. */
export interface DocumentSummaryReadWire extends DocumentSummaryWire {
  document_type: DocumentTypeWire;
}

export interface DocumentListQuery {
  q?: string;
  type?: DocumentTypeWire;
  source?: string;
  /**
   * INTERIM (PD-DOC-TITLES): structured-ness inferred from the
   * ".negraph.json" connector filename convention; REMOVE when the real
   * `structured` flag lands on the document wire. Omitted/`false` (the
   * default) EXCLUDES operational-graph records; `true` is the explicit
   * opt-in that puts them back in the list.
   */
  structured?: boolean;
  cursor?: string;
  limit?: number;
}

export interface DocumentListWire {
  documents: DocumentSummaryReadWire[];
  next_cursor: string | null;
  document_types: DocumentTypeWire[];
  source_systems: string[];
  /**
   * INTERIM (PD-DOC-TITLES): honest count of ".negraph.json" rows in the
   * caller's ACL-visible corpus (not the page). REMOVE alongside the rest of
   * PD-DOC-TITLES when the real `structured` flag lands on the document wire.
   */
  operational_graph_count: number;
}

export interface DocumentStageWire {
  stage: DocumentStageNameWire;
  status: DocumentStageStatusWire;
  occurred_at: string | null;
  audit_seq: number | null;
  detail: string;
}

export interface DocumentVersionWire {
  version_seq: number;
  content_hash: string;
  ingested_at: string | null;
  ingested_audit_seq: number | null;
  superseded_at: string | null;
  superseded_audit_seq: number | null;
  chunk_count: number;
  status: "indexing" | "current" | "superseded";
}

export type DocumentComparisonStateWire =
  "first_version" | "available" | "unchanged" | "predecessor_hidden";

export interface DocumentComparisonWire {
  state: DocumentComparisonStateWire;
  predecessor_version: number | null;
}

/** Legacy P1 journey shape retained for its mounted presentation caller. */
export interface DocumentJourneyWire {
  document_id: string;
  display_name: string;
  source_system: string;
  selected_version: number;
  selected_content: string;
  diff: string | null;
  stages: DocumentStageWire[];
  versions: DocumentVersionWire[];
}

/** Full current /v1/documents/{id}/journey projection. */
export interface DocumentJourneyReadWire extends DocumentJourneyWire {
  document_type: DocumentTypeWire;
  selected_content_hash: string;
  comparison: DocumentComparisonWire;
}

export interface DocumentStageFactWire {
  key: string;
  label: string;
  value: string | number | boolean | null;
  available: boolean;
}

export type DocumentArtifactKindWire = "chunk" | "embedding";
export type DocumentArtifactStateWire =
  "available" | "empty" | "skipped" | "unavailable" | "withheld";

/** Normalized current PR 673 artifact projection. Every field is explicit. */
export interface DocumentStageArtifactWire {
  kind: DocumentArtifactKindWire;
  label: string;
  state: DocumentArtifactStateWire;
  ordinal: number;
  identifier: string | null;
  size_bytes: number | null;
  artifact_count: number | null;
  dimensions: number | null;
  model: string | null;
  index_reference: string | null;
  fingerprint: string | null;
  numeric_preview: number[] | null;
  reason: string | null;
}

export interface DocumentSemanticChunkWire {
  /** Opaque deterministic identifier served by the version-bound backend receipt. */
  identifier: string;
  size_bytes: number;
}

export interface DocumentSemanticChunksWire {
  state: "available" | "unavailable";
  reason: string | null;
  chunks: DocumentSemanticChunkWire[];
  /** True only when the server capped its post-ACL projection. */
  truncated: boolean;
}

export interface DocumentGraphEntityWire {
  /** Server-owned opaque graph key. */
  key: string;
  entity_class: string;
  display_name: string;
  source_system: string;
}

export interface DocumentGraphRelationshipWire {
  /** Server-owned opaque relationship key. */
  key: string;
  edge_kind: string;
  subject: DocumentGraphEntityWire;
  object: DocumentGraphEntityWire;
}

export interface DocumentGraphEvidenceWire {
  state: "available" | "empty" | "skipped" | "unavailable";
  reason: string | null;
  entities: DocumentGraphEntityWire[];
  relationships: DocumentGraphRelationshipWire[];
  /** True only when the server capped its post-ACL projection. */
  truncated: boolean;
}

export interface DocumentStageEvidenceWire {
  document_id: string;
  version: number;
  content_hash: string;
  document_type: DocumentTypeWire;
  stage: DocumentStageNameWire;
  status: DocumentStageStatusWire;
  occurred_at: string | null;
  audit_seq: number | null;
  detail: string;
  facts: DocumentStageFactWire[];
  artifacts: DocumentStageArtifactWire[];
  comparison: DocumentComparisonWire;
  /** Non-null only for CHUNK stage evidence; null for every other stage. */
  semantic_chunks: DocumentSemanticChunksWire | null;
  /** Non-null only for GRAPH stage evidence; null for every other stage. */
  graph_evidence: DocumentGraphEvidenceWire | null;
}

/**
 * Mirrors ne.flywheel.review.StoredReviewEvent — the result of submitting an
 * approve/reject/apply action onto the ProposalReviewService. `actor` is
 * SERVER-derived from the verified OIDC bearer — never client-supplied.
 */
export interface ReviewEventView {
  review_id: string;
  proposal_id: string;
  action: "approve" | "reject" | "apply";
  from_status: ProposalStatusWire;
  to_status: ProposalStatusWire;
  actor: string;
  surface: string;
  session_id: string;
  rejection_code: RejectionCodeWire | null;
  note: string | null;
  applied_ref: string | null;
  config_hash: string;
  created_at: string;
}

/** Server-computed per-lane proposal counts for the weekly digest. */
export interface StewardLaneDigest {
  lane: FixLaneWire;
  proposed: number;
  approved: number;
  rejected: number;
  applied: number;
}

/** NE-SKILL-4 rot-card sections — server-computed by the monthly Skill Gardener,
 * rendered verbatim (no local aggregation). Mirrors the ne-api
 * StewardSkills*View wire models. */
export interface StewardSkillsRetirement {
  name: string;
  reason: string;
  fired_count: number;
}
export interface StewardSkillsFailure {
  name: string;
  known_fires: number;
  failed: number;
  ratio: number;
}
export interface StewardSkillsOverlap {
  phrase: string;
  names: string[];
}
export interface StewardSkillsNearDup {
  a: string;
  b: string;
  method: string;
  score: number;
}
export interface StewardSkillsUnknownTools {
  name: string;
  unknown: string[];
}
export interface StewardSkillsStaleProposal {
  proposal_id: string;
  kind: string;
  name: string;
  age_days: number;
}
export interface StewardSkillsDigestView {
  ran_at: string;
  retirement_candidates: StewardSkillsRetirement[];
  failure_flags: StewardSkillsFailure[];
  trigger_overlaps: StewardSkillsOverlap[];
  near_dups: StewardSkillsNearDup[];
  unknown_requires_tools: StewardSkillsUnknownTools[];
  stale_proposals: StewardSkillsStaleProposal[];
}

/** The governed /v1/steward/digest response — server-computed, deterministic
 * given inputs. The console renders these fields verbatim (no local
 * aggregation/sorting). */
export interface StewardDigestView {
  week_start: string;
  week_end: string;
  generated_at: string;
  /** 015A0-ENFORCEMENT: `principal_visible` means the server aggregated ONLY the
   * rows this principal may currently see, so the totals are that principal's
   * view and not the deployment's. Absent on the pre-ENFORCEMENT wire. */
  scope?: "principal_visible";
  totals: { proposed: number; approved: number; rejected: number; applied: number };
  by_lane: StewardLaneDigest[];
  /** The optional monthly Skill Gardener rot card (NE-SKILL-4) — null before the
   * first run (the console omits the card entirely). A `principal_visible`
   * envelope sets this null: the Gardener report is one global fold and is not
   * a principal-effective projection, so it may not ride a scoped envelope. */
  skills?: StewardSkillsDigestView | null;
}

/**
 * The governed /v1/steward/latency response: server-computed time from
 * proposal-created -> first steward-decision. Nulls when there is no data
 * (e.g. zero decided proposals for median/p90, or zero pending for the
 * oldest-pending age).
 */
export interface StewardLatencyView {
  median_wait_s: number | null;
  p90_wait_s: number | null;
  decided_count: number;
  pending_count: number;
  oldest_pending_age_s: number | null;
  generated_at: string;
}

/**
 * One row of the governed GET /v1/steward/skills library list (NE-SKILL-1a).
 * Server-computed folds ONLY (name/source/status/version/labels/fired-count/
 * tampered) — INCLUDING quarantined + pending-forge names. The console renders
 * every text field INERT (no raw-HTML injection). `current_version` is null
 * for a version-less (quarantined-pending-forge) name; `last_fired_at` is null
 * until first fired.
 */
export interface SkillLibraryRow {
  name: string;
  source: string;
  status: string;
  current_version: number | null;
  description: string;
  acl_labels: string[];
  fired_count: number;
  last_fired_at: string | null;
  tampered: boolean;
}

// ===========================================================================
// NE-131 sovereignty-posture surface — wire types
//
// MIRROR the ne-api posture wire models (packages/ne-api/src/ne/api/wire.py):
// PostureConstraintView / ModelLaneView / ConfigHashSpanView / PostureView. The
// `attested` flag is SERVER-real (drift-guarded against the readiness harness);
// a guardian-less constraint arrives honestly attested:false / mode:"manual" (none
// build-time guardian references — the console never recomputes attestation.
// ===========================================================================

/** One C1-C12 constraint's attestation as GET /v1/posture returns it. */
export interface PostureConstraintView {
  id: string;
  short: string;
  enforcement: string;
  attested: boolean;
  mode: string;
  evidence: string[];
}

/** One composed model-registry lane (opaque — no license/origin tags).
 *
 * CONFIGURED, NOT MEASURED. `model_id` is the routing ALIAS
 * (`frontier-default`); `configured_model` is what that alias RESOLVES to
 * (`claude-sonnet-5`) and `context_window` is the window this deployment
 * declared for it. All five fields are reads of the frozen gateway settings —
 * none of them says the lane answered, or that an endpoint agrees. That claim
 * is `LaneServingView`, it is measured, and it exists only for probeable
 * private lanes. Never render one in the other's voice. */
export interface ModelLaneView {
  model_id: string;
  provider: string;
  lane: string;
  configured_model: string;
  context_window: number;
}

/** One config_hash span on the timeline. `to` is null for the span IN FORCE. */
export interface ConfigHashSpanView {
  hash: string;
  from: string;
  to: string | null;
}

/** The §2.5 pending marker for a parked governed air-gap change: the approval
 * request id + the requester (ids only — parameters are approver-gated). */
export interface AirGapPendingView {
  request_id: string;
  requested_by: string;
}

/** The governed /v1/posture response — the sovereignty-posture surface.
 * `air_gap`/`egress_allowlist` are a direct read from the same effective state
 * the enforcing egress guard reads; this is not an endpoint liveness probe.
 * The server-authored observation instants date each runtime read so cached
 * payloads visibly age instead of becoming current at browser fetch time.
 * `air_gap_pending` is non-null while a governed air-gap change awaits
 * approval (the displayed posture stays the in-force one). */
export interface PostureView {
  constraints: PostureConstraintView[];
  air_gap: boolean;
  egress_allowlist: string[];
  /** Absent only when talking to a pre-freshness server; the UI then says the
   * age is unknown rather than manufacturing browser-time freshness. */
  network_observed_at?: string;
  model_lanes: ModelLaneView[];
  config_hash: string;
  config_hash_history: ConfigHashSpanView[];
  configuration_observed_at?: string;
  attestation_source: string;
  air_gap_pending: AirGapPendingView | null;
}

/** A governed write endpoint's §2.5 answer: applied (Tier 0/1) or pending
 * (parked at the approval gate; `request_id` set). Denied never lands here —
 * it arrives as an `ApiError` 403 whose `detail` is the server's reason. */
export interface GovernedChangeResult {
  status: "applied" | "pending";
  request_id: string | null;
}

// ===========================================================================
// NE-MODEL-ROUTING — the governed runtime model route (wire types)
//
// MIRROR the ne-api wire models (TaskRoutingView / RouteTargetView). Every
// field here is the SERVER's answer. `selectable` in particular is a judgement
// combining the boot model set, the gateway's budget arithmetic, the tier table
// and the live egress posture — a console that computed it would be
// transliterating three server-side facts, and the first divergence would be a
// menu offering a change the server refuses.
//
// C12: model ALIASES and lane names only. No api_base, no provider credential,
// no endpoint ever crosses this surface.
// ===========================================================================

/** One candidate model for a task, with the server's verdict on it. */
export interface RouteTargetView {
  model: string;
  lane: string;
  /** The governed action a repoint to this model would ride. */
  action: string;
  /** The approval tier that action requires. */
  tier: number;
  selectable: boolean;
  /** The server's own words when `selectable` is false; null otherwise. */
  reason: string | null;
}

/** One gateway task: what serves it now, what shipped, and what may replace it. */
export interface TaskRoutingView {
  task: string;
  /** The retained route alias. It is serving only when `available` is true. */
  model: string;
  lane: string;
  /** Server-owned lifecycle/serving verdict for the retained route. */
  available: boolean;
  /** Server-authored explanation when the retained route is unavailable. */
  unavailable_reason: string | null;
  default_model: string;
  source: "default" | "override";
  changed_by: string | null;
  approved_by: string | null;
  changed_at: string | null;
  pending_change: {
    request_id: string;
    requested_by: string;
    model: string;
  } | null;
  targets: RouteTargetView[];
}

/** GET /v1/models/routing. */
export interface ModelRoutingView {
  tasks: TaskRoutingView[];
}

// ===========================================================================
// BD-15 governed per-run model choices
//
// This is a server-authored availability catalog, not a client copy of the
// boot registry. An alias appears only while the API has current authoritative
// serving evidence for this surface; the console neither probes nor ages it.
// ===========================================================================

export type ModelChoiceSurface = "ask" | "agentic-workflow";

export interface ModelChoiceView {
  model: string;
  provider: string;
  lane: string;
  /** Non-null only on a catalog that MEASURED availability. The run-model
   * picker lists the CONFIGURED models, so both fields are null there — the
   * absence of a measurement is never dressed up as a stale observation. */
  evidence: "private-serving-probe" | "frontier-provider-attempt" | null;
  observed_at: string | null;
}

export interface ModelChoiceCatalogView {
  surface: ModelChoiceSurface;
  task: string;
  /** Server-proven default for an omitted selection; null means no model may
   * run and the owning surface must disable submit. */
  effective_default: string | null;
  choices: ModelChoiceView[];
}

// ===========================================================================
// Governed inference target lifecycle (ADR-0021)
//
// Closed, server-authored status/action surface. The console never receives a
// command, executable, endpoint, secret reference, provider argv or raw host
// output. It renders these values and submits only the exact action the server
// advertised for the target's current version.
// ===========================================================================

export type InferenceLifecycleAction = "start" | "stop";

export interface PendingInferenceLifecycleChange {
  request_id: string;
  requested_by: string;
  desired_state: "ready" | "stopped";
}

export interface InferenceLifecycleTargetView {
  target_id: string;
  display_name: string;
  aliases: string[];
  state: string;
  desired_state: string;
  version: number;
  transition_id: string | null;
  code: string;
  observed_at: string | null;
  updated_at: string;
  cooldown_until: string | null;
  pending_change: PendingInferenceLifecycleChange | null;
  available_actions: InferenceLifecycleAction[];
}

export interface InferenceLifecycleView {
  registry_hash: string;
  transport_available: boolean;
  targets: InferenceLifecycleTargetView[];
}

export interface InferenceStopConfirmationView {
  confirmation_token: string;
  expires_at: string;
}

// ===========================================================================
// NE-SERVING-STATUS live serving surface — wire types
//
// MIRROR the ne-api serving wire models (LaneServingView / ServingStatusView).
// Every non-config field is a MEASUREMENT the server formed against the lane's
// endpoint; the console renders the verdict and never contacts a model
// endpoint itself. The failure states carry no model data by construction.
// ===========================================================================

/** The server's closed observation vocabulary. `not_probed` is the fail-closed
 * refusal to contact a malformed endpoint — the only state with no
 * `checked_at`, because no observation exists. */
export type ServingState =
  "ok" | "timeout" | "unreachable" | "http_error" | "malformed" | "not_probed";

/** One probeable lane's live observation. `checked_at` is when THIS verdict
 * was formed (a served cache hit keeps the older stamp — age arrives with the
 * data); `last_ok_at` is the newest success the server has ever formed for
 * this lane, surviving failures, so "down since when" is answerable and a
 * never-answered lane is distinguishable (null). */
export interface LaneServingView {
  model_id: string;
  lane: string;
  /** scheme://host[:port] of the probed endpoint — server-redacted identity. */
  endpoint: string;
  /** What the configuration names — served so a mismatch renders as the
   * endpoint's word against the configuration's. */
  configured_model: string;
  state: ServingState;
  checked_at: string | null;
  latency_ms: number | null;
  http_status: number | null;
  /** The model ids the ENDPOINT reports serving; non-empty only on `ok`. */
  served_models: string[];
  serves_configured_model: boolean | null;
  /** The runtime serving window the endpoint reports (vLLM max_model_len). */
  observed_context_window: number | null;
  engine_version: string | null;
  last_ok_at: string | null;
}

/** GET /v1/models/serving — one row per probeable lane. Empty on a deployment
 * with no privately-served lane, and the screen renders nothing for it. */
export interface ServingStatusView {
  lanes: LaneServingView[];
}

// ===========================================================================
// NE-132 connector-sync status surface — wire types
//
// MIRROR the ne-api connect-status wire models (packages/ne-api/src/ne/api/wire.py):
// QuarantineReasonView / ConnectorStatusView / ConnectStatusView. EVERY nullable
// field is honestly absent — no state source had data (§0 law 5): a configured-but-
// never-synced connector carries null health, an unreadable quarantine store yields
// a null count/total. A null is NEVER a fabricated fresh/green/zero. The console
// renders these facts verbatim — no local aggregation, no default 'ok'.
// ===========================================================================

/** One sanitized quarantine reason-CATEGORY bucket (allowlisted fixed label). */
export interface QuarantineReasonView {
  reason: string;
  count: number;
}

/** One connector lane as GET /v1/connect/status returns it (SPEC-row shape).
 * TWO freshness facts, never one (SPEC §12 item 9): `last_checked_age_s` is when
 * the sync cycle last RAN (advances every cycle, found something or not; null =
 * never checked), `cursor_age_s` is how old the newest ACCEPTED record is
 * (advances only when a batch accepts a record — a CDC heartbeat/empty batch
 * advances only `last_checked_age_s`; null = no accepted-record fact). `acl_mirror`
 * is the LATEST sync event's fail-closed health: 'ok' = ACL mirror verified,
 * 'degraded' = fail-closed, null = no sync recorded yet. */
export interface ConnectorStatusView {
  id: string;
  kind: string;
  /** Server-owned execution posture. A snapshot is historical/read-only even
   * if an older or inconsistent capability bit says sync is allowed. */
  posture: "live" | "snapshot";
  cursor_age_s: number | null;
  last_checked_age_s: number | null;
  acl_mirror: "ok" | "degraded" | null;
  last_sync_seq: number | null;
  quarantined_count: number | null;
  /** Server-owned advisory visibility only; POST re-authorizes authoritatively. */
  sync_allowed: boolean;
}

/** The governed /v1/connect/status response. `quarantine_total` is null when no
 * quarantine store was readable (honestly unknown, never a fake zero).
 * `quarantine_reasons` is the additive §4.3 reason-category ledger. */
export interface ConnectStatusView {
  connectors: ConnectorStatusView[];
  quarantine_total: number | null;
  quarantine_reasons: QuarantineReasonView[];
}

/** Counts-only terminal result from a governed connector Sync now request. */
export interface ConnectSyncCountsView {
  mode: string;
  records_seen: number;
  written: number;
  skipped_unchanged: number;
  invalid: number;
  quarantined: number;
}

/** Durable request metadata; no source body, cursor, credential or principal map. */
export interface ConnectSyncRequestView {
  request_id: string;
  source: string;
  status: "queued" | "running" | "succeeded" | "failed";
  created_at: string;
  expires_at: string;
  counts: ConnectSyncCountsView | null;
  error_class: string | null;
}

/** Closed safe metadata for one current source-authority recovery attempt. */
export interface AuthorityAttemptView {
  attempt_id: string;
  source_kind: "m365-sharepoint" | "salesforce";
  source_binding: "m365" | "salesforce";
  scope_hash: string;
  identity_hash: string;
  stage: "claimed" | "deletion_validated" | "authority_tombstoned";
  source_revision: string;
  contract_epoch: number;
  prior_artifact_id: string | null;
  receipt_id: string | null;
  receipt_hash: string | null;
}

/** Stable, server-filtered queue of current pending recovery attempts. */
export interface AuthorityAttemptsView {
  attempts: AuthorityAttemptView[];
}

/** One parked write on the OPS-GATED /v1/connect/quarantine drill-down. Carries
 * the RAW stored reason — the contract validator's own message naming the field
 * and the offending value — and the parked row itself (string values bounded
 * server-side; `payload_truncated` says so). `source`/`record_id` are null when
 * the park's provenance shape is unrecognised — honest, never guessed. */
export interface QuarantinedItemView {
  item_id: string;
  source: string | null;
  record_id: string | null;
  reason: string;
  category: string;
  shape: string;
  created_at: string;
  payload: Record<string, unknown>;
  payload_truncated: boolean;
}

/** GET /v1/connect/quarantine (ops-gated). `total` is null when no quarantine
 * store was readable (honestly unknown, never a fake zero). */
export interface ConnectQuarantineView {
  items: QuarantinedItemView[];
  total: number | null;
}
// NE-134 graph wire (mirrors ne-api graph_wire.py — ACL-stripped by
// construction, C7)
//
// These MIRROR the six-field ACL-stripped views the graph read routes return
// (packages/ne-api/src/ne/api/graph_wire.py): GraphNodeView / GraphEdgeView /
// SubgraphView / GraphPathRecordView / GraphPathsView. The console types EXACTLY
// these fields — never `acl`, `content_hash`, or `config_hash` — so the chamber
// can render only what the trimmed routes hand it (design §6.2). `properties` is
// an opaque server-provided map; the console never interprets or filters it.
// ===========================================================================

/** Exact optional filters for GET /v1/graph/entities. Cursor bytes are opaque. */
export interface GraphEntityListQuery {
  q?: string;
  entity_class?: string;
  source?: string;
  assurance?: "source" | "confirmed" | "extracted";
  cursor?: string;
  limit?: number;
}

/** One ACL/source-health-trimmed discovery row; no properties or source ref. */
export interface GraphEntitySummaryWire {
  key: string;
  entity_class: string;
  display_name: string;
  source_system: string;
  assurance: string;
}

/** A strict server-ordered discovery page. Totals are deliberately absent, and
 * facets are NOT folded onto the page — they are their own additive read below,
 * so this shape is byte-identical to the one the console already consumed. */
export interface GraphEntityListWire {
  entities: GraphEntitySummaryWire[];
  next_cursor: string | null;
}

/**
 * GET /v1/graph/facets — the entity classes and source systems PRESENT in the
 * calling principal's own visible slice (`ne.api.graph_wire.
 * GraphEntityFacetsView`).
 *
 * The server derives both lists by folding the SAME ACL-trimmed scan that
 * serves `/v1/graph/entities` rows, so a value appears here only when at least
 * one entity this principal can see carries it. The console therefore renders
 * these verbatim and NEVER falls back to a built-in class list: the compile-time
 * `ne-schema` registry is what shipped the dead Class dropdown (four of five
 * options matched nothing in the live graph) and must never be the option
 * source again.
 *
 * No counts — the server's scan is bounded, so a count would be a
 * possibly-wrong number stated as a fact. `truncated` says when that bound was
 * hit and the lists may be incomplete.
 */
export interface GraphEntityFacetsWire {
  entity_classes: string[];
  source_systems: string[];
  truncated: boolean;
}

/**
 * A trimmed graph node on the wire — EXACTLY seven fields, no `acl`.
 *
 * `assurance` (NE-T2X-4, threaded on this `/v1/graph` explorer surface;
 * NE-T2X-5 consumes it for the "LLM-extracted — unconfirmed" chip, design
 * §6.1) is REQUIRED — no default, mirroring the server's own anti-silent-drop
 * stance (`ne.api.graph_wire.GraphNodeView`): a fixture that forgets it fails
 * `tsc`, not a silent `"source"` default.
 */
export interface GraphNodeWire {
  key: string;
  entity_class: string;
  display_name: string;
  source_system: string;
  source_ref: string;
  properties: Record<string, unknown>;
  assurance: string;
}

/** A trimmed graph edge on the wire — EXACTLY seven fields, no `acl`.
 * `assurance` — see {@link GraphNodeWire}'s docstring. */
export interface GraphEdgeWire {
  key: string;
  edge_kind: string;
  from_key: string;
  to_key: string;
  source_system: string;
  source_ref: string;
  assurance: string;
}

/** A trimmed subgraph result (query / neighborhood) with a `truncated` flag. */
export interface SubgraphWire {
  nodes: GraphNodeWire[];
  edges: GraphEdgeWire[];
  truncated: boolean;
}

/** One ordered trimmed path (nodes + edges in traversal order). */
export interface GraphPathWire {
  nodes: GraphNodeWire[];
  edges: GraphEdgeWire[];
}

/** A set of trimmed paths (POST /v1/graph/paths) with a `truncated` flag. */
export interface GraphPathsWire {
  paths: GraphPathWire[];
  truncated: boolean;
}

// ===========================================================================
// NE-136 ontology/verified wire (mirrors ne-api wire.py — redacted by
// construction, C12/C7)
//
// These MIRROR the NE-135 governed ontology + verified-answer views
// (packages/ne-api/src/ne/api/wire.py): OntologyClassView / OntologyClassesResponse
// / OntologyShapeView / OntologyShapesResponse / OntologyViolationView /
// OntologyViolationsResponse / VerifiedTemplateView / VerifiedTemplatesResponse /
// VerifiedInvokeRequest / VerifiedAnswerView / VerifiedInvokeResponse. The
// violation views carry counts/ids/hashes ONLY — never `delta_nt` or any SHACL
// message string (C12). No request type exposes a raw-SPARQL/`query` field: the
// invoke body is exactly {template_id, version?, args} (server extra='forbid'),
// and identity is the OIDC bearer — there is intentionally no principal/actor
// field. The console renders EXACTLY these fields (C7).
// ===========================================================================

/** One ontology class in the BFO/CCO/neop browser tree (mirrors
 * OntologyClassView). `parents` links up the class tree; `source` groups it. */
export interface OntologyClassWire {
  iri: string;
  label: string | null;
  parents: string[];
  source: string;
}

/** GET /v1/ontology/classes — the class tree + sources map + shapes hash. */
export interface OntologyClassesWire {
  classes: OntologyClassWire[];
  total: number;
  sources: Record<string, string>;
  shapes_hash: string;
}

/** One compiled SHACL node shape on the wire (mirrors OntologyShapeView). */
export interface OntologyShapeWire {
  iri: string;
  target_class: string | null;
  scope: string;
}

/** GET /v1/ontology/shapes — the compiled shape list + shapes hash. */
export interface OntologyShapesWire {
  shapes: OntologyShapeWire[];
  shapes_hash: string;
}

/** One quarantine-lane entry on the wire — REDACTED (mirrors
 * OntologyViolationView). STRUCTURALLY omits `delta_nt` and every SHACL message
 * string (C12): counts / ids / hashes / the quarantined-whole flag only. */
export interface OntologyViolationWire {
  quarantine_id: string;
  created_at: string;
  graph_iri: string | null;
  shapes_hash: string;
  violation_count: number;
  quarantined_whole: boolean;
  provenance: string;
}

/** GET /v1/ontology/violations — honest `configured:false` when the quarantine
 * lane is not inspectable in this deployment. */
export interface OntologyViolationsWire {
  configured: boolean;
  count: number;
  entries: OntologyViolationWire[];
}

/** A generated strict args JSON Schema for a verified-answer template
 * (the Pydantic-emitted object schema on `VerifiedTemplateView.args_schema`). */
export interface ArgsJsonSchema {
  title?: string;
  type?: string;
  properties?: Record<string, ArgsFieldSchema>;
  required?: string[];
  additionalProperties?: boolean;
}

/** One field's schema within an `ArgsJsonSchema.properties`. */
export interface ArgsFieldSchema {
  type?: string;
  format?: string;
  pattern?: string;
  maxLength?: number;
  title?: string;
  /** Closed vocabulary authored by the verified template and served verbatim by
   * ne-api. The console may render these values; it must never invent its own. */
  enum?: string[];
  /** NE-KG-MULTIKEY: a template arg declaring `list_max` generates a UNION field
   * — `anyOf: [<scalar>, {type:"array", items:<same scalar>, minItems, maxItems}]`
   * with the IDENTICAL per-element constraints on both branches. The form model
   * (`knowledge/t3/argsForm.ts`) recognizes EXACTLY that shape and renders a
   * bounded repeatable input; any other `anyOf` stays honestly unsupported. */
  anyOf?: ArgsFieldSchema[];
  items?: ArgsFieldSchema;
  minItems?: number;
  maxItems?: number;
}

/** One registered verified-answer template (mirrors VerifiedTemplateView).
 * `args_schema` is the generated strict args JSON Schema; `citation_var` names
 * the column whose per-row value is the `urn:ne:entity:<key>` citation — the
 * MACHINE identifier an auditor quotes. `label_var` names the HUMAN-readable
 * label column, or null when the template projects none: renderers lead with
 * the label and demote (never hide) the identifier; with no label they render
 * the declared order verbatim, fabricating nothing. Both are the template
 * author's own declarations, echoed by the server.
 *
 * `label_var` is OPTIONAL because an API predating the field omits the key
 * entirely, so during a rolling upgrade the value a reader gets is `undefined`,
 * not `null`. Declaring it required made the type vouch for a shape the server
 * does not always send, and every consumer that read it unguarded inherited that
 * promise; `displayOrder` treats absent and null as the same declaration. */
export interface VerifiedTemplateWire {
  template_id: string;
  version: number;
  title: string;
  tool_name: string;
  args_schema: ArgsJsonSchema;
  columns: string[];
  citation_var: string;
  label_var?: string | null;
  max_rows: number | null;
  /** The template's own `[scope] graph_classes` declaration, echoed by the server
   * — the ACL-labelled graph families this query may read. OPTIONAL for the same
   * rolling-upgrade reason as `label_var`: an API predating the field omits the
   * key; absent and empty both mean "not stated" and the renderer shows nothing. */
  graph_classes?: string[];
}

/** GET /v1/verified/templates — `enabled:false` when verified answers are off. */
export interface VerifiedTemplatesWire {
  enabled: boolean;
  registry_hash: string | null;
  templates: VerifiedTemplateWire[];
}

/** The deterministic verified-answer table on the wire (mirrors
 * VerifiedAnswerView). `citations[i]` pairs 1:1 with `rows[i]` (the
 * `citation_var` column) and is a `urn:ne:entity:<GraphNodeWire.key>` string.
 * `executed_at` is an ISO-8601 datetime string; it is EXCLUDED from the
 * server-side replay compare. */
export interface VerifiedAnswerWire {
  template_id: string;
  template_version: number;
  args: Record<string, unknown>;
  columns: string[];
  rows: string[][];
  citations: string[];
  truncated: boolean;
  row_limit: number;
  // NE-CTXB-5 envelope (mirrors VerifiedAnswerView): the shown-row count
  // (== rows.length), the trusted total (== returned_rows when untruncated, else
  // null until the opt-in COUNT), and the truncation reason class.
  returned_rows: number;
  total_rows: number | null;
  truncation_reason: "rows" | "tokens" | null;
  config_hash: string;
  ontology_version: string;
  executed_at: string;
}

/** POST /v1/verified/invoke — the answer + the SERVER's replay-diff verdict.
 * `replay_match` is the server's double-invoke `answers_equal` result
 * (executed_at excluded) — the console renders it verbatim, never recomputes. */
export interface VerifiedInvokeWire {
  answer: VerifiedAnswerWire;
  replay_match: boolean;
}

// ===========================================================================
// NE-167b trace-assembly wire types (mirrors ne-api trace_assembly.py 1:1)
//
// These MIRROR the NE-167a assembler's pydantic models FIELD-FOR-FIELD
// (packages/ne-api/src/ne/api/trace_assembly.py). Datetimes cross the wire as
// ISO-8601 strings. `kind` is typed as a plain `string` — deliberately NOT a
// closed union — so an emitter the assembler does not yet recognize (its own
// "event" raw fallback, or any future kind) renders exactly like any other
// unrecognized value (§0 law 5, forward-compatible: never a TS narrowing that
// could silently drop an honest raw node).
// ===========================================================================

/** A `subagent.dispatch`/`subagent.complete` bracket (mirrors SubagentSpan).
 * `completed` is a plain boolean (server default `false`, never nullable). */
export interface TraceSubagentSpanView {
  agent: string | null;
  depth: number | null;
  completed: boolean;
  ended_at: string | null;
  duration_ms: number | null;
  outcome: string | null;
}

/** A `subagent.route` per-child routing decision (mirrors RouteSpan). */
export interface TraceRouteSpanView {
  agent: string | null;
  route_task: string | null;
  route_lane: string | null;
  tier: string | null;
  task_source: string | null;
  lane_source: string | null;
  hint_clamped: boolean | null;
  reason: string | null;
}

/** A `ne.gateway.complete`/`ne.gateway.stream` model call (mirrors
 * ModelCallSpan). `cost_usd`/`input_tokens`/`output_tokens` are read
 * DEFENSIVELY from NE-163a provenance server-side — absent ⇒ `null`, never a
 * fabricated 0 (see `format.ts`'s `formatTokens`/`formatCost`). */
export interface TraceModelCallView {
  task: string | null;
  lane: string | null;
  /** The explicit per-run alias the caller requested. Optional for rolling
   * upgrades; null/absent means no explicit selection was recorded. */
  requested_model?: string | null;
  model_name: string | null;
  response_model: string | null;
  cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  /** B12 measured call latency in whole ms. `null` (every pre-B12 row) renders
   * absent; a served `0` is a real sub-millisecond measurement and renders
   * "0 ms" — absent and zero are different facts. */
  latency_ms: number | null;
  /** B12 time-to-first-token in whole ms — streams only; `null` on non-stream
   * calls and pre-B12 rows. Same absent-vs-zero rule as `latency_ms`. */
  ttft_ms: number | null;
  /** Where the model that ANSWERED sat in this request's attempt sequence, as
   * the gateway recorded it: `"primary"` (nothing was attempted before it) or
   * `"fallback"` (something was, and did not serve). `null` — and, on an older
   * API, absent from the wire entirely — means the record never said, and MUST
   * render as unknown: "the route's first choice served" is a verdict, and the
   * console has no route table to derive it from (C7).
   *
   * OPTIONAL, and the `?` is doing work `| null` cannot. An API deployed
   * before this field omits the key, so a reader gets `undefined`. Declared
   * `string | null`, the house narrowing idiom `x !== null && x.startsWith(…)`
   * COMPILES — `undefined !== null` is true, tsc narrows to `string`, and the
   * deref throws on the old wire. With `?`, that same idiom is `TS18048` and
   * the compiler names the site. */
  route_position?: string | null;
  /** The declared model NAMES the request was routed past, comma-joined by the
   * server, present only alongside a `fallback` position. Names and lanes only
   * (C12) — no endpoint, key or credential rides this wire. Optional for the
   * same reason `route_position` is: absent on a pre-routing-keys wire. */
  fallback_from?: string | null;
}

/** A `redact.pre_egress` frontier-egress redaction pass (mirrors
 * RedactionSpan). `categories` is SHAPE ONLY — category name + count, never a
 * redacted VALUE (C7); it defaults to `{}` server-side, never null. */
export interface TraceRedactionView {
  hits: number | null;
  categories: Record<string, number>;
  task: string | null;
  lane: string | null;
  model_name: string | null;
}

/** A `knowledge.retrieve` retrieval (mirrors KnowledgeSpan). */
export interface TraceKnowledgeView {
  candidates: number | null;
  returned: number | null;
  acl_trim_delta: number | null;
}

/** A `pdp.decide` policy decision (mirrors PdpSpan). */
export interface TracePdpView {
  effect: string | null;
  reason: string | null;
  tier: number | null;
  policy_version: string | null;
  requested_action: string | null;
}

/** An `ApprovalEvent` row (mirrors ApprovalSpan). */
export interface TraceApprovalView {
  approval_id: string | null;
  tier: number | null;
  decision: string | null;
  subject_ref: string | null;
}

/** A `guardrail.block` denial (mirrors GuardrailSpan) — the emitter ships rule
 * names only, never message content. */
export interface TraceGuardrailView {
  detail: string | null;
}

/** A `knowledge.verified_read` counts-only read receipt (mirrors
 * VerifiedReadSpan). `rows: 0` is a real fact — the read matched nothing —
 * and is distinct from `null` (an older record whose detail didn't parse). */
export interface TraceVerifiedReadView {
  rows: number | null;
  row_limit: number | null;
  truncated: boolean | null;
}

/** A `grounding.assist` gate event (mirrors GroundingSpan) — enums/booleans
 * only, never draft text. `phase` is the server's own vocabulary
 * (`"pass-polish"` / `"fail-redraft"`); an unrecognized phase renders the
 * generic form, never an invented verdict. */
export interface TraceGroundingView {
  phase: string | null;
  adopted: boolean | null;
  error_class: string | null;
}

/** A `session.run` terminal record (mirrors SessionSpan) — the conversation
 * record this run belongs to, as a POINTER (id) only; the chain never carries
 * conversation text. */
export interface TraceSessionView {
  agent: string | null;
  department: string | null;
  session_id: string | null;
  succeeded: boolean | null;
}

/**
 * One node in the assembled trace tree (mirrors TraceNodeView). Exactly one
 * of the typed span fields is populated per `kind` (never a pydantic union —
 * see the server module's comment on why); `detail` carries the raw §0-law-5
 * fallback text for an unrecognized `kind` (the server's `"event"` kind).
 * `children` is always an array server-side (default `[]`), never null.
 * Recursive interface — legal TypeScript.
 */
export interface TraceNodeView {
  event_id: string;
  seq: number;
  action: string | null;
  event_type: string;
  actor: string;
  resource: string | null;
  outcome: string | null;
  occurred_at: string;
  kind: string;
  subagent: TraceSubagentSpanView | null;
  route: TraceRouteSpanView | null;
  model_call: TraceModelCallView | null;
  redaction: TraceRedactionView | null;
  knowledge: TraceKnowledgeView | null;
  pdp: TracePdpView | null;
  approval: TraceApprovalView | null;
  guardrail: TraceGuardrailView | null;
  tool: TraceToolView | null;
  verified_read: TraceVerifiedReadView | null;
  grounding: TraceGroundingView | null;
  session: TraceSessionView | null;
  detail: string | null;
  children: TraceNodeView[];
}

/**
 * A `tool.execute` governed execution (mirrors ToolSpan). `approval_id` is the
 * durable approval request id the execution consumed, stamped server-side at
 * the execution site — `null` for records written before the field existed and
 * for ungated tier-0/1 executions, rendered as NO authorised-by line (the
 * console never infers a join the server did not record).
 *
 * `template_id` / `args` / `result_count` are the verified-answer executor's
 * redacted detail lifted into typed fields server-side: template name,
 * argument NAMES only (values never reach the chain), and the row count the
 * caller saw. `result_count: 0` is a real "matched nothing" fact and renders
 * distinctly; `null` (a write tool, an older record, an unparseable emit)
 * renders NO count — absent and zero are different facts.
 */
export interface TraceToolView {
  approval_id: string | null;
  /**
   * OPTIONAL, all four, for the reason catalogue row #19 states as its rule: a
   * server that predates the verified-answer lift OMITS these keys entirely —
   * it does not serve them as null. That is a deployment-skew fact, not a
   * schema fact, so nothing mechanical can derive it; declaring them required
   * made the type vouch for a shape a running server does not always send, and
   * `tsc` had no complaint about an unguarded `tool.args.length` (`TS18048`
   * only fires once the field admits `undefined`). These four were the fields
   * row #19 was DERIVED from — catalogue row #14's own `.args.length` crash
   * path — and they were still violating its rule two interfaces above the one
   * it fixed.
   */
  template_id?: string | null;
  template_version?: number | null;
  args?: string[];
  result_count?: number | null;
}

/** Per-model rollup over every `model_call` node in the tree (mirrors
 * ModelAggregateView). `input_tokens`/`output_tokens` are plain (non-nullable)
 * ints that DEFAULT to 0 when the server cannot vouch for a per-node total —
 * the NE-167a Codex P2 case: the console must render "—" for a 0 total via
 * `formatTokens`, never a fabricated hard zero (a completed model call with a
 * genuine 0-token total cannot exist). */
export interface TraceModelAggregateView {
  model: string;
  lane: string | null;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  /** The distinct tasks this model served in this run, in the server's
   * first-served order — the run's task→model routing at a glance. Empty means
   * no call on this model named its task; it renders absent, never as a blank
   * cell.
   *
   * OPTIONAL for the same reason `VerifiedTemplateWire.label_var` is: an API
   * predating this field omits the key entirely, so during a rolling upgrade
   * the value a reader gets is `undefined` — not `[]`. Declaring it required
   * made the type vouch for a shape the server does not always send, and every
   * consumer that read `tasks.length` unguarded inherited that promise as a
   * `TypeError` that takes the whole Models table down with it. Absent and
   * empty are the same declaration to every renderer: nothing to show. */
  tasks?: string[];
}

/** The run-level rollup: event count, total cost, and wall-clock span
 * (mirrors TraceRollupView). */
export interface TraceRollupView {
  trace_id: string;
  total_events: number;
  total_cost_usd: number | null;
  started_at: string;
  ended_at: string;
  duration_ms: number;
}

/** The server-computed chain-verification verdict for a trace's FULL
 * (unbounded) chain walk — field-for-field IDENTICAL to `ChainVerificationView`
 * by NE-167a's design (the server copies `ne.audit.verify.ChainVerification`
 * verbatim into this shape too). Kept as a distinct alias so trace call sites
 * read naturally without implying a coupling that could later diverge. */
export type TraceVerificationView = ChainVerificationView;

/** `GET /v1/trace/{trace_id}` (and the `read.trace` governed tool) response
 * body — mirrors TraceAssemblyView. 100% server-computed: nesting, durations,
 * aggregates, rollup, and the verification verdict all arrive pre-computed;
 * the console renders this shape VERBATIM (thin-client C6/C7) and never sums,
 * subtracts, hashes, or re-derives any of it. */
export interface TraceAssemblyView {
  trace_id: string;
  rollup: TraceRollupView;
  models: TraceModelAggregateView[];
  nodes: TraceNodeView[];
  verification: TraceVerificationView;
}

// ===========================================================================
// NE-168 governed delegation demo trigger (mirrors ne-api routers/demo.py 1:1)
//
// POST /v1/demo/delegate dispatches the three registered sub-agents — triage
// and reason to `private-vllm`, adjudicate to `frontier-api` where the ne.govern
// `subagent.route` rule POLICY-DENIES it before any model call (with the runtime
// gate as a second, independent denial) — inside ONE run_trace_scope, so every
// leg shares a single trace_id readable back via GET /v1/trace/{trace_id}.
//
// This is the ONLY honest way this console can show "a model router with
// persistent context across model switches": it is real, it is on the wire, and
// there is NO lane-switch endpoint to build a FRONTIER/PRIVATE toggle against.
// The response carries IDENTIFIERS / ENUMS / BOOLS ONLY — never model output.
// ===========================================================================

/** One dispatched sub-agent leg (mirrors DelegateLegView). */
export interface DelegateLegView {
  agent: string;
  route_task: string | null;
  route_lane: string | null;
  tier: string;
  outcome: string;
  succeeded: boolean;
}

/** POST /v1/demo/delegate — the shared run trace_id + per-leg outcomes. */
export interface DelegateRunView {
  trace_id: string;
  legs: DelegateLegView[];
}

// ===========================================================================
// NE-WORKFLOWS (mirrors ne-api routers/workflows.py + ne/api/workflows.py 1:1)
//
// GET  /v1/workflows                  — what this deployment offers.
// POST /v1/workflows/{id}/run         — run one, end to end, under ONE trace id.
//
// Both are ops-gated (`_require_ops`) and 404 when the feature is off, exactly
// like /v1/demo/delegate above. The run response carries IDENTIFIERS, COUNTS,
// ENUMS and BOOLS only — never a note body, an account name, or model output
// (C12); the approver reads the note itself through the approvals disclosure
// route, which is the one audited surface for that material.
//
// OPTIONALITY IS DECIDED BY THE SERVER'S OWN DEFAULTS, not by what today's
// deployment happens to send (vacuous-guards §4 row 19). A pydantic field that
// carries a DEFAULT is a field the model can gain or lose without breaking its
// own contract, so a console that is newer or older than its API may meet the
// key ABSENT — those are `?` here, and `tsc` then refuses every unguarded
// dereference. A field with NO default is structural and is required here.
// `| null` would NOT do this job: `x !== null && x.foo` compiles and throws.
// ===========================================================================

/** One governed step of a run (mirrors StepView). `detail` is metadata only. */
export interface WorkflowStepView {
  step: number;
  kind: string;
  tool?: string | null;
  outcome: string;
  detail?: Record<string, string | number | boolean>;
}

/** One drafted write and what became of it (mirrors ProposalView). */
export interface WorkflowProposalView {
  subject_ref: string;
  crm_record_id?: string | null;
  /** The parked NE-035 approval this proposal became — the link to /approvals. */
  approval_request_id?: string | null;
  outcome: string;
  reason?: string;
  body_chars?: number;
  evidence_refs?: string[];
}

/** One server-attributed finding from a deterministic Meridian workflow. */
export interface WorkflowFindingView {
  subject_ref: string;
  related_ref?: string | null;
  outcome: string;
  evidence_refs?: string[];
  detail?: Record<string, string | number | boolean>;
}

/** The typed read-only answer carried by Meridian deterministic runs. */
export interface WorkflowResultView {
  outcome: string;
  summary: string;
  findings?: WorkflowFindingView[];
  missing_fields?: string[];
  policy_artifact_id?: string | null;
  policy_version?: string | null;
  control_outcome?: string | null;
  control_amount_cents?: number | null;
  control_evidence_refs?: string[];
}

/** POST /v1/workflows/{id}/run — one trace id, the ordered steps, the parked
 * proposals, the three counts, and any deterministic answer the run reports. */
export interface WorkflowRunView {
  /** 015A0-ENFORCEMENT. Anything but `available` withholds every source-derived
   * part of this run: steps, proposals, counts and the typed result. */
  disclosure_status?: SourceDisclosureStatusWire;
  workflow_id: string;
  trace_id: string;
  steps?: WorkflowStepView[];
  proposals?: WorkflowProposalView[];
  read_row_count?: number;
  subjects_considered?: number;
  subjects_selected?: number;
  /** Absent on older servers and null on workflows without a typed result. */
  result?: WorkflowResultView | null;
}

/** One model-planned governed tool call. The stated intent and served identities
 * are persisted by the server; the console derives neither. */
export interface AgenticWorkflowStepView {
  step: number;
  tool: string;
  stated_intent: string;
  outcome: string;
  served_row_ids?: string[];
  evidence_refs?: string[];
  principal_id: string;
}

export interface AgenticWorkflowBudgetView {
  max_tool_calls: number;
  token_budget: number;
  wall_clock_seconds: number;
  max_parked_writes: number;
  requests_used?: number | null;
  tool_calls_used?: number | null;
  tokens_used?: number | null;
  exhausted_cap?: string | null;
}

/** POST /v1/agentic-workflows/{id}/run. `model_planned: true` is server
 * provenance for this endpoint; deterministic results use WorkflowRunView. */
export interface AgenticWorkflowRunView {
  /** 015A0-ENFORCEMENT. Anything but `available` withholds the answer, the
   * stated intents and the served evidence identities. */
  disclosure_status?: SourceDisclosureStatusWire;
  run_id: string;
  workflow_id: string;
  trace_id: string;
  principal_id: string;
  outcome: "complete" | "context-exhausted" | "budget-exhausted";
  model_planned: true;
  answer?: string | null;
  steps?: AgenticWorkflowStepView[];
  served_row_ids?: string[];
  budgets: AgenticWorkflowBudgetView;
}

/** Server-owned background run ticket. A running/failed ticket has no result;
 * only a terminal model-planned result may populate it. */
export interface AgenticWorkflowRunStatusView {
  run_id: string;
  workflow_id: string;
  trace_id: string;
  principal_id: string;
  status: "running" | "complete" | "context-exhausted" | "budget-exhausted" | "failed";
  result?: AgenticWorkflowRunView | null;
  error_class?: string | null;
}

export interface WorkflowModeView {
  mode: "deterministic" | "agentic";
  status: "available" | "pending";
  endpoint?: string | null;
  reason?: string;
}

/** GET /v1/workflows — one offered workflow. `reads`/`writes` are the governed
 * tool names this workflow is permitted to call: the permission contract, which
 * this console renders BEFORE anything runs. */
export interface WorkflowView {
  workflow_id: string;
  title: string;
  description: string;
  reads: string[];
  writes: string[];
  /** Added by Meridian BD-5. Absent on an older server wire. */
  modes?: WorkflowModeView[];
}
