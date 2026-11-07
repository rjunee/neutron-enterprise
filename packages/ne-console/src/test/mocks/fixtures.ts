// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The ONLY corpus in this unit. It embodies "the server already trimmed": the
// mock knows about THREE documents including `doc-secret` (sentinel text
// EYES-ONLY-SENTINEL), but the trimmed API responses NEVER surface doc-secret —
// search returns 2 of 3, and retrieve returns nothing for doc-secret's chunk.
// The console has no way to reach doc-secret; that is the C7 proof.

import type {
  ApprovalPayloadView,
  AuditRecordView,
  ChainVerificationView,
  Citation,
  ConnectQuarantineView,
  ConnectStatusView,
  EvalRunView,
  KpiEntryView,
  KpiSummaryView,
  PendingApprovalView,
  PostureView,
  PrincipalView,
  ReplayResultView,
  RetrievalGraphPathWire,
  ReviewEventView,
  ScoredChunk,
  SkillLibraryRow,
  StewardDigestView,
  StewardLatencyView,
  StewardProposalView,
  WorkflowView,
} from "../../api/types";

export const SENTINEL = "EYES-ONLY-SENTINEL";

/** NE-CONSOLE-ISSUER: the runtime OIDC config `GET /config.json` returns. The
 * issuer mirrors the ne-api conftest ISSUER for cross-package symmetry. */
export const RUNTIME_CONFIG = {
  issuer: "https://idp.ne.test/realms/ne-test",
  client_id: "ne-console",
  audience: "ne-console",
  api_base: "/api",
};

export const PRINCIPAL: PrincipalView = {
  id: "alice@example.com",
  groups: ["engineering"],
};

/** Two DIFFERENT, disjoint-and-unequal entitlement sets — the subset guard
 * needs at least two such principals so "renders the entitled subset" is
 * proven against two distinct answers, not merely "some subset of the full
 * set". Neither is a superset of the other and neither is empty (an
 * entitled-to-everything principal would make filtering untestable) or the
 * full/default PRINCIPAL above (which carries no `entitlements` field at all
 * and stays the unknown-deployment control). */
export const PRINCIPAL_APPROVER: PrincipalView = {
  id: "amber@example.com",
  groups: ["approvers"],
  entitlements: ["approvals"],
};

export const PRINCIPAL_OPS_STEWARD: PrincipalView = {
  id: "oscar@example.com",
  groups: ["platform"],
  entitlements: ["ops", "steward"],
};

/** Full internal corpus — doc-secret must NEVER leave the trimmed endpoints. */
export const FULL_CORPUS: ScoredChunk[] = [
  {
    chunk: {
      chunk_id: "chunk-alpha",
      document_id: "doc-alpha",
      text: "Alpha handbook: onboarding checklist for new engineers.",
      metadata: { title: "Alpha Handbook" },
    },
    score: 0.91,
  },
  {
    chunk: {
      chunk_id: "chunk-bravo",
      document_id: "doc-bravo",
      text: "Bravo runbook: how to rotate service credentials safely.",
      metadata: { title: "Bravo Runbook" },
    },
    score: 0.82,
  },
  {
    chunk: {
      chunk_id: "chunk-secret",
      document_id: "doc-secret",
      text: `Executive comp plan — ${SENTINEL} — do not distribute.`,
      metadata: { title: "Secret" },
    },
    score: 0.99,
  },
];

/** What the C7-trimmed /brain/search returns for this principal: 2 of 3. */
export const TRIMMED_RESULTS: ScoredChunk[] = FULL_CORPUS.filter(
  (s) => s.chunk.document_id !== "doc-secret",
);

export const TRIMMED_CITATIONS: Citation[] = TRIMMED_RESULTS.map((s) => ({
  chunk_id: s.chunk.chunk_id,
  document_id: s.chunk.document_id,
  source_system: "sharepoint",
  source_ref: `ref/${s.chunk.document_id}`,
  source_url: `https://sharepoint.example.com/${s.chunk.document_id}`,
}));

/** Citation the chat stream attaches — points at a permitted chunk. */
export const CHAT_CITATION: Citation = {
  chunk_id: "chunk-alpha",
  document_id: "doc-alpha",
  source_system: "sharepoint",
  source_ref: "ref/doc-alpha",
  source_url: "https://sharepoint.example.com/doc-alpha",
};

/** Citation the chat stream could attach for a FORBIDDEN chunk — retrieve()
 * must return empty for it so the EvidencePanel renders access-denied. */
export const SECRET_CITATION: Citation = {
  chunk_id: "chunk-secret",
  document_id: "doc-secret",
  source_system: "sharepoint",
  source_ref: "ref/doc-secret",
  source_url: "https://sharepoint.example.com/doc-secret",
};

/** chunk_ids the trimmed /brain/retrieve will honor. doc-secret is excluded. */
export const PERMITTED_CHUNK_IDS = new Set(["chunk-alpha", "chunk-bravo"]);

// ===========================================================================
// NE-T2X-5 (design §6.1) — Tier-2 graph-fact fixtures for the search response.
//
// One all-"source" hop (the control: no chip, no banner contribution) and one
// "extracted" hop whose `source_ref` follows the writer-stamped
// `llm:<sha16>:<ref>` grammar, so the search-page chip's confirm link can be
// asserted end-to-end against a known sha16.
// ===========================================================================

/** The sha16 the "extracted" hop's `source_ref` derives — the fixture's
 * flywheel deep-link target (`/flywheel?tab=queue&proposal=<this>`). */
