// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Pure display helpers for the trace drill-down. They format SERVER values only
// — nothing here adds, subtracts or derives a quantity. Money, durations and
// counts come from the shared §2.7 formatters; what is left is the rules this
// screen owns.

import type { TraceNodeView } from "../api/types";
import { formatCount } from "../primitives/format";

/**
 * A token total the server could not vouch for renders as absent.
 *
 * The per-model aggregate sums default an absent per-node total to 0 (the wire
 * field is a plain int), so 0 is ambiguous between "genuinely none" and
 * "unknown" — and a completed model call with a genuine 0-token total cannot
 * exist. Absent beats a hard zero, which would be a measurement nobody made.
 */
export function formatTokens(v: number | null): string | null {
  if (v === null || v === 0) return null;
  return formatCount(v);
}

/**
 * The routing-tier chroma for a node's marker. Uses the server's own
 * vocabularies verbatim: lane strings on model-call/redaction spans, the tier
 * string on routing spans. Anything else is unclassified rather than guessed —
 * and the class is decoration only; what the reader sees is the server's own
 * lane string.
 */
export type LaneTier = "frontier" | "local" | "unpinned";

export function laneTier(lane: string | null, tier: string | null): LaneTier {
  if (lane === "frontier-api") return "frontier";
  if (lane === "private-vllm") return "local";
  if (tier === "frontier") return "frontier";
  if (tier === "private") return "local";
  return "unpinned";
}

/**
 * What a span IS, in words a reader has (§13b: no span-kind jargon in labels).
 * A kind the assembler does not enumerate — including its own raw fallback —
 * has no invented name: the caller renders the server's own `detail` text.
 */
const KIND_LABEL: Readonly<Record<string, string>> = {
  subagent: "Sub-agent",
  subagent_route: "Routing",
  model_call: "Model call",
  redaction: "Redaction",
  knowledge: "Knowledge",
  pdp: "Policy decision",
  approval: "Approval",
  guardrail: "Guardrail",
  tool: "Tool execution",
  verified_read: "Data read",
  grounding: "Answer check",
  session: "Conversation turn",
};

export function kindLabel(kind: string): string | null {
  return KIND_LABEL[kind] ?? null;
}

/**
 * The served template id, as words: `"tickets-by-status"` → "tickets by
 * status". A mechanical respelling of a SERVER identifier — hyphens and
 * underscores become spaces, nothing is added or renamed. A node without a
 * served `template_id` gets NO label from here: the caller falls back to the
 * server's own action string rather than a name nobody recorded.
 */
export function templateLabel(node: TraceNodeView): string | null {
  const id = node.tool?.template_id ?? null;
  if (id === null || id === "") return null;
  return id.replace(/[-_]+/g, " ");
}

/**
 * The row's result fact, stated from served counts only — and ONLY when the
 * server recorded one. `empty` marks a served zero (the fetch genuinely
 * matched nothing): visually distinct because a reader who sees "0 rows" at a
 * glance catches what raw JSON hides. An absent count states nothing — never
 * "0" — because absent and zero are different facts.
 */
export interface RowFact {
  text: string;
  empty: boolean;
}

export type TimelineFactTone = "neutral" | "affirm" | "caution" | "danger";

/**
 * One compact, typed fact shown on the timeline row. These are mechanical
 * projections of fields the trace assembler served; they never join sibling
 * events or infer a lifecycle from adjacency.
 */
export interface TimelineFact {
  key: string;
  label: string;
  value: string;
  tone: TimelineFactTone;
  mono?: boolean;
}

function effectTone(value: string): TimelineFactTone {
  const normalized = value.toLowerCase();
  if (
    [
      "allow",
      "allowed",
      "approve",
      "approved",
      "complete",
      "completed",
      "pass",
      "passed",
      "permit",
      "permitted",
      "success",
      "succeeded",
    ].includes(normalized)
  ) {
    return "affirm";
  }
  if (["deny", "denied", "reject", "rejected", "blocked", "failed"].includes(normalized)) {
    return "danger";
  }
  if (["pending", "requested", "required"].includes(normalized)) return "caution";
  return "neutral";
}

function fact(
  key: string,
  label: string,
  value: string | number | null | undefined,
  tone: TimelineFactTone = "neutral",
  mono = false,
): TimelineFact | null {
  if (value === null || value === undefined || value === "") return null;
  return { key, label, value: typeof value === "number" ? formatCount(value) : value, tone, mono };
}

