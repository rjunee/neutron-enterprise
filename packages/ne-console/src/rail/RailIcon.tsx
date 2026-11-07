// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.2 — THE RAIL'S PER-DESTINATION ICON. The round-3 approved renders put a
// 15px mark on every rail destination; the shipped rail never had one, because
// the rail was not among the recomposition units. This is that mark.
//
// PORTED, NOT REDRAWN. design/round3/screens/home.html carries a <symbol> sprite
// of twenty ids that map 1:1 onto the twenty destinations of navSections.ts. The
// geometry below is that sprite's, verbatim; RailIcon.test.tsx re-reads the
// render and compares every element and attribute, so the code and the approved
// artwork cannot drift.
//
// INLINE, NOT A SPRITE FILE AND NOT A FONT. The console makes zero external
// requests, so <use href="external.svg#id"> and an icon font are both out; and
// an inline <use> against a document-level <defs> would put a second rendering
// path (and a shadow tree) between the token layer and the glyph for nothing.
// Twenty small components cost less than one indirection.

import type { ReactElement } from "react";

/**
 * ONE DESTINATION'S ARTWORK, keyed by the route it belongs to.
 *
 * Ported verbatim from the sprite in design/round3/screens/home.html — every
 * coordinate, every stroke width, every cap. Each comment names the render's
 * sprite id so the provenance of a glyph is one grep away, and
 * RailIcon.test.tsx re-reads that render and compares the geometry element by
 * element, so a redrawn icon fails rather than ships.
 *
 * A route with no entry renders NO icon rather than a stand-in glyph: an
 * invented mark for an unknown destination is a fabricated affordance. The
 * completeness of this table against NAV_SECTIONS + NAV_FOOT is a test.
 */