export const EXTRACTED_HOP_SHA16 = "0123456789abcdef";

/** The sha16 an "extracted" ENDPOINT's own `source_ref` derives
 * (NE-T2X-5-ENDPOINT-EVIDENCE-REF) — deliberately DISTINCT from
 * {@link EXTRACTED_HOP_SHA16} so a test can prove which ref reached the chip. */
export const ENDPOINT_EXTRACTED_SHA16 = "fedcba9876543210";

export const GRAPH_PATHS_FIXTURE: RetrievalGraphPathWire[] = [
  {
    hops: [
      {
        subject: {
          entity_class: "Account",
          entity_key: "acct-acme",
          display: "Acme Corp",
          assurance: "source",
          source_ref: "crm/acct-acme",
        },
        edge_kind: "WORKS_AT",
        obj: {
          entity_class: "Person",
          entity_key: "person-dana",
          display: "Dana Ops",
          assurance: "source",
          source_ref: "hr/person-dana",
        },
        source_system: "workday",
        source_ref: "hr/emp-1",
        assurance: "source",
      },
    ],
  },
  {
    // An extracted RELATION between two already-backbone entities (design
    // §3.3/A-P1-4): only the asserting edge is "extracted" — both endpoints
    // stay "source" — so exactly ONE chip renders for this hop.
    hops: [
      {
        subject: {
          entity_class: "Account",
          entity_key: "acct-acme",
          display: "Acme Corp",
          assurance: "source",
          source_ref: "crm/acct-acme",
        },
        edge_kind: "ACQUIRED",
        obj: {
          entity_class: "Account",
          entity_key: "acct-globex",
          display: "Globex Inc",
          assurance: "source",
          source_ref: "crm/acct-globex",
        },
        source_system: "ne:llm",
        source_ref: `llm:${EXTRACTED_HOP_SHA16}:doc-alpha#chunk-alpha`,
        assurance: "extracted",
      },
    ],
  },
];

// ===========================================================================
// NE-062 ops-surface fixtures
//
// FIXED epoch for deterministic SLA/expiry timer rendering. Tests pin the clock
// with vi.setSystemTime(new Date(NOW_ISO)); every timestamp below is expressed
// as an offset from this instant so the countdowns are stable.
// ===========================================================================
export const NOW_ISO = "2026-07-07T12:00:00Z";

const NOW_MS = Date.parse(NOW_ISO);
const iso = (deltaMs: number): string => new Date(NOW_MS + deltaMs).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/** Pending Tier-2/3 approvals as the DurableApprovalGate read-model exposes them.
 * These carry NO payload/target (the gate redacts it) — the UI derives "target"
 * from action + request_id. */
export const PENDING_APPROVALS: PendingApprovalView[] = [
  {
    request_id: "req-t2-fresh",
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales-drafts",
    requested_at: iso(-10 * MIN),
    sla_deadline: iso(50 * MIN),
    expires_at: iso(24 * HOUR),
    escalated: false,
  },
  {
    request_id: "req-t2-overdue",
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales-drafts",
    requested_at: iso(-65 * MIN),
    sla_deadline: iso(-5 * MIN),
    expires_at: iso(24 * HOUR),
    escalated: true,
  },
  {
    request_id: "req-t3-wire",
    action: "payments.wire_transfer",
    tier: 3,
    principal_id: "agent://finance-ops",
    requested_at: iso(-20 * MIN),
    sla_deadline: iso(2 * HOUR),
    expires_at: iso(24 * HOUR),
    escalated: false,
  },
  {
    // NE-T2X-5 (design §6.3): an escalated Tier-2 approval whose context
    // included two unconfirmed LLM-extracted facts — the ApprovalsPage
    // provisional placard fixture. One ref is parseable (deep-links to the
    // flywheel queue); the other is a near-miss shape to prove the placard
    // renders it as plain text with no link.
    request_id: "req-t2-provisional",
    action: "write.internal.notes.append",
    tier: 2,
    principal_id: "agent://research-assist",
    requested_at: iso(-5 * MIN),
    sla_deadline: iso(55 * MIN),
    expires_at: iso(24 * HOUR),
    escalated: false,
    provisional_source_count: 2,
    provisional_source_refs: [
      `llm:${EXTRACTED_HOP_SHA16}:doc-alpha#chunk-alpha`,
      "not-a-parseable-ref",
    ],
  },
];

/** Detectable probe values for the NE-APPROVAL-PAYLOAD disclosure tests —
 * these must appear when a named approver reveals the wire-transfer payload,
 * and NOWHERE when a non-named approver's read is withheld (control). */
export const MATERIAL_AMOUNT = "250,000.00";
export const MATERIAL_COUNTERPARTY = "ACME GLOBAL SUPPLY LTD";

/** GET /v1/approvals/{id}/payload fixtures, keyed by request_id (owner ruling
 * 2026-07-31, NE-APPROVAL-PAYLOAD). `req-t3-wire` is the Tier-3 wire-transfer
 * disclosure carrying the probe values; `req-t2-fresh` is a legacy row with
 * no recorded material parameters. `req-payload-withheld` has no fixture here
 * — the handler 403s it (a non-named approver's request never reaches a
 * disclosure body). */