/**
 * Governance-first facts for the settled timeline. Unknown nodes deliberately
 * return none: their safe event metadata already appears in the row/pane, and
 * an arbitrary raw payload is never promoted into presenter copy.
 */
export function timelineFacts(node: TraceNodeView): TimelineFact[] {
  const facts: Array<TimelineFact | null> = [];

  if (node.model_call) {
    const call = node.model_call;
    facts.push(
      fact("model-requested", "Requested model", call.requested_model, "neutral", true),
      fact("model-served", "Served model", call.model_name ?? call.response_model, "neutral", true),
    );
  } else if (node.pdp) {
    const effect = node.pdp.effect;
    facts.push(
      fact("pdp-effect", "Policy", effect, effect ? effectTone(effect) : "neutral"),
      fact("pdp-tier", "Tier", node.pdp.tier),
      fact("pdp-action", "Action", node.pdp.requested_action, "neutral", true),
    );
  } else if (node.approval) {
    const decision = node.approval.decision;
    facts.push(
      fact("approval-decision", "Approval", decision, decision ? effectTone(decision) : "neutral"),
      fact("approval-id", "Request", node.approval.approval_id, "neutral", true),
      fact("approval-tier", "Tier", node.approval.tier),
    );
  } else if (node.tool) {
    // This is the only approval/tool join: the exact id recorded ON the tool.
    // An adjacent approval span is intentionally invisible to this function.
    facts.push(
      fact("tool-approval", "Approval", node.tool.approval_id, "neutral", true),
      fact(
        "tool-args",
        "Arguments",
        node.tool.args?.length ? node.tool.args.join(", ") : null,
        "neutral",
        true,
      ),
    );
  } else if (node.redaction) {
    const redaction = node.redaction;
    const categories = Object.entries(redaction.categories)
      .map(([category, count]) => `${category} ${formatCount(count)}`)
      .join(" · ");
    facts.push(
      fact(
        "redaction-count",
        "Redacted",
        redaction.hits,
        redaction.hits !== null && redaction.hits > 0 ? "caution" : "neutral",
      ),
      fact("redaction-categories", "Categories", categories),
    );
  } else if (node.knowledge) {
    facts.push(
      fact("knowledge-candidates", "Candidates", node.knowledge.candidates),
      fact(
        "knowledge-returned",
        "Returned",
        node.knowledge.returned,
        node.knowledge.returned === 0 ? "caution" : "neutral",
      ),
      fact(
        "knowledge-withheld",
        "Permission withheld",
        node.knowledge.acl_trim_delta,
        node.knowledge.acl_trim_delta !== null && node.knowledge.acl_trim_delta > 0
          ? "caution"
          : "neutral",
      ),
    );
  } else if (node.guardrail) {
    facts.push(
      fact(
        "guardrail",
        "Guardrail",
        node.guardrail.detail,
        node.outcome ? effectTone(node.outcome) : "neutral",
      ),
    );
  }

  return facts.filter((item): item is TimelineFact => item !== null);
}

/** The grounding vocabulary the server emits today, in plain words — keyed on
 * the PAIR the server serves: the phase AND its `adopted` verdict. `adopted`
 * is the server's own statement of whether the assist's text went out; a line
 * keyed on phase alone would claim the assist happened when the server said
 * it did not (the assist errored, or its rewrite failed the re-check). An
 * unrecognized phase states nothing (the pane still shows the raw fields). */
const GROUNDING_FACT: Readonly<Record<string, Readonly<{ adopted: string; notAdopted: string }>>> =
  {
    "fail-redraft": {
      adopted: "draft withheld — rewritten from fetched data",
      notAdopted: "draft withheld — fetched data sent as tables",
    },
    "pass-polish": {
      adopted: "wording improved",
      notAdopted: "wording unchanged — original draft sent",
    },
  };

/**
 * The grounding fact from the served (phase, adopted) pair — and null when
 * the server did not serve both. Which text went out is exactly the fact this
 * line exists to state, so a record without a served `adopted` states
 * nothing, never a guess. `== null` also covers keys an OLDER API's wire
 * shape omits entirely (rolling upgrade: absent, not null).
 */
export function groundingFact(grounding: TraceNodeView["grounding"] | undefined): string | null {
  if (grounding == null || grounding.phase == null || grounding.adopted == null) return null;
  const copy = GROUNDING_FACT[grounding.phase];
  if (copy === undefined) return null;
  return grounding.adopted ? copy.adopted : copy.notAdopted;
}

