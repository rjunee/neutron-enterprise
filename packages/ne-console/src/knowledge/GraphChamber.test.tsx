// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChamberEdge, ChamberGraph, ChamberNode } from "./chamberSim";
import { GraphChamber } from "./GraphChamber";

function node(key: string, name: string, entityClass: string): ChamberNode {
  return {
    key,
    entity_class: entityClass,
    display_name: name,
    source_system: "fixture",
    source_ref: `fixture/${key}`,
    properties: {},
    assurance: "source",
  };
}

function graph(): ChamberGraph {
  const account = node("acct-1", "Acme Account", "Account");
  const person = node("person-1", "Dana Ops", "Person");
  const edge: ChamberEdge = {
    key: "edge-1",
    edge_kind: "WORKS_AT",
    from_key: person.key,
    to_key: account.key,
    source_system: "workday",
    source_ref: "workday/edge-1",
    assurance: "source",
    source: person,
    target: account,
  };
  return { nodes: [account, person], edges: [edge], truncated: false };
}

function view() {
  return { selectedKey: null, litKeys: new Set<string>(), highlightEdgeKeys: new Set<string>() };
}

function canvasContext(): CanvasRenderingContext2D {
  return {
    globalAlpha: 1,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    font: "",
    textBaseline: "alphabetic",
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    closePath: vi.fn(),
    rect: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    fillRect: vi.fn(),
    fillText: vi.fn(),
    measureText: vi.fn((text: string) => ({ width: text.length * 7 })),
    setLineDash: vi.fn(),
  } as unknown as CanvasRenderingContext2D;
}

describe("Graph Chamber viewport and semantics", () => {
  beforeEach(() => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("exposes every served relationship in exact source-label-target direction", () => {
    render(<GraphChamber graph={graph()} view={view()} onSelectNode={() => {}} />);

    const relationships = screen.getByRole("list", { name: "Graph relationships" });
    expect(within(relationships).getAllByRole("listitem")).toHaveLength(1);
    expect(relationships).toHaveTextContent(
      "Dana Ops — WORKS_AT → Acme Account. Directed from Dana Ops to Acme Account.",
    );
    expect(relationships).not.toHaveTextContent("Acme Account — WORKS_AT → Dana Ops");
  });

  it("offers bounded zoom, fit and reset controls plus keyboard pan", async () => {
    render(<GraphChamber graph={graph()} view={view()} onSelectNode={() => {}} />);
    const viewport = screen.getByRole("region", { name: "Graph viewport" });
    expect(viewport).toHaveAttribute("data-viewport-scale", "1.000");

    await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(viewport).toHaveAttribute("data-viewport-scale", "1.200");
    for (let i = 0; i < 12; i += 1) {
      await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    }
    expect(viewport).toHaveAttribute("data-viewport-scale", "3.000");

    const beforePan = Number(viewport.getAttribute("data-viewport-x"));
    fireEvent.keyDown(viewport, { key: "ArrowLeft" });
    expect(Number(viewport.getAttribute("data-viewport-x"))).toBeGreaterThan(beforePan);
    await userEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(viewport).toHaveAttribute("data-viewport-scale", "1.000");
    expect(viewport).toHaveAttribute("data-viewport-x", "0.0");

    await userEvent.click(screen.getByRole("button", { name: "Fit" }));
    expect(Number(viewport.getAttribute("data-viewport-scale"))).toBeGreaterThan(1);
  });

  it("inverse-transforms a panned canvas hit before selecting the node", () => {
    const one = graph();
    one.nodes = [one.nodes[0]!];
    one.edges = [];
    const onSelect = vi.fn();
    const { container } = render(
      <GraphChamber graph={one} view={view()} onSelectNode={onSelect} />,
    );
    const viewport = screen.getByRole("region", { name: "Graph viewport" });
    const canvas = container.querySelector("canvas")!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 960,
      bottom: 640,
      width: 960,
      height: 640,
      toJSON: () => ({}),
    });

    fireEvent.keyDown(viewport, { key: "ArrowLeft" });
    fireEvent.click(canvas, { clientX: 512, clientY: 320 });
    expect(onSelect).toHaveBeenCalledWith("acct-1");
  });

  it("settles and paints one static frame without requesting animation under reduced motion", () => {
    vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue(canvasContext());
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        matches: true,
        media: "(prefers-reduced-motion: reduce)",
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    const requestFrame = vi.fn(() => 1);
    vi.stubGlobal("requestAnimationFrame", requestFrame);
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    render(<GraphChamber graph={graph()} view={view()} onSelectNode={() => {}} />);
    expect(requestFrame).not.toHaveBeenCalled();
  });
});
