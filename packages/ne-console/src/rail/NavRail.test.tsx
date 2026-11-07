// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.2 — the rail. Replaces the ContainmentRail suite, which tested a rail that
// no longer exists: half its cases pinned the twelve-constraint annunciator (its
// row-quantised scroll box, its fitted row count, its pinned "ALL 12 ON POSTURE"
// escape hatch, its roving tabindex over twelve rows) — a compliance widget that
// has moved to Posture, where a compliance question is actually being asked.
//
// What survives, because it is about the rail rather than about the annunciator:
// the wordmark and the deployment name, the ORDERED destination identity for the
// entitlement-less control AND for two principals with different entitlement
// sets (the load-bearing subset guard), and the pending badge's refusal/zero
// behaviour.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { RuntimeConfigContext } from "../AppBootstrap";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { PendingApprovalsProvider } from "../hooks/usePendingApprovals";
import { PostureProvider } from "../posture/PostureContext";
import { AuthStub } from "../test/authStub";
import {
  PENDING_APPROVALS,
  PRINCIPAL_APPROVER,
  PRINCIPAL_OPS_STEWARD,
  RUNTIME_CONFIG,
} from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { badgeLabel, NavRail } from "./NavRail";

// THE ORDER MIRRORS App.tsx's AuthedLayout: PrincipalProvider sits OUTSIDE the
// polls so the polls themselves can consult its entitlements. A harness nested
// differently from production is a test-local composition that proves nothing
// about the shell.
function renderRail(collapsed = false) {
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <MemoryRouter>
          <PrincipalProvider>
            <AuditPollProvider intervalMs={100000}>
              <PendingApprovalsProvider intervalMs={100000}>
                <PostureProvider>
                  <NavRail collapsed={collapsed} />
                </PostureProvider>
              </PendingApprovalsProvider>
            </AuditPollProvider>
          </PrincipalProvider>
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

/** Every destination the rail offers, in DOM order, including the pinned foot. */
const FULL_HREFS = [
  "/",
  "/knowledge/documents",
  "/knowledge/graph",
  "/knowledge/ontology",
  "/agents",
  "/search",
  "/verified-queries",
  "/sources",
  "/models",
  "/evaluations",
  "/improvement",
  "/approvals",
  "/audit",
  "/conversation-ledger",
  "/posture",
  "/insights",
  "/settings",
];

function railHrefs(container: HTMLElement): Array<string | null> {
  return [...container.querySelectorAll(".ne-rail__nav a, .ne-rail__foot a")].map((a) =>
    a.getAttribute("href"),
  );
}

function railGroupLabels(container: HTMLElement): Array<string | null> {
  return [...container.querySelectorAll(".ne-rail__grouplabel")].map((g) => g.textContent);
}

/** The destinations that render as skeleton slots while GET /v1/me is in
 * flight — exactly the ROUTE_SURFACE-mapped rail routes, nothing else.
 *
 * `/workflows` is ROUTE_SURFACE-mapped ("ops") but is no longer a RAIL route —
 * the owner directed the destination out of the product on 2026-08-28 (SPEC
 * §13.1 item 8, applied in 6480ea04) — so it holds no slot here. A route with
 * a surface but no rail row is still filtered and refused on its own terms
 * when it is reached directly; it simply has nothing in the rail to hold. */
const GATED_HREFS = ["/approvals", "/models", "/improvement", "/audit", "/conversation-ledger"];

/** FULL_HREFS minus the gated routes: what the in-flight rail may show as links. */
const OPEN_HREFS = FULL_HREFS.filter((href) => !GATED_HREFS.includes(href));

async function settled(container: HTMLElement): Promise<void> {
  await waitFor(() => expect(container.querySelector(".ne-rail__slotpending")).toBeNull());
}

describe("NavRail (§1.2)", () => {
  it("renders the wordmark and the deployment name, and nothing to configure", () => {
    const { container } = renderRail();
    expect(screen.getByText("Neutron Enterprise")).toBeInTheDocument();
    // RUNTIME_CONFIG carries no deployment_name → the served host, alone. §1.2:
    // "no instruction to configure it anywhere in the UI."
    expect(container.textContent).toContain("localhost:3000");
    expect(container.textContent).not.toContain("config.json");
    expect(container.textContent).not.toContain("UNNAMED");
  });

  it("carries the destinations of §1.2, in order, with Settings pinned at the foot", async () => {
    const { container } = renderRail();
    // The gated destinations are loading slots until GET /v1/me resolves; this
    // deployment answers with no advisory, so the full set then renders.
    await settled(container);
    expect(railHrefs(container)).toEqual(FULL_HREFS);
    expect(railGroupLabels(container)).toEqual([
      "KNOWLEDGE LAYER",
      "EXECUTION",
      "OPERATIONS",
      "GOVERNANCE",
      "INSIGHTS",
    ]);
    // Settings is outside the nav block entirely — it is the rail's foot.
    expect(container.querySelector(".ne-rail__foot a")?.getAttribute("href")).toBe("/settings");
  });

  it("CARRIES NO ANNUNCIATOR, no chain lamp, no config hash and no air-gap row", async () => {
    // All four moved: the annunciator to Posture, the rest to the top bar's
    // status popover (§1.2's "deleted from the rail" list). This is the case
    // that keeps them from creeping back one row at a time.
    const { container } = renderRail();
    await waitFor(() => expect(screen.getByText("Posture")).toBeInTheDocument());
    expect(container.querySelector(".ne-rail__lamprows")).toBeNull();
    expect(container.querySelector(".ne-rail__annunciator")).toBeNull();
    expect(container.textContent).not.toContain("CONSTRAINTS");
    expect(container.textContent).not.toContain("attested");
    expect(container.textContent).not.toContain("CONFIG_HASH");
    expect(container.textContent).not.toContain("TRIMMED AS");
    expect(container.textContent).not.toContain("Air gap");
    // No 64-char hex anywhere in the rail — the overflow-paint defect's root.
    expect(container.textContent ?? "").not.toMatch(/[0-9a-f]{32,}/);
    // And no theme toggle: it is a preference, and preferences live in the
    // account menu and on Settings (§1.2).
    expect(container.querySelector(".ne-rail__theme")).toBeNull();
  });

  describe("the role-filtered subset guard", () => {
    // VACUITY TRAP: an entitled-to-everything principal makes filtering
    // untestable. This deployment sends NO entitlements — the standing
    // UNKNOWN-IS-NOT-DENIED control the two mutations below are measured
    // against: once its answer arrives WITHOUT an advisory, nothing may be
    // hidden. (While the answer is in flight the gated slots are skeletons —
    // that state has its own suite below; this control pins the settled state.)
    it("renders the FULL destination set for the entitlement-less deployment (control)", async () => {
      const { container } = renderRail();
      await settled(container);
      expect(railHrefs(container)).toEqual(FULL_HREFS);
    });

    it("renders exactly the APPROVER's subset — Improvement, Models and Audit log ABSENT", async () => {
      server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_APPROVER)));
      const { container } = renderRail();
      // Until GET /v1/me resolves the gated destinations are skeleton slots,
      // never links — wait for the verdict before asserting identity.
      await settled(container);
      // FULL ORDERED IDENTITY, not presence and not a length check: a
      // destination that should be hidden is proven absent by not appearing
      // anywhere in this array.
      expect(railHrefs(container)).toEqual([
        "/",
        "/knowledge/documents",
        "/knowledge/graph",
        "/knowledge/ontology",
        "/agents",
        "/search",
        "/verified-queries",
        "/sources",
        "/evaluations",
        "/approvals",
        "/posture",
        "/insights",
        "/settings",
      ]);
    });

    it("renders exactly the OPS_STEWARD's subset — Approvals ABSENT", async () => {
      // DISJOINT from the approver fixture, which is a stronger guard than
      // overlapping: every destination present for one principal is absent for
      // the other, so a filter that ignored its input could not satisfy both.
      server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_OPS_STEWARD)));
      const { container } = renderRail();
      await settled(container);
      expect(railHrefs(container)).toEqual([
        "/",
        "/knowledge/documents",
        "/knowledge/graph",
        "/knowledge/ontology",
        "/agents",
        "/search",
        "/verified-queries",
        "/sources",
        "/models",
        "/evaluations",
        "/improvement",
        "/audit",
        "/conversation-ledger",
        "/posture",
        "/insights",
        "/settings",
      ]);
    });
  });
});

