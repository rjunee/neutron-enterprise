// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-167b trace-assembly fixtures. These mirror the NE-167a TraceAssemblyView
// shape EXACTLY (packages/ne-api/src/ne/api/trace_assembly.py) and are the
// Phase-5 mocked-seam stand-in for a live GET /v1/trace/{trace_id} (live e2e
// parked per plan §0.6 — LIVE-PARKABLE). Kept in a SEPARATE file from
// fixtures.ts/handlers.ts to stay conflict-free with concurrent console work,
// following the graphFixtures.ts convention.
//
// TRACE_ASSEMBLY is the demo "money shot": a local model answers FAST
// (qwen3-local, private-vllm), then the run delegates a subagent to the
// frontier lane (claude-fable-5) — routing, a knowledge retrieval, a
// pre-egress redaction pass, the frontier model call itself, a Tier-2
// approval, the EXECUTED WRITE joined to that approval (the tool node's
// approval_id is the server-recorded execution→approval join), and a
// guardrail check all nest under that subagent span. A final RAW node (kind
// "event", an action the assembler does not recognize) proves §0 law 5: an
// unrecognized emitter renders honestly, never fabricated.
//
// The frontier model_call's null input/output tokens and the claude-fable-5
// aggregate's 0/0 token totals are the NE-167a Codex P2 fixture: the server's
// per-node fields are legitimately absent (frontier usage was never captured)
// and its aggregate sum therefore defaults to 0 — the console must render
// "—" for both, never a fabricated hard zero (see trace/format.ts).

import type { TraceAssemblyView, TraceNodeView, TraceToolView } from "../../api/types";

export const TRACE_ID = "trace-run-1";

/** Build a TraceNodeView with every field defaulted to null/empty, so each
 * fixture node below only sets the fields that make it distinctive. */
function node(
  overrides: Partial<TraceNodeView> &
    Pick<TraceNodeView, "event_id" | "seq" | "event_type" | "actor" | "occurred_at" | "kind">,
): TraceNodeView {
  return {
    action: null,
    resource: null,
    outcome: null,
    subagent: null,
    route: null,
    model_call: null,
    redaction: null,
    knowledge: null,
    pdp: null,
    approval: null,
    guardrail: null,
    tool: null,
    verified_read: null,
    grounding: null,
    session: null,
    detail: null,
    children: [],
    ...overrides,
  };
}

const PDP_NODE = node({
  event_id: "evt-t-pdp",
  seq: 1,
  action: "pdp.decide",
  event_type: "authorization",
  actor: "agent://root",
  outcome: "allow",
  occurred_at: "2026-07-16T12:00:00Z",
  kind: "pdp",
  pdp: {
    effect: "allow",
    reason: "policy ok",
    tier: 0,
    policy_version: "1",
    requested_action: "agent.run",
  },
});

/** LOCAL ANSWERED FAST — the private-vllm lane answers before any delegation. */
const LOCAL_MODEL_CALL_NODE = node({
  event_id: "evt-t-local",
  seq: 2,
  action: "ne.gateway.complete",
  event_type: "model_call",
  actor: "agent://root",
  outcome: "success",
  occurred_at: "2026-07-16T12:00:00.5Z",
  kind: "model_call",
  model_call: {
    task: "chat.answer",
    lane: "private-vllm",
    model_name: "qwen3-local",
    response_model: "qwen3-local",
    cost_usd: null,
    input_tokens: 850,
    output_tokens: 210,
    // B12: a measured non-stream call — latency present, TTFT null (a
    // non-stream call has no first-token fact; the pane shows the dash).
    latency_ms: 480,
    ttft_ms: null,
    // A record that predates the routing keys, as the server serves it: NULL,
    // not "primary". The pane must read this as unknown.
    route_position: null,
    fallback_from: null,
  },
});

const ROUTE_NODE = node({
  event_id: "evt-t-route",
  seq: 4,
  action: "subagent.route",
  event_type: "routing",
  actor: "agent://reason-lane-analyst",
  occurred_at: "2026-07-16T12:00:01.2Z",
  kind: "subagent_route",
  route: {
    agent: "reason-lane-analyst",
    route_task: "ap.adjudicate",
    route_lane: "frontier-api",
    tier: "frontier",
    task_source: "definition",
    lane_source: "definition",
    hint_clamped: false,
    reason: null,
  },
});

