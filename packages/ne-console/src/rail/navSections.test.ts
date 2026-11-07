// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The pure filter (rail/navSections.ts), tested at the seam without any
// provider. The advisory has THREE states and the filter must honour each:
// PENDING (in flight — gated destinations are loading slots, never links),
// UNADVISED (answer arrived with no list — unknown is not denied, everything
// renders), and KNOWN (the server's verdict, honoured in both directions).
// The palette index and the `g`-shortcut set are derived from the same filter,
// and the agreement is pinned here across all three states.

import { describe, expect, it } from "vitest";
import type { Access, Entitlements, Surface } from "../auth/entitlements";
import type { NavSection } from "./navSections";
import {
  NAV_FOOT,
  NAV_SECTIONS,
  GO_KEYS,
  flattenSections,
  railSections,
  routeVisibility,
  shortcutRoutes,
  visibleSections,
} from "./navSections";

/** /v1/me is IN FLIGHT: an answer is coming, nothing is granted yet. */
const PENDING: Entitlements = {
  access: () => "unknown",
  refused: () => false,
  known: false,
  pending: true,
};

/** /v1/me RESOLVED without an entitlements list: no verdict is coming. */
const UNADVISED: Entitlements = {
  access: () => "unknown",
  refused: () => false,
  known: false,
  pending: false,
};

function known(refusedSurfaces: Surface[]): Entitlements {
  const refused = new Set(refusedSurfaces);
  const access = (surface: Surface): Access => (refused.has(surface) ? "refused" : "granted");
  return { access, refused: (s) => access(s) === "refused", known: true, pending: false };
}

const SECTIONS: NavSection[] = [
  { label: "Home", to: "/" },
  {
    label: "Workspace",
    items: [
      { label: "Ask", to: "/ask" },
      { label: "Search", to: "/search" },
    ],
  },
  { label: "Approvals", to: "/approvals" },
  {
    label: "Operations",
    items: [
      { label: "Sources", to: "/sources" },
      { label: "Improvement", to: "/improvement" },
    ],
  },
];

describe("routeVisibility — one destination, all three advisory states", () => {
  it("a surface-mapped route is pending in flight, shown when granted, hidden ONLY when refused", () => {
    // /approvals maps to the "approvals" surface in the real ROUTE_SURFACE.
    expect(routeVisibility("/approvals", PENDING)).toBe("pending");
    expect(routeVisibility("/approvals", known([]))).toBe("shown");
    expect(routeVisibility("/approvals", known(["approvals"]))).toBe("hidden");
    // UNADVISED is not denied AND not pending: nothing will arrive to trim
    // against, so the destination shows and the server refuses on its own terms.
    expect(routeVisibility("/approvals", UNADVISED)).toBe("shown");
  });

  it("CONTROL: an unmapped route is shown in every state — no advisory can gate it", () => {
    for (const advisory of [
      PENDING,
      UNADVISED,
      known([]),
      known(["approvals", "steward", "ops"]),
    ]) {
      expect(routeVisibility("/search", advisory)).toBe("shown");
    }
  });

  it("keeps the monitored answer out of global navigation so Ask owns its entry point", () => {
    const execution = NAV_SECTIONS.find((section) => section.label === "Execution");
    expect(execution?.items).not.toContainEqual({ label: "Answer Ledger", to: "/answer-ledger" });
    expect(routeVisibility("/answer-ledger", PENDING)).toBe("shown");
    expect(shortcutRoutes(PENDING).has("/answer-ledger")).toBe(false);
  });

  it("publishes Verified Queries once under Execution with its canonical name and route", () => {
    const execution = NAV_SECTIONS.find((section) => section.label === "Execution");
    expect(execution?.items?.filter((item) => item.to === "/verified-queries")).toEqual([
      { label: "Verified Queries", to: "/verified-queries" },
    ]);
    expect(flattenSections(NAV_SECTIONS)).toContainEqual({
      label: "Execution › Verified Queries",
      to: "/verified-queries",
    });
    expect(shortcutRoutes(PENDING).has("/verified-queries")).toBe(true);
  });

  it("publishes exactly one destination-only Agents entry under Execution", () => {
    const execution = NAV_SECTIONS.find((section) => section.label === "Execution");
    expect(execution?.items?.filter((item) => item.to === "/agents")).toEqual([
      { label: "Agents", to: "/agents" },
    ]);
    expect(flattenSections(NAV_SECTIONS).filter((item) => item.label.includes("Agents"))).toEqual([
      { label: "Execution › Agents", to: "/agents" },
    ]);
  });

  it("publishes the exact durable information architecture with no Library or Entities destination", () => {
    expect(
      NAV_SECTIONS.map((section) => [
        section.label,
        section.items?.map((item) => `${item.label}:${item.to}`) ?? section.to,
      ]),
    ).toEqual([
      ["Home", "/"],
      [
        "Knowledge Layer",
        [
          "Documents:/knowledge/documents",
          "Graph:/knowledge/graph",
          "Ontology:/knowledge/ontology",
        ],
      ],
      [
        "Execution",
        // Ask is deliberately NOT here: Agents leads Execution and Ask is the
        // configured Agent's Chat tab inside it (owner, 2026-08-27). The `/ask`
        // ROUTE still exists and redirects — this list decides the rail, the
        // palette index and the `g` chord, not what is routable.
        // Workflows is deliberately NOT here either: the owner directed it
        // out of the product (2026-08-28, SPEC §13.1 item 8
        // PD-WORKFLOWS-REMOVE, "Workflows just seems pointless, we should rip
        // it out"), and 6480ea04 removed the rail entry. The `/workflows`
        // ROUTE, the page and their suites deliberately remain until the rest
        // of that item is done — again, this list decides the rail, the
        // palette index and the `g` chord, not what is routable.
        ["Agents:/agents", "Search:/search", "Verified Queries:/verified-queries"],
      ],
      [
        "Operations",
        [
          "Sources:/sources",
          "Models:/models",
          "Evaluations:/evaluations",
          "Improvement:/improvement",
        ],
      ],
      [
        "Governance",
        [
          "Approvals:/approvals",
          "Audit log:/audit",
          "Conversation ledger:/conversation-ledger",
          "Posture:/posture",
        ],
      ],
      ["Insights", ["KPIs:/insights"]],
    ]);
    expect(flattenSections(NAV_SECTIONS).map((item) => item.to)).not.toContain("/library");
    expect(flattenSections(NAV_SECTIONS).map((item) => item.to)).not.toContain("/access");
    expect(flattenSections(NAV_SECTIONS).map((item) => item.label)).not.toContain("Entities");
  });

  it("binds the knowledge shortcuts to the two canonical workspaces", () => {
    expect(GO_KEYS.l).toBe("/knowledge/documents");
    expect(GO_KEYS.g).toBe("/knowledge/graph");
  });

  it("the Conversation ledger is an ops-gated Governance destination", () => {
    const governance = NAV_SECTIONS.find((section) => section.label === "Governance");
    expect(governance?.items).toContainEqual({
      label: "Conversation ledger",
      to: "/conversation-ledger",
    });
    expect(routeVisibility("/conversation-ledger", PENDING)).toBe("pending");
    expect(routeVisibility("/conversation-ledger", known([]))).toBe("shown");
    expect(routeVisibility("/conversation-ledger", known(["ops"]))).toBe("hidden");
  });
});