describe("NavRail — PENDING IS NOT PERMITTED: the in-flight rail asserts nothing (§1.2, §2.4)", () => {
  // THE OWNER'S DEFECT, PINNED AT ITS OWN SEAM. Signed in as a key the server
  // refuses approvals to, a reload used to render the full rail — Approvals
  // included — and then snatch the refused links away when GET /v1/me landed.
  // The first render after mount is the exact window the flash lived in, so
  // these assertions run SYNCHRONOUSLY on it, before any answer can arrive.

  it("never renders a gated link before the verdict: skeleton slots, then the refused link JUST ISN'T", async () => {
    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_OPS_STEWARD)));
    const { container } = renderRail();
    // The flash window: no gated destination is a link…
    expect(railHrefs(container)).toEqual(OPEN_HREFS);
    // …their slots are held by skeletons (one per gated destination, so the
    // rail's geometry does not jump when the verdict lands)…
    expect(container.querySelectorAll(".ne-rail__slotpending").length).toBe(GATED_HREFS.length);
    // …announced once, in the redesign's loading vocabulary, not silently.
    expect(screen.getByRole("status")).toHaveTextContent("Loading navigation");

    // The verdict lands: the skeletons resolve, Approvals never having been
    // shown — nothing was snatched away — and nothing keeps shimmering.
    await settled(container);
    expect(railHrefs(container)).not.toContain("/approvals");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("CONTROL — pending is not an invented denial: the approver's granted link ARRIVES from its slot", async () => {
    server.use(http.get("*/api/v1/me", () => HttpResponse.json(PRINCIPAL_APPROVER)));
    const { container } = renderRail();
    // Same in-flight window, same honest answer: not shown yet, not denied.
    expect(railHrefs(container)).toEqual(OPEN_HREFS);
    await settled(container);
    // The server said yes, so the link is there — a client that "fixed" the
    // flash by hiding gated destinations outright would fail HERE.
    expect(railHrefs(container)).toContain("/approvals");
    expect(container.querySelectorAll(".ne-rail__slotpending").length).toBe(0);
  });

  it("CONTROL — the open destinations are links IMMEDIATELY: loading gates only what a verdict could gate", () => {
    // An over-broad "skeleton everything while loading" (or an empty rail)
    // would flag this synchronous assertion: Home, the Workspace, Sources,
    // Evaluations, Governance's open rows, KPIs and Settings owe no surface
    // an answer and render as links from the first paint.
    const { container } = renderRail();
    expect(railHrefs(container)).toEqual(OPEN_HREFS);
    expect(railGroupLabels(container)).toEqual([
      "KNOWLEDGE LAYER",
      "EXECUTION",
      "OPERATIONS",
      "GOVERNANCE",
      "INSIGHTS",
    ]);
  });
});