const RAIL_ICON_ART: Readonly<Record<string, ReactElement>> = {
  // Home — the render's #i-home.
  "/": (
    <>
      <path
        d="M2.5 8 8 3l5.5 5M4.5 7v6h7V7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="square"
      />
    </>
  ),
  // Documents — the render's #i-documents.
  "/knowledge/documents": (
    <>
      <path
        d="M4 2.5h5.5L12 5v8.5H4zM9.5 2.5V5H12M6 8h4M6 10.5h4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="square"
      />
    </>
  ),
  // Graph — the render's #i-graph.
  "/knowledge/graph": (
    <>
      <circle cx="4" cy="4.5" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="12" cy="5.5" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="7.5" cy="12" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M5.7 4.8l4.5.5M4.9 6.1l1.8 4.2M11 7l-2.3 3.5"
        stroke="currentColor"
        strokeWidth="1.2"
      />
    </>
  ),
  // Ontology — the render's #i-ontology.
  "/knowledge/ontology": (
    <>
      <rect
        x="5.75"
        y="2"
        width="4.5"
        height="3.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <rect
        x="1.75"
        y="10.5"
        width="4.5"
        height="3.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <rect
        x="9.75"
        y="10.5"
        width="4.5"
        height="3.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <path d="M8 5.2v2.6M4 10.5V7.8h8v2.7" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </>
  ),
  // Ask — the render's #i-ask.
  "/ask": (
    <>
      <path
        d="M2.5 3h11v7.5H7L4 13v-2.5H2.5z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="square"
      />
      <path d="M5 6h6" stroke="currentColor" strokeWidth="1.3" />
    </>
  ),
  // Search — the render's #i-search.
  "/search": (
    <>
      <circle cx="7" cy="7" r="3.8" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M9.9 9.9L13.5 13.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="square" />
    </>
  ),
  // Agents — the render's #i-agents.
  "/agents": (
    <>
      <rect
        x="3.5"
        y="5"
        width="9"
        height="7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path d="M8 5V2.7M6.9 2.7h2.2" stroke="currentColor" strokeWidth="1.3" />
      <path d="M6 8.2v1.4M10 8.2v1.4" stroke="currentColor" strokeWidth="1.5" />
    </>
  ),
  // Verified Queries — the render's #i-verified.
  "/verified-queries": (
    <>
      <path
        d="M8 2l5 1.8v3.6c0 3.2-2 5.4-5 6.6-3-1.2-5-3.4-5-6.6V3.8z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="miter"
      />
      <path
        d="M5.7 7.8l1.7 1.7 3-3.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="square"
      />
    </>
  ),
  // Workflows — the render's #i-workflows.
  "/workflows": (
    <>
      <rect
        x="2"
        y="2.5"
        width="4.4"
        height="4.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <rect
        x="9.6"
        y="9.1"
        width="4.4"
        height="4.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path
        d="M6.4 4.7h3.4v4.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="square"
      />
    </>
  ),
  // Sources — the render's #i-sources.
  "/sources": (
    <>
      <ellipse cx="8" cy="4" rx="5" ry="2" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4M3 8c0 1.1 2.2 2 5 2s5-.9 5-2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
    </>
  ),
  // Models — the render's #i-models.
  "/models": (
    <>
      <rect
        x="4.5"
        y="4.5"
        width="7"
        height="7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path
        d="M6.8 4.5V2.2M9.2 4.5V2.2M6.8 13.8v-2.3M9.2 13.8v-2.3M4.5 6.8H2.2M4.5 9.2H2.2M13.8 6.8h-2.3M13.8 9.2h-2.3"
        stroke="currentColor"
        strokeWidth="1.2"
      />
    </>
  ),
  // Evaluations — the render's #i-evaluations.
  "/evaluations": (
    <>
      <path d="M2.5 12.5a5.5 5.5 0 0 1 11 0" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M8 12.5L11 8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="square" />
      <path d="M2.5 12.5h11" stroke="currentColor" strokeWidth="1.4" />
    </>
  ),
  // Improvement — the render's #i-improvement.
  "/improvement": (
    <>
      <path
        d="M12.8 8A4.8 4.8 0 1 1 8 3.2h2.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path
        d="M9 1.4l1.9 1.8L9 5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="square"
      />
    </>
  ),
  // Approvals — the render's #i-approvals.
  "/approvals": (
    <>
      <rect
        x="2.5"
        y="2.5"
        width="11"
        height="11"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path
        d="M5 8.2l2.2 2.2 4-4.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="square"
      />
    </>
  ),
  // Audit log — the render's #i-audit.
  "/audit": (
    <>
      <path d="M4 3.2h9M4 8h9M4 12.8h9" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="1.9" cy="3.2" r="1" fill="currentColor" />
      <circle cx="1.9" cy="8" r="1" fill="currentColor" />
      <circle cx="1.9" cy="12.8" r="1" fill="currentColor" />
    </>
  ),
  // Conversation ledger — the render's #i-ledgerchat.
  "/conversation-ledger": (
    <>
      <path
        d="M2.5 2.5h11V10h-11zM5 12.5h6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="square"
      />
      <path d="M5 5h6M5 7.5h4" stroke="currentColor" strokeWidth="1.2" />
    </>
  ),
  // Posture — the render's #i-posture.
  "/posture": (
    <>
      <path
        d="M8 2l5 1.8v3.6c0 3.2-2 5.4-5 6.6-3-1.2-5-3.4-5-6.6V3.8z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <path d="M8 5.2v3.6" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="8" cy="10.6" r=".9" fill="currentColor" />
    </>
  ),
  // Access — the render's #i-access.
  "/access": (
    <>
      <circle cx="5.2" cy="5.2" r="3" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M7.4 7.4l6 6M10.5 10.5l1.8-1.8M12.3 12.3l1.6-1.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="square"
      />
    </>
  ),
  // KPIs — the render's #i-kpis.
  "/insights": (
    <>
      <path d="M2.5 13.5h11" stroke="currentColor" strokeWidth="1.4" />
      <path d="M4 13V8.5M8 13V4.5M12 13V6.5" stroke="currentColor" strokeWidth="1.8" />
    </>
  ),
  // Settings — the render's #i-settings.
  "/settings": (
    <>
      <circle cx="8" cy="8" r="2.2" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M8 2.2v2M8 11.8v2M2.2 8h2M11.8 8h2M3.9 3.9l1.4 1.4M10.7 10.7l1.4 1.4M12.1 3.9l-1.4 1.4M5.3 10.7l-1.4 1.4"
        stroke="currentColor"
        strokeWidth="1.3"
      />
    </>
  ),
};

/**
 * The 15px mark beside a rail destination's label (§1.2, round-3 render).
 *
 * DECORATIVE, ALWAYS. `aria-hidden` — the destination's name is the adjacent
 * `.ne-rail__nodelabel`, which the link's `aria-labelledby` points at, in the
 * expanded rail AND in the collapsed one (where the label is display:none and
 * still names the link, because aria-labelledby resolves hidden text). The icon
 * never enters an accessible name and never has one of its own.
 *
 * MONOCHROME, `currentColor`. The icon inherits the row's colour and takes the
 * cherenkov only on the active row, through .ne-rail__icon in shell.css. No
 * icon encodes a state by hue: colour stays evidence-bearing.
 */
export function RailIcon({ to }: { to: string }) {
  const art = RAIL_ICON_ART[to];
  if (art === undefined) return null;
  return (
    <svg className="ne-rail__icon" viewBox="0 0 16 16" aria-hidden="true">
      {art}
    </svg>
  );
}
