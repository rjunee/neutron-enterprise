// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The design-language registry for the Graph Chamber (design §5.1): the 5
// canonical entity-class glyphs and the 9 canonical edge kinds. The classes and
// kinds mirror ne-schema's ENTITY_CLASSES / EDGE_KINDS exactly (verified against
// packages/ne-schema/.../operational_graph.py). A glyph is a PURE canvas painter
// (ctx, x, y, r): it strokes/fills a path in the CURRENT ctx.strokeStyle /
// fillStyle (chamberTheme sets those from tokens — NO color literals live here,
// so the token-hardcode guardian stays green). An unknown class/kind falls back
// to an honest generic form — we NEVER invent a shape for data we don't model.

/** The 5 canonical entity classes (ne-schema ENTITY_CLASSES). */
export type EntityClass = "Account" | "Person" | "Document" | "Ticket" | "Order";

/** A pure canvas painter: draws the glyph outline at (x,y) with radius r using
 * the ctx's current stroke/fill styles. Returns nothing (side-effect on ctx). */
export type GlyphPainter = (ctx: CanvasRenderingContext2D, x: number, y: number, r: number) => void;

function square(ctx: CanvasRenderingContext2D, x: number, y: number, r: number): void {
  ctx.beginPath();
  ctx.rect(x - r, y - r, r * 2, r * 2);
  ctx.fill();
  ctx.stroke();
}

function circle(ctx: CanvasRenderingContext2D, x: number, y: number, r: number): void {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
}

/** Page-pentagon: a document sheet with a folded corner (5 points). */
function pagePentagon(ctx: CanvasRenderingContext2D, x: number, y: number, r: number): void {
  const w = r * 1.6;
  const fold = r * 0.7;
  ctx.beginPath();
  ctx.moveTo(x - w / 2, y - r);
  ctx.lineTo(x + w / 2 - fold, y - r);
  ctx.lineTo(x + w / 2, y - r + fold);
  ctx.lineTo(x + w / 2, y + r);
  ctx.lineTo(x - w / 2, y + r);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
}

function diamond(ctx: CanvasRenderingContext2D, x: number, y: number, r: number): void {
  ctx.beginPath();
  ctx.moveTo(x, y - r);
  ctx.lineTo(x + r, y);
  ctx.lineTo(x, y + r);
  ctx.lineTo(x - r, y);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
}

function hexagon(ctx: CanvasRenderingContext2D, x: number, y: number, r: number): void {
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i - Math.PI / 6;
    const px = x + r * Math.cos(a);
    const py = y + r * Math.sin(a);
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  }
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
}

/** Honest fallback for a class we do not model: a small outlined square (no
 * fill) — visibly "unknown", never masquerading as a canonical glyph. */
function unknownGlyph(ctx: CanvasRenderingContext2D, x: number, y: number, r: number): void {
  const s = r * 0.7;
  ctx.beginPath();
  ctx.rect(x - s, y - s, s * 2, s * 2);
  ctx.stroke();
}

/** The 5 canonical class -> painter registry (ne-schema ENTITY_CLASSES). */
export const ENTITY_GLYPHS: Record<EntityClass, GlyphPainter> = {
  Account: square,
  Person: circle,
  Document: pagePentagon,
  Ticket: diamond,
  Order: hexagon,
};

/** Resolve a glyph for any class string — canonical class -> its glyph, unknown
 * -> the honest outlined-square fallback. */
export function glyphFor(entityClass: string): GlyphPainter {
  return (ENTITY_GLYPHS as Record<string, GlyphPainter>)[entityClass] ?? unknownGlyph;
}

/** The 9 canonical edge kinds (ne-schema EDGE_KINDS). */
export const EDGE_KINDS = [
  "ABOUT",
  "ACCOUNT",
  "ASSIGNEE",
  "AUTHOR",
  "BELONGS_TO",
  "OWNED_BY",
  "PLACED_BY",
  "REQUESTER",
  "WORKS_AT",
] as const;

export type EdgeKind = (typeof EDGE_KINDS)[number];

/** SAME_AS is the special ER-merge edge (design §5.1 gesture 4): it is labeled
 * and still drawn in Cherenkov light, but is NOT one of the 9 operational
 * kinds — it is asserted by the entity-resolution flywheel, not the source
 * connectors. */
export const SAME_AS = "SAME_AS";

const CANONICAL = new Set<string>(EDGE_KINDS);

/** A human label for an edge kind — canonical kinds and SAME_AS render their
 * own name; an unknown kind renders verbatim (never dropped, never invented). */
export function edgeLabel(edgeKind: string): string {
  return edgeKind;
}

/** True for the 9 canonical operational kinds (used only for label styling). */
export function isCanonicalEdge(edgeKind: string): boolean {
  return CANONICAL.has(edgeKind);
}

/** True for the special SAME_AS ER edge. */
export function isSameAs(edgeKind: string): boolean {
  return edgeKind === SAME_AS;
}
