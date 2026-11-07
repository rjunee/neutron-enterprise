// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The trace drill-down's own display rules, without a DOM: a token total the
// server could not vouch for is absent rather than zero, a span's label is a
// word rather than its wire kind, and an unrecognized kind gets no invented
// name at all.

import { describe, expect, it } from "vitest";
import type { TraceNodeView } from "../api/types";
import {
  flatten,
  formatTokens,
  kindLabel,
  laneOf,
  laneTier,
  nodeName,
  routingFact,
  rowFact,
  templateLabel,
  timelineFacts,
} from "./format";

function node(
  overrides: Partial<TraceNodeView> & Pick<TraceNodeView, "event_id" | "kind">,
): TraceNodeView {
  return {
    seq: 1,
    action: null,
    event_type: "event",
    actor: "agent://root",
    resource: null,
    outcome: null,
    occurred_at: "2026-07-27T12:00:00Z",
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

describe("trace display rules", () => {
  it("renders a 0 or null token total as absent, and a real one grouped", () => {
    expect(formatTokens(0)).toBeNull();
    expect(formatTokens(null)).toBeNull();
    expect(formatTokens(842)).toBe("842");
    expect(formatTokens(24_500)).toBe("24,500");
  });

  it("classifies the chroma from the server's own lane and tier vocabularies", () => {
    expect(laneTier("frontier-api", null)).toBe("frontier");
    expect(laneTier("private-vllm", null)).toBe("local");
    expect(laneTier(null, "frontier")).toBe("frontier");
    expect(laneTier(null, "private")).toBe("local");
    expect(laneTier(null, null)).toBe("unpinned");
    expect(laneTier("something-new", null)).toBe("unpinned");
  });

  it("labels a span in words, and invents no name for a kind it does not know", () => {
    expect(kindLabel("model_call")).toBe("Model call");
    expect(kindLabel("pdp")).toBe("Policy decision");
    expect(kindLabel("subagent_route")).toBe("Routing");
    expect(kindLabel("event")).toBeNull();
    expect(kindLabel("some_future_kind")).toBeNull();
  });

  it("reads a node's lane and name from the span the server populated", () => {
    const call = node({
      event_id: "evt-model",
      kind: "model_call",
      action: "complete",
      model_call: {
        task: "chat.answer",
        lane: "private-vllm",
        model_name: "qwen3-local",
        response_model: "qwen3-local",
        cost_usd: null,
        input_tokens: 850,
        output_tokens: 210,
        // A pre-B12 row: the server serves the keys as null rather than omitting
        // them, so `null` here is the historical case this fixture stands for.
        latency_ms: null,
        ttft_ms: null,
        route_position: null,
        fallback_from: null,
      },
    });
    expect(nodeName(call)).toBe("qwen3-local");
    expect(laneOf(call)).toBe("private-vllm");

    // No typed span: the name falls back to the action, then the event type, and
    // the lane is absent rather than guessed.
    const raw = node({ event_id: "evt-raw", kind: "event", action: "future.unknown_action" });
    expect(nodeName(raw)).toBe("future.unknown_action");
    expect(laneOf(raw)).toBeNull();
    expect(nodeName(node({ event_id: "evt-bare", kind: "event" }))).toBe("event");
  });

  it("flattens the tree depth-first, and stops at a collapsed parent", () => {
    const child = node({ event_id: "evt-child", kind: "event" });
    const parent = node({ event_id: "evt-parent", kind: "subagent", children: [child] });
    expect(flatten([parent], new Set()).map((r) => r.node.event_id)).toEqual([
      "evt-parent",
      "evt-child",
    ]);
    expect(flatten([parent], new Set())[1]?.depth).toBe(1);
    expect(flatten([parent], new Set(["evt-parent"])).map((r) => r.node.event_id)).toEqual([
      "evt-parent",
    ]);
  });

  it("preserves the server's sibling/tree order even when sequence values are non-monotonic", () => {
    const servedFirst = node({ event_id: "evt-served-first", kind: "event", seq: 90 });
    const servedSecond = node({ event_id: "evt-served-second", kind: "event", seq: 4 });

    expect(flatten([servedFirst, servedSecond], new Set()).map((row) => row.node.event_id)).toEqual(
      ["evt-served-first", "evt-served-second"],
    );
  });
});

/** A tool node with the server's lifted template fields. */
function toolNode(
  overrides: Partial<NonNullable<TraceNodeView["tool"]>>,
  detail: string | null = null,
): TraceNodeView {
  return node({
    event_id: "evt-tool",
    kind: "tool",
    action: "tool.execute",
    tool: {
      approval_id: null,
      template_id: null,
      template_version: null,
      args: [],
      result_count: null,
      ...overrides,
    },
    detail,
  });
}

describe("plain-language rows from served fields", () => {
  it("names a tool row by its served template id, as words", () => {
    // The label is a mechanical respelling of the SERVER's id — nothing added.
    expect(nodeName(toolNode({ template_id: "tickets-by-status" }))).toBe("tickets by status");
    expect(nodeName(toolNode({ template_id: "accounts-directory" }))).toBe("accounts directory");
    expect(templateLabel(toolNode({ template_id: "accounts-directory" }))).toBe(
      "accounts directory",
    );
  });

  it("joins a tool to governance only through its own exact approval id", () => {
    expect(timelineFacts(toolNode({ approval_id: "apr-exact" }))).toContainEqual({
      key: "tool-approval",
      label: "Approval",
      value: "apr-exact",
      tone: "neutral",
      mono: true,
    });
    expect(timelineFacts(toolNode({ approval_id: null }))).not.toContainEqual(
      expect.objectContaining({ key: "tool-approval" }),
    );
  });

  it("gives a tool row WITHOUT a served template id the generic form, never a label", () => {
    // Absent field ⇒ say less: the server's own action string, not a guess.
    expect(templateLabel(toolNode({}))).toBeNull();
    expect(nodeName(toolNode({}))).toBe("tool.execute");
    // An empty-string template id is treated as absent, not respelled to "".
    expect(templateLabel(toolNode({ template_id: "" }))).toBeNull();
  });

  it("states a served row count, and marks ONLY a served zero as empty", () => {
    expect(rowFact(toolNode({ result_count: 120 }))).toEqual({ text: "120 rows", empty: false });
    expect(rowFact(toolNode({ result_count: 1 }))).toEqual({ text: "1 row", empty: false });
    // The zero-row fetch is the fact this screen exists to surface.
    expect(rowFact(toolNode({ result_count: 0 }))).toEqual({ text: "0 rows", empty: true });
    // No served count ⇒ NO fact at all — absent is not zero.
    expect(rowFact(toolNode({}))).toBeNull();
  });

  it("states a verified read's served row count with the same zero rule", () => {
    const read = (rows: number | null) =>
      node({
        event_id: "evt-read",
        kind: "verified_read",
        action: "knowledge.verified_read",
        verified_read: { rows, row_limit: 500, truncated: false },
      });
    expect(rowFact(read(120))).toEqual({ text: "120 rows", empty: false });
    expect(rowFact(read(0))).toEqual({ text: "0 rows", empty: true });
    expect(rowFact(read(null))).toBeNull();
    // With served rows the internal action string leaves the row name; without
    // them the row falls back to the server's own action string.
    expect(nodeName(read(120))).toBe("");
    expect(nodeName(read(null))).toBe("knowledge.verified_read");
  });

  it("maps the grounding phase vocabulary to words, and invents none for an unknown", () => {
    const grounding = (phase: string | null) =>
      node({
        event_id: "evt-grounding",
        kind: "grounding",
        action: "grounding.assist",
        grounding: { phase, adopted: true, error_class: null },
      });
    expect(rowFact(grounding("fail-redraft"))).toEqual({
      text: "draft withheld — rewritten from fetched data",
      empty: false,
    });
    expect(rowFact(grounding("pass-polish"))).toEqual({ text: "wording improved", empty: false });
    // An unrecognized phase states nothing and the row keeps the action string.
    expect(rowFact(grounding("some-future-phase"))).toBeNull();
    expect(nodeName(grounding("some-future-phase"))).toBe("grounding.assist");
    expect(nodeName(grounding("fail-redraft"))).toBe("");
  });

  it("states the server's adopted verdict — never an assist the server said was not used", () => {
    const grounding = (phase: string, adopted: boolean | null, error: string | null = null) =>
      node({
        event_id: "evt-grounding",
        kind: "grounding",
        action: "grounding.assist",
        grounding: { phase, adopted, error_class: error },
      });
    // adopted:false on pass-polish — the polish errored or was rejected by the
    // re-check; the ORIGINAL passing draft went out unchanged. The row must
    // state that, not "wording improved".
    expect(rowFact(grounding("pass-polish", false, "TimeoutError"))).toEqual({
      text: "wording unchanged — original draft sent",
      empty: false,
    });
    // adopted:false on fail-redraft — the redraft was rejected; the
    // deterministic fallback tables went out, not a rewrite.
    expect(rowFact(grounding("fail-redraft", false))).toEqual({
      text: "draft withheld — fetched data sent as tables",
      empty: false,
    });
    // CONTROLS: the adopted:true copy is unchanged by the adopted:false split.
    expect(rowFact(grounding("pass-polish", true))).toEqual({
      text: "wording improved",
      empty: false,
    });
    expect(rowFact(grounding("fail-redraft", true))).toEqual({
      text: "draft withheld — rewritten from fetched data",
      empty: false,
    });
    // A record without a served `adopted` states NOTHING — which text went out
    // is exactly the fact this line exists to state, and the server did not
    // say. The row keeps the action string; the pane shows the raw fields.
    expect(rowFact(grounding("pass-polish", null))).toBeNull();
    expect(rowFact(grounding("fail-redraft", null))).toBeNull();
    expect(nodeName(grounding("pass-polish", null))).toBe("grounding.assist");
    // adopted:false rows still carry a fact, so the action string stays off.
    expect(nodeName(grounding("pass-polish", false))).toBe("");
    expect(nodeName(grounding("fail-redraft", false))).toBe("");
  });

  it("names an older API's nodes without crashing — an absent key states the action", () => {
    // A rolling upgrade serves this bundle nodes from an OLDER assembler whose
    // wire shape lacks keys this bundle's types declare. JSON.parse delivers
    // those as ABSENT (undefined), not null — the cast smuggles that runtime
    // shape past the current types exactly as production does.
    const oldWire = (overrides: Record<string, unknown>): TraceNodeView =>
      ({
        event_id: "evt-old",
        seq: 1,
        event_type: "audit",
        actor: "user-1",
        occurred_at: "2026-08-04T12:00:00Z",
        children: [],
        ...overrides,
      }) as unknown as TraceNodeView;
    const oldTool = oldWire({
      kind: "tool",
      action: "tool.execute",
      tool: { approval_id: null },
    });
    expect(nodeName(oldTool)).toBe("tool.execute");
    expect(rowFact(oldTool)).toBeNull();
    const oldAssist = oldWire({
      kind: "grounding",
      action: "grounding.assist",
      grounding: { phase: "pass-polish" },
    });
    expect(nodeName(oldAssist)).toBe("grounding.assist");
    expect(rowFact(oldAssist)).toBeNull();
  });

  // THE WORDING IS THE ASSERTION.
  //
  // `route_position: "primary"` means nothing was attempted before the model
  // that served — a fact about this request's attempt sequence. It does NOT
  // mean the route's declared first choice served: the gateway filters
  // candidates by lane before attempting any, so a request pinned to a lane
  // whose route declares a frontier primary records "primary" for the on-box
  // model that served, with the declared first choice never eligible. "First
  // choice" carries that second, false reading; "First attempt" does not. This
  // test exists so the string cannot drift back.
  describe("routingFact — the verdict is the record's, and so is the wording", () => {
    function call(over: Partial<NonNullable<TraceNodeView["model_call"]>>) {
      return {
        task: "chat.answer",
        lane: "private-vllm",
        model_name: "qwen3-local",
        response_model: "qwen3-local",
        cost_usd: null,
        input_tokens: 10,
        output_tokens: 20,
        latency_ms: null,
        ttft_ms: null,
        route_position: null,
        fallback_from: null,
        ...over,
      };
    }

    it("words a served primary as an attempt, never as the route's choice", () => {
      const fact = routingFact(call({ route_position: "primary" }));
      expect(fact).toEqual({ fallback: false, text: "First attempt" });
      // The reading this copy exists to exclude: a claim about the declared
      // route rather than about what this request actually attempted.
      expect(fact?.text).not.toMatch(/choice/i);
    });

    it("names the candidate passed over, and stays a fallback without one", () => {
      expect(
        routingFact(call({ route_position: "fallback", fallback_from: "private-default" })),
      ).toEqual({ fallback: true, text: "Fallback — private-default did not serve" });
      expect(routingFact(call({ route_position: "fallback", fallback_from: null }))).toEqual({
        fallback: true,
        text: "Fallback",
      });
    });

    it("states nothing for a null, an unknown, or an absent position", () => {
      expect(routingFact(call({ route_position: null }))).toBeNull();
      expect(routingFact(call({ route_position: "who-knows" }))).toBeNull();
      // Rolling upgrade: the key is not on the wire at all. NOT cast — a wire
      // without the key is a legal `TraceModelCallView`, because the field is
      // declared optional. Re-declare it required and this line stops
      // compiling, which is the only detector this shape has.
      const { route_position: _dropped, ...older } = call({});
      expect(routingFact(older)).toBeNull();
      expect(routingFact(undefined)).toBeNull();
    });
  });

  it("names a session row by its served agent", () => {
    const session = node({
      event_id: "evt-session",
      kind: "session",
      action: "session.run",
      session: {
        agent: "grounded-analyst",
        department: "engineering",
        session_id: "sess-1",
        succeeded: true,
      },
    });
    expect(nodeName(session)).toBe("grounded-analyst");
    expect(kindLabel("session")).toBe("Conversation turn");
    expect(kindLabel("verified_read")).toBe("Data read");
    expect(kindLabel("grounding")).toBe("Answer check");
  });
});
