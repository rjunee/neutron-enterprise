// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import {
  fitGraphViewport,
  GRAPH_MAX_SCALE,
  GRAPH_MIN_SCALE,
  identityViewport,
  panViewport,
  screenToWorld,
  worldToScreen,
  zoomViewportAt,
} from "./graphViewport";

describe("Graph viewport", () => {
  it("round-trips world coordinates through a zoomed and panned viewport", () => {
    const viewport = { x: -137.5, y: 84.25, scale: 2.4 };
    const world = { x: 912.75, y: -44.5 };
    expect(screenToWorld(viewport, worldToScreen(viewport, world))).toEqual(world);
  });

  it("clamps zoom while keeping the anchor's world point stationary", () => {
    const anchor = { x: 300, y: 180 };
    const original = { x: -20, y: 40, scale: 1 };
    const anchoredWorld = screenToWorld(original, anchor);
    const zoomed = zoomViewportAt(original, 2, anchor);
    expect(worldToScreen(zoomed, anchoredWorld)).toEqual(anchor);
    expect(zoomViewportAt(zoomed, 100, anchor).scale).toBe(GRAPH_MAX_SCALE);
    expect(zoomViewportAt(original, 0.001, anchor).scale).toBe(GRAPH_MIN_SCALE);
  });

  it("bounds repeated pan input to a finite chamber-relative envelope", () => {
    expect(panViewport(identityViewport(), 100_000, -100_000, { width: 900, height: 600 })).toEqual(
      { x: 3600, y: -3600, scale: 1 },
    );
  });

  it("fits every node glyph within the requested padding", () => {
    const nodes = [
      { x: -200, y: 20 },
      { x: 40, y: 300 },
      { x: 780, y: -140 },
    ];
    const size = { width: 960, height: 540 };
    const radius = 9;
    const padding = 48;
    const viewport = fitGraphViewport(nodes, size, radius, padding);

    for (const node of nodes) {
      const point = worldToScreen(viewport, node);
      const scaledRadius = radius * viewport.scale;
      expect(point.x - scaledRadius).toBeGreaterThanOrEqual(padding);
      expect(point.x + scaledRadius).toBeLessThanOrEqual(size.width - padding);
      expect(point.y - scaledRadius).toBeGreaterThanOrEqual(padding);
      expect(point.y + scaledRadius).toBeLessThanOrEqual(size.height - padding);
    }
  });

  it("returns the stable identity for an empty or unpositioned graph", () => {
    expect(fitGraphViewport([], { width: 960, height: 640 }, 9)).toEqual(identityViewport());
    expect(fitGraphViewport([{ x: undefined, y: 1 }], { width: 960, height: 640 }, 9)).toEqual(
      identityViewport(),
    );
  });
});