const KNOWLEDGE_NODE = node({
  event_id: "evt-t-knowledge",
  seq: 5,
  action: "knowledge.retrieve",
  event_type: "retrieval",
  actor: "agent://reason-lane-analyst",
  outcome: "success",
  occurred_at: "2026-07-16T12:00:02Z",
  kind: "knowledge",
  knowledge: { candidates: 14, returned: 6, acl_trim_delta: 8 },
});

const REDACTION_NODE = node({
  event_id: "evt-t-redaction",
  seq: 6,
  action: "redact.pre_egress",
  event_type: "redaction",
  actor: "agent://reason-lane-analyst",
  occurred_at: "2026-07-16T12:00:03.5Z",
  kind: "redaction",
  redaction: {
    hits: 3,
    categories: { EMAIL_ADDRESS: 2, US_SSN: 1 },
    task: null,
    lane: "frontier-api",
    model_name: "claude-fable-5",
  },
});

/** THE DELEGATED FRONTIER CALL — nulls on input/output tokens prove the
 * per-node '—' rule (NE-167a Codex P2): frontier usage was never captured. */
const FRONTIER_MODEL_CALL_NODE = node({
  event_id: "evt-t-frontier",
  seq: 7,
  action: "ne.gateway.complete",
  event_type: "model_call",
  actor: "agent://reason-lane-analyst",
  outcome: "success",
  occurred_at: "2026-07-16T12:00:04Z",
  kind: "model_call",
  model_call: {
    task: "ap.adjudicate",
    lane: "frontier-api",
    model_name: "claude-fable-5",
    response_model: "claude-fable-5",
    cost_usd: 0.018,
    input_tokens: null,
    output_tokens: null,
    // B12 nulls: a historical (pre-B12) row — no measurement was recorded,
    // so Latency/First token render "—", never a fabricated zero.
    latency_ms: null,
    ttft_ms: null,
    route_position: null,
    fallback_from: null,
  },
});

const APPROVAL_NODE = node({
  event_id: "evt-t-approval",
  seq: 8,
  action: "approval.decide",
  event_type: "approval",
  actor: "user:alice@example.com",
  outcome: "approved",
  occurred_at: "2026-07-16T12:00:05Z",
  kind: "approval",
  approval: { approval_id: "apr-1", tier: 2, decision: "approved", subject_ref: "write.erp" },
});

/** THE EXECUTED WRITE, joined to its approval: the server recorded WHICH
 * approval (`apr-1`, the Tier-2 grant above) this execution consumed, stamped
 * at the execution site — the pane renders "Authorised by approval". */
export const TOOL_EXECUTE_NODE = node({
  event_id: "evt-t-tool",
  seq: 9,
  action: "tool.execute",
  event_type: "audit",
  actor: "agent://reason-lane-analyst",
  outcome: "success",
  occurred_at: "2026-07-16T12:00:05.5Z",
  kind: "tool",
  tool: {
    approval_id: "apr-1",
    template_id: null,
    template_version: null,
    args: [],
    result_count: null,
  },
  detail: '{"what_action": "write.erp"}',
});

const GUARDRAIL_NODE = node({
  event_id: "evt-t-guardrail",
  seq: 10,
  action: "guardrail.check",
  event_type: "guardrail",
  actor: "agent://reason-lane-analyst",
  outcome: "pass",
  occurred_at: "2026-07-16T12:00:06Z",
  kind: "guardrail",
  guardrail: { detail: "spend-cap ok" },
});

const SUBAGENT_NODE = node({
  event_id: "evt-t-subagent",
  seq: 3,
  action: "subagent.dispatch",
  event_type: "subagent",
  actor: "agent://root",
  outcome: "success",
  occurred_at: "2026-07-16T12:00:01Z",
  kind: "subagent",
  subagent: {
    agent: "reason-lane-analyst",
    depth: 1,
    completed: true,
    ended_at: "2026-07-16T12:00:07Z",
    duration_ms: 6000,
    outcome: "success",
  },
  children: [
    ROUTE_NODE,
    KNOWLEDGE_NODE,
    REDACTION_NODE,
    FRONTIER_MODEL_CALL_NODE,
    APPROVAL_NODE,
    TOOL_EXECUTE_NODE,
    GUARDRAIL_NODE,
  ],
});

