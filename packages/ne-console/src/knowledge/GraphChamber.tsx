// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The <canvas> graph renderer. It owns the DPR-aware canvas and the d3-force
// draw loop, but ALL interaction and assertion routes through an accessible DOM
// sibling layer (a visually-hidden list with one button per node), so screen
// readers and tests drive selection without touching pixels — and so jsdom (no
// 2D context, no ResizeObserver) degrades cleanly.
//
// REDESIGN §7: the canvas and its selection layer, and nothing else. The node/
// edge count and the truncation state moved OUT of here and onto the tab's
// toolbar — "how much of this am I seeing" belongs beside the count of what is
// being seen, not floating over the picture (§2.2).
//
// Perf/motion discipline (design law 4 + §5.1): ticks are driven by us off the
// render loop; rAF runs ONLY while alpha ≥ 0.005; prefers-reduced-motion settles
// synchronously and paints one static frame. If getContext('2d') is null (jsdom)
// we skip drawing entirely and never schedule a frame.

import { useEffect, useId, useRef, useState } from "react";
import type { ChamberGraph, ChamberNode, ChamberSimulation } from "./chamberSim";
import { buildSimulation, DEFAULT_HEIGHT, DEFAULT_WIDTH, NODE_RADIUS, settle } from "./chamberSim";
import { drawChamber, type ChamberView } from "./drawChamber";
import { readChamberTheme } from "./chamberTheme";
import {
  fitGraphViewport,
  GRAPH_KEYBOARD_PAN,
  GRAPH_ZOOM_FACTOR,
  identityViewport,
  panViewport,
  screenToWorld,
  zoomViewportAt,
  type GraphViewport,
} from "./graph/graphViewport";
import "./graph/graphViewport.css";

