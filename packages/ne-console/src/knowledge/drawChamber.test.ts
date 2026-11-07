// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it, vi } from "vitest";
import { NODE_RADIUS, type ChamberEdge, type ChamberNode } from "./chamberSim";
import type { ChamberTheme } from "./chamberTheme";
import {
  drawLabels,
  drawEdgeLabels,
  drawEdges,
  drawNodes,
  drawSelectionRing,
  edgeKindLabel,
  FULL_LABEL_NODE_LIMIT,
  labelledNodeKeys,
  litEdgeKeys,
  placeLabels,
  projectChamberGraph,
  type LabelRect,
  type ChamberView,
} from "./drawChamber";

const THEME: ChamberTheme = {
  ground: "ground-token",
  panel: "panel-token",
  cherenkov: "cherenkov-token",
  cherenkovDeep: "deep-token",
  text: "text-token",
  textDim: "dim-token",
  rule: "rule-token",
  typeCell: "cell-font-token",
  typeSmall: "small-font-token",
};

function node(index: number): ChamberNode {
  return {
    key: `node-${index}`,
    entity_class: index === 0 ? "Account" : "Person",
    display_name: index === 0 ? "Harbor Metals" : `Person ${index}`,
    source_system: "fixture",
    source_ref: `fixture/${index}`,
    properties: {},
    assurance: "source",
    x: 80 + index,
    y: 80 + index,
  };
}

function view(overrides: Partial<ChamberView> = {}): ChamberView {
  return {
    selectedKey: null,
    litKeys: new Set(),
    highlightEdgeKeys: new Set(),
    focusedKey: null,
    pointerKey: null,
    ...overrides,
  };
}

interface LabelCall {
  text: string;
  font: string;
  fillStyle: string;
}

function context(): { ctx: CanvasRenderingContext2D; labels: LabelCall[] } {
  const labels: LabelCall[] = [];
  const stub = {
    font: "",
    fillStyle: "",
    globalAlpha: 1,
    textBaseline: "alphabetic",
    measureText: vi.fn((text: string) => ({ width: text.length * 7 })),
    fillRect: vi.fn(),
    fillText: vi.fn(function (this: { font: string; fillStyle: string }, text: string) {
      labels.push({ text, font: this.font, fillStyle: this.fillStyle });
    }),
  };
  return { ctx: stub as unknown as CanvasRenderingContext2D, labels };
}

function paintContext(): { ctx: CanvasRenderingContext2D; dashCalls: number[][] } {
  const dashCalls: number[][] = [];
  const ctx = {
    globalAlpha: 1,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 0,
    font: "",
    textBaseline: "alphabetic",
    beginPath: vi.fn(),
    arc: vi.fn(),
    rect: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    closePath: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    fillRect: vi.fn(),
    fillText: vi.fn(),
    measureText: vi.fn((text: string) => ({ width: text.length * 7 })),
    setLineDash: vi.fn((segments: number[]) => dashCalls.push([...segments])),
  } as unknown as CanvasRenderingContext2D;
  return { ctx, dashCalls };
}

function edge(
  source: ChamberNode,
  target: ChamberNode,
  overrides: Partial<Pick<ChamberEdge, "key" | "edge_kind" | "assurance">> = {},
): ChamberEdge {
  return {
    key: "edge-1",
    edge_kind: "WORKS_AT",
    from_key: source.key,
    to_key: target.key,
    source_system: "fixture",
    source_ref: "fixture/edge-1",
    assurance: "source",
    source,
    target,
    ...overrides,
  };
}