describe("visibleSections", () => {
  // WHAT THE OLD (a) CONTROL BECAME. It used to assert the unknown case returns
  // the input untouched — but "unknown" conflated two states, and for the
  // in-flight one that behaviour WAS the defect (the Approvals flash: every
  // link shown, then the refused ones snatched away). The control splits along
  // the two unknowns and keeps its old job — proving the subset tests below
  // measure real filtering, not a filter that hides things it has no verdict
  // for: (a1) pins that PENDING hides ONLY the surface-mapped routes (an
  // over-broad "hide while loading" flags it), and (a2) pins that UNADVISED
  // still returns every destination (a naive "unknown = hide" flags it).
  it("(a1) PENDING advisory: surface-mapped destinations are withheld, every unmapped one survives", () => {
    const out = visibleSections(SECTIONS, PENDING);
    // /approvals (mapped) is not a link while the answer is in flight…
    expect(flattenSections(out).map((i) => i.to)).not.toContain("/approvals");
    expect(flattenSections(out).map((i) => i.to)).not.toContain("/improvement");
    // …and (the control half) every unmapped destination is untouched, in order.
    expect(flattenSections(out).map((i) => i.to)).toEqual(["/", "/ask", "/search", "/sources"]);
  });

  it("(a2) UNADVISED advisory: the input is returned in full — unknown is NOT denied (the control)", () => {
    expect(visibleSections(SECTIONS, UNADVISED)).toEqual(SECTIONS);
  });

  it("(b) known + refused: drops exactly the items mapped to the refused surface", () => {
    // Approvals maps to "approvals" in the real ROUTE_SURFACE; Improvement maps
    // to "steward". Refusing "approvals" only must drop the Approvals section
    // and leave Improvement (an unrelated surface) untouched.
    const out = visibleSections(SECTIONS, known(["approvals"]));
    const labels = out.map((s) => s.label);
    expect(labels).toEqual(["Home", "Workspace", "Operations"]);
    const operations = out.find((s) => s.label === "Operations");
    expect(operations?.items?.map((i) => i.label)).toEqual(["Sources", "Improvement"]);
  });

  it("(c) a group whose EVERY item is refused disappears, header included", () => {
    // This branch is unreachable through the real NAV_SECTIONS with today's
    // surface map (every real group keeps at least one unmapped route), so it
    // is proven here at the pure seam with a synthetic section.
    const bothMapped: NavSection[] = [
      {
        label: "AllGated",
        items: [
          { label: "Sources", to: "/sources" },
          { label: "Improvement", to: "/improvement" },
        ],
      },
    ];
    const out = visibleSections(bothMapped, known(["steward"]));
    // Sources has no ROUTE_SURFACE entry, so it survives on its own — refute
    // that first so the "whole group gone" case below is actually testing
    // BOTH items being dropped, not one already-unmapped route surviving.
    expect(out[0]?.items?.map((i) => i.label)).toEqual(["Sources"]);

    // Now map BOTH to a refused surface: the group vanishes entirely.
    const outAllGone = visibleSections(
      [
        {
          label: "AllGated",
          items: [{ label: "Improvement", to: "/improvement" }],
        },
      ],
      known(["steward"]),
    );
    expect(outAllGone).toEqual([]);
  });

  it("(d) items with no ROUTE_SURFACE entry always survive, no matter what is refused", () => {
    // Ask and Search carry no surface mapping in the real ROUTE_SURFACE, so
    // even a maximally-refused advisory (every known surface refused) leaves
    // them untouched.
    const out = visibleSections(SECTIONS, known(["approvals", "steward", "ops"]));
    const workspace = out.find((s) => s.label === "Workspace");
    expect(workspace?.items?.map((i) => i.to)).toEqual(["/ask", "/search"]);
  });
});

