// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// PURE canvas draw functions for the Graph Chamber. No React, no state, no
// network — just (ctx, nodes, edges, view, theme, size) -> pixels. All colour
// comes from the ChamberTheme (resolved from tokens); LUMINANCE grading is done
// exclusively with ctx.globalAlpha (design law 1 + the token-hardcode guardian
// bans hex/rgba in TS). Edges draw first (Cherenkov, alpha graded by highlight),
// then node glyphs, then a globalAlpha halo pass over lit/selected nodes.
//
// CR-B10 (screens/graph.html §canvas): selection is the ONE cherenkov spend —
// the selected node gets its own ring (drawSelectionRing), distinct from the
// softer halo every other lit/focused/hovered node still gets, and its OWN
// edges light up with their relationship kind named on the canvas
// (litEdgeKeys + drawEdgeLabels). An edge whose own assertion is unconfirmed
// draws dashed regardless of selection — that is an assurance fact, not a
// highlight.

import type { ChamberEdge, ChamberNode } from "./chamberSim";
import { NODE_RADIUS } from "./chamberSim";
import { edgeLabel, glyphFor } from "./glyphs";
import type { ChamberTheme } from "./chamberTheme";
import { identityViewport, worldToScreen, type GraphViewport } from "./graph/graphViewport";
import { isProvisionalAssurance } from "../primitives/assurance";

/** What is highlighted this frame — the render is a pure function of this. */
export interface ChamberView {
  /** The currently selected node (detail drawer target). */
  selectedKey: string | null;
  /** Nodes lit by a citation chip / verified-answer row (design §5.1). */
  litKeys: ReadonlySet<string>;
  /** Edges on the currently traced path (TRACE ignite). */
  highlightEdgeKeys: ReadonlySet<string>;
  /** Keyboard focus. It identifies a node but never activates it. */
  focusedKey?: string | null;
  /** Canvas pointer hover. It identifies a node but never activates it. */
  pointerKey?: string | null;
}

const DIM_EDGE_ALPHA = 0.22;
const HOT_EDGE_ALPHA = 1;
const HALO_ALPHA = 0.28;
const RING_ALPHA = 0.28;
const RING_RADIUS_SCALE = 2.6;
const RING_WIDTH = 6;
const UNCONFIRMED_DASH: readonly [number, number] = [5, 4];
const NO_DASH: readonly [] = [];
const LABEL_PANEL_ALPHA = 0.9;
const LABEL_GAP = 7;
const LABEL_PAD_X = 5;
const LABEL_PAD_Y = 4;
const LABEL_LINE_HEIGHT = 16;
const LABEL_EDGE = 8;
const LABEL_MAX_WIDTH = 280;
const LABEL_CLEARANCE = 4;
const ARROW_LENGTH = 8;
const ARROW_HALF_WIDTH = 4;
const EDGE_LABEL_PAD_X = 5;
const EDGE_LABEL_PAD_Y = 3;

export interface LabelRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LabelPlacement {
  key: string;
  priority: boolean;
  rect: LabelRect;
  name: string;
  meta: string;
}

/**
 * A compact neighborhood can name every node. A large graph cannot: 500 labels
 * would turn the picture into a wall of overlapping text. Above this limit the
 * graph progressively discloses only its orientation/interaction points — the
 * served anchor, keyboard/pointer hover, and selection.
 */
export const FULL_LABEL_NODE_LIMIT = 24;

function hasXY(n: ChamberNode): n is ChamberNode & { x: number; y: number } {
  return typeof n.x === "number" && typeof n.y === "number";
}

/** Clear the frame (CSS px space — caller has applied the DPR transform). */
export function clearChamber(ctx: CanvasRenderingContext2D, width: number, height: number): void {
  ctx.clearRect(0, 0, width, height);
}

/**
 * The edges lit this frame: the caller's explicit highlight set (TRACE
 * ignite / a verified-answer path) UNION the selected node's own relations.
 * "Selection is the one cherenkov spend" (screens/graph.html §canvas) — the
 * selected node's edges light up and name their kind (drawEdgeLabels below)
 * with NO extra caller wiring; every other edge stays at rest. Exported so
 * that fact is asserted once, directly, rather than re-derived per caller.
 */
export function litEdgeKeys(edges: readonly ChamberEdge[], view: ChamberView): ReadonlySet<string> {
  const lit = new Set(view.highlightEdgeKeys);
  if (view.selectedKey !== null) {
    for (const e of edges) {
      if (e.source.key === view.selectedKey || e.target.key === view.selectedKey) {
        lit.add(e.key);
      }
    }
  }
  return lit;
}