describe("Graph Chamber directional edges and viewport projection", () => {
  it("draws a geometric arrowhead at the exact served target", () => {
    const source = { ...node(0), x: 80, y: 100 };
    const target = { ...node(1), x: 180, y: 100 };
    const { ctx } = paintContext();

    drawEdges(ctx, [edge(source, target)], view(), THEME);

    expect(ctx.moveTo).toHaveBeenNthCalledWith(1, 80 + NODE_RADIUS, 100);
    expect(ctx.lineTo).toHaveBeenNthCalledWith(1, 180 - NODE_RADIUS, 100);
    expect(ctx.moveTo).toHaveBeenNthCalledWith(2, 180 - NODE_RADIUS, 100);
    expect(ctx.moveTo).toHaveBeenNthCalledWith(3, 180 - NODE_RADIUS, 100);
    expect(ctx.lineTo).toHaveBeenNthCalledWith(2, 180 - NODE_RADIUS - 8, 96);
    expect(ctx.lineTo).toHaveBeenNthCalledWith(3, 180 - NODE_RADIUS - 8, 104);
  });

  it("projects nodes and both edge endpoints through one viewport without mutating physics", () => {
    const source = { ...node(0), x: 10, y: 20 };
    const target = { ...node(1), x: 40, y: 60 };
    const projected = projectChamberGraph([source, target], [edge(source, target)], {
      x: -5,
      y: 7,
      scale: 2,
    });

    expect(projected.nodes.map(({ x, y }) => ({ x, y }))).toEqual([
      { x: 15, y: 47 },
      { x: 75, y: 127 },
    ]);
    expect(projected.edges[0]?.source).toBe(projected.nodes[0]);
    expect(projected.edges[0]?.target).toBe(projected.nodes[1]);
    expect({ x: source.x, y: source.y }).toEqual({ x: 10, y: 20 });
  });
});

describe("CR-B10 selection lighting, edge kind labels, and unconfirmed dashing", () => {
  it("litEdgeKeys unions the explicit highlight set with the selected node's own edges", () => {
    const a = node(0);
    const b = node(1);
    const c = node(2);
    const ab = edge(a, b, { key: "e-ab" });
    const bc = edge(b, c, { key: "e-bc" });

    expect(
      [
        ...litEdgeKeys(
          [ab, bc],
          view({ selectedKey: a.key, highlightEdgeKeys: new Set(["e-bc"]) }),
        ),
      ].sort(),
    ).toEqual(["e-ab", "e-bc"]);
    expect([...litEdgeKeys([ab, bc], view())]).toEqual([]);
  });

  it("edgeKindLabel names the kind, appending UNCONFIRMED only for the edge's own provisional assertion", () => {
    const a = node(0);
    const b = node(1);
    expect(edgeKindLabel(edge(a, b, { edge_kind: "ACQUIRED", assurance: "extracted" }))).toBe(
      "ACQUIRED · UNCONFIRMED",
    );
    expect(edgeKindLabel(edge(a, b, { edge_kind: "ACQUIRED", assurance: "confirmed" }))).toBe(
      "ACQUIRED",
    );
  });

  it("labels only the selected node's own edges; a non-adjacent edge stays dark and unlabeled", () => {
    const a = { ...node(0), x: 0, y: 0 };
    const b = { ...node(1), x: 100, y: 0 };
    const c = { ...node(2), x: 200, y: 0 };
    const adjacent = edge(a, b, { key: "e-ab", edge_kind: "WORKS_AT" });
    const distant = edge(b, c, { key: "e-bc", edge_kind: "ABOUT" });
    const { ctx, labels } = context();

    drawEdgeLabels(ctx, [adjacent, distant], view({ selectedKey: a.key }), THEME);

    // Only the selected node's own edge is named — the render's rule: "kind
    // labels on lit edges only." The edge/kind label colour is ALWAYS
    // theme.textDim, never a fainter tone (NOTES.md "Named divergences").
    expect(labels.map((l) => l.text)).toEqual(["WORKS_AT"]);
    expect(labels[0]?.fillStyle).toBe(THEME.textDim);
  });

  it("marks an extracted assertion's on-canvas label UNCONFIRMED, with a confirmed-edge control", () => {
    const a = { ...node(0), x: 0, y: 0 };
    const b = { ...node(1), x: 100, y: 0 };
    const acquired = edge(a, b, {
      key: "e-acquired",
      edge_kind: "ACQUIRED",
      assurance: "extracted",
    });
    const owns = edge(a, b, { key: "e-owns", edge_kind: "OWNS", assurance: "confirmed" });
    const { ctx, labels } = context();

    drawEdgeLabels(ctx, [acquired, owns], view({ selectedKey: a.key }), THEME);

    expect(labels.map((l) => l.text)).toEqual(
      expect.arrayContaining(["ACQUIRED · UNCONFIRMED", "OWNS"]),
    );
    expect(labels.some((l) => l.text === "OWNS · UNCONFIRMED")).toBe(false);
  });

  it("draws a dashed stroke for an unconfirmed edge, with a confirmed-edge control, regardless of selection", () => {
    const a = { ...node(0), x: 0, y: 0 };
    const b = { ...node(1), x: 100, y: 0 };
    const c = { ...node(2), x: 200, y: 100 };
    const confirmed = edge(a, b, { key: "e-confirmed", assurance: "confirmed" });
    const extracted = edge(b, c, { key: "e-extracted", assurance: "extracted" });
    const { ctx, dashCalls } = paintContext();

    drawEdges(ctx, [confirmed, extracted], view(), THEME);

    expect(dashCalls).toEqual([[], [5, 4], []]);
  });

  it("rings only the selected node with a stroked cherenkov circle, distinct from the halo", () => {
    const a = { ...node(0), x: 50, y: 60 };
    const b = { ...node(1), x: 150, y: 60 };
    const { ctx } = paintContext();

    drawSelectionRing(ctx, [a, b], view({ selectedKey: a.key }), THEME);

    expect(ctx.arc).toHaveBeenCalledWith(50, 60, NODE_RADIUS * 2.6, 0, Math.PI * 2);
    expect(ctx.arc).not.toHaveBeenCalledWith(150, 60, expect.anything(), 0, Math.PI * 2);
    expect(ctx.strokeStyle).toBe(THEME.cherenkov);
    expect(ctx.stroke).toHaveBeenCalledTimes(1);
    expect(ctx.fill).not.toHaveBeenCalled();
  });

  it("does nothing when nothing is selected or the selection has no laid-out position yet", () => {
    const a = { ...node(0), x: 50, y: 60 };
    // A node before the simulation has seeded it a position — d3 only writes
    // x/y once ticked (chamberSim.ts), so this is a real, reachable state.
    const unplaced: ChamberNode = { ...node(2), x: undefined, y: undefined };

    const none = paintContext();
    drawSelectionRing(none.ctx, [a], view(), THEME);
    expect(none.ctx.arc).not.toHaveBeenCalled();

    const missing = paintContext();
    drawSelectionRing(missing.ctx, [unplaced], view({ selectedKey: unplaced.key }), THEME);
    expect(missing.ctx.arc).not.toHaveBeenCalled();
  });

  it("excludes the selected node from the softer halo pass — the ring is its one spend", () => {
    const a = { ...node(0), x: 80, y: 80 };
    const b = { ...node(1), x: 81, y: 81 };
    const { ctx } = paintContext();

    drawNodes(ctx, [a, b], view({ selectedKey: a.key, litKeys: new Set([a.key]) }), THEME);

    expect(ctx.arc).not.toHaveBeenCalledWith(80, 80, NODE_RADIUS * 2.2, 0, Math.PI * 2);
  });
});