/** §0 LAW 5 FIXTURE — an action the assembler does not recognize renders as
 * its raw event: no typed span, no fabricated icon, just the scrubbed detail. */
const RAW_NODE = node({
  event_id: "evt-t-raw",
  seq: 11,
  action: "future.unknown_action",
  event_type: "event",
  actor: "agent://root",
  occurred_at: "2026-07-16T12:00:07.5Z",
  kind: "event",
  detail: "unrecognized emitter payload",
});

const ROLLUP = {
  trace_id: TRACE_ID,
  total_events: 11,
  total_cost_usd: 0.018,
  started_at: "2026-07-16T12:00:00Z",
  ended_at: "2026-07-16T12:00:08Z",
  duration_ms: 8000,
};

const MODELS = [
  {
    model: "qwen3-local",
    lane: "private-vllm",
    calls: 3,
    input_tokens: 2400,
    output_tokens: 800,
    cost_usd: null,
    tasks: [],
  },
  // NE-167a Codex P2 case: 0/0 token totals — the server cannot distinguish
  // "unknown" from a genuine zero, so the console renders "—" for both.
  {
    model: "claude-fable-5",
    lane: "frontier-api",
    calls: 1,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: 0.018,
    tasks: [],
  },
];

export const TRACE_ASSEMBLY: TraceAssemblyView = {
  trace_id: TRACE_ID,
  rollup: ROLLUP,
  models: MODELS,
  nodes: [PDP_NODE, LOCAL_MODEL_CALL_NODE, SUBAGENT_NODE, RAW_NODE],
  verification: {
    valid: true,
    basis: "full-chain",
    contradiction: false,
    checked: 42,
    first_break_seq: null,
    first_break_event_id: null,
    reason: null,
    detail: "chain intact",
  },
};

/** A broken-chain verdict over the same tree — the server-authoritative BREAK
 * badge must render this verbatim. */
export const TRACE_ASSEMBLY_BROKEN: TraceAssemblyView = {
  ...TRACE_ASSEMBLY,
  verification: {
    valid: false,
    basis: "full-chain",
    contradiction: false,
    checked: 7,
    first_break_seq: 5,
    first_break_event_id: "evt-5",
    reason: "record_hash mismatch",
    detail: "chain walk stopped at the first divergent record",
  },
};

/** The render-verbatim crux fixture: a rollup total (999.99) that flatly
 * contradicts the model aggregates (which sum to $0.018), paired with a VALID
 * verdict. The console must still render "$999.99" and "CHAIN VERIFIED" —
 * proving it renders server values verbatim and computes nothing itself. */
export const TRACE_ASSEMBLY_IMPOSSIBLE: TraceAssemblyView = {
  ...TRACE_ASSEMBLY,
  rollup: { ...ROLLUP, total_cost_usd: 999.99 },
};

// ---------------------------------------------------------------------------
// A grounded-agent chat turn, mirroring a REAL production trace shape
// (2026-08-04, demo box): one verified template read that matched (120 rows),
// one that matched NOTHING (0 rows — the served zero must render distinctly),
// one tool row with NO lifted template fields (a write tool / older record —
// the row falls back to the server's action string, never an invented label),
// the grounding gate's fail-redraft assist, and the terminal session.run row
// carrying the conversation-record pointer.
// ---------------------------------------------------------------------------

export const GROUNDED_SESSION_ID = "a522d327-30d2-4d68-9e34-a8395eed7b26";

const GROUNDED_TOOL_FULL = node({
  event_id: "evt-g-tool-full",
  seq: 2,
  action: "tool.execute",
  event_type: "audit",
  actor: "user-1",
  outcome: "success",
  occurred_at: "2026-08-04T22:10:43.2Z",
  kind: "tool",
  tool: {
    approval_id: null,
    template_id: "accounts-directory",
    template_version: 1,
    args: [],
    result_count: 120,
  },
  detail: '{"template_id": "accounts-directory", "result_count": 120}',
});

const GROUNDED_READ_FULL = node({
  event_id: "evt-g-read-full",
  seq: 1,
  action: "knowledge.verified_read",
  event_type: "audit",
  actor: "user-1",
  outcome: "success",
  occurred_at: "2026-08-04T22:10:43.1Z",
  kind: "verified_read",
  verified_read: { rows: 120, row_limit: 500, truncated: false },
  detail: "rows=120 row_limit=500 truncated=false truncation_reason=none",
});

