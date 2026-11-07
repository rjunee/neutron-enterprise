// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 — the ADDITIVE trim-contraction overlay (design §5.3, §5.4). It renders
// the UNION of the two server-trimmed subgraphs (key A ∪ key B) and animates the
// difference: nodes present under key A but ABSENT under key B implode (damped
// scale-to-zero, distance-staggered), edges retract, and the node-count ticker
// rolls DOWN to key B's server-returned counts (drum feel). NO red X's — the
// removal is honest: the client only ever renders what each key's server response
// contained (§0 law 5 / C7). It does NOT modify GraphChamber/drawChamber.ts — it
// mirrors their jsdom-safe pattern (guarded getContext/matchMedia, canvas skipped
// under test) and drives ALL assertion + AT surface through a DOM node list whose
// data-compare-state flips on the SAME schedule as the canvas.

import { useEffect, useMemo, useRef, useState } from "react";
import type { SubgraphWire } from "../../api/types";
import {
  buildSimulation,
  DEFAULT_HEIGHT,
  DEFAULT_WIDTH,
  NODE_RADIUS,
  settle,
  toChamberGraph,
} from "../chamberSim";
import { readChamberTheme } from "../chamberTheme";
import { IMPLODE_MS, implodeScale, planContraction } from "./compareMotion";

export interface CompareChamberProps {
  graphA: SubgraphWire;
  graphB: SubgraphWire;
  anchorKeys: string[];
  anchorCitations: string[];
  /** Fired once the contraction has fully settled (final frame reached). */
  onSettled?: () => void;
}

type CompareState = "imploding" | "imploded" | "retained" | "b-only";

/** Key-B-only nodes fade in over this tail after the contraction settles. */
const B_ONLY_FADE_MS = 200;

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/** Union the two trimmed subgraphs by key (nodes AND edges), A first so its
 * source objects win on a tie — we only render what one of the two servers sent. */
function unionSubgraph(a: SubgraphWire, b: SubgraphWire): SubgraphWire {
  const nodes = [...a.nodes];
  const nodeKeys = new Set(a.nodes.map((n) => n.key));
  for (const n of b.nodes) if (!nodeKeys.has(n.key)) nodes.push(n);
  const edges = [...a.edges];
  const edgeKeys = new Set(a.edges.map((e) => e.key));
  for (const e of b.edges) if (!edgeKeys.has(e.key)) edges.push(e);
  return { nodes, edges, truncated: a.truncated || b.truncated };
}