export const APPROVAL_PAYLOADS: Record<string, ApprovalPayloadView> = {
  "req-t3-wire": {
    // 015A0-ENFORCEMENT: the gate states its disclosure decision EXPLICITLY.
    // The console fails closed on a body that does not, so a fixture without
    // this field would be asserting the pre-ENFORCEMENT wire.
    disclosure_status: "available",
    request_id: "req-t3-wire",
    action: "payments.wire_transfer",
    tier: 3,
    principal_id: "agent://finance-ops",
    target_resource: "account:ops-payables",
    description: "Wire transfer to a named counterparty.",
    material_parameters: { amount: MATERIAL_AMOUNT, counterparty: MATERIAL_COUNTERPARTY },
    reversibility: "irreversible — financial/legal action class (SPEC §4.2 Tier-3)",
  },
  "req-t2-fresh": {
    disclosure_status: "available",
    request_id: "req-t2-fresh",
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales-drafts",
    target_resource: "tool:crm.create_note",
    description: "Create a CRM note.",
    material_parameters: null,
    reversibility:
      "external-facing / PII-touching — reversal may require external remediation (SPEC §4.2 Tier-2)",
  },
};

/** Three audit rows sharing one trace, with realistic 64-hex hashes. The hash
 * fields are display-only; the client never compares them. */
export const AUDIT_EVENTS: AuditRecordView[] = [
  {
    id: 1,
    seq: 1,
    event_id: "evt-1",
    event_type: "tool.invoke",
    config_hash: "a".repeat(64),
    actor: "agent://sales-drafts",
    occurred_at: iso(-30 * MIN),
    kpi_tag: null,
    trace_id: "trace-1",
    action: "tool.invoke",
    resource: "crm.create_note",
    outcome: "success",
    payload: { tool: "crm.create_note" },
    prev_hash: "0".repeat(64),
    record_hash: "b".repeat(64),
  },
  {
    id: 2,
    seq: 2,
    event_id: "evt-2",
    event_type: "approval.request",
    config_hash: "a".repeat(64),
    actor: "agent://finance-ops",
    occurred_at: iso(-20 * MIN),
    kpi_tag: "approval_latency_p50_s",
    trace_id: "trace-1",
    action: "approval.request",
    resource: "payments.wire_transfer",
    outcome: "pending",
    payload: { tier: 3 },
    prev_hash: "b".repeat(64),
    record_hash: "c".repeat(64),
  },
  {
    id: 3,
    seq: 3,
    event_id: "evt-3",
    event_type: "approval.decide",
    config_hash: "a".repeat(64),
    actor: "user:alice@example.com",
    occurred_at: iso(-10 * MIN),
    kpi_tag: null,
    trace_id: "trace-1",
    action: "approval.decide",
    resource: "payments.wire_transfer",
    outcome: "denied",
    payload: { decision: "rejected" },
    prev_hash: "c".repeat(64),
    record_hash: "d".repeat(64),
  },
];

/** The server's VERIFIED verdict for an intact chain. */
export const VERIFICATION_OK: ChainVerificationView = {
  valid: true,
  basis: "full-chain",
  contradiction: false,
  checked: 3,
  first_break_seq: null,
  first_break_event_id: null,
  reason: null,
  detail: "chain intact: 3 record(s) verified",
};

/** The server's BROKEN verdict — a tampered chain the UI must render prominently. */
export const VERIFICATION_BROKEN: ChainVerificationView = {
  valid: false,
  basis: "full-chain",
  contradiction: false,
  checked: 2,
  first_break_seq: 3,
  first_break_event_id: "evt-3",
  reason: "record_hash mismatch",
  detail: "chain broken at seq 3 (event_id=evt-3): record_hash mismatch",
};

/** Server replay of trace-1: the ordered events + config_hashes + context_ids. */
export const REPLAY_RESULT: ReplayResultView = {
  events: AUDIT_EVENTS,
  config_hashes: ["a".repeat(64)],
  context_ids: ["chunk-alpha", "chunk-bravo"],
};

/** Eval runs. run-fail-server-verdict has metrics that NUMERICALLY beat their
 * thresholds but passed:false — proving the UI renders the SERVER verdict and
 * never recomputes pass/fail from the numbers. */
export const EVAL_RUNS: EvalRunView[] = [
  {
    run_id: "run-pass",
    suite: "retrieval-baseline",
    config_hash: "e".repeat(64),
    golden_set_hash: "f".repeat(64),
    judge_model_id: "claude-opus-4",
    judge_family: "claude",
    rubric_versions: { faithfulness: "v3" },
    // permission is the §5 "permission 100%" gauge: value 1.0 meets threshold 1.0.
    metrics: { recall_at_10: 0.98, faithfulness: 1.0, permission: 1.0 },
    thresholds: { recall_at_10: 0.85, faithfulness: 0.8, permission: 1.0 },
    statuses: { recall_at_10: "pass", faithfulness: "pass", permission: "pass" },
    judge_error_rate: 0.0,
    passed: true,
    created_at: iso(-2 * DAY),
  },
  {
    run_id: "run-fail-server-verdict",
    suite: "safety-redteam",
    config_hash: "1".repeat(64),
    golden_set_hash: "2".repeat(64),
    judge_model_id: "claude-opus-4",
    judge_family: "claude",
    rubric_versions: { faithfulness: "v3" },
    metrics: { recall_at_10: 0.99, faithfulness: 0.95 },
    thresholds: { recall_at_10: 0.85, faithfulness: 0.8 },
    statuses: { recall_at_10: "pass", faithfulness: "fail" },
    judge_error_rate: 0.12,
    passed: false,
    created_at: iso(-1 * DAY),
  },
];

/** A mixed audit window for the PDP-DECISIONS narrowing (NE-139 §4.9d): two
 * tool.* rows + one api.*.unauthorized row are PDP outcomes; the approval.* and
 * brain.* rows are not. Chained placeholder hashes (display-only). */
