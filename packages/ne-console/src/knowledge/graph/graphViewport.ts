// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

export interface GraphPoint {
  readonly x: number;
  readonly y: number;
}

export interface GraphSize {
  readonly width: number;
  readonly height: number;
}

export interface GraphViewport {
  /** Screen-space translation in CSS pixels. */
  readonly x: number;
  readonly y: number;
  readonly scale: number;
}

export interface PositionedGraphItem {
  readonly x?: number;
  readonly y?: number;
}

export const GRAPH_MIN_SCALE = 0.5;
export const GRAPH_MAX_SCALE = 3;
export const GRAPH_ZOOM_FACTOR = 1.2;
export const GRAPH_KEYBOARD_PAN = 32;
export const GRAPH_FIT_PADDING = 48;

export function identityViewport(): GraphViewport {
  return { x: 0, y: 0, scale: 1 };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function finite(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback;
}

/** Project a simulation/world coordinate into the canvas's CSS-pixel space. */
export function worldToScreen(viewport: GraphViewport, point: GraphPoint): GraphPoint {
  return {
    x: point.x * viewport.scale + viewport.x,
    y: point.y * viewport.scale + viewport.y,
  };
}

/** The inverse used for canvas hit testing after zoom and pan. */
export function screenToWorld(viewport: GraphViewport, point: GraphPoint): GraphPoint {
  const scale = clamp(finite(viewport.scale, 1), GRAPH_MIN_SCALE, GRAPH_MAX_SCALE);
  return {
    x: (point.x - finite(viewport.x, 0)) / scale,
    y: (point.y - finite(viewport.y, 0)) / scale,
  };
}

/**
 * Zoom around a stable screen-space point, keeping the world point beneath the
 * pointer or chamber centre fixed. Scale always remains within the UI bounds.
 */
export function zoomViewportAt(
  viewport: GraphViewport,
  factor: number,
  anchor: GraphPoint,
): GraphViewport {
  const oldScale = clamp(finite(viewport.scale, 1), GRAPH_MIN_SCALE, GRAPH_MAX_SCALE);
  const nextScale = clamp(oldScale * finite(factor, 1), GRAPH_MIN_SCALE, GRAPH_MAX_SCALE);
  const ratio = nextScale / oldScale;
  return {
    x: anchor.x - (anchor.x - finite(viewport.x, 0)) * ratio,
    y: anchor.y - (anchor.y - finite(viewport.y, 0)) * ratio,
    scale: nextScale,
  };
}

/**
 * Pan within a generous finite envelope. This prevents an accidental held key
 * or pointer stream from sending the graph to unbounded coordinates while
 * retaining ample room to inspect a large server-capped neighborhood.
 */
export function panViewport(
  viewport: GraphViewport,
  dx: number,
  dy: number,
  size: GraphSize,
): GraphViewport {
  const limit = Math.max(1, finite(size.width, 1), finite(size.height, 1)) * 4;
  return {
    x: clamp(finite(viewport.x, 0) + finite(dx, 0), -limit, limit),
    y: clamp(finite(viewport.y, 0) + finite(dy, 0), -limit, limit),
    scale: clamp(finite(viewport.scale, 1), GRAPH_MIN_SCALE, GRAPH_MAX_SCALE),
  };
}

/** Fit every positioned node, including its glyph radius, inside the chamber. */
export function fitGraphViewport(
  items: readonly PositionedGraphItem[],
  size: GraphSize,
  nodeRadius: number,
  padding = GRAPH_FIT_PADDING,
): GraphViewport {
  const positioned = items.filter(
    (item): item is { x: number; y: number } =>
      typeof item.x === "number" &&
      Number.isFinite(item.x) &&
      typeof item.y === "number" &&
      Number.isFinite(item.y),
  );
  if (positioned.length === 0 || size.width <= 0 || size.height <= 0) return identityViewport();

  const radius = Math.max(0, finite(nodeRadius, 0));
  const inset = Math.max(0, finite(padding, GRAPH_FIT_PADDING));
  const minX = Math.min(...positioned.map((item) => item.x - radius));
  const maxX = Math.max(...positioned.map((item) => item.x + radius));
  const minY = Math.min(...positioned.map((item) => item.y - radius));
  const maxY = Math.max(...positioned.map((item) => item.y + radius));
  const spanX = Math.max(1, maxX - minX);
  const spanY = Math.max(1, maxY - minY);
  const availableWidth = Math.max(1, size.width - inset * 2);
  const availableHeight = Math.max(1, size.height - inset * 2);
  const scale = clamp(
    Math.min(availableWidth / spanX, availableHeight / spanY),
    GRAPH_MIN_SCALE,
    GRAPH_MAX_SCALE,
  );
  return {
    x: size.width / 2 - ((minX + maxX) / 2) * scale,
    y: size.height / 2 - ((minY + maxY) / 2) * scale,
    scale,
  };
}