describe("NavRail — the pending-approvals badge (§1.2)", () => {
  it("names both quantities in its accessible name, and never labels the total 'overdue'", () => {
    // Pure, so every combination is reachable without a provider or a fixture.
    expect(badgeLabel(2, 1)).toBe("2 pending, 1 overdue");
    expect(badgeLabel(2, 0)).toBe("2 pending");
    // The defect this replaces: the visible string read "2 OVERDUE" for a queue
    // of 2 with 1 past its deadline. A numeral is never adjacent to a word it
    // does not describe.
    for (const [count, over] of [
      [2, 1],
      [5, 2],
      [9, 1],
    ] as const) {
      expect(badgeLabel(count, over)).not.toContain(`${count} overdue`);
      expect(badgeLabel(count, over)).toContain(`${over} overdue`);
    }
    // The control: with everything overdue, "3 overdue" is CORRECT, so the rule
    // above was not achieved by forbidding a true statement.
    expect(badgeLabel(3, 3)).toContain("3 overdue");
  });

  it("renders the COUNT ALONE as its text, with the full sentence as its name", async () => {
    const { container } = renderRail();
    const breached = PENDING_APPROVALS.filter(
      (a) => Date.parse(a.sla_deadline) < Date.now(),
    ).length;
    const badge = await screen.findByLabelText(badgeLabel(PENDING_APPROVALS.length, breached));
    // §1.2: "Text is {count} alone."
    expect(badge.textContent).toContain(String(PENDING_APPROVALS.length));
    expect(badge.textContent).not.toContain("pending");
    // It rides the Approvals destination, so it is present on every route.
    expect(badge.closest("a")).toHaveAttribute("href", "/approvals");
    expect(
      screen.getByRole("link", {
        name: new RegExp(`Approvals ${PENDING_APPROVALS.length} pending`),
      }),
    ).toBe(badge.closest("a"));
    expect(container.querySelector(".ne-rail__badge")).not.toBeNull();
  });

  it("uses one persistent portal tooltip in compact mode without masking Approvals detail", async () => {
    renderRail(true);
    const breached = PENDING_APPROVALS.filter(
      (a) => Date.parse(a.sla_deadline) < Date.now(),
    ).length;
    const link = await screen.findByRole("link", {
      name: new RegExp(`Approvals ${badgeLabel(PENDING_APPROVALS.length, breached)}`),
    });
    const badge = screen.getByLabelText(badgeLabel(PENDING_APPROVALS.length, breached));
    expect(badge).not.toHaveAttribute("title");
    await userEvent.hover(link);
    const tooltip = screen.getByRole("tooltip", { name: "Approvals" });
    expect(screen.getAllByRole("tooltip")).toHaveLength(1);
    expect(link).toHaveAttribute("aria-describedby", tooltip.id);
    const hitRegion = tooltip.parentElement!;
    await userEvent.unhover(link);
    await userEvent.hover(hitRegion);
    expect(screen.getByRole("tooltip", { name: "Approvals" })).toBeInTheDocument();
    await userEvent.unhover(hitRegion);
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  });

  it("takes the overdue tint AND a glyph — never colour alone", async () => {
    const now = Date.now();
    server.use(
      http.get("*/api/v1/approvals/pending", () =>
        HttpResponse.json({
          approvals: [
            { ...PENDING_APPROVALS[0]!, sla_deadline: new Date(now + 30 * 60_000).toISOString() },
            {
              ...PENDING_APPROVALS[1]!,
              request_id: "req-late",
              sla_deadline: new Date(now - 5 * 60_000).toISOString(),
            },
          ],
        }),
      ),
    );
    const { container } = renderRail();
    const badge = await screen.findByLabelText("2 pending, 1 overdue");
    expect(badge.className).toContain("ne-rail__badge--overdue");
    expect(container.querySelector(".ne-rail__badgeglyph")).not.toBeNull();
  });

  it("renders NO badge — never a zero — for a refused queue", async () => {
    server.use(
      http.get("*/api/v1/approvals/pending", () =>
        HttpResponse.json({ detail: "not an approver" }, { status: 403 }),
      ),
    );
    const { container } = renderRail();
    await waitFor(() => expect(screen.getByText("Approvals")).toBeInTheDocument());
    await new Promise((r) => setTimeout(r, 30));
    // A key that was never shown the queue must not be told there is nothing in it.
    expect(container.querySelector(".ne-rail__badge")).toBeNull();
    expect(screen.getByRole("link", { name: "Approvals" })).toBeInTheDocument();
  });

  it("renders no badge for an EMPTY queue either — nothing waiting needs no annunciation", async () => {
    server.use(http.get("*/api/v1/approvals/pending", () => HttpResponse.json({ approvals: [] })));
    const { container } = renderRail();
    await waitFor(() => expect(screen.getByText("Approvals")).toBeInTheDocument());
    await new Promise((r) => setTimeout(r, 30));
    expect(container.querySelector(".ne-rail__badge")).toBeNull();
    expect(screen.getByRole("link", { name: "Approvals" })).toBeInTheDocument();
  });
});