/** Draw the edges. A lit edge (traced or touching the selection) burns at full
 * luminance; the rest sit at a low alpha so the graphite ground reads
 * through. An edge whose own assertion is unconfirmed (extracted, not a
 * human-vouched source/confirmed label) draws dashed — an assurance fact
 * that holds regardless of whether the edge happens to be lit this frame. */
export function drawEdges(
  ctx: CanvasRenderingContext2D,
  edges: ChamberEdge[],
  view: ChamberView,
  theme: ChamberTheme,
): void {
  const lit = litEdgeKeys(edges, view);
  for (const e of edges) {
    if (!hasXY(e.source) || !hasXY(e.target)) continue;
    const angle = Math.atan2(e.target.y - e.source.y, e.target.x - e.source.x);
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const startX = e.source.x + cos * NODE_RADIUS;
    const startY = e.source.y + sin * NODE_RADIUS;
    const tipX = e.target.x - cos * NODE_RADIUS;
    const tipY = e.target.y - sin * NODE_RADIUS;
    const backX = tipX - cos * ARROW_LENGTH;
    const backY = tipY - sin * ARROW_LENGTH;
    const hot = lit.has(e.key);
    ctx.globalAlpha = hot ? HOT_EDGE_ALPHA : DIM_EDGE_ALPHA;
    ctx.lineWidth = hot ? 2 : 1;
    ctx.strokeStyle = theme.cherenkov;
    ctx.setLineDash(isProvisionalAssurance(e.assurance) ? [...UNCONFIRMED_DASH] : [...NO_DASH]);
    ctx.beginPath();
    ctx.moveTo(startX, startY);
    ctx.lineTo(tipX, tipY);
    // Direction is geometry, not colour: every served relationship terminates
    // in a visible arrowhead pointing at its exact served target.
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(backX + sin * ARROW_HALF_WIDTH, backY - cos * ARROW_HALF_WIDTH);
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(backX - sin * ARROW_HALF_WIDTH, backY + cos * ARROW_HALF_WIDTH);
    ctx.stroke();
  }
  ctx.setLineDash([...NO_DASH]);
  ctx.globalAlpha = 1;
}

/**
 * The selection ring — the one cherenkov spend (screens/graph.html §canvas):
 * a stroked ring around the selected node ONLY, distinct from the softer
 * halo every other lit/focused/hovered node still gets (drawn separately,
 * below). A no-op when nothing is selected or the selected node has no laid
 * -out position yet.
 */
export function drawSelectionRing(
  ctx: CanvasRenderingContext2D,
  nodes: readonly ChamberNode[],
  view: ChamberView,
  theme: ChamberTheme,
): void {
  if (view.selectedKey === null) return;
  const selected = nodes.find((n) => n.key === view.selectedKey);
  if (selected === undefined || !hasXY(selected)) return;
  ctx.globalAlpha = RING_ALPHA;
  ctx.strokeStyle = theme.cherenkov;
  ctx.lineWidth = RING_WIDTH;
  ctx.beginPath();
  ctx.arc(selected.x, selected.y, NODE_RADIUS * RING_RADIUS_SCALE, 0, Math.PI * 2);
  ctx.stroke();
  ctx.globalAlpha = 1;
}

/** Draw the node glyphs, with a Cherenkov halo pass over lit/focused/hovered
 * nodes (the selected node draws its own ring instead — drawSelectionRing). */
