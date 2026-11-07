// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { GraphNodeWire, SubgraphWire } from "../api/types";
import {
  buildSimulation,
  CLIENT_NODE_CAP,
  mergeSubgraphs,
  settle,
  toChamberGraph,
} from "./chamberSim";
import { GRAPH_FIXTURE, NEIGHBORHOOD_FIXTURE } from "../test/mocks/graphFixtures";

describe("buildSimulation + settle", () => {
  it("gives every node a finite x/y after settling to the freeze", () => {
    const graph = toChamberGraph(GRAPH_FIXTURE);
    const sim = buildSimulation(graph);
    settle(sim);
    for (const n of sim.nodes()) {
      expect(Number.isFinite(n.x)).toBe(true);
      expect(Number.isFinite(n.y)).toBe(true);
    }
    // The freeze actually happened (alpha decayed under the threshold).
    expect(sim.alpha()).toBeLessThan(0.005);
  });

  it("is reproducible across runs (LCG random source)", () => {
    const a = buildSimulation(toChamberGraph(GRAPH_FIXTURE));
    settle(a);
    const b = buildSimulation(toChamberGraph(GRAPH_FIXTURE));
    settle(b);
    const ax = a.nodes().map((n) => n.x);
    const bx = b.nodes().map((n) => n.x);
    expect(ax).toEqual(bx);
  });
});

describe("toChamberGraph", () => {
  it("threads each edge's own assurance through unchanged (CR-B10: dashed = unconfirmed)", () => {
    const a: GraphNodeWire = {
      key: "acct-a",
      entity_class: "Account",
      display_name: "A",
      source_system: "crm",
      source_ref: "crm/a",
      properties: {},
      assurance: "source",
    };
    const b: GraphNodeWire = { ...a, key: "acct-b", display_name: "B", source_ref: "crm/b" };
    const sub: SubgraphWire = {
      nodes: [a, b],
      edges: [
        {
          key: "edge-acquired",
          edge_kind: "ACQUIRED",
          from_key: "acct-a",
          to_key: "acct-b",
          source_system: "crm",
          source_ref: "crm/edge-acquired",
          assurance: "extracted",
        },
      ],
      truncated: false,
    };

    const graph = toChamberGraph(sub);

    expect(graph.edges[0]?.assurance).toBe("extracted");
  });
});

describe("mergeSubgraphs", () => {
  it("dedupes by key and preserves existing node positions", () => {
    const current = toChamberGraph(GRAPH_FIXTURE);
    const sim = buildSimulation(current);
    settle(sim);
    const runbookBefore = current.nodes.find((n) => n.key === "doc-runbook");
    expect(runbookBefore?.x).toBeDefined();
    const xBefore = runbookBefore?.x;
    const yBefore = runbookBefore?.y;

    const { merged, overflowed } = mergeSubgraphs(current, NEIGHBORHOOD_FIXTURE);
    expect(overflowed).toBe(false);

    // 8 existing + 2 new (the duplicate doc-runbook is NOT added again).
    expect(merged.nodes).toHaveLength(10);
    const keys = merged.nodes.map((n) => n.key);
    expect(keys.filter((k) => k === "doc-runbook")).toHaveLength(1);
    expect(keys).toContain("person-grace");
    expect(keys).toContain("ticket-99");

    // The existing node's laid-out position rode along unchanged.
    const runbookAfter = merged.nodes.find((n) => n.key === "doc-runbook");
    expect(runbookAfter?.x).toBe(xBefore);
    expect(runbookAfter?.y).toBe(yBefore);
  });

  it("reports overflowed:true when a merge would exceed the 500-node cap", () => {
    const bigNodes: GraphNodeWire[] = [];
    for (let i = 0; i < CLIENT_NODE_CAP; i++) {
      bigNodes.push({
        key: `n-${i}`,
        entity_class: "Person",
        display_name: `Person ${i}`,
        source_system: "workday",
        source_ref: `hr/${i}`,
        properties: {},
        assurance: "source",
      });
    }
    const current = toChamberGraph({ nodes: bigNodes, edges: [], truncated: false });
    const incoming: SubgraphWire = {
      nodes: [
        {
          key: "n-overflow",
          entity_class: "Person",
          display_name: "One Too Many",
          source_system: "workday",
          source_ref: "hr/overflow",
          properties: {},
          assurance: "source",
        },
      ],
      edges: [],
      truncated: false,
    };
    const { merged, overflowed } = mergeSubgraphs(current, incoming);
    expect(overflowed).toBe(true);
    expect(merged.nodes).toHaveLength(CLIENT_NODE_CAP);
    expect(merged.truncated).toBe(true); // the by-design placard will show
  });
});