export const AUDIT_EVENTS_PDP_MIX: AuditRecordView[] = [
  {
    id: 101,
    seq: 1,
    event_id: "evt-pdp-1",
    event_type: "tool.execute",
    config_hash: "a".repeat(64),
    actor: "agent://sales-drafts",
    occurred_at: iso(-50 * MIN),
    kpi_tag: null,
    trace_id: "trace-pdp",
    action: "tool.execute",
    resource: "crm.create_note",
    outcome: "allow",
    payload: {},
    prev_hash: "0".repeat(64),
    record_hash: "a".repeat(64),
  },
  {
    id: 102,
    seq: 2,
    event_id: "evt-pdp-2",
    event_type: "approval.request",
    config_hash: "a".repeat(64),
    actor: "agent://finance-ops",
    occurred_at: iso(-40 * MIN),
    kpi_tag: null,
    trace_id: "trace-pdp",
    action: "approval.request",
    resource: "payments.wire_transfer",
    outcome: "pending",
    payload: {},
    prev_hash: "a".repeat(64),
    record_hash: "b".repeat(64),
  },
  {
    id: 103,
    seq: 3,
    event_id: "evt-pdp-3",
    event_type: "api.request",
    config_hash: "a".repeat(64),
    actor: "user:mallory@example.com",
    occurred_at: iso(-30 * MIN),
    kpi_tag: null,
    trace_id: "trace-pdp",
    action: "api.chat.unauthorized",
    resource: "chat.completion",
    outcome: "deny",
    payload: {},
    prev_hash: "b".repeat(64),
    record_hash: "c".repeat(64),
  },
  {
    id: 104,
    seq: 4,
    event_id: "evt-pdp-4",
    event_type: "brain.search",
    config_hash: "a".repeat(64),
    actor: "agent://sales-drafts",
    occurred_at: iso(-20 * MIN),
    kpi_tag: null,
    trace_id: "trace-pdp",
    action: "brain.search",
    resource: "knowledge",
    outcome: "success",
    payload: {},
    prev_hash: "c".repeat(64),
    record_hash: "d".repeat(64),
  },
  {
    id: 105,
    seq: 5,
    event_id: "evt-pdp-5",
    event_type: "tool.toolset_denied",
    config_hash: "a".repeat(64),
    actor: "agent://sales-drafts",
    occurred_at: iso(-10 * MIN),
    kpi_tag: null,
    trace_id: "trace-pdp",
    action: "tool.toolset_denied",
    resource: "crm.delete_record",
    outcome: "deny",
    payload: {},
    prev_hash: "d".repeat(64),
    record_hash: "e".repeat(64),
  },
];

/** The server's VERIFIED verdict for the PDP-mix window (intact, 5 records). */
export const VERIFICATION_PDP_MIX: ChainVerificationView = {
  valid: true,
  basis: "full-chain",
  contradiction: false,
  checked: 5,
  first_break_seq: null,
  first_break_event_id: null,
  reason: null,
  detail: "chain intact: 5 record(s) verified",
};

// ===========================================================================
// NE-067 steward-surface fixtures
//
// These model the reality NE-066 documents: `artifact.patch` / `pr_body` are
// server-SANITIZED (already routed through diffs.py's inline/fenced/comment
// sanitizers) but are still PLAIN TEXT — the console must display them INERT
// (as React text nodes inside <pre>), never re-interpret them as markdown/HTML.
// INERT_ATTACK models a proposal-carried string that, if ever fed to
// dangerouslySetInnerHTML or a markdown parser, would execute/render as live
// HTML — the queue tests assert it never does.
// ===========================================================================

export const INERT_ATTACK =
  '<img src="x" onerror="window.__ne_pwned = true"><script>window.__ne_pwned = true</script>\n# not-a-heading\n**not-bold** [not-a-link](https://evil.example)';

const CONTENT_PATCH = `--- /dev/null
+++ b/proposals/content/8f19c2a41b0d7e33.md
@@ -0,0 +1,6 @@
+# Proposal review 8f19c2a41b0d7e33
+
+- artifact: 8f19c2a41b0d7e33
+- lane: content
+- cluster_id: cluster-content-1
+${INERT_ATTACK}
`;

const CONTENT_PR_BODY = `# [flywheel:content] proposal 8f19c2a41b0d7e33

- artifact: 8f19c2a41b0d7e33
- lane: content

## Summary (proposal-carried, untrusted)

\`\`\`
The chatbot answered with outdated pricing.
${INERT_ATTACK}
\`\`\`

## Proposed change (proposals/content/8f19c2a41b0d7e33.md)

\`\`\`diff
${CONTENT_PATCH}
\`\`\`
`;

const POLICY_PATCH = `--- /dev/null
+++ b/policies/proposals/2b6a9d0c1e4f7788_review.rego
@@ -0,0 +1,4 @@
+# OPA policy-diff proposal 2b6a9d0c1e4f7788
+#
+# proposal_id: prop-policy-2
+# lane: policy
`;

// The entity-merge-candidate artifact. Its `patch` is a git unified diff of the
// NE-066 `_review_document` (a new .md file), so every body line is `+`-prefixed
// — INCLUDING the `_entity_merge_lines` block (diffs.py:288-310). The block is
// reproduced VERBATIM in the renderer's format so EntityMergeCompare parses the
// real live shape (the parser strips the leading diff `+`). One display value
// carries a hostile-but-SANITIZED string — exactly what `_safe` emits (single
// line, markdown/HTML backslash-escaped) — to prove text-node rendering: it must
// appear as literal text, never as an <img>/<script> element.
const ER_HOSTILE_DISPLAY = "Acme \\<img src=x onerror=alert\\(1\\)\\> Holdings";