describe("Graph Chamber labels", () => {
  it("keeps keyboard focus and pointer hover independently haloed", () => {
    const { ctx } = paintContext();

    drawNodes(ctx, [node(0), node(1)], view({ focusedKey: "node-0", pointerKey: "node-1" }), THEME);

    expect(ctx.arc).toHaveBeenCalledWith(80, 80, NODE_RADIUS * 2.2, 0, Math.PI * 2);
    expect(ctx.arc).toHaveBeenCalledWith(81, 81, NODE_RADIUS * 2.2, 0, Math.PI * 2);
    expect(ctx.strokeStyle).toBe(THEME.cherenkov);
  });

  it("names every node in a compact graph, with the human name primary and class/state quiet", () => {
    const nodes = [node(0), node(1), node(2)];
    const { ctx, labels } = context();

    drawLabels(
      ctx,
      nodes,
      view({ litKeys: new Set(["node-0"]), selectedKey: "node-1" }),
      THEME,
      960,
      480,
    );

    expect(labels).toEqual(
      expect.arrayContaining([
        { text: "Harbor Metals", font: THEME.typeCell, fillStyle: THEME.text },
        { text: "Account · anchor", font: THEME.typeSmall, fillStyle: THEME.textDim },
        { text: "Person 1", font: THEME.typeCell, fillStyle: THEME.text },
        { text: "Person · selected", font: THEME.typeSmall, fillStyle: THEME.textDim },
        { text: "Person 2", font: THEME.typeCell, fillStyle: THEME.text },
        { text: "Person", font: THEME.typeSmall, fillStyle: THEME.textDim },
      ]),
    );
  });

  it("progressively discloses only anchor, hover and selection labels in a large graph", () => {
    const nodes = Array.from({ length: FULL_LABEL_NODE_LIMIT + 1 }, (_, index) => node(index));
    const focused = view({
      litKeys: new Set(["node-0"]),
      selectedKey: "node-1",
      focusedKey: "node-2",
      pointerKey: "node-3",
    });

    expect([...labelledNodeKeys(nodes, focused)]).toEqual(["node-0", "node-2", "node-3", "node-1"]);

    const { ctx, labels } = context();
    drawLabels(ctx, nodes, focused, THEME, 960, 480);
    expect(
      labels.filter((entry) => entry.font === THEME.typeCell).map((entry) => entry.text),
    ).toEqual(["Harbor Metals", "Person 1", "Person 2", "Person 3"]);
  });

  it("keeps a long human label inside the measured chamber while the semantic value stays exact", () => {
    const long = node(0);
    long.display_name =
      "A human-readable account name that is intentionally far wider than the chamber";
    long.x = 470;
    long.y = 10;
    const { ctx, labels } = context();

    drawLabels(ctx, [long], view(), THEME, 480, 480);

    expect(labels[0]?.text).toMatch(/…$/);
    expect(long.display_name).toBe(
      "A human-readable account name that is intentionally far wider than the chamber",
    );
    const rect = vi.mocked(ctx.fillRect).mock.calls[0];
    expect(rect).toBeDefined();
    expect(rect![0]).toBeGreaterThanOrEqual(8);
    expect(rect![0] + rect![2]).toBeLessThanOrEqual(472);
    expect(rect![1]).toBeGreaterThanOrEqual(8);
  });

  it("reserves a priority panel before placing a collocated ordinary label", () => {
    const anchor = node(0);
    const ordinary = node(1);
    ordinary.x = anchor.x;
    ordinary.y = anchor.y;
    ordinary.display_name = anchor.display_name;
    const placements = placeLabels(
      context().ctx,
      [anchor, ordinary],
      view({ litKeys: new Set([anchor.key]) }),
      THEME,
      480,
      480,
    );
    const priority = placements.find((placement) => placement.key === anchor.key);
    const secondary = placements.find((placement) => placement.key === ordinary.key);

    expect(priority).toBeDefined();
    expect(secondary === undefined || !rectsOverlap(priority!.rect, secondary.rect)).toBe(true);
  });

  it.each([6, 10, 16, 24])(
    "reserves deterministic bounded priority panels among %i realistic long names",
    (count) => {
      const columns = 4;
      const nodes = Array.from({ length: count }, (_, index) => ({
        ...node(index),
        display_name: `${index % 2 === 0 ? "Northwest Regional" : "International Commercial"} ${
          index % 3 === 0 ? "Risk and Compliance" : "Customer Operations"
        } Account ${String(index + 1).padStart(2, "0")}`,
        x: 170 + (index % columns) * 170,
        y: 75 + Math.floor(index / columns) * 70,
      }));
      const active = view({
        litKeys: new Set(["node-0"]),
        focusedKey: "node-1",
        pointerKey: "node-2",
        selectedKey: "node-3",
      });
      const first = placeLabels(context().ctx, nodes, active, THEME, 960, 480);
      const second = placeLabels(context().ctx, nodes, active, THEME, 960, 480);

      expect(second).toEqual(first);
      const priority = first.filter((placement) => placement.priority);
      expect(priority.map((placement) => placement.key)).toEqual([
        "node-0",
        "node-1",
        "node-2",
        "node-3",
      ]);
      for (const placement of first) {
        expect(placement.rect.x).toBeGreaterThanOrEqual(8);
        expect(placement.rect.y).toBeGreaterThanOrEqual(8);
        expect(placement.rect.x + placement.rect.width).toBeLessThanOrEqual(952);
        expect(placement.rect.y + placement.rect.height).toBeLessThanOrEqual(472);
      }
      for (const panel of priority) {
        for (const other of first) {
          if (panel.key === other.key) continue;
          expect(rectsOverlap(panel.rect, other.rect)).toBe(false);
        }
      }
    },
  );
});

function rectsOverlap(a: LabelRect, b: LabelRect): boolean {
  return !(
    a.x + a.width <= b.x ||
    b.x + b.width <= a.x ||
    a.y + a.height <= b.y ||
    b.y + b.height <= a.y
  );
}