export function CompareChamber({
  graphA,
  graphB,
  anchorKeys,
  anchorCitations,
  onSettled,
}: CompareChamberProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const settledRef = useRef(onSettled);
  useEffect(() => {
    settledRef.current = onSettled;
  }, [onSettled]);

  const reduced = useMemo(() => prefersReducedMotion(), []);
  const plan = useMemo(
    () => planContraction(graphA, graphB, anchorKeys, anchorCitations),
    [graphA, graphB, anchorKeys, anchorCitations],
  );
  const union = useMemo(() => unionSubgraph(graphA, graphB), [graphA, graphB]);
  const chamberGraph = useMemo(() => toChamberGraph(union), [union]);

  const classify = useMemo(() => {
    const implodingKeys = new Map(plan.imploding.map((e) => [e.key, e.delayMs]));
    const bOnly = new Set(plan.bOnly);
    const finalOf = (key: string): CompareState =>
      implodingKeys.has(key) ? "imploded" : bOnly.has(key) ? "b-only" : "retained";
    const initialOf = (key: string): CompareState =>
      implodingKeys.has(key) ? "imploding" : bOnly.has(key) ? "b-only" : "retained";
    return { implodingKeys, finalOf, initialOf };
  }, [plan]);

  // DOM-mirror compare states — the test/AT surface. Lazily initialized to the
  // FINAL states under reduced motion (one static frame, no timers), or to the
  // pre-contraction states otherwise.
  const [states, setStates] = useState<Record<string, CompareState>>(() => {
    const out: Record<string, CompareState> = {};
    for (const n of union.nodes)
      out[n.key] = reduced ? classify.finalOf(n.key) : classify.initialOf(n.key);
    return out;
  });

  const [ticker, setTicker] = useState<{ nodes: number; edges: number }>(() =>
    reduced
      ? { nodes: graphB.nodes.length, edges: graphB.edges.length }
      : { nodes: graphA.nodes.length, edges: graphA.edges.length },
  );

  // The contraction schedule (DOM mirror + ticker). Timers only — the lazy
  // initializers above own the first frame, so no synchronous set-state-in-effect.
  useEffect(() => {
    if (reduced) {
      // Final state is already set by the initializers; just announce settle.
      settledRef.current?.();
      return;
    }
    const timers: ReturnType<typeof setTimeout>[] = [];
    for (const entry of plan.imploding) {
      timers.push(
        setTimeout(() => {
          setStates((cur) => ({ ...cur, [entry.key]: "imploded" }));
          setTicker((cur) => ({ nodes: cur.nodes - 1, edges: cur.edges }));
        }, entry.delayMs + IMPLODE_MS),
      );
    }
    timers.push(
      setTimeout(() => {
        // Snap to key B's server-returned counts exactly (accounts for b-only).
        setTicker({ nodes: graphB.nodes.length, edges: graphB.edges.length });
        settledRef.current?.();
      }, plan.totalMs),
    );
    return () => {
      for (const t of timers) clearTimeout(t);
    };
  }, [plan, reduced, graphB]);

  // Measure the overlay for the canvas (guarded for jsdom).
  const [size, setSize] = useState<{ width: number; height: number }>({
    width: DEFAULT_WIDTH,
    height: DEFAULT_HEIGHT,
  });
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (box && box.width > 0 && box.height > 0) {
        setSize({ width: Math.round(box.width), height: Math.round(box.height) });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Canvas draw loop. Null 2D context (jsdom) → skip drawing, never rAF.
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas ? canvas.getContext("2d") : null;
    if (!ctx || !canvas) return;

    const { width, height } = size;
    const sim = buildSimulation(chamberGraph, width, height);
    settle(sim);
    const posByKey = new Map(sim.nodes().map((n) => [n.key, n]));

    const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    const theme = readChamberTheme();
    const totalWindow = plan.totalMs + B_ONLY_FADE_MS;

    const scaleAt = (key: string, elapsed: number): number => {
      const delay = classify.implodingKeys.get(key);
      if (delay === undefined) return 1; // retained or b-only stay full size
      const progress = (elapsed - delay) / IMPLODE_MS;
      return implodeScale(progress);
    };
    const alphaAt = (key: string, elapsed: number): number => {
      if (!plan.bOnly.includes(key)) return 1;
      const t = (elapsed - plan.totalMs) / B_ONLY_FADE_MS;
      return t <= 0 ? 0 : t >= 1 ? 1 : t;
    };

    const start = typeof performance !== "undefined" ? performance.now() : Date.now();
    const draw = (elapsed: number) => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      // Edges retract with the smaller of their two endpoint scales.
      ctx.strokeStyle = theme.cherenkovDeep;
      ctx.lineWidth = 1;
      for (const e of chamberGraph.edges) {
        const s = posByKey.get(e.from_key);
        const t = posByKey.get(e.to_key);
        if (!s || !t) continue;
        const k = Math.min(scaleAt(e.from_key, elapsed), scaleAt(e.to_key, elapsed));
        const a = Math.min(alphaAt(e.from_key, elapsed), alphaAt(e.to_key, elapsed));
        if (k <= 0.01 || a <= 0.01) continue;
        // Retract: shrink the segment toward its source endpoint.
        const mx = s.x! + (t.x! - s.x!) * k;
        const my = s.y! + (t.y! - s.y!) * k;
        ctx.globalAlpha = a * 0.6;
        ctx.beginPath();
        ctx.moveTo(s.x!, s.y!);
        ctx.lineTo(mx, my);
        ctx.stroke();
      }
      // Nodes.
      for (const n of chamberGraph.nodes) {
        const k = scaleAt(n.key, elapsed);
        const a = alphaAt(n.key, elapsed);
        if (k <= 0.01 || a <= 0.01) continue;
        ctx.globalAlpha = a;
        ctx.fillStyle = classify.implodingKeys.has(n.key) ? theme.cherenkovDeep : theme.cherenkov;
        ctx.beginPath();
        ctx.arc(n.x!, n.y!, NODE_RADIUS * k, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    };

    if (reduced) {
      draw(totalWindow); // one static final frame
      return () => {
        sim.stop();
      };
    }
    let raf = 0;
    const loop = () => {
      const now = typeof performance !== "undefined" ? performance.now() : Date.now();
      const elapsed = now - start;
      draw(elapsed);
      if (elapsed < totalWindow) raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      sim.stop();
    };
  }, [chamberGraph, plan, reduced, size, classify]);

  return (
    <div ref={wrapRef} className="ne-compare__chamber">
      <canvas ref={canvasRef} className="ne-compare__canvas" aria-hidden="true" />
      <div className="ne-compare__ticker ne-mono" aria-live="polite">
        {ticker.nodes} NODES · {ticker.edges} EDGES
      </div>
      {/* The accessible / test surface: one entry per union node, its
          data-compare-state flipping on the same schedule as the canvas. */}
      <ul className="ne-compare__nodes ne-visually-hidden" aria-label="Compare nodes">
        {union.nodes.map((n) => (
          <li key={n.key}>
            <span data-entity-key={n.key} data-compare-state={states[n.key] ?? "retained"}>
              {n.display_name} · {n.entity_class}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
