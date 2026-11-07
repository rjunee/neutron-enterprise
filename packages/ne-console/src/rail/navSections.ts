// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.2 — THE RAIL'S CONTENTS. Wordmark, deployment name, this navigation, theme
// control. Nothing else, and specifically not the twelve-constraint annunciator,
// which is a compliance widget and now lives on Posture, where a compliance
// question is actually being asked.
//
//   (none)      Home
//   Knowledge Layer   Documents · Graph · Ontology
//   Execution         Agents · Search · Verified Queries
//   Operations        Sources · Models · Evaluations · Improvement
//   Governance        Approvals · Audit log · Conversation ledger · Posture
//   Insights    KPIs
//   (none)      Settings                    pinned at the rail's foot
//
// FORWARD-COMPATIBLE BY CONSTRUCTION (§19). A section is `{label, to}` (a
// destination) OR `{label, items}` (a header over children) — never a flat list.
// The long-running-agent surface the owner intends lands as one more Workspace
// child; its attention items join Home's waiting-on-you table; its proposed
// actions decide in the existing Approvals destination. None of that needs a new
// shape here, only a new Execution row below.
//
// ROLE-FILTERING IS LEGIBILITY, NOT AUTHORIZATION (C7). `visibleSections` only
// decides what the rail SHOWS; the server still refuses every request on its own
// terms, and a principal who reaches a hidden route regardless still gets the
// server's own refusal rendered by the page (test/nav-refusal.test.tsx).
//
// THE TWO UNKNOWNS ARE NOT THE SAME (auth/entitlements.ts):
//   * PENDING — /v1/me is in flight. A gated destination renders as a skeleton
//     slot, never as a link: showing it would be the client asserting a
//     permission the server has not granted (the "Approvals flash" defect —
//     the rail showed everything, then snatched the refused links away when
//     the answer landed). Pending is a loading state, not a verdict.
//   * UNADVISED — the answer arrived with no entitlements list (older build,
//     or the read failed). UNKNOWN IS NOT DENIED: nothing will arrive later to
//     trim against, every destination renders, and the server refuses each
//     request on its own terms.
// In every state the client never computes a denial: "hidden" happens only on
// the server's positive refusal.

import type { Entitlements } from "../auth/entitlements";
import { ROUTE_SURFACE } from "../auth/entitlements";

export interface NavItem {
  label: string;
  to: string;
}

/** A direct-link section (Home, Approvals, Settings) — goes somewhere itself. */
export interface NavLeaf {
  label: string;
  to: string;
  items?: never;
}

/** A group section — a header over children, never a destination itself. */
export interface NavGroup {
  label: string;
  to?: never;
  items: readonly NavItem[];
}

/**
 * DISCRIMINATED, not two optional fields. With both optional, `{ label: "Agents" }`
 * type-checks and renders `<NavLink to={undefined}>` through a `section.to!`
 * assertion. A section is either a destination or a header over children, and
 * neither-nor is a compile error at the point the mistake is made.
 */
export type NavSection = NavLeaf | NavGroup;

// ASK IS NOT A RAIL DESTINATION ANY MORE (owner, 2026-08-27: "The top level
// should be Agents... then clicking on one opens the Ask panel to chat with
// that agent"). Ask was never a second chat system -- SPEC calls it "a
// lightweight route/projection onto one real configured default Q&A Agent" --
// and the defect was that the projection was invisible: the rail presented Ask
// and Agents as peers when one is a view of the other. `Agents` now leads
// Execution, and the Agent workspace mounts the real Ask surface as the
// configured Agent's Chat tab.
//
// THE `/ask` ROUTE IS ALIVE AND REDIRECTS (App.tsx). Removing a destination
// from this list removes it from the rail, the palette index and the `g` chord,
// all of which are derived from it -- it does NOT unroute the path, and
// deliberately so: `/ask` is in the guided tour, on the far end of the `/chat`
// redirect, and in bookmarks this project cannot enumerate.
export const NAV_SECTIONS: readonly NavSection[] = [
  { label: "Home", to: "/" },
  {
    label: "Knowledge Layer",
    items: [
      { label: "Documents", to: "/knowledge/documents" },
      { label: "Graph", to: "/knowledge/graph" },
      { label: "Ontology", to: "/knowledge/ontology" },
    ],
  },
  {
    label: "Execution",
    items: [
      { label: "Agents", to: "/agents" },
      { label: "Search", to: "/search" },
      { label: "Verified Queries", to: "/verified-queries" },
    ],
  },
  {
    label: "Operations",
    items: [
      { label: "Sources", to: "/sources" },
      { label: "Models", to: "/models" },
      { label: "Evaluations", to: "/evaluations" },
      { label: "Improvement", to: "/improvement" },
    ],
  },
  {
    label: "Governance",
    items: [
      { label: "Approvals", to: "/approvals" },
      { label: "Audit log", to: "/audit" },
      { label: "Conversation ledger", to: "/conversation-ledger" },
      { label: "Posture", to: "/posture" },
      // Access remains directly routable so an existing bookmark receives the
      // truthful deployment fallback. It returns to navigation only with B6's
      // principals, roles, Tier-3 and governed-assignment API group.
    ],
  },
  {
    label: "Insights",
    items: [{ label: "KPIs", to: "/insights" }],
  },
];

/** Pinned at the rail's foot, below everything and outside every group (§1.2). */
export const NAV_FOOT: NavLeaf = { label: "Settings", to: "/settings" };