const GROUNDED_TOOL_EMPTY = node({
  event_id: "evt-g-tool-empty",
  seq: 4,
  action: "tool.execute",
  event_type: "audit",
  actor: "user-1",
  outcome: "success",
  occurred_at: "2026-08-04T22:10:53.6Z",
  kind: "tool",
  tool: {
    approval_id: null,
    template_id: "tickets-by-status",
    template_version: 1,
    args: ["status"],
    result_count: 0,
  },
  detail: '{"template_id": "tickets-by-status", "args_shape": ["status"], "result_count": 0}',
});

const GROUNDED_READ_EMPTY = node({
  event_id: "evt-g-read-empty",
  seq: 3,
  action: "knowledge.verified_read",
  event_type: "audit",
  actor: "user-1",
  outcome: "success",
  occurred_at: "2026-08-04T22:10:53.5Z",
  kind: "verified_read",
  verified_read: { rows: 0, row_limit: 500, truncated: false },
  detail: "rows=0 row_limit=500 truncated=false truncation_reason=none",
});

/** A tool row the server could NOT lift template fields for. */
const GROUNDED_TOOL_BARE = node({
  event_id: "evt-g-tool-bare",
  seq: 5,
  action: "tool.execute",
  event_type: "audit",
  actor: "user-1",
  outcome: "success",
  occurred_at: "2026-08-04T22:10:54.7Z",
  kind: "tool",
  tool: {
    approval_id: null,
    template_id: null,
    template_version: null,
    args: [],
    result_count: null,
  },
  detail: '{"what_action": "write.erp"}',
});

const GROUNDED_ASSIST = node({
  event_id: "evt-g-assist",
  seq: 6,
  action: "grounding.assist",
  event_type: "audit",
  actor: "ne-runtime",
  outcome: "success",
  occurred_at: "2026-08-04T22:11:09.15Z",
  kind: "grounding",
  grounding: { phase: "fail-redraft", adopted: true, error_class: null },
});

const GROUNDED_SESSION = node({
  event_id: "evt-g-session",
  seq: 7,
  action: "session.run",
  event_type: "audit",
  actor: "user-1",
  outcome: "success",
  occurred_at: "2026-08-04T22:11:09.16Z",
  kind: "session",
  session: {
    agent: "grounded-analyst",
    department: "engineering",
    session_id: GROUNDED_SESSION_ID,
    succeeded: true,
  },
});

// ---------------------------------------------------------------------------
// A rolling-upgrade wire shape: an OLDER API serving THIS console bundle. The
// server pins its span key sets per version, not across versions, so a node
// from an older assembler can lack keys this bundle's types declare —
// JSON.parse delivers them as ABSENT (undefined), never null. The casts are
// the point: they smuggle that older runtime shape past the current types
// exactly as production does. The console must render the absent-field dash
// for what the server did not say — never crash, never invent a verdict.
// ---------------------------------------------------------------------------

const OLD_WIRE_BASE = {
  action: null,
  resource: null,
  outcome: "success",
  event_type: "audit",
  actor: "user-1",
  detail: null,
  children: [],
} as const;

/** A pre-lift tool span: only the approval-join key, no template/args/count.
 *
 * The tool payload is ANNOTATED, not cast, so `tsc` checks it — the ratchet
 * catalogue row #19's rule asks for, applied to the fields row #19 was derived
 * from. Re-declare any of `template_id`/`template_version`/`args`/
 * `result_count` REQUIRED on `TraceToolView` and this line is `TS2741` on the
 * day it happens, instead of a fixture silently vouching for a shape an older
 * server does not send.
 *
 * The NODE wrapper still needs `as unknown as` — for an unrelated reason,
 * named rather than left to look like the same defect: `TraceNodeView`'s
 * twelve span slots are declared required, and `OLD_WIRE_BASE` is `as const`
 * (so `children` is `readonly []`). Widening those twelve is a separate
 * decision about the whole trace wire — see row #19's residual. */
const OLD_WIRE_TOOL_SPAN: TraceToolView = { approval_id: null };

