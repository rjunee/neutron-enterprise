// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 — the PURE, unit-testable motion math for the trim contraction (design
// §5.4). It operates ONLY on the two ACL-trimmed wire subgraphs the server sent
// each keyholder: the "imploding" set is exactly A∖B (nodes key A saw that key B
// was never served), staggered by BFS hop distance from the anchor so the
// contraction radiates inward — never a fabricated removal (§0 law 5 / C7). No
// canvas, no React, no timers here: the CompareChamber consumes this plan.

import type { SubgraphWire } from "../../api/types";

/** Per-hop stagger (design §5.4 — distance-staggered implosion). */
export const STAGGER_MS = 60;
/** Damped scale-to-zero implode duration (design §5.4). */
export const IMPLODE_MS = 200;

/** One node scheduled to implode: its key and the delay before it starts. */
export interface ImplodeEntry {
  key: string;
  delayMs: number;
}

/** The full contraction plan derived from the two server responses. */
export interface ContractionPlan {
  /** A∖B: nodes present under key A, absent under key B — they implode. */
  imploding: ImplodeEntry[];
  /** A∩B: nodes both keys were served — they stay. */
  retained: string[];
  /** B∖A: nodes only key B was served — they fade in after the contraction. */
  bOnly: string[];
  /** Total schedule length: the last implode's delay + the implode duration. */
  totalMs: number;
}

/** Build the undirected adjacency map over graph A's edges (keys only). */
function adjacency(graph: SubgraphWire): Map<string, string[]> {
  const adj = new Map<string, string[]>();
  const push = (a: string, b: string) => {
    const list = adj.get(a);
    if (list) list.push(b);
    else adj.set(a, [b]);
  };
  for (const e of graph.edges) {
    push(e.from_key, e.to_key);
    push(e.to_key, e.from_key);
  }
  return adj;
}

/**
 * Multi-source BFS hop distance from the anchor node set over graph A. Every
 * A-node gets a distance; nodes unreachable from any anchor are assigned the
 * max reachable distance + 1 (the last implosion wave), so nothing is ever
 * scheduled before its anchor.
 */
function hopDistances(graphA: SubgraphWire, anchors: Set<string>): Map<string, number> {
  const dist = new Map<string, number>();
  const adj = adjacency(graphA);
  const queue: string[] = [];
  for (const n of graphA.nodes) {
    if (anchors.has(n.key)) {
      dist.set(n.key, 0);
      queue.push(n.key);
    }
  }
  let head = 0;
  let maxReachable = 0;
  while (head < queue.length) {
    const cur = queue[head++]!;
    const d = dist.get(cur)!;
    if (d > maxReachable) maxReachable = d;
    for (const next of adj.get(cur) ?? []) {
      if (!dist.has(next)) {
        dist.set(next, d + 1);
        queue.push(next);
      }
    }
  }
  // Unreachable A-nodes land in the final wave.
  for (const n of graphA.nodes) {
    if (!dist.has(n.key)) dist.set(n.key, maxReachable + 1);
  }
  return dist;
}

/**
 * Plan the trim contraction between the two server-trimmed subgraphs. `anchorKeys`
 * / `anchorCitations` locate the contraction origin exactly as the anchored query
 * did; when neither matches any A-node, every A-node is treated as an anchor
 * (distance 0) so the fallback is honest, not a crash.
 */
export function planContraction(
  graphA: SubgraphWire,
  graphB: SubgraphWire,
  anchorKeys: string[],
  anchorCitations: string[],
): ContractionPlan {
  const aKeys = new Set(graphA.nodes.map((n) => n.key));
  const bKeys = new Set(graphB.nodes.map((n) => n.key));

  const keySet = new Set(anchorKeys);
  const citSet = new Set(anchorCitations);
  const anchors = new Set<string>();
  for (const n of graphA.nodes) {
    if (keySet.has(n.key) || citSet.has(n.source_ref)) anchors.add(n.key);
  }
  // Fallback: no anchor matched — treat all A-nodes as distance-0 anchors.
  if (anchors.size === 0) for (const n of graphA.nodes) anchors.add(n.key);

  const dist = hopDistances(graphA, anchors);

  const imploding: ImplodeEntry[] = [];
  const retained: string[] = [];
  for (const n of graphA.nodes) {
    if (bKeys.has(n.key)) {
      retained.push(n.key);
    } else {
      imploding.push({ key: n.key, delayMs: (dist.get(n.key) ?? 0) * STAGGER_MS });
    }
  }
  // Deterministic order: nearer first, key-sorted tie-break.
  imploding.sort((a, b) => a.delayMs - b.delayMs || a.key.localeCompare(b.key));
  retained.sort((a, b) => a.localeCompare(b));

  const bOnly = graphB.nodes
    .map((n) => n.key)
    .filter((k) => !aKeys.has(k))
    .sort((a, b) => a.localeCompare(b));

  const maxDelay = imploding.reduce((m, e) => Math.max(m, e.delayMs), 0);
  return { imploding, retained, bOnly, totalMs: maxDelay + IMPLODE_MS };
}

function easeOutCubic(t: number): number {
  return 1 - Math.pow(1 - t, 3);
}

/**
 * Damped scale for an imploding node given normalized progress t ∈ [0,1]:
 * monotonically 1 → 0 (the node shrinks to nothing). Clamped so out-of-range
 * inputs are safe.
 */
export function implodeScale(t: number): number {
  const clamped = t <= 0 ? 0 : t >= 1 ? 1 : t;
  return 1 - easeOutCubic(clamped);
}
