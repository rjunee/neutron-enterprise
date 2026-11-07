// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Pure-function tests for the trim-contraction motion math. They pin the honest
// set arithmetic (A∖B implode, A∩B retain, B∖A arrive), the BFS distance stagger
// (60ms/hop), the deterministic key-sorted tie-break, the unreachable-last-wave
// rule, totalMs = maxDelay + 200, both anchor-resolution paths, and that
// implodeScale is a monotonic 1→0 damp.

import { describe, expect, it } from "vitest";
import type { GraphEdgeWire, GraphNodeWire, SubgraphWire } from "../../api/types";
import { IMPLODE_MS, implodeScale, planContraction, STAGGER_MS } from "./compareMotion";

function node(key: string, source_ref = `ref/${key}`): GraphNodeWire {
  return {
    key,
    entity_class: "Account",
    display_name: key,
    source_system: "test",
    source_ref,
    properties: {},
    assurance: "source",
  };
}

function edge(from_key: string, to_key: string): GraphEdgeWire {
  return {
    key: `e-${from_key}-${to_key}`,
    edge_kind: "LINK",
    from_key,
    to_key,
    source_system: "test",
    source_ref: `ref/e-${from_key}-${to_key}`,
    assurance: "source",
  };
}

function sub(nodes: GraphNodeWire[], edges: GraphEdgeWire[]): SubgraphWire {
  return { nodes, edges, truncated: false };
}

// A: a-b-c-d chain + isolated e. B: a, b retained + f (b-only).
const GRAPH_A = sub(
  [node("a"), node("b"), node("c"), node("d"), node("e")],
  [edge("a", "b"), edge("b", "c"), edge("c", "d")],
);
const GRAPH_B = sub([node("a"), node("b"), node("f")], []);

describe("planContraction — set arithmetic + BFS stagger", () => {
  const plan = planContraction(GRAPH_A, GRAPH_B, ["a"], []);

  it("splits A∖B (imploding), A∩B (retained), B∖A (b-only)", () => {
    expect(plan.imploding.map((e) => e.key)).toEqual(["c", "d", "e"]);
    expect(plan.retained).toEqual(["a", "b"]);
    expect(plan.bOnly).toEqual(["f"]);
  });

  it("staggers implode delays by BFS hop distance from the anchor (60ms/hop)", () => {
    const byKey = new Map(plan.imploding.map((e) => [e.key, e.delayMs]));
    // a=0, b=1, c=2, d=3 → c/d imploding at 2*60 and 3*60.
    expect(byKey.get("c")).toBe(2 * STAGGER_MS);
    expect(byKey.get("d")).toBe(3 * STAGGER_MS);
  });

  it("assigns unreachable nodes the last wave (maxReachable+1)", () => {
    // e is disconnected: maxReachable distance is 3 (d), so e lands at 4.
    const byKey = new Map(plan.imploding.map((e) => [e.key, e.delayMs]));
    expect(byKey.get("e")).toBe(4 * STAGGER_MS);
  });

  it("orders imploding by delay then key (deterministic tie-break)", () => {
    // Two nodes at the same distance sort by key. Build a fresh fork: b1,b2 both
    // one hop from the anchor.
    const a = sub(
      [node("root"), node("zeta"), node("alpha")],
      [edge("root", "zeta"), edge("root", "alpha")],
    );
    const b = sub([node("root")], []);
    const p = planContraction(a, b, ["root"], []);
    expect(p.imploding.map((e) => e.key)).toEqual(["alpha", "zeta"]);
    expect(p.imploding.every((e) => e.delayMs === STAGGER_MS)).toBe(true);
  });

  it("totalMs = maxDelay + IMPLODE_MS", () => {
    expect(plan.totalMs).toBe(4 * STAGGER_MS + IMPLODE_MS);
  });
});

describe("planContraction — anchor resolution", () => {
  it("resolves anchors by citation (source_ref match)", () => {
    // Anchor 'a' by its source_ref; distances match the key-anchored run.
    const plan = planContraction(GRAPH_A, GRAPH_B, [], ["ref/a"]);
    const byKey = new Map(plan.imploding.map((e) => [e.key, e.delayMs]));
    expect(byKey.get("c")).toBe(2 * STAGGER_MS);
    expect(byKey.get("d")).toBe(3 * STAGGER_MS);
  });

  it("falls back to all-A-nodes-at-distance-0 when no anchor matches", () => {
    const plan = planContraction(GRAPH_A, GRAPH_B, ["nonexistent"], []);
    // Every imploding node starts at delay 0 (all are anchors).
    expect(plan.imploding.every((e) => e.delayMs === 0)).toBe(true);
    expect(plan.totalMs).toBe(IMPLODE_MS);
  });
});

describe("implodeScale", () => {
  it("is a monotonic 1→0 damp, clamped", () => {
    expect(implodeScale(0)).toBe(1);
    expect(implodeScale(1)).toBe(0);
    expect(implodeScale(-5)).toBe(1); // clamped low
    expect(implodeScale(9)).toBe(0); // clamped high
    const samples = [0, 0.2, 0.4, 0.6, 0.8, 1].map(implodeScale);
    for (let i = 1; i < samples.length; i++) {
      expect(samples[i]).toBeLessThanOrEqual(samples[i - 1]!);
    }
  });
});