export function drawNodes(
  ctx: CanvasRenderingContext2D,
  nodes: ChamberNode[],
  view: ChamberView,
  theme: ChamberTheme,
): void {
  // Halo pass first (under the glyphs) for lit/focused/hovered nodes. The
  // selection gets its own distinct ring (drawSelectionRing), not this halo.
  for (const n of nodes) {
    if (!hasXY(n)) continue;
    if (n.key === view.selectedKey) continue;
    const lit = view.litKeys.has(n.key) || view.focusedKey === n.key || view.pointerKey === n.key;
    if (!lit) continue;
    ctx.globalAlpha = HALO_ALPHA;
    ctx.fillStyle = theme.cherenkovDeep;
    ctx.beginPath();
    ctx.arc(n.x, n.y, NODE_RADIUS * 2.2, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  // Glyph pass.
  ctx.lineWidth = 1.5;
  for (const n of nodes) {
    if (!hasXY(n)) continue;
    const lit =
      view.litKeys.has(n.key) ||
      view.selectedKey === n.key ||
      view.focusedKey === n.key ||
      view.pointerKey === n.key;
    ctx.globalAlpha = 1;
    ctx.fillStyle = theme.ground;
    ctx.strokeStyle = lit ? theme.cherenkov : theme.textDim;
    glyphFor(n.entity_class)(ctx, n.x, n.y, NODE_RADIUS);
  }
  ctx.globalAlpha = 1;
}

/** The exact nodes whose labels are visible in this frame. */
export function labelledNodeKeys(
  nodes: readonly ChamberNode[],
  view: ChamberView,
): ReadonlySet<string> {
  if (nodes.length <= FULL_LABEL_NODE_LIMIT) return new Set(nodes.map((node) => node.key));
  const keys = new Set(view.litKeys);
  if (view.focusedKey) keys.add(view.focusedKey);
  if (view.pointerKey) keys.add(view.pointerKey);
  if (view.selectedKey) keys.add(view.selectedKey);
  return keys;
}

function isPriorityLabel(key: string, view: ChamberView): boolean {
  return (
    view.litKeys.has(key) ||
    view.selectedKey === key ||
    view.focusedKey === key ||
    view.pointerKey === key
  );
}

function nodeMeta(node: ChamberNode, view: ChamberView): string {
  const states: string[] = [];
  if (view.litKeys.has(node.key)) states.push("anchor");
  if (view.selectedKey === node.key) states.push("selected");
  return [node.entity_class, ...states].join(" · ");
}

/** Preserve the exact value in the semantic DOM; fit only its canvas rendering. */
function fitLabel(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let end = text.length;
  while (end > 1 && ctx.measureText(`${text.slice(0, end)}…`).width > maxWidth) end -= 1;
  return `${text.slice(0, end)}…`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function overlaps(a: LabelRect, b: LabelRect): boolean {
  return !(
    a.x + a.width + LABEL_CLEARANCE <= b.x ||
    b.x + b.width + LABEL_CLEARANCE <= a.x ||
    a.y + a.height + LABEL_CLEARANCE <= b.y ||
    b.y + b.height + LABEL_CLEARANCE <= a.y
  );
}

function boundedRect(
  x: number,
  y: number,
  boxWidth: number,
  boxHeight: number,
  width: number,
  height: number,
): LabelRect {
  return {
    x: clamp(x, LABEL_EDGE, Math.max(LABEL_EDGE, width - LABEL_EDGE - boxWidth)),
    y: clamp(y, LABEL_EDGE, Math.max(LABEL_EDGE, height - LABEL_EDGE - boxHeight)),
    width: boxWidth,
    height: boxHeight,
  };
}

function sameRect(a: LabelRect, b: LabelRect): boolean {
  return a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;
}

/** Candidate order is stable so settled frames do not make labels jump. */
function localCandidates(
  node: ChamberNode & { x: number; y: number },
  boxWidth: number,
  boxHeight: number,
  width: number,
  height: number,
): LabelRect[] {
  const right = node.x + NODE_RADIUS + LABEL_GAP;
  const left = node.x - NODE_RADIUS - LABEL_GAP - boxWidth;
  const above = node.y - NODE_RADIUS - LABEL_GAP - boxHeight;
  const below = node.y + NODE_RADIUS + LABEL_GAP;
  const centreX = node.x - boxWidth / 2;
  const centreY = node.y - boxHeight / 2;
  const raw: Array<[number, number]> = [
    [right, centreY],
    [left, centreY],
    [centreX, above],
    [centreX, below],
    [right, above],
    [left, above],
    [right, below],
    [left, below],
  ];
  const candidates: LabelRect[] = [];
  for (const [x, y] of raw) {
    const rect = boundedRect(x, y, boxWidth, boxHeight, width, height);
    if (!candidates.some((candidate) => sameRect(candidate, rect))) candidates.push(rect);
  }
  return candidates;
}

/** Priority labels may use a deterministic canvas lane when local space is full. */
function priorityFallbackCandidates(
  boxWidth: number,
  boxHeight: number,
  width: number,
  height: number,
): LabelRect[] {
  const candidates: LabelRect[] = [];
  const maxX = Math.max(LABEL_EDGE, width - LABEL_EDGE - boxWidth);
  const maxY = Math.max(LABEL_EDGE, height - LABEL_EDGE - boxHeight);
  const stepX = Math.max(1, boxWidth + LABEL_CLEARANCE);
  const stepY = Math.max(1, boxHeight + LABEL_CLEARANCE);
  for (let y = LABEL_EDGE; y <= maxY; y += stepY) {
    for (let x = LABEL_EDGE; x <= maxX; x += stepX) {
      candidates.push({ x, y, width: boxWidth, height: boxHeight });
    }
    if ((maxX - LABEL_EDGE) % stepX !== 0) {
      candidates.push({ x: maxX, y, width: boxWidth, height: boxHeight });
    }
  }
  if ((maxY - LABEL_EDGE) % stepY !== 0) {
    for (let x = LABEL_EDGE; x <= maxX; x += stepX) {
      candidates.push({ x, y: maxY, width: boxWidth, height: boxHeight });
    }
    candidates.push({ x: maxX, y: maxY, width: boxWidth, height: boxHeight });
  }
  return candidates;
}

function measureLabel(
  ctx: CanvasRenderingContext2D,
  node: ChamberNode,
  view: ChamberView,
  theme: ChamberTheme,
  width: number,
): Omit<LabelPlacement, "rect"> {
  const maxTextWidth = Math.max(
    1,
    Math.min(LABEL_MAX_WIDTH, width - LABEL_EDGE * 2 - LABEL_PAD_X * 2),
  );
  ctx.font = theme.typeCell;
  const name = fitLabel(ctx, node.display_name, maxTextWidth);
  ctx.font = theme.typeSmall;
  const meta = fitLabel(ctx, nodeMeta(node, view), maxTextWidth);
  return {
    key: node.key,
    priority: isPriorityLabel(node.key, view),
    name,
    meta,
  };
}

function placeLabelGroup(
  ctx: CanvasRenderingContext2D,
  nodes: readonly ChamberNode[],
  view: ChamberView,
  theme: ChamberTheme,
  width: number,
  height: number,
  priority: boolean,
  occupied: readonly LabelPlacement[],
): LabelPlacement[] {
  const placements: LabelPlacement[] = [];
  const blocked = [...occupied];
  for (const node of nodes) {
    if (!hasXY(node) || isPriorityLabel(node.key, view) !== priority) continue;
    const measured = measureLabel(ctx, node, view, theme, width);
    ctx.font = theme.typeCell;
    const nameWidth = ctx.measureText(measured.name).width;
    ctx.font = theme.typeSmall;
    const metaWidth = ctx.measureText(measured.meta).width;
    const boxWidth = Math.max(nameWidth, metaWidth) + LABEL_PAD_X * 2;
    const boxHeight = LABEL_LINE_HEIGHT * 2 + LABEL_PAD_Y * 2;
    const local = localCandidates(node, boxWidth, boxHeight, width, height);
    const candidates = priority
      ? [...local, ...priorityFallbackCandidates(boxWidth, boxHeight, width, height)]
      : local;
    const rect = candidates.find((candidate) =>
      blocked.every((placement) => !overlaps(candidate, placement.rect)),
    );
    if (!rect) continue;
    const placement = { ...measured, rect };
    placements.push(placement);
    blocked.push(placement);
  }
  return placements;
}

/**
 * Measure and place labels without painting them. Priority interaction panels
 * reserve space first; ordinary panels can move to another local position or
 * disappear, but can never cover an anchor/focus/hover/selection panel.
 */
export function placeLabels(
  ctx: CanvasRenderingContext2D,
  nodes: readonly ChamberNode[],
  view: ChamberView,
  theme: ChamberTheme,
  width: number,
  height: number,
): LabelPlacement[] {
  const labelled = labelledNodeKeys(nodes, view);
  const candidates = nodes.filter((node) => labelled.has(node.key));
  const priority = placeLabelGroup(ctx, candidates, view, theme, width, height, true, []);
  const ordinary = placeLabelGroup(ctx, candidates, view, theme, width, height, false, priority);
  return [...priority, ...ordinary];
}

/**
 * Draw human names after the graph so labels stay legible over edges. The
 * display name is primary; class and anchor/selection state are the quiet
 * second line. Placement flips left near the right edge and clamps vertically,
 * keeping the label inside the measured chamber rather than clipping it.
 */
export function drawLabels(
  ctx: CanvasRenderingContext2D,
  nodes: readonly ChamberNode[],
  view: ChamberView,
  theme: ChamberTheme,
  width: number,
  height: number,
): LabelPlacement[] {
  const placements = placeLabels(ctx, nodes, view, theme, width, height);
  // Paint ordinary panels first as a final visual safeguard; the reserved
  // geometry already prevents overlap, while priority panels remain topmost.
  for (const placement of [...placements].sort((a, b) => Number(a.priority) - Number(b.priority))) {
    const { name, meta, rect } = placement;
    ctx.globalAlpha = LABEL_PANEL_ALPHA;
    ctx.fillStyle = theme.panel;
    ctx.fillRect(rect.x, rect.y, rect.width, rect.height);
    ctx.globalAlpha = 1;

    ctx.font = theme.typeCell;
    ctx.fillStyle = theme.text;
    ctx.textBaseline = "top";
    ctx.fillText(name, rect.x + LABEL_PAD_X, rect.y + LABEL_PAD_Y);

    ctx.font = theme.typeSmall;
    ctx.fillStyle = theme.textDim;
    ctx.fillText(meta, rect.x + LABEL_PAD_X, rect.y + LABEL_PAD_Y + LABEL_LINE_HEIGHT);
  }
  ctx.globalAlpha = 1;
  return placements;
}

/**
 * An edge's on-canvas kind label (screens/graph.html §canvas): the relation
 * kind, plus " · UNCONFIRMED" when the asserting edge itself is provisional
 * — "an extracted assertion is a dashed edge labelled ACQUIRED · UNCONFIRMED
 * — the claim and its assurance in the same glance." A PURE function of the
 * edge alone, independent of whether this frame actually draws it (that is
 * litEdgeKeys's decision), so it is directly assertable without a canvas.
 */
export function edgeKindLabel(edge: ChamberEdge): string {
  const kind = edgeLabel(edge.edge_kind);
  return isProvisionalAssurance(edge.assurance) ? `${kind} · UNCONFIRMED` : kind;
}

/**
 * Name the lit edges' relationship kinds at their midpoint — never on a
 * resting (unlit) edge. Colour is always `theme.textDim`, never a fainter
 * tone (NOTES.md "Named divergences": measured 9.3:1 dark / 5.2:1 light on
 * the sunken canvas).
 */
export function drawEdgeLabels(
  ctx: CanvasRenderingContext2D,
  edges: readonly ChamberEdge[],
  view: ChamberView,
  theme: ChamberTheme,
): void {
  const lit = litEdgeKeys(edges, view);
  for (const e of edges) {
    if (!lit.has(e.key) || !hasXY(e.source) || !hasXY(e.target)) continue;
    const text = edgeKindLabel(e);
    const midX = (e.source.x + e.target.x) / 2;
    const midY = (e.source.y + e.target.y) / 2;
    ctx.font = theme.typeSmall;
    const width = ctx.measureText(text).width;
    const boxWidth = width + EDGE_LABEL_PAD_X * 2;
    const boxHeight = LABEL_LINE_HEIGHT - 2;
    const rectX = midX - boxWidth / 2;
    const rectY = midY - boxHeight / 2;

    ctx.globalAlpha = LABEL_PANEL_ALPHA;
    ctx.fillStyle = theme.panel;
    ctx.fillRect(rectX, rectY, boxWidth, boxHeight);
    ctx.globalAlpha = 1;

    ctx.fillStyle = theme.textDim;
    ctx.textBaseline = "top";
    ctx.fillText(text, rectX + EDGE_LABEL_PAD_X, rectY + EDGE_LABEL_PAD_Y);
  }
}

/** Full-frame draw: clear, edges, nodes, the selection ring, then labels. */
export function drawChamber(
  ctx: CanvasRenderingContext2D,
  nodes: ChamberNode[],
  edges: ChamberEdge[],
  view: ChamberView,
  theme: ChamberTheme,
  width: number,
  height: number,
  viewport: GraphViewport = identityViewport(),
): void {
  clearChamber(ctx, width, height);
  const projected = projectChamberGraph(nodes, edges, viewport);
  drawEdges(ctx, projected.edges, view, theme);
  drawNodes(ctx, projected.nodes, view, theme);
  drawSelectionRing(ctx, projected.nodes, view, theme);
  drawLabels(ctx, projected.nodes, view, theme, width, height);
  drawEdgeLabels(ctx, projected.edges, view, theme);
}

/**
 * Project simulation coordinates without mutating d3's world state. Glyph and
 * label sizes therefore remain legible at every zoom level while positions,
 * edges and hit testing share one exact affine viewport.
 */
export function projectChamberGraph(
  nodes: readonly ChamberNode[],
  edges: readonly ChamberEdge[],
  viewport: GraphViewport,
): { nodes: ChamberNode[]; edges: ChamberEdge[] } {
  const projectedNodes = nodes.map((node) => {
    if (!hasXY(node)) return { ...node };
    const point = worldToScreen(viewport, node);
    return { ...node, x: point.x, y: point.y };
  });
  const byKey = new Map(projectedNodes.map((node) => [node.key, node]));
  const projectedEdges = edges.flatMap((edge) => {
    const source = byKey.get(edge.from_key);
    const target = byKey.get(edge.to_key);
    return source && target ? [{ ...edge, source, target }] : [];
  });
  return { nodes: projectedNodes, edges: projectedEdges };
}
