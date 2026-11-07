// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The shell (NE-130). Proves "/" renders the WORK SURFACE (no redirect to /chat,
// and no pitch — the demo script that used to be the page body is now the
// dismissible first-run panel, proved in demo/firstRun.test.tsx); the placeholder
// routes render honest "AWAITING NE-1xx" placards naming their feeding route (with the
// /knowledge→/search link); /flywheel renders the shipped Flywheel Dashboard and
// /steward + /steward/digest redirect into its tabs (NE-138); a pre-existing
// route still renders its real page inside the new rail shell; and the rail is
// present on every authed route.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "./App";
import { RuntimeConfigContext } from "./AppBootstrap";
import { STATIONS } from "./demo/stations";
import { dismissTour } from "./demo/tourStorage";
import { AuthStub } from "./test/authStub";
import { RUNTIME_CONFIG } from "./test/mocks/fixtures";
import { server } from "./test/mocks/server";

function renderAt(path: string) {
  window.history.pushState({}, "", path);
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

afterEach(() => {
  window.history.pushState({}, "", "/");
});

describe("App shell", () => {
  it("'/' renders the work surface, not a pitch and not a redirect to /chat", async () => {
    renderAt("/");
    // The home screen is a working page: it opens on what needs this person.
    // §4: the h1 is the DEPLOYMENT NAME — the serving host here, since the test
    // runtime config names none — and the work is a table with rows in it.
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(window.location.host);
    expect(screen.getByRole("table", { name: "Waiting on you" })).toBeInTheDocument();
    expect(await screen.findByText("payments.wire_transfer")).toBeInTheDocument();
    // The old pitch is gone from the route entirely.
    expect(
      screen.queryByText("AI in production, in your infrastructure, never locked to a vendor."),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("AWAITING NE-131")).not.toBeInTheDocument();
    // No redirect: the chat composer is absent.
    expect(screen.queryByPlaceholderText(/message/i)).not.toBeInTheDocument();
  });

  it("'/posture' renders the live posture board (AwaitingUnit gone)", async () => {
    renderAt("/posture");
    // The board renders live from GET /v1/posture (fixture) — not an AwaitingUnit.
    expect(await screen.findByRole("heading", { name: "Posture" })).toBeInTheDocument();
    // Round 3 (CR-B8) recomposes the controls table into a twelve-tile
    // annunciator: a tile carries only id/name/state, and the server's own
    // enforcement sentence — the competitor-comparison table's live
    // replacement (NE-CONSOLE-IA) — now renders one click away, in the docked
    // pane a tile click fills.
    await userEvent.click(await screen.findByText("PDP CHOKEPOINT"));
    expect(
      screen.getByText("Governed executor: no-bypass + PDP audit coupling"),
    ).toBeInTheDocument();
    expect(screen.queryByText("AWAITING NE-131")).not.toBeInTheDocument();
  });

  it("'/models' renders the live lanes table, and no fabricated spend (§10)", async () => {
    renderAt("/models");
    // Inside the rail shell, like every other authed route.
    expect(screen.getByText("Neutron Enterprise")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Models" })).toBeInTheDocument();
    // The lanes the posture read serves, in a real table (§10 Policy tab).
    const table = await screen.findByRole("table", { name: "Model policy" });
    expect(await within(table).findByText("qwen3-32b")).toBeInTheDocument();
    // The usage rollup is not served by this deployment, so no price is
    // rendered anywhere on the route — and no zero standing in for one.
    const body = document.querySelector(".ne-screen__body");
    expect(body).not.toBeNull();
    expect(body?.textContent ?? "").not.toMatch(/\$/);
    expect(body?.textContent ?? "").not.toMatch(/NE-\d/);
  });

  it("'/workflows' reaches the workflow engine from the browser (NE-WORKFLOWS)", async () => {
    renderAt("/workflows");
    // Inside the rail shell, like every other authed route.
    expect(screen.getByText("Neutron Enterprise")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Workflows" })).toBeInTheDocument();
    // The served workflows, in a real table — the engine had NO route at all
    // before this unit and was reachable only by curl.
    const table = await screen.findByRole("table", { name: "Workflows" });
    expect(await within(table).findByText("Unresolved ticket review")).toBeInTheDocument();
    // And the permission contract is on screen without anything having run.
    expect(
      within(screen.getByRole("list", { name: "Writes" })).getByText("write.internal.crm.note"),
    ).toBeInTheDocument();
  });

  it("routes an approver through the content-free Run shell without reading the owner Run", async () => {
    const ownerRunReads: string[] = [];
    server.use(
      http.get("*/api/v1/approvals/pending", () =>
        HttpResponse.json({
          approvals: [
            {
              request_id: "approval-exact",
              action: "draft.email.create",
              tier: 2,
              principal_id: "user:owner",
              requested_at: "2026-08-23T00:00:00Z",
              sla_deadline: "2026-08-23T01:00:00Z",
              expires_at: "2026-08-23T02:00:00Z",
              escalated: false,
            },
          ],
        }),
      ),
      http.get("*/api/v1/approvals/approval-exact/payload", () =>
        HttpResponse.json({
          disclosure_status: "available",
          request_id: "approval-exact",
          action: "draft.email.create",
          tier: 2,
          principal_id: "user:owner",
          target_resource: "protected Run action",
          description: "Protected source-derived Run action",
          material_parameters: null,
          reversibility: "Draft can be discarded.",
          run_context: {
            approval_id: "approval-exact",
            agent_id: "agt-meridian",
            run_id: "run-exact",
          },
        }),
      ),
      http.get("*/api/v1/agents/:agentId/runs/:runId", ({ request }) => {
        ownerRunReads.push(request.url);
        return HttpResponse.json({ detail: "owner Run must not be read" }, { status: 500 });
      }),
    );
    renderAt("/agents/agt-meridian/runs/run-exact/approval/approval-exact");

    expect(await screen.findByRole("heading", { name: "draft.email.create" })).toBeInTheDocument();
    expect(ownerRunReads).toEqual([]);
  });

  it("/connect redirects to the live Sources screen (§9)", async () => {
    renderAt("/connect");
    // The rename ships with its redirect, and the screen renders live from the
    // served connector status (fixture).
    expect(await screen.findByRole("heading", { name: "Sources" })).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Sources" })).toBeInTheDocument();
    expect(screen.queryByText("AWAITING NE-132")).not.toBeInTheDocument();
  });

  it("/knowledge redirects to the canonical Documents workspace", async () => {
    server.use(
      http.get("*/api/v1/documents", () =>
        HttpResponse.json({
          documents: [],
          next_cursor: null,
          document_types: [],
          source_systems: [],
        }),
      ),
    );
    renderAt("/knowledge");
    expect(await screen.findByRole("heading", { name: "Documents", level: 1 })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/knowledge/documents");
  });

  it("/flywheel redirects to Improvement and renders its proposal queue inside the rail shell", async () => {
    renderAt("/flywheel");
    // The rail is present on this authed route.
    expect(screen.getByText("Neutron Enterprise")).toBeInTheDocument();
    // The queue, populated from the governed steward read.
    expect(
      await screen.findByText("[flywheel:content] proposal 8f19c2a41b0d7e33"),
    ).toBeInTheDocument();
  });

  it("/steward redirects into the improvement queue tab", async () => {
    renderAt("/steward");
    const queueTab = screen.getByRole("link", { name: "Queue" });
    expect(queueTab).toHaveAttribute("aria-current", "page");
    expect(await screen.findByRole("table", { name: "Proposals" })).toBeInTheDocument();
  });

  it("/steward/digest redirects into the improvement digest tab", async () => {
    renderAt("/steward/digest");
    const digestTab = screen.getByRole("link", { name: "Digest" });
    expect(digestTab).toHaveAttribute("aria-current", "page");
    // The week the server described, both ends of it, each in its own <time>.
    expect(await screen.findByText("2026-06-29")).toBeInTheDocument();
    expect(screen.getByText("2026-07-05")).toBeInTheDocument();
  });

  it("/trace is a drill-down: the route resolves, and nothing browses to it", () => {
    renderAt("/trace");
    // §13b: a trace is opened ABOUT something — an audit record, an answer, an
    // approval. With no run named, the route says where one comes from; it
    // offers no dispatch trigger and no paste-an-id box, and the rail carries no
    // "Trace" link.
    expect(
      screen.getByText(
        "No run selected. Open a trace from an audit record, an answer, or an approval.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("link", { name: "Trace" })).toBeNull();
    expect(screen.getByText("Neutron Enterprise")).toBeInTheDocument();
  });

  it("a pre-existing route (/kpi) still renders its real page inside the rail shell", async () => {
    renderAt("/kpi");
    expect(screen.getByRole("heading", { name: "Insights", level: 1 })).toBeInTheDocument();
    // The rail is present on this authed route.
    expect(screen.getByText("Neutron Enterprise")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("table", { name: "Utilization" })).toBeInTheDocument(),
    );
  });

  it("the rail renders on the landing route too", () => {
    renderAt("/");
    expect(screen.getByText("Neutron Enterprise")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });

  it("the account menu summons the guided tour and Escape dismisses it", async () => {
    // Start from a browser that has already seen the tour off, so this exercises
    // the summoned path. The first-run path has its own suite (demo/firstRun).
    dismissTour();
    renderAt("/");
    expect(screen.queryByRole("dialog", { name: "guided tour" })).not.toBeInTheDocument();
    // §1.2/§1.3: the tour control LEFT the rail — it is a thing most people open
    // once, and it occupied a permanent rail row on every route. It reopens at
    // the first station from the account menu, where preferences live.
    await userEvent.click(screen.getByRole("button", { name: "Account" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Guided tour" }));
    const overlay = screen.getByRole("dialog", { name: "guided tour" });
    expect(overlay).toBeInTheDocument();
    expect(within(overlay).getByRole("heading", { name: STATIONS[0]!.title })).toBeInTheDocument();
    // Escape dismisses it.
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "guided tour" })).not.toBeInTheDocument();
  });
});

// COLD ENTRY (2026-08 audit walk). A hard reload / pasted URL is a fresh mount
// of <App> at that path — exactly what renderAt() does. Before the fix,
// /documents (and any other unmatched path) matched NO route: <Routes> rendered
// null, AuthedLayout (and RequireAuth inside it) never mounted, and the page
// was a permanently blank root with an empty console. These tests are the
// regression net for that whole class.
describe("cold entry", () => {
  it("'/documents' (the rail label, never a real route) redirects to /knowledge/documents", () => {
    renderAt("/documents");
    // The redirect landed: URL rewritten, and the Documents screen's title row
    // is on screen (DocumentsPage renders its h1 in every load state).
    expect(window.location.pathname).toBe("/knowledge/documents");
    expect(screen.getByRole("heading", { name: "Documents", level: 1 })).toBeInTheDocument();
    // Inside the authed rail shell — NOT a blank root.
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });

  it("'/knowledge/documents' renders the Documents screen directly on cold entry", () => {
    renderAt("/knowledge/documents");
    expect(window.location.pathname).toBe("/knowledge/documents");
    expect(screen.getByRole("heading", { name: "Documents", level: 1 })).toBeInTheDocument();
  });

  it("an unmatched path renders a visible router miss, never a blank root", () => {
    renderAt("/no/such/route");
    // The silent-blank class is closed: something is ALWAYS on screen.
    expect(screen.getByRole("heading", { name: "Page not found", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("/no/such/route")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
    // And the rail is still there to navigate away with.
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });
});

afterEach(() => {
  sessionStorage.clear();
});