/**
 * What one destination gets in the rail, decided ONLY from the server's answer:
 *   * "shown"    — no surface to hold (open to every authenticated key), or the
 *                  server positively granted it, or the deployment offers no
 *                  advisory at all (unadvised: unknown is not denied).
 *   * "pending"  — the surface's answer is still in flight. Neither a link (a
 *                  permission the client would be asserting) nor an absence (a
 *                  denial the client would be inventing): a loading slot.
 *   * "hidden"   — the server POSITIVELY refused the surface. The only state
 *                  the client did not decide anything to reach.
 */
export type RouteVisibility = "shown" | "pending" | "hidden";

export function routeVisibility(to: string, advisory: Entitlements): RouteVisibility {
  const surface = ROUTE_SURFACE[to];
  if (surface === undefined) return "shown";
  const access = advisory.access(surface);
  if (access === "refused") return "hidden";
  if (access === "granted") return "shown";
  return advisory.pending ? "pending" : "shown";
}

/** One slot in the rendered rail: a real destination, or — while the server's
 * answer for its surface is in flight — a skeleton holding that destination's
 * row. `to` stays on the pending shape as the slot's stable identity. */
export type RailSlot =
  { kind: "link"; label: string; to: string } | { kind: "pending"; to: string };

export type RailSection = RailSlot | { kind: "group"; label: string; slots: readonly RailSlot[] };

function slotOf(label: string, to: string, advisory: Entitlements): RailSlot | null {
  const visibility = routeVisibility(to, advisory);
  if (visibility === "hidden") return null;
  if (visibility === "pending") return { kind: "pending", to };
  return { kind: "link", label, to };
}

/**
 * THE ONE FILTER — what the rail actually renders. `visibleSections` (the
 * palette's index) and `shortcutRoutes` (the `g` chord) are both DERIVED from
 * this model's links, so the three surfaces cannot disagree: a destination that
 * is not a link in the rail is not in the palette and not reachable by
 * shortcut, in every advisory state.
 *
 * A group whose every slot is gone disappears, header included — there is
 * nothing left for the header to label.
 */
export function railSections(
  sections: readonly NavSection[],
  advisory: Entitlements,
): readonly RailSection[] {
  const out: RailSection[] = [];
  for (const section of sections) {
    if (section.items) {
      const slots: RailSlot[] = [];
      for (const item of section.items) {
        const slot = slotOf(item.label, item.to, advisory);
        if (slot !== null) slots.push(slot);
      }
      if (slots.length === 0) continue;
      out.push({ kind: "group", label: section.label, slots });
      continue;
    }
    const slot = slotOf(section.label, section.to, advisory);
    if (slot !== null) out.push(slot);
  }
  return out;
}

/**
 * The pure destination filter, DERIVED from `railSections`: exactly the links —
 * pending slots are not destinations, and hidden routes are gone. This is what
 * the palette indexes and the shortcut set is built from.
 */
export function visibleSections(
  sections: readonly NavSection[],
  advisory: Entitlements,
): readonly NavSection[] {
  const out: NavSection[] = [];
  for (const section of railSections(sections, advisory)) {
    if (section.kind === "group") {
      const items = section.slots
        .filter((slot): slot is RailSlot & { kind: "link" } => slot.kind === "link")
        .map((slot) => ({ label: slot.label, to: slot.to }));
      if (items.length === 0) continue;
      out.push({ label: section.label, items });
      continue;
    }
    if (section.kind === "link") out.push({ label: section.label, to: section.to });
  }
  return out;
}

/**
 * Every visible destination as a flat, ordered list — what the command palette
 * indexes (§3, rank 2). It is derived from the SAME filtered sections the rail
 * renders, so a role-refused destination can never be reachable from the palette
 * while being hidden in the rail.
 */
export function flattenSections(sections: readonly NavSection[]): readonly NavItem[] {
  const out: NavItem[] = [];
  for (const section of sections) {
    if (section.items) {
      for (const item of section.items) {
        out.push({ label: `${section.label} › ${item.label}`, to: item.to });
      }
      continue;
    }
    out.push({ label: section.label, to: section.to });
  }
  return out;
}

/**
 * The routes the `g` chord may navigate to for this advisory — derived from the
 * SAME filtered sections the rail and the palette render, plus the pinned foot.
 * `useShellKeys` consults this at press time, so a shortcut to a destination the
 * rail does not show DOES NOTHING rather than routing somewhere the rail says
 * does not exist. This decides an affordance, not authorization: a hand-typed
 * URL still mounts the route and still gets the server's own refusal
 * (test/nav-refusal.test.tsx).
 */
export function shortcutRoutes(advisory: Entitlements): ReadonlySet<string> {
  const routes = new Set(
    flattenSections(visibleSections(NAV_SECTIONS, advisory)).map((item) => item.to),
  );
  routes.add(NAV_FOOT.to);
  return routes;
}

/**
 * §2.6 — `g` then a letter: the FULL letter map. Reachability is not decided
 * here — `useShellKeys` checks each press against `shortcutRoutes`.
 */
export const GO_KEYS: Readonly<Record<string, string>> = {
  h: "/",
  a: "/approvals",
  s: "/search",
  l: "/knowledge/documents",
  g: "/knowledge/graph",
  o: "/sources",
  m: "/models",
  w: "/workflows",
  i: "/improvement",
  e: "/evaluations",
  u: "/audit",
  p: "/posture",
  c: "/access",
  ",": "/settings",
};