describe("railSections — the pending slot holds the gated destination's place", () => {
  it("PENDING advisory: each mapped destination is a pending slot IN ITS OWN POSITION, never a link", () => {
    const out = railSections(SECTIONS, PENDING);
    expect(out).toEqual([
      { kind: "link", label: "Home", to: "/" },
      {
        kind: "group",
        label: "Workspace",
        slots: [
          { kind: "link", label: "Ask", to: "/ask" },
          { kind: "link", label: "Search", to: "/search" },
        ],
      },
      // The defect's exact site: this used to be a LINK while the answer was
      // in flight, which is the flash. It is a slot that says "unknown".
      { kind: "pending", to: "/approvals" },
      {
        kind: "group",
        label: "Operations",
        slots: [
          { kind: "link", label: "Sources", to: "/sources" },
          { kind: "pending", to: "/improvement" },
        ],
      },
    ]);
  });

  it("KNOWN advisory renders links or nothing — a verdict leaves no pending slot behind", () => {
    // Granted: the slot is the link (so pending was never an invented denial)…
    const granted = railSections(SECTIONS, known([]));
    expect(granted.flatMap((s) => (s.kind === "group" ? s.slots : [s])).map((s) => s.kind)).toEqual(
      ["link", "link", "link", "link", "link", "link"],
    );
    // …and refused: the slot is GONE, not a skeleton that shimmers forever.
    const refused = railSections(SECTIONS, known(["approvals", "steward"]));
    expect(refused).toEqual([
      { kind: "link", label: "Home", to: "/" },
      {
        kind: "group",
        label: "Workspace",
        slots: [
          { kind: "link", label: "Ask", to: "/ask" },
          { kind: "link", label: "Search", to: "/search" },
        ],
      },
      {
        kind: "group",
        label: "Operations",
        slots: [{ kind: "link", label: "Sources", to: "/sources" }],
      },
    ]);
  });
});

describe("the palette and the `g` chord agree with the rail, in all three states (§3, §2.6)", () => {
  function railLinkRoutes(advisory: Entitlements): ReadonlySet<string> {
    const routes = new Set<string>();
    for (const section of railSections(NAV_SECTIONS, advisory)) {
      if (section.kind === "link") routes.add(section.to);
      if (section.kind === "group") {
        for (const slot of section.slots) if (slot.kind === "link") routes.add(slot.to);
      }
    }
    routes.add(NAV_FOOT.to);
    return routes;
  }

  it("shortcutRoutes IS the rail's link set (plus the pinned foot) for pending, unadvised and known", () => {
    for (const advisory of [PENDING, UNADVISED, known(["approvals"]), known(["ops", "steward"])]) {
      expect(shortcutRoutes(advisory)).toEqual(railLinkRoutes(advisory));
      expect(shortcutRoutes(advisory).has("/access")).toBe(false);
    }
  });

  it("PENDING: no gated route is reachable by shortcut — and (control) every open one still is", () => {
    const routes = shortcutRoutes(PENDING);
    for (const gated of ["/approvals", "/models", "/improvement", "/audit"]) {
      expect(routes.has(gated)).toBe(false);
    }
    // The control an over-broad "nothing works while loading" would flag:
    for (const open of ["/", "/search", "/posture", "/settings"]) {
      expect(routes.has(open)).toBe(true);
    }
  });

  it("KNOWN: the shortcut honours the verdict in BOTH directions", () => {
    // Refused: gone. Granted: reachable. The client added no rule of its own.
    const opsStewardKey = shortcutRoutes(known(["approvals"]));
    expect(opsStewardKey.has("/approvals")).toBe(false);
    expect(opsStewardKey.has("/audit")).toBe(true);
    const approverKey = shortcutRoutes(known(["ops", "steward"]));
    expect(approverKey.has("/approvals")).toBe(true);
    expect(approverKey.has("/audit")).toBe(false);
  });

  it("UNADVISED: every destination is reachable — the shortcut invents no denial either", () => {
    const routes = shortcutRoutes(UNADVISED);
    for (const item of flattenSections(NAV_SECTIONS)) {
      expect(routes.has(item.to)).toBe(true);
    }
    expect(routes.has(NAV_FOOT.to)).toBe(true);
  });
});
