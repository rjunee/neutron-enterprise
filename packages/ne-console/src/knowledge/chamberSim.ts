// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The pure, canvas-free physics core of the Graph Chamber (design §5.1). It
// converts an ACL-trimmed SubgraphWire into typed sim nodes/links and owns the
// vendored d3-force simulation. WE drive the ticks (the simulation is
// constructed then immediately .stop()'d) so ticks stay OFF the render loop and
// the static-friction freeze (alpha < 0.005) is deterministic — the §5.1 perf
// budget. d3-force v3's built-in LCG makes layouts reproducible in tests.
//
// Client caps mirror the server caps (500 nodes / 2000 edges); mergeSubgraphs
// enforces them on a BLOOM merge and reports overflow honestly (§0 law 5) so the
// truncation placard can render even when the overflow is client-side.

import type { GraphEdgeWire, GraphNodeWire, SubgraphWire } from "../api/types";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "../vendor/d3/d3-force/index.js";

/** Client-side caps — identical to the server caps in graph_wire.py. */
export const CLIENT_NODE_CAP = 500;
export const CLIENT_EDGE_CAP = 2000;

/** Default chamber extent used for layout when no measured size is available
 * (tests + first paint before the ResizeObserver reports). */
export const DEFAULT_WIDTH = 960;
export const DEFAULT_HEIGHT = 640;

/** Visual node radius (layout collision + hit-test share this). */
export const NODE_RADIUS = 9;

/** A wire node carried into the simulation — d3-force writes x/y/vx/vy in place. */
export interface ChamberNode extends SimulationNodeDatum {
  key: string;
  entity_class: string;
  display_name: string;
  source_system: string;
  source_ref: string;
  properties: Record<string, unknown>;
  /** NE-T2X-5 (design §6.1): threaded from GraphNodeWire so the DetailDrawer
   * node placard can render the "LLM-extracted — unconfirmed" chip. */
  assurance: string;
}

/** A wire edge with resolved node references (d3-force link datum). */
export interface ChamberEdge extends SimulationLinkDatum<ChamberNode> {
  key: string;
  edge_kind: string;
  from_key: string;
  to_key: string;
  source_system: string;
  source_ref: string;
  /** The ASSERTING relation's own assurance (design §5.1 / CR-B10): an
   * extracted, unconfirmed assertion draws dashed on the canvas regardless of
   * either endpoint's own assurance. */
  assurance: string;
  source: ChamberNode;
  target: ChamberNode;
}

/** The rendered subgraph: sim nodes/edges plus the honest truncated flag
 * (server-truncated OR client-overflowed). */
export interface ChamberGraph {
  nodes: ChamberNode[];
  edges: ChamberEdge[];
  truncated: boolean;
}

export type ChamberSimulation = Simulation<ChamberNode, ChamberEdge>;

function toNode(wire: GraphNodeWire): ChamberNode {
  return {
    key: wire.key,
    entity_class: wire.entity_class,
    display_name: wire.display_name,
    source_system: wire.source_system,
    source_ref: wire.source_ref,
    properties: wire.properties,
    assurance: wire.assurance,
  };
}

/** Resolve edges to node references by key, dropping any dangling edge whose
 * endpoints are not both present (the server already filters these; this is
 * defense-in-depth so a stray edge never crashes the layout). */
function resolveEdges(edges: GraphEdgeWire[], byKey: Map<string, ChamberNode>): ChamberEdge[] {
  const out: ChamberEdge[] = [];
  for (const e of edges) {
    const source = byKey.get(e.from_key);
    const target = byKey.get(e.to_key);
    if (!source || !target) continue;
    out.push({
      key: e.key,
      edge_kind: e.edge_kind,
      from_key: e.from_key,
      to_key: e.to_key,
      source_system: e.source_system,
      source_ref: e.source_ref,
      assurance: e.assurance,
      source,
      target,
    });
  }
  return out;
}

/** Convert an ACL-trimmed SubgraphWire into a fresh ChamberGraph (no positions
 * yet — buildSimulation seeds them). */
export function toChamberGraph(sub: SubgraphWire): ChamberGraph {
  const nodes = sub.nodes.map(toNode);
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  return { nodes, edges: resolveEdges(sub.edges, byKey), truncated: sub.truncated };
}