const OLD_WIRE_TOOL = {
  ...OLD_WIRE_BASE,
  event_id: "evt-ow-tool",
  seq: 1,
  action: "tool.execute",
  occurred_at: "2026-08-04T22:10:43Z",
  kind: "tool",
  tool: OLD_WIRE_TOOL_SPAN,
} as unknown as TraceNodeView;

/** A pre-`adopted` grounding span: the phase alone, no adopted/error keys. */
const OLD_WIRE_ASSIST = {
  ...OLD_WIRE_BASE,
  event_id: "evt-ow-assist",
  seq: 2,
  action: "grounding.assist",
  occurred_at: "2026-08-04T22:10:44Z",
  kind: "grounding",
  grounding: { phase: "pass-polish" },
} as unknown as TraceNodeView;

export const TRACE_ASSEMBLY_OLD_WIRE: TraceAssemblyView = {
  trace_id: TRACE_ID,
  rollup: {
    trace_id: TRACE_ID,
    total_events: 2,
    total_cost_usd: null,
    started_at: "2026-08-04T22:10:43Z",
    ended_at: "2026-08-04T22:10:44Z",
    duration_ms: 1000,
  },
  models: [],
  nodes: [OLD_WIRE_TOOL, OLD_WIRE_ASSIST],
  verification: {
    valid: true,
    basis: "full-chain",
    contradiction: false,
    checked: 2,
    first_break_seq: null,
    first_break_event_id: null,
    reason: null,
    detail: "chain intact",
  },
};

export const TRACE_ASSEMBLY_GROUNDED: TraceAssemblyView = {
  trace_id: TRACE_ID,
  rollup: {
    trace_id: TRACE_ID,
    total_events: 7,
    total_cost_usd: null,
    started_at: "2026-08-04T22:10:43Z",
    ended_at: "2026-08-04T22:11:09.2Z",
    duration_ms: 26200,
  },
  models: [],
  nodes: [
    GROUNDED_READ_FULL,
    GROUNDED_TOOL_FULL,
    GROUNDED_READ_EMPTY,
    GROUNDED_TOOL_EMPTY,
    GROUNDED_TOOL_BARE,
    GROUNDED_ASSIST,
    GROUNDED_SESSION,
  ],
  verification: {
    valid: true,
    basis: "full-chain",
    contradiction: false,
    checked: 7,
    first_break_seq: null,
    first_break_event_id: null,
    reason: null,
    detail: "chain intact",
  },
};

// ---------------------------------------------------------------------------
// THE ROUTING BEAT — one run whose four model calls traverse the demo box's
// declared chain by task complexity, mirroring a real NE_GATEWAY_TASKS
// deployment: a cheap on-box triage, a `reason` call the frontier candidate
// did NOT serve (the on-box fallback answered instead), a frontier
// adjudication, and a call whose record predates the routing keys.
//
// The four calls are deliberately four DIFFERENT routing states, because they
// must render as four different things:
//   1. served as the route's first choice          (route_position "primary")
//   2. served as a fallback, naming what failed    ("fallback" + fallback_from)
//   3. served, position NULL                       (older record: unknown)
//   4. served, keys ABSENT FROM THE WIRE ENTIRELY  (rolling upgrade: unknown)
// States 3 and 4 must read as UNKNOWN, never as "first choice" — the console
// may only repeat a verdict the server made.
// ---------------------------------------------------------------------------

const ROUTED_PRIMARY = node({
  event_id: "evt-r-triage",
  seq: 1,
  action: "ne.gateway.complete",
  event_type: "model_call",
  actor: "agent://root",
  outcome: "success",
  occurred_at: "2026-08-05T09:00:00Z",
  kind: "model_call",
  model_call: {
    task: "triage",
    lane: "private-vllm",
    model_name: "aux-model",
    response_model: "aux-model",
    cost_usd: null,
    input_tokens: 120,
    output_tokens: 30,
    latency_ms: 210,
    ttft_ms: null,
    route_position: "primary",
    fallback_from: null,
  },
});

/** THE GOVERNANCE FACT: `reason` declares a frontier primary with an on-box
 * fallback. Here the primary did not serve — so the answer came off the box,
 * and the record says so. Rendering `private-default` for this call (the
 * configured route) would be the exact defect this fixture exists to catch. */