const ENTITY_MERGE_PATCH = `--- /dev/null
+++ b/proposals/entity_resolution/prop-er-4-merge.md
@@ -0,0 +1,20 @@
+# Proposal review d4e5f6a7b8c9d0e1
+
+- artifact: d4e5f6a7b8c9d0e1
+- lane: entity_resolution
+- cluster_id: cluster-er-4
+
+## Summary (proposal-carried, untrusted)
+
+\`\`\`
+Splink flagged two Account entities as the same organization.
+\`\`\`
+
+## Entity-merge candidate
+
+- left entity_class: cco:CommercialOrganization
+- left entity_key: account:acme-001
+- left display: ACME Corporation
+- right entity_class: cco:CommercialOrganization
+- right entity_key: account:acme-holdings-002
+- right display: ${ER_HOSTILE_DISPLAY}
+- match_probability: 0.94
+- match_weight: 8.7
+- threshold: 0.9
+- blocking_rule: l.postal_code = r.postal_code
+- model config_hash: cfg-er-1
`;

/** A 4th proposal on the JOB-EMITTED entity_resolution lane: a Splink ER
 * merge candidate. Its artifact carries the deterministic side-by-side block
 * EntityMergeCompare extracts. Kept OUT of the default STEWARD_PROPOSALS list so
 * the migrated queue tests keep their exact counts; suites that want it push it
 * via a server.use() override (or use STEWARD_PROPOSALS_WITH_ER). */
export const STEWARD_PROPOSAL_ENTITY_MERGE: StewardProposalView = {
  disclosure_status: "available",
  proposal_id: "prop-er-4",
  lane: "entity_resolution",
  kind: "entity_merge_candidate",
  status: "proposed",
  cluster_id: "cluster-er-4",
  confidence: 0.87,
  priority: "medium",
  evidence_event_ids: ["evt-401", "evt-402"],
  evidence_signal_ids: [41],
  config_hash: "cfg-er-1",
  created_at: "2026-07-06T20:00:00Z",
  artifact: {
    proposal_id: "prop-er-4",
    lane: "entity_resolution",
    kind: "git_unified_diff",
    path: "proposals/entity_resolution/prop-er-4-merge.md",
    title: "[flywheel:entity_resolution] proposal d4e5f6a7b8c9d0e1",
    patch: ENTITY_MERGE_PATCH,
    pr_body:
      "# [flywheel:entity_resolution] proposal d4e5f6a7b8c9d0e1\n\nEntity-merge candidate review.",
  },
};

/** Non-terminal fix proposals as GET /v1/steward/proposals returns them. */
export const STEWARD_PROPOSALS: StewardProposalView[] = [
  {
    disclosure_status: "available",
    proposal_id: "prop-content-1",
    lane: "content",
    kind: "fix",
    status: "proposed",
    cluster_id: "cluster-content-1",
    confidence: 0.82,
    priority: "high",
    evidence_event_ids: ["evt-101", "evt-102"],
    evidence_signal_ids: [11, 12, 13],
    config_hash: "cfg-1",
    created_at: "2026-07-06T09:00:00Z",
    artifact: {
      proposal_id: "prop-content-1",
      lane: "content",
      kind: "git_unified_diff",
      path: "proposals/content/8f19c2a41b0d7e33.md",
      title: "[flywheel:content] proposal 8f19c2a41b0d7e33",
      patch: CONTENT_PATCH,
      pr_body: CONTENT_PR_BODY,
    },
  },
  {
    disclosure_status: "available",
    proposal_id: "prop-policy-2",
    lane: "policy",
    kind: "fix",
    status: "approved",
    cluster_id: "cluster-policy-2",
    confidence: 0.9,
    priority: "high",
    evidence_event_ids: ["evt-201"],
    evidence_signal_ids: [21],
    config_hash: "cfg-1",
    created_at: "2026-07-05T14:30:00Z",
    artifact: {
      proposal_id: "prop-policy-2",
      lane: "policy",
      kind: "opa_policy_diff",
      path: "policies/proposals/2b6a9d0c1e4f7788_review.rego",
      title: "[flywheel:policy] proposal 2b6a9d0c1e4f7788",
      patch: POLICY_PATCH,
      pr_body: "OPA policy-diff proposal 2b6a9d0c1e4f7788 — review stub.",
    },
  },
  {
    disclosure_status: "available",
    proposal_id: "prop-retrieval-3",
    lane: "retrieval",
    kind: "golden_set_candidate",
    status: "proposed",
    cluster_id: "cluster-retrieval-3",
    confidence: 0.6,
    priority: "medium",
    evidence_event_ids: ["evt-301", "evt-302"],
    evidence_signal_ids: [31, 32],
    config_hash: "cfg-1",
    created_at: "2026-07-06T18:15:00Z",
    artifact: {
      proposal_id: "prop-retrieval-3",
      lane: "retrieval",
      kind: "git_unified_diff",
      path: "proposals/retrieval/9c31f0a2b4d5e677.md",
      title: "[flywheel:retrieval] proposal 9c31f0a2b4d5e677",
      patch: `--- /dev/null
+++ b/proposals/retrieval/9c31f0a2b4d5e677.md
@@ -0,0 +1,3 @@
+# Proposal review 9c31f0a2b4d5e677
+
+- lane: retrieval
`,
      pr_body: "# [flywheel:retrieval] proposal 9c31f0a2b4d5e677\n\nGolden-set candidate review.",
    },
  },
];