/** Merge an incoming SubgraphWire (a BLOOM neighborhood) into the current
 * ChamberGraph: dedupe nodes AND edges by key, PRESERVE existing node positions
 * (so the bloom radiates from a stable core), and enforce the client caps.
 * `overflowed` is true iff the merge would exceed 500 nodes or 2000 edges — the
 * excess is dropped and the caller shows the by-design truncation placard. */
export function mergeSubgraphs(
  current: ChamberGraph,
  incoming: SubgraphWire,
): { merged: ChamberGraph; overflowed: boolean } {
  const byKey = new Map<string, ChamberNode>();
  // Existing nodes first — their x/y/vx/vy ride along, anchoring the layout.
  for (const n of current.nodes) byKey.set(n.key, n);

  let nodeOverflow = false;
  for (const wire of incoming.nodes) {
    if (byKey.has(wire.key)) continue; // dedupe by key
    if (byKey.size >= CLIENT_NODE_CAP) {
      nodeOverflow = true;
      continue;
    }
    byKey.set(wire.key, toNode(wire));
  }
  const nodes = [...byKey.values()];

  // Dedupe edges by key across current + incoming; resolve against the merged
  // node set; cap at the edge limit.
  const edgeByKey = new Map<string, ChamberEdge>();
  for (const e of current.edges) edgeByKey.set(e.key, e);
  const incomingResolved = resolveEdges(incoming.edges, byKey);
  let edgeOverflow = false;
  for (const e of incomingResolved) {
    if (edgeByKey.has(e.key)) continue;
    if (edgeByKey.size >= CLIENT_EDGE_CAP) {
      edgeOverflow = true;
      continue;
    }
    edgeByKey.set(e.key, e);
  }
  // Re-point every kept edge at the merged node objects (an existing edge may
  // reference a node object that is still current — identity is preserved — so
  // this is a no-op in practice, but guarantees no stale ref survives).
  const edges: ChamberEdge[] = [];
  for (const e of edgeByKey.values()) {
    const source = byKey.get(e.from_key);
    const target = byKey.get(e.to_key);
    if (!source || !target) continue;
    edges.push({ ...e, source, target });
  }

  const overflowed = nodeOverflow || edgeOverflow;
  return {
    merged: {
      nodes,
      edges,
      truncated: current.truncated || incoming.truncated || overflowed,
    },
    overflowed,
  };
}

/** Build (and immediately stop) a d3-force simulation over a ChamberGraph. The
 * forces are the §5.1 recipe: charge repulsion (quadtree-accelerated), link
 * distance, collision, and a weak centering pull. Existing node positions are
 * respected (d3 only seeds NaN positions), so a merged graph blooms from its
 * stable core. Caller drives ticks via settle()/rAF — never the internal timer. */
export function buildSimulation(
  graph: ChamberGraph,
  width = DEFAULT_WIDTH,
  height = DEFAULT_HEIGHT,
): ChamberSimulation {
  const sim = forceSimulation<ChamberNode, ChamberEdge>(graph.nodes);
  sim.stop(); // WE drive ticks; keep the sim off the internal rAF/timer loop.
  sim
    .force(
      "link",
      forceLink<ChamberNode, ChamberEdge>(graph.edges)
        .id((n) => n.key)
        .distance(70)
        .strength(0.35),
    )
    .force("charge", forceManyBody<ChamberNode>().strength(-140).distanceMax(320))
    .force("collide", forceCollide<ChamberNode>(NODE_RADIUS * 2))
    .force("center", forceCenter<ChamberNode>(width / 2, height / 2))
    .force("x", forceX<ChamberNode>(width / 2).strength(0.03))
    .force("y", forceY<ChamberNode>(height / 2).strength(0.03))
    .velocityDecay(0.5);
  return sim;
}

/** Tick the simulation to the static-friction freeze: until alpha < 0.005 or a
 * hard cap of 300 ticks (design §5.1). Synchronous — the caller uses this for
 * the reduced-motion static frame and for deterministic test layouts. */
export function settle(sim: ChamberSimulation, maxTicks = 300): void {
  for (let i = 0; i < maxTicks; i++) {
    sim.tick();
    if (sim.alpha() < 0.005) break;
  }
}