const ROUTED_FALLBACK = node({
  event_id: "evt-r-reason",
  seq: 2,
  action: "ne.gateway.complete",
  event_type: "model_call",
  actor: "agent://root",
  outcome: "success",
  occurred_at: "2026-08-05T09:00:01Z",
  kind: "model_call",
  model_call: {
    task: "reason",
    lane: "private-vllm",
    requested_model: "private-default",
    model_name: "aux-model",
    response_model: "aux-model",
    cost_usd: null,
    input_tokens: 900,
    output_tokens: 240,
    latency_ms: 3100,
    ttft_ms: null,
    route_position: "fallback",
    fallback_from: "private-default",
  },
});

/** A served NULL position — the server answered the question with "unknown". */
const ROUTED_UNKNOWN = node({
  event_id: "evt-r-adjudicate",
  seq: 3,
  action: "ne.gateway.complete",
  event_type: "model_call",
  actor: "agent://root",
  outcome: "success",
  occurred_at: "2026-08-05T09:00:02Z",
  kind: "model_call",
  model_call: {
    task: "adjudicate",
    lane: "frontier-api",
    model_name: "frontier-best",
    response_model: "frontier-best",
    cost_usd: 0.021,
    input_tokens: 1400,
    output_tokens: 380,
    latency_ms: 5200,
    ttft_ms: null,
    route_position: null,
    fallback_from: null,
  },
});

/** ROLLING UPGRADE: an older API serializes NO routing keys at all. Absent is
 * the same unknown as a served null — never a crash, never a fabricated
 * position.
 *
 * NOT CAST — TYPE-CHECKED. `route_position`/`fallback_from` are optional on
 * `TraceModelCallView`, so this really IS a legal value of the current type
 * and needs no `as unknown as` escape. That is the whole point: the day
 * someone re-declares either key required, `tsc -b` fails HERE, on the
 * fixture that describes the wire the server actually sends. A cast would
 * have made the fixture agree with any declaration at all. */
const ROUTED_OLD_WIRE: TraceNodeView = {
  event_id: "evt-r-assist",
  seq: 4,
  action: "ne.gateway.complete",
  event_type: "model_call",
  actor: "agent://root",
  resource: null,
  outcome: "success",
  occurred_at: "2026-08-05T09:00:03Z",
  kind: "model_call",
  model_call: {
    task: "grounding-assist",
    lane: "private-vllm",
    model_name: "private-default",
    response_model: "private-default",
    cost_usd: null,
    input_tokens: null,
    output_tokens: null,
    latency_ms: null,
    ttft_ms: null,
  },
  subagent: null,
  route: null,
  redaction: null,
  knowledge: null,
  pdp: null,
  approval: null,
  guardrail: null,
  tool: null,
  verified_read: null,
  grounding: null,
  session: null,
  detail: null,
  children: [],
};

export const TRACE_ASSEMBLY_ROUTED: TraceAssemblyView = {
  trace_id: TRACE_ID,
  rollup: {
    trace_id: TRACE_ID,
    total_events: 4,
    total_cost_usd: 0.021,
    started_at: "2026-08-05T09:00:00Z",
    ended_at: "2026-08-05T09:00:04Z",
    duration_ms: 4000,
  },
  models: [
    {
      model: "aux-model",
      lane: "private-vllm",
      calls: 2,
      input_tokens: 1020,
      output_tokens: 270,
      cost_usd: null,
      tasks: ["triage", "reason"],
    },
    {
      model: "frontier-best",
      lane: "frontier-api",
      calls: 1,
      input_tokens: 1400,
      output_tokens: 380,
      cost_usd: 0.021,
      tasks: ["adjudicate"],
    },
    {
      model: "private-default",
      lane: "private-vllm",
      calls: 1,
      input_tokens: 0,
      output_tokens: 0,
      cost_usd: null,
      tasks: ["grounding-assist"],
    },
  ],
  nodes: [ROUTED_PRIMARY, ROUTED_FALLBACK, ROUTED_UNKNOWN, ROUTED_OLD_WIRE],
  verification: {
    valid: true,
    basis: "full-chain",
    contradiction: false,
    checked: 4,
    first_break_seq: null,
    first_break_event_id: null,
    reason: null,
    detail: "chain intact",
  },
};