/** An APPROVED forged skill candidate (NE-SKILL-1b) — the Apply-skill flow. The
 * body carries an INERT_ATTACK-style string so a test proves it renders inert. */
export const STEWARD_PROPOSAL_SKILL: StewardProposalView = {
  disclosure_status: "available",
  proposal_id: "prop-skill-9",
  lane: "skills",
  kind: "skill_candidate",
  status: "approved",
  cluster_id: "sig9:r0",
  confidence: 0.5,
  priority: "low",
  evidence_event_ids: ["evt-901"],
  evidence_signal_ids: [],
  config_hash: "cfg-1",
  created_at: "2026-07-07T08:00:00Z",
  artifact: {
    proposal_id: "prop-skill-9",
    lane: "skills",
    kind: "git_unified_diff",
    path: "proposals/skills/prop-skill-9.md",
    title: "[flywheel:skills] proposal sig9",
    patch: "--- /dev/null\n+++ b/proposals/skills/prop-skill-9.md\n@@ -0,0 +1,1 @@\n+skill\n",
    pr_body: "# [flywheel:skills] proposal sig9\n\nForged skill candidate review.",
  },
  skill: {
    name: "deploy-runbook",
    version: 1,
    description: "Repeat the standard deploy-and-verify workflow",
    triggers: ["deploy the service", "roll out a release"],
    suggested_acl_labels: ["grp:platform"],
    provenance_line: "forged from session sess-1 on 2026-07-07",
    body: `## When to Use\n${INERT_ATTACK}\n## Procedure\nsteps\n## Pitfalls\nx\n## Verification\nx\n`,
  },
};

/** The full queue INCLUDING the entity_resolution merge candidate — for suites
 * exercising the side-by-side extractor through the real list path. */
export const STEWARD_PROPOSALS_WITH_ER: StewardProposalView[] = [
  ...STEWARD_PROPOSALS,
  STEWARD_PROPOSAL_ENTITY_MERGE,
];

/** Server-computed weekly digest (GET /v1/steward/digest). Includes the fifth
 * entity_resolution lane row so the digest panel proves it renders all lanes. */
export const STEWARD_DIGEST: StewardDigestView = {
  week_start: "2026-06-29",
  week_end: "2026-07-05",
  generated_at: NOW_ISO,
  totals: { proposed: 8, approved: 4, rejected: 2, applied: 3 },
  by_lane: [
    { lane: "content", proposed: 3, approved: 2, rejected: 1, applied: 2 },
    { lane: "retrieval", proposed: 2, approved: 1, rejected: 1, applied: 0 },
    { lane: "schema", proposed: 1, approved: 1, rejected: 0, applied: 1 },
    { lane: "policy", proposed: 1, approved: 0, rejected: 0, applied: 0 },
    { lane: "entity_resolution", proposed: 1, approved: 0, rejected: 0, applied: 0 },
  ],
};

/** Server-computed steward-latency health metric (GET /v1/steward/latency). */
export const STEWARD_LATENCY: StewardLatencyView = {
  median_wait_s: 5400,
  p90_wait_s: 86400,
  decided_count: 9,
  pending_count: 3,
  oldest_pending_age_s: 259200,
  generated_at: NOW_ISO,
};

/** Server-computed skill-library list (GET /v1/steward/skills, NE-SKILL-1a). A
 * hostile description proves the console renders skill text INERT. Includes an
 * active bundled pack + a quarantined-pending-forge (version-less) manual pack. */
export const STEWARD_SKILLS: SkillLibraryRow[] = [
  {
    name: "quarterly-close",
    source: "bundled",
    status: "active",
    current_version: 2,
    description: `Close the books ${SENTINEL} <img src=x onerror=alert(1)>`,
    acl_labels: ["grp:finance"],
    fired_count: 12,
    last_fired_at: NOW_ISO,
    tampered: false,
  },
  {
    name: "draft-skill",
    source: "manual",
    status: "quarantined",
    current_version: null,
    description: "A hand-dropped pack awaiting the forge gate.",
    acl_labels: [],
    fired_count: 0,
    last_fired_at: null,
    tampered: false,
  },
];

/** Build a ReviewEventView response, e.g. for MSW handlers echoing a decision. */
export function reviewEvent(overrides: Partial<ReviewEventView> = {}): ReviewEventView {
  return {
    review_id: "rev-1",
    proposal_id: "prop-content-1",
    action: "approve",
    from_status: "proposed",
    to_status: "approved",
    actor: "alice@example.com",
    surface: "console",
    session_id: "sess-1",
    rejection_code: null,
    note: null,
    applied_ref: null,
    config_hash: "cfg-1",
    created_at: NOW_ISO,
    ...overrides,
  };
}

// ===========================================================================
// NE-131 sovereignty-posture fixtures
//
// Mirrors the real GET /v1/posture shape: 12 constraints C1-C12 — all
// attested:true / mode:"static". C10 single-tenant was retired 2026-07-17, then
// re-purposed 2026-07-18 for audit-log integrity (NE-AUDIT-CRON). A guardian-less
// constraint would render honestly attested:false / mode:"manual" — none currently
// are; the hollow/MANUAL rendering path is covered by a synthetic-constraint test
// in PosturePage.test.tsx.
// Also the EFFECTIVE AIR_GAP + egress state, composed model lanes, and the frozen
// config_hash + its audit-derived history. POSTURE is air-gapped (allowlist
// empty); POSTURE_NO_AIRGAP is the sanctioned-egress variant for panel tests.
// ===========================================================================