/**
 * The routing verdict for a model call: WAS this the model the route asked for
 * first, or the one that answered after another candidate did not?
 *
 * Read from the server's own `route_position` and nothing else. There are
 * three outcomes and they are three different facts:
 *
 * * `"fallback"` — a fallback served. `fallback_from` names what was passed
 *   over when the server sent it; a fallback with no named candidate still
 *   reads as a fallback, with no invented name.
 * * `"primary"` — nothing was attempted before the model that served.
 * * anything else, including `null` and an absent key (rolling upgrade) —
 *   NOTHING. The caller renders the em dash. Answering "First attempt" here
 *   would be the console asserting a route held on a record that never said
 *   so, which is exactly the failure this field exists to make impossible.
 *
 * THE COPY IS "First attempt", NOT "First choice". `route_position` is a fact
 * about THIS request's attempt sequence, and the gateway filters candidates by
 * lane before it attempts any of them: a request pinned to `private-vllm`
 * whose route declares a frontier primary records `"primary"` for the on-box
 * model, because nothing was attempted before it — even though the route's
 * declared first choice was never eligible. "First choice" reads as a claim
 * about the ROUTE; only "first attempt" is a claim about the request, and the
 * request is the only thing this record describes.
 *
 * This function reads `route_position` and nothing else — it never consults a
 * route declaration to decide what served. (The Models page is BUILT to render
 * the configured `task_routes` when the policy router lands — `models/policy.ts`
 * §18 B3 — and renders none today: `policyReader` probes for a `client.models`
 * read `ApiClient` does not implement, so the policy branch is dead on every
 * deployment, pinned by `models/reads.test.ts::are ABSENT on the client this
 * deployment ships`. That would be the DECLARED chain: a different surface and
 * a different fact. What C7 forbids is a trace verdict DERIVED from a
 * declaration instead of read from the record.)
 */
export interface RoutingFact {
  fallback: boolean;
  text: string;
}

export function routingFact(call: TraceNodeView["model_call"] | undefined): RoutingFact | null {
  if (call == null) return null;
  if (call.route_position === "fallback") {
    const from = call.fallback_from;
    return {
      fallback: true,
      text: from ? `Fallback — ${from} did not serve` : "Fallback",
    };
  }
  if (call.route_position === "primary") return { fallback: false, text: "First attempt" };
  return null;
}

export function rowFact(node: TraceNodeView): RowFact | null {
  const count = node.tool?.result_count ?? node.verified_read?.rows ?? null;
  if (count != null) {
    return { text: count === 1 ? "1 row" : `${formatCount(count)} rows`, empty: count === 0 };
  }
  if (node.grounding) {
    const text = groundingFact(node.grounding);
    return text === null ? null : { text, empty: false };
  }
  return null;
}

/** The lane a node ran on, when it carries one. Never inferred. */
export function laneOf(node: TraceNodeView): string | null {
  return node.model_call?.lane ?? node.redaction?.lane ?? node.route?.route_lane ?? null;
}

/** The name a span row shows: the agent, the model, the template (as words),
 * the session's agent, the action — in that order of specificity, falling back
 * to the server's own event type. A typed data-read or answer-check row whose
 * fact line already states the content returns "" — the internal action string
 * stays off the row when the served fields cover it (the pane still shows the
 * action verbatim). */
export function nodeName(node: TraceNodeView): string {
  const template = templateLabel(node);
  const named =
    node.subagent?.agent ?? node.model_call?.model_name ?? template ?? node.session?.agent;
  if (named !== null && named !== undefined) return named;
  // `?.` / groundingFact: an OLDER API's wire shape (rolling upgrade) can
  // omit these keys entirely — an absent span reads as no fact, never a
  // crash. The action string leaves the row ONLY when the fact line it
  // yields to actually renders (rowFact yields non-null for this node).
  if (node.verified_read?.rows != null) return "";
  if (groundingFact(node.grounding) !== null) return "";
  return node.action ?? node.event_type;
}

/** Depth-first flattening of the assembled tree, honouring collapsed parents. */
export function flatten(
  nodes: readonly TraceNodeView[],
  collapsed: ReadonlySet<string>,
  depth = 0,
): Array<{ node: TraceNodeView; depth: number }> {
  const out: Array<{ node: TraceNodeView; depth: number }> = [];
  for (const node of nodes) {
    out.push({ node, depth });
    if (node.children.length > 0 && !collapsed.has(node.event_id)) {
      out.push(...flatten(node.children, collapsed, depth + 1));
    }
  }
  return out;
}
