// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it, vi } from "vitest";
import {
  EDGE_KINDS,
  ENTITY_GLYPHS,
  edgeLabel,
  glyphFor,
  isCanonicalEdge,
  isSameAs,
} from "./glyphs";

/** A minimal ctx stub — the glyph painters only issue path calls. */
function ctxStub(): CanvasRenderingContext2D {
  return {
    beginPath: vi.fn(),
    rect: vi.fn(),
    arc: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    closePath: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
  } as unknown as CanvasRenderingContext2D;
}

describe("ENTITY_GLYPHS", () => {
  it("covers exactly the 5 canonical ne-schema entity classes", () => {
    expect(Object.keys(ENTITY_GLYPHS).sort()).toEqual([
      "Account",
      "Document",
      "Order",
      "Person",
      "Ticket",
    ]);
  });

  it("each canonical glyph paints without throwing", () => {
    const ctx = ctxStub();
    for (const painter of Object.values(ENTITY_GLYPHS)) {
      expect(() => painter(ctx, 10, 10, 9)).not.toThrow();
    }
  });

  it("an unknown class falls back to a painter (never throws, never invents a canonical glyph)", () => {
    const ctx = ctxStub();
    const fallback = glyphFor("Spaceship");
    expect(fallback).not.toBe(ENTITY_GLYPHS.Account);
    expect(() => fallback(ctx, 0, 0, 9)).not.toThrow();
  });
});

describe("EDGE_KINDS", () => {
  it("lists exactly the 9 canonical kinds", () => {
    expect([...EDGE_KINDS].sort()).toEqual([
      "ABOUT",
      "ACCOUNT",
      "ASSIGNEE",
      "AUTHOR",
      "BELONGS_TO",
      "OWNED_BY",
      "PLACED_BY",
      "REQUESTER",
      "WORKS_AT",
    ]);
  });

  it("tolerates SAME_AS and unknown kinds without throwing", () => {
    expect(isSameAs("SAME_AS")).toBe(true);
    expect(isCanonicalEdge("SAME_AS")).toBe(false);
    expect(isCanonicalEdge("ABOUT")).toBe(true);
    expect(isCanonicalEdge("MYSTERY")).toBe(false);
    expect(edgeLabel("SAME_AS")).toBe("SAME_AS");
    expect(edgeLabel("MYSTERY")).toBe("MYSTERY");
  });
});