const CONSTRAINT_SEED: Array<[string, string, string]> = [
  ["C1", "SELF-HOSTED", "No egress: sealed-stack air-gap proof"],
  ["C2", "SEALED EGRESS", "No egress: per-package egress guardians"],
  ["C3", "MODEL-INDEPENDENT", "Import boundaries: lint-imports contracts enforced in CI"],
  ["C4", "LICENSE GATE", "License gate (Python + npm) enforced in supply-chain CI"],
  ["C5", "SWAPPABLE SLOTS", "Swap-parity: dual-backend + Cedar/Rego conformance"],
  ["C6", "PDP CHOKEPOINT", "Governed executor: no-bypass + PDP audit coupling"],
  ["C7", "TRIMMED KNOWLEDGE", "Provable ACL trimming: vector + graph retrievers"],
  ["C8", "KPI-FIRST", "KPI tagging: event schema + observability models"],
  ["C9", "DEPLOY ANYWHERE", "Deploy hygiene: Helm + Terraform + EKS smoke lanes"],
  ["C10", "AUDIT INTEGRITY", "Tamper-evident audit: append-only + hash-chained + verifiable"],
  ["C11", "SUPPLY CHAIN", "Supply-chain integrity: lock/pins/provenance/signing/SBOM"],
  ["C12", "INJECTION-ASSUMED", "Abuse defense: red-team + rate-limit + recipient allowlist"],
];

const POSTURE_CONSTRAINTS = CONSTRAINT_SEED.map(([id, short, enforcement]) => ({
  id,
  short,
  enforcement,
  attested: true,
  mode: "static",
  evidence: [`packages/ne-x/tests/test_${id.toLowerCase()}_guardian.py`],
}));

const CURRENT_HASH = "c".repeat(64);
const PRIOR_HASH = "9".repeat(64);

const POSTURE_BASE = {
  constraints: POSTURE_CONSTRAINTS,
  model_lanes: [
    {
      model_id: "qwen3-32b",
      provider: "vllm",
      lane: "private-vllm",
      configured_model: "Qwen/Qwen3-32B",
      context_window: 32768,
    },
    {
      model_id: "claude-fable-5",
      provider: "anthropic",
      lane: "frontier-api",
      configured_model: "claude-fable-5",
      context_window: 1000000,
    },
  ],
  config_hash: CURRENT_HASH,
  config_hash_history: [
    { hash: PRIOR_HASH, from: "2026-07-01T00:00:00Z", to: "2026-07-06T00:00:00Z" },
    { hash: CURRENT_HASH, from: "2026-07-06T00:00:00Z", to: null },
  ],
  network_observed_at: "2026-08-03T11:59:56Z",
  configuration_observed_at: "2026-08-03T11:59:57Z",
  attestation_source:
    "build-time guardian evidence (static snapshot; the generating readiness " +
    "harness is not part of this release)",
};

/** Air-gapped posture: frontier lane killed, sanctioned allowlist empty. */
export const POSTURE: PostureView = {
  ...POSTURE_BASE,
  air_gap: true,
  egress_allowlist: [],
  air_gap_pending: null,
};

/** Not air-gapped: one sanctioned frontier host on the allowlist. */
export const POSTURE_NO_AIRGAP: PostureView = {
  ...POSTURE_BASE,
  air_gap: false,
  egress_allowlist: ["api.anthropic.com"],
  air_gap_pending: null,
};

/** Air-gapped WITH a parked governed disengage (NE-AIRGAP-TOGGLE §2.5): the
 * in-force posture stays On; the pending marker rides the read-back. */
export const POSTURE_PENDING_CHANGE: PostureView = {
  ...POSTURE,
  air_gap_pending: { request_id: "req-airgap-1", requested_by: "user:ops-olga" },
};

// ===========================================================================
// NE-132 connector-sync status — GET /v1/connect/status fixtures.
//
// CONNECT_STATUS: three LIVE connectors (m365 fresh + verified, a sub-minute CDC
// lane with one quarantined write, jira degraded/fail-closed with two) — the other
// five canonical lanes (slack/teams/salesforce/zendesk/sql) are ABSENT from the
// response, so the view renders them as honest NOT CONFIGURED — IDLE. The reason
// ledger is the sanitized first-colon-segment categories. CONNECT_STATUS_EMPTY: no
// connectors, quarantine_total null (no store readable — honestly unknown).
//
// The two freshness facts are DELIBERATELY divergent per lane (SPEC §12 item 9):
// every pull lane's last_checked_age_s (run age) differs from its cursor_age_s
// (newest-record age), and the two column sorts produce DIFFERENT orders —
// Last checked desc: m365 15 < debezium 22 < jira 610; Newest record desc:
// debezium 22 < m365 340 < jira 9200 — so a test cannot pass with the columns
// reading the same field. The CDC lane's two ages coincide HERE because its
// latest ingest batch accepted a record; a heartbeat-only CDC lane serves
// cursor_age_s null (no record ever accepted — honest absence, never fresh).
// ===========================================================================