export interface GraphChamberProps {
  graph: ChamberGraph;
  view: ChamberView;
  onSelectNode: (key: string) => void;
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

export function GraphChamber({ graph, view, onSelectNode }: GraphChamberProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const simRef = useRef<ChamberSimulation | null>(null);
  const drawRef = useRef<() => void>(() => {});
  const viewportRef = useRef<GraphViewport>(identityViewport());
  const dragRef = useRef<{
    pointerId: number;
    x: number;
    y: number;
    moved: boolean;
  } | null>(null);
  const suppressClickRef = useRef(false);
  const viewRef = useRef<ChamberView>({ ...view, focusedKey: null, pointerKey: null });
  const semanticId = useId();
  const [focusedKey, setFocusedKey] = useState<string | null>(null);
  const [pointerKey, setPointerKey] = useState<string | null>(null);
  const [viewport, setViewport] = useState<GraphViewport>(() => identityViewport());

  const [size, setSize] = useState<{ width: number; height: number }>({
    width: DEFAULT_WIDTH,
    height: DEFAULT_HEIGHT,
  });

  // Measure the chamber via ResizeObserver where available (guarded for jsdom).
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

  // Build + run the simulation whenever the graph or measured size changes.
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas ? canvas.getContext("2d") : null;
    const { width, height } = size;
    const sim = buildSimulation(graph, width, height);
    simRef.current = sim;

    if (!ctx || !canvas) {
      // No 2D context (jsdom): settle once so positions exist, never rAF.
      settle(sim);
      drawRef.current = () => {};
      return () => {
        sim.stop();
      };
    }

    const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));

    const renderFrame = () => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawChamber(
        ctx,
        sim.nodes(),
        graph.edges,
        viewRef.current,
        readChamberTheme(),
        width,
        height,
        viewportRef.current,
      );
    };
    drawRef.current = renderFrame;

    if (prefersReducedMotion()) {
      settle(sim);
      renderFrame();
      return () => {
        sim.stop();
      };
    }

    let raf = 0;
    const loop = () => {
      sim.tick();
      renderFrame();
      if (sim.alpha() >= 0.005) {
        raf = requestAnimationFrame(loop);
      }
    };
    raf = requestAnimationFrame(loop);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      sim.stop();
    };
  }, [graph, size]);

  // Repaint (without rebuilding physics) when the highlight view changes.
  useEffect(() => {
    viewRef.current = { ...view, focusedKey, pointerKey };
    drawRef.current();
  }, [focusedKey, pointerKey, view]);

  // Viewport changes repaint settled/current physics without rebuilding it.
  useEffect(() => {
    viewportRef.current = viewport;
    drawRef.current();
  }, [viewport]);

  const updateViewport = (next: GraphViewport) => {
    viewportRef.current = next;
    setViewport(next);
  };

  const centre = () => ({ x: size.width / 2, y: size.height / 2 });
  const zoomBy = (factor: number, anchor = centre()) => {
    updateViewport(zoomViewportAt(viewportRef.current, factor, anchor));
  };
  const fit = () => {
    const nodes = simRef.current?.nodes() ?? [];
    updateViewport(fitGraphViewport(nodes, size, NODE_RADIUS));
  };
  const resetViewport = () => updateViewport(identityViewport());

  const nodeAt = (clientX: number, clientY: number): ChamberNode | undefined => {
    const sim = simRef.current;
    const canvas = canvasRef.current;
    if (!sim || !canvas) return undefined;
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    const world = screenToWorld(viewportRef.current, { x, y });
    return sim.find(world.x, world.y, (NODE_RADIUS * 2.5) / viewportRef.current.scale);
  };

  const handleCanvasMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (drag?.pointerId === event.pointerId) {
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      drag.x = event.clientX;
      drag.y = event.clientY;
      drag.moved = drag.moved || Math.abs(dx) + Math.abs(dy) > 1;
      updateViewport(panViewport(viewportRef.current, dx, dy, size));
      setPointerKey(null);
      return;
    }
    const next = nodeAt(event.clientX, event.clientY)?.key ?? null;
    setPointerKey((current) => (current === next ? current : next));
  };

  const handlePointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      moved: false,
    };
  };

  const finishPointer = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (drag?.pointerId !== event.pointerId) return;
    suppressClickRef.current = drag.moved;
    dragRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };

  // Canvas click -> nearest-node hit test -> selection (screen readers use the
  // DOM buttons below instead).
  const handleCanvasClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    const hit = nodeAt(e.clientX, e.clientY);
    if (hit) onSelectNode(hit.key);
  };

  const handleWheel = (event: React.WheelEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    zoomBy(event.deltaY < 0 ? GRAPH_ZOOM_FACTOR : 1 / GRAPH_ZOOM_FACTOR, {
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    });
  };

  const handleViewportKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    let next: GraphViewport | null = null;
    if (event.key === "ArrowLeft")
      next = panViewport(viewportRef.current, GRAPH_KEYBOARD_PAN, 0, size);
    else if (event.key === "ArrowRight")
      next = panViewport(viewportRef.current, -GRAPH_KEYBOARD_PAN, 0, size);
    else if (event.key === "ArrowUp")
      next = panViewport(viewportRef.current, 0, GRAPH_KEYBOARD_PAN, size);
    else if (event.key === "ArrowDown")
      next = panViewport(viewportRef.current, 0, -GRAPH_KEYBOARD_PAN, size);
    else if (event.key === "+" || event.key === "=")
      next = zoomViewportAt(viewportRef.current, GRAPH_ZOOM_FACTOR, centre());
    else if (event.key === "-" || event.key === "_")
      next = zoomViewportAt(viewportRef.current, 1 / GRAPH_ZOOM_FACTOR, centre());
    else if (event.key === "0") next = identityViewport();
    else if (event.key.toLowerCase() === "f") {
      event.preventDefault();
      fit();
      return;
    }
    if (next !== null) {
      event.preventDefault();
      updateViewport(next);
    }
  };

  return (
    <div ref={wrapRef} className="ne-chamber">
      <div
        className="ne-chamber__viewport"
        role="region"
        aria-label="Graph viewport"
        aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown + - 0 F"
        tabIndex={0}
        data-viewport-scale={viewport.scale.toFixed(3)}
        data-viewport-x={viewport.x.toFixed(1)}
        data-viewport-y={viewport.y.toFixed(1)}
        onKeyDown={handleViewportKey}
      >
        <canvas
          ref={canvasRef}
          className="ne-chamber__canvas"
          data-node-hovered={pointerKey === null ? undefined : "true"}
          onPointerDown={handlePointerDown}
          onPointerMove={handleCanvasMove}
          onPointerUp={finishPointer}
          onPointerCancel={finishPointer}
          onPointerLeave={() => setPointerKey(null)}
          onWheel={handleWheel}
          onClick={handleCanvasClick}
          aria-hidden="true"
        />
      </div>
      <div className="ne-chamber__viewport-controls" role="group" aria-label="Graph view controls">
        <button
          type="button"
          aria-label="Zoom out"
          title="Zoom out (-)"
          onClick={() => zoomBy(1 / GRAPH_ZOOM_FACTOR)}
        >
          −
        </button>
        <output className="ne-chamber__viewport-zoom" aria-live="polite">
          {Math.round(viewport.scale * 100)}%
        </output>
        <button
          type="button"
          aria-label="Zoom in"
          title="Zoom in (+)"
          onClick={() => zoomBy(GRAPH_ZOOM_FACTOR)}
        >
          +
        </button>
        <button type="button" onClick={fit} title="Fit graph (F)">
          Fit
        </button>
        <button type="button" onClick={resetViewport} title="Reset view (0)">
          Reset
        </button>
      </div>
      {/* Accessible interaction layer: one button per node. Visually hidden
          (the canvas is the visual), but it is the real selection surface for
          assistive tech and tests. */}
      <ul className="ne-chamber__nodes ne-visually-hidden" aria-label="Graph nodes">
        {graph.nodes.map((n, index) => {
          const lit = view.litKeys.has(n.key);
          const descriptionId = `${semanticId}-anchor-${index}`;
          return (
            <li key={n.key}>
              <button
                type="button"
                aria-pressed={view.selectedKey === n.key}
                aria-describedby={lit ? descriptionId : undefined}
                data-lit={lit ? "true" : undefined}
                data-focused={focusedKey === n.key ? "true" : undefined}
                data-pointer={pointerKey === n.key ? "true" : undefined}
                data-entity-key={n.key}
                onFocus={() => setFocusedKey(n.key)}
                onBlur={() => setFocusedKey(null)}
                onClick={() => onSelectNode(n.key)}
              >
                {n.display_name} · {n.entity_class}
              </button>
              {lit ? (
                <span className="ne-visually-hidden" id={descriptionId}>
                  Anchor node
                </span>
              ) : null}
            </li>
          );
        })}
      </ul>
      <ul className="ne-chamber__relationships ne-visually-hidden" aria-label="Graph relationships">
        {graph.edges.map((edge) => (
          <li key={edge.key}>
            {edge.source.display_name} — {edge.edge_kind} → {edge.target.display_name}. Directed
            from {edge.source.display_name} to {edge.target.display_name}.
          </li>
        ))}
      </ul>
    </div>
  );
}