export const CONNECT_STATUS: ConnectStatusView = {
  connectors: [
    {
      id: "m365",
      kind: "m365",
      posture: "live",
      cursor_age_s: 340,
      last_checked_age_s: 15,
      acl_mirror: "ok",
      last_sync_seq: 41,
      quarantined_count: 0,
      sync_allowed: true,
    },
    {
      id: "debezium",
      kind: "cdc",
      posture: "live",
      cursor_age_s: 22,
      last_checked_age_s: 22,
      acl_mirror: "ok",
      last_sync_seq: 44,
      quarantined_count: 1,
      sync_allowed: false,
    },
    {
      id: "jira",
      kind: "jira",
      posture: "live",
      cursor_age_s: 9200,
      last_checked_age_s: 610,
      acl_mirror: "degraded",
      last_sync_seq: 39,
      quarantined_count: 2,
      sync_allowed: true,
    },
  ],
  quarantine_total: 3,
  quarantine_reasons: [
    { reason: "schema-validation", count: 2 },
    { reason: "unknown-table", count: 1 },
  ],
};

export const CONNECT_STATUS_EMPTY: ConnectStatusView = {
  connectors: [],
  quarantine_total: null,
  quarantine_reasons: [],
};

// CONNECT_QUARANTINE: the ops-gated drill-down rows behind CONNECT_STATUS's
// counts — REAL reason shapes: two contract-validator messages (the raw
// jsonschema text an ops principal is entitled to read) and one CDC
// unknown-table park. CONNECT_QUARANTINE_EMPTY mirrors an unreadable store
// (total null — honestly unknown, never a fake zero).
export const CONNECT_QUARANTINE: ConnectQuarantineView = {
  items: [
    {
      item_id: "q-csv-rnw-1007",
      source: "csv",
      record_id: "RNW-1007",
      reason: "$.contract_end: 'ASAP' does not match '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
      category: "schema-validation",
      shape: "odcs:csv",
      created_at: "2026-07-11T10:05:00+00:00",
      payload: {
        renewal_id: "RNW-1007",
        account: "Meridian Logistics Ltd",
        contract_end: "ASAP",
        annual_value_usd: "58000",
      },
      payload_truncated: false,
    },
    {
      item_id: "q-csv-row-13",
      source: "csv",
      record_id: "row-13",
      reason: "$.renewal_id: '' does not match '^RNW-[0-9]{4}$'",
      category: "schema-validation",
      shape: "odcs:csv",
      created_at: "2026-07-11T10:04:00+00:00",
      payload: { renewal_id: "", account: "Bluepeak Aerospace Ltd" },
      payload_truncated: false,
    },
    {
      item_id: "q-cdc-orders-77",
      source: "cdc",
      record_id: "orders-77",
      reason: "unknown-table",
      category: "unknown-table",
      shape: "ne.connect.record",
      created_at: "2026-07-11T09:00:00+00:00",
      payload: { table: "public.orders_v2" },
      payload_truncated: false,
    },
  ],
  total: 3,
};

export const CONNECT_QUARANTINE_EMPTY: ConnectQuarantineView = {
  items: [],
  total: null,
};

/** KPI summary — the REAL U-P-B tags emitted by ne-api routers/kpi.py _entries.
 * utilization.events_total is DELIBERATELY OMITTED so KpiPage renders its honest
 * NOT-INSTRUMENTED slot (a value skipped server-side is an instrumentation gap,
 * not a zero). Every entry mirrors the router's dimensions/window shape. */
const KPI_ENTRY = (kpi_tag: string, value: number, unit: string): KpiEntryView => ({
  kpi_tag,
  value,
  unit,
  dimensions: { period: "2026-07" },
  window_start: iso(-30 * DAY),
  window_end: NOW_ISO,
});

export const KPI_SUMMARY: KpiSummaryView = {
  generated_at: NOW_ISO,
  kpis: [
    KPI_ENTRY("utilization.agent_turns", 128, "count"),
    KPI_ENTRY("utilization.tool_calls", 342, "count"),
    KPI_ENTRY("utilization.active_users", 9, "count"),
    KPI_ENTRY("utilization.sessions", 41, "count"),
    // utilization.events_total OMITTED — drives the NOT-INSTRUMENTED slot test.
    KPI_ENTRY("proficiency.eval_runs", 6, "count"),
    KPI_ENTRY("proficiency.eval_pass_rate", 0.833, "ratio"),
    KPI_ENTRY("proficiency.correction_signals_total", 12, "count"),
    KPI_ENTRY("proficiency.positive_signals", 30, "count"),
    KPI_ENTRY("proficiency.negative_signals", 4, "count"),
    KPI_ENTRY("proficiency.negative_signal_rate", 0.118, "ratio"),
    KPI_ENTRY("business_value.hours_saved_floor", 41.5, "hours"),
  ],
};

/** NE-WORKFLOWS: the two workflows GET /v1/workflows serves, each with the
 * governed tool names it reads and the single tool it writes. */
export const WORKFLOWS: WorkflowView[] = [
  {
    workflow_id: "unresolved-ticket-review",
    title: "Unresolved ticket review",
    description:
      "Reads every open and pending ticket, ranks accounts by unresolved volume and filed " +
      "priority, and parks one CRM note per account for approval.",
    reads: ["read.kg.tickets_by_status", "read.knowledge.search", "read.crm.account_lookup"],
    writes: ["write.internal.crm.note"],
  },
  {
    workflow_id: "recurring-issue-sweep",
    title: "Recurring issue sweep",
    description:
      "Finds a ticket subject filed verbatim by more than one account, then parks a CRM note " +
      "for each affected account's owner.",
    reads: ["read.kg.tickets_by_status", "read.crm.account_lookup"],
    writes: ["write.internal.crm.note"],
  },
];
