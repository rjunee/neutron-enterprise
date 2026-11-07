// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4 — HOME, held to what the spec says it is: actionable rows, in the order
// urgency puts them, from reads the server actually answered.
//
// The cases that matter here are the ones the shipped page failed:
//   * there IS a table, with rows and a stated default order;
//   * a refused surface produces NO tile, NO section and NO zero;
//   * a failed read produces the em dash and a named failure, never a value;
//   * the browser-local journal is gone and does not come back through a test.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConsoleRuntimeConfig } from "../api/runtimeConfig";
import { RuntimeConfigContext } from "../AppBootstrap";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { DirectoryProvider } from "../directory/DirectoryContext";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { PendingApprovalsProvider } from "../hooks/usePendingApprovals";
import { PostureProvider } from "../posture/PostureContext";
import { NavRail } from "../rail/NavRail";
import { AuthStub } from "../test/authStub";
import {
  AUDIT_EVENTS,
  AUDIT_EVENTS_PDP_MIX,
  NOW_ISO,
  PENDING_APPROVALS,
  PRINCIPAL_APPROVER,
  PRINCIPAL_OPS_STEWARD,
  RUNTIME_CONFIG,
  STEWARD_PROPOSALS,
  VERIFICATION_OK,
  VERIFICATION_PDP_MIX,
} from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { LandingPage } from "./LandingPage";

/** Where a row click actually went. */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
}

function renderHome(config: Partial<ConsoleRuntimeConfig> = {}, withRail = false) {
  return render(
    <AuthStub>
      <MemoryRouter initialEntries={["/"]}>
        <RuntimeConfigContext.Provider value={{ ...RUNTIME_CONFIG, ...config }}>
          <PrincipalProvider>
            <DirectoryProvider>
              <PendingApprovalsProvider intervalMs={5_000}>
                <AuditPollProvider intervalMs={5_000}>
                  <PostureProvider>
                    {withRail ? <NavRail /> : null}
                    <LocationProbe />
                    <Routes>
                      <Route path="/" element={<LandingPage />} />
                      <Route path="/approvals" element={<p>Approvals screen</p>} />
                      <Route path="/improvement" element={<p>Improvement screen</p>} />
                    </Routes>
                  </PostureProvider>
                </AuditPollProvider>
              </PendingApprovalsProvider>
            </DirectoryProvider>
          </PrincipalProvider>
        </RuntimeConfigContext.Provider>
      </MemoryRouter>
    </AuthStub>,
  );
}

/** Refuse a governed read exactly as the deployment does. */
function refuse(path: string, status = 403) {
  server.use(http.get(path, () => HttpResponse.json({ detail: "not authorized" }, { status })));
}

const APPROVALS = "*/api/v1/approvals/pending";
const PROPOSALS = "*/api/v1/steward/proposals";
const CONNECT = "*/api/v1/connect/status";
const AUDIT = "*/api/v1/audit/events";

function queueTable() {
  return screen.getByRole("table", { name: "Waiting on you" });
}

/** One line per rendered row: Type · Item · Due. Three columns name every
 * fixture row uniquely, so an order assertion is about row IDENTITY and not
 * about how many rows came back. */
function queueRows(): string[] {
  return within(queueTable())
    .getAllByRole("row")
    .slice(1)
    .map((row) => {
      const cells = within(row).getAllByRole("cell");
      return [cells[0], cells[1], cells[4]].map((c) => c?.textContent?.trim() ?? "").join(" · ");
    });
}

/** The default order §4 states: approvals by deadline ascending, then proposals
 * by age ascending. Written out, not computed, so the test states the answer. */
const URGENCY_ORDER = [
  "Approval · crm.create_note · Overdue",
  "Approval · crm.create_note · in 50 min",
  "Approval · write.internal.notes.append · in 55 min",
  "Approval · payments.wire_transfer · in 2 h",
  "Review · [flywheel:policy] proposal 2b6a9d0c1e4f7788 · —",
  "Review · [flywheel:content] proposal 8f19c2a41b0d7e33 · —",
  "Review · [flywheel:retrieval] proposal 9c31f0a2b4d5e677 · —",
];

beforeEach(() => {
  // Every Age/Due/oldest string in this file is measured from the fixture epoch.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(NOW_ISO));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Home — the page header", () => {
  it("leads with the deployment name and nothing else", async () => {
    renderHome({ deployment_name: "Fenwick Plant 3" });
    const h1 = await screen.findByRole("heading", { level: 1 });
    expect(h1).toHaveTextContent("Fenwick Plant 3");
  });

  it("falls back to the serving host when the deployment is unnamed", async () => {
    renderHome();
    const h1 = await screen.findByRole("heading", { level: 1 });
    expect(h1).toHaveTextContent(window.location.host);
    // No instruction to go and configure one — that was a developer's note
    // rendered as permanent brand chrome.
    expect(document.body.textContent).not.toMatch(/name this deployment/i);
  });
});

describe("Home — the status tiles", () => {
  it("shares one approvals request and one served count with the rail badge", async () => {
    let hits = 0;
    server.use(
      http.get(APPROVALS, () => {
        hits += 1;
        return HttpResponse.json({ approvals: PENDING_APPROVALS });
      }),
    );
    renderHome({}, true);

    const tile = document.querySelector('.ne-factcard[href="/approvals"]');
    expect(tile).not.toBeNull();
    await waitFor(() => expect(tile).toHaveTextContent("4 pending"));
    expect(screen.getByLabelText("4 pending, 1 overdue")).toBeInTheDocument();
    // Restoring Home's useGovernedRead bypass makes this 2: one provider poll
    // for the rail and one page-local request for the tile/table.
    expect(hits).toBe(1);
  });

  it("renders each tile from the read that owns it, as a link to it", async () => {
    renderHome();

    const approvals = await screen.findByRole("link", { name: /Approvals/ });
    expect(approvals).toHaveAttribute("href", "/approvals");
    await waitFor(() => expect(approvals).toHaveTextContent("4 pending"));
    // Oldest of the four fixture requests: 65 minutes back.
    expect(approvals).toHaveTextContent("oldest 1 h ago");

    const audit = await screen.findByRole("link", { name: /Audit log/ });
    expect(audit).toHaveAttribute("href", "/audit");
    expect(audit).toHaveTextContent("Valid");
    expect(audit).toHaveTextContent("3 records");

    const controls = await screen.findByRole("link", { name: /Controls/ });
    expect(controls).toHaveAttribute("href", "/posture");
    await waitFor(() => expect(controls).toHaveTextContent("12/12 verified"));
    expect(controls).toHaveTextContent("Air gap on");

    const sources = await screen.findByRole("link", { name: /Sources/ });
    expect(sources).toHaveAttribute("href", "/sources");
    await waitFor(() => expect(sources).toHaveTextContent("3 configured"));
    expect(sources).toHaveTextContent("1 degraded");
  });

  it("omits a refused tile entirely, and never stands a zero in for it", async () => {
    refuse(APPROVALS);
    renderHome();

    // The other three still arrive, so the omission is the refusal and not a
    // page that failed to render.
    await screen.findByRole("link", { name: /Audit log/ });
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: /Approvals/ })).not.toBeInTheDocument(),
    );
    expect(document.body.textContent).not.toContain("0 pending");
    // And no lock, no explanation of a job that is not this person's.
    expect(document.body.textContent).not.toMatch(/approver key|not visible to your role/i);
  });

  it("dashes a tile whose read failed, and says so in two words", async () => {
    server.use(http.get(CONNECT, () => new HttpResponse(null, { status: 503 })));
    renderHome();

    const sources = await screen.findByRole("link", { name: /Sources/ });
    await waitFor(() => expect(sources).toHaveTextContent("Couldn’t load"));
    expect(sources).toHaveTextContent("—");
    expect(sources.textContent).not.toMatch(/\d/);
  });
});

describe("Home — waiting on you", () => {
  it("waits for the principal answer and never asks a refused steward queue", async () => {
    let resolvePrincipal!: (response: Response) => void;
    const principal = new Promise<Response>((resolve) => {
      resolvePrincipal = resolve;
    });
    let proposalHits = 0;
    server.use(
      http.get("*/api/v1/me", () => principal),
      http.get(PROPOSALS, () => {
        proposalHits += 1;
        return HttpResponse.json({ detail: "not authorized" }, { status: 403 });
      }),
    );

    renderHome();
    await screen.findByRole("heading", { level: 1 });
    expect(proposalHits).toBe(0);

    resolvePrincipal(HttpResponse.json(PRINCIPAL_APPROVER));
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Approvals/ })).toHaveTextContent("4 pending"),
    );
    expect(proposalHits).toBe(0);
  });

  it("starts the steward queue exactly once after the principal grants it", async () => {
    let resolvePrincipal!: (response: Response) => void;
    const principal = new Promise<Response>((resolve) => {
      resolvePrincipal = resolve;
    });
    let proposalHits = 0;
    server.use(
      http.get("*/api/v1/me", () => principal),
      http.get(PROPOSALS, () => {
        proposalHits += 1;
        return HttpResponse.json({ proposals: STEWARD_PROPOSALS });
      }),
    );

    renderHome();
    await screen.findByRole("heading", { level: 1 });
    expect(proposalHits).toBe(0);

    resolvePrincipal(HttpResponse.json(PRINCIPAL_OPS_STEWARD));
    await waitFor(() => expect(proposalHits).toBe(1));
    expect(await screen.findByText(STEWARD_PROPOSALS[0]!.artifact.title)).toBeInTheDocument();
  });

  it("renders the merged queue in the urgency order the spec states", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));
  });

  it("sorts on a header click, and says which header is active", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderHome();
    await waitFor(() => expect(queueRows()).toHaveLength(URGENCY_ORDER.length));

    await user.click(within(queueTable()).getByRole("button", { name: /Item/ }));

    const header = within(queueTable()).getByRole("columnheader", { name: /Item/ });
    expect(header).toHaveAttribute("aria-sort", "ascending");
    const items = queueRows().map((line) => line.split(" · ")[1]);
    expect(items).toEqual([
      "[flywheel:content] proposal 8f19c2a41b0d7e33",
      "[flywheel:policy] proposal 2b6a9d0c1e4f7788",
      "[flywheel:retrieval] proposal 9c31f0a2b4d5e677",
      "crm.create_note",
      "crm.create_note",
      "payments.wire_transfer",
      "write.internal.notes.append",
    ]);

    // A second click reverses it — and the rows are the same rows.
    await user.click(within(queueTable()).getByRole("button", { name: /Item/ }));
    expect(within(queueTable()).getByRole("columnheader", { name: /Item/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(queueRows().map((line) => line.split(" · ")[1])).toEqual([...items].reverse());
  });

  it("marks the breached row OVERDUE and leaves the one inside its deadline PENDING", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toHaveLength(URGENCY_ORDER.length));
    const rows = within(queueTable()).getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByText("OVERDUE")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("PENDING")).toBeInTheDocument();
  });

  it("caps the list at eight and links the remainder to the screen that owns it", async () => {
    server.use(
      http.get(APPROVALS, () =>
        HttpResponse.json({
          approvals: Array.from({ length: 10 }, (_, i) => ({
            ...PENDING_APPROVALS[0]!,
            request_id: `req-bulk-${i}`,
            sla_deadline: new Date(Date.parse(NOW_ISO) + (i + 1) * 60_000).toISOString(),
          })),
        }),
      ),
    );
    renderHome();

    await waitFor(() => expect(queueRows()).toHaveLength(8));
    // 10 approvals + 3 proposals: eight approvals are shown, so two approvals
    // and every proposal are behind the footer links.
    expect(screen.getByRole("link", { name: "2 more in Approvals" })).toHaveAttribute(
      "href",
      "/approvals",
    );
    expect(screen.getByRole("link", { name: "3 more in Improvement" })).toHaveAttribute(
      "href",
      "/improvement",
    );
  });

  it("opens the row on the screen that owns it — Home decides nothing", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));

    // There is ONE decision surface, and it is not this one.
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();

    await user.click(within(queueTable()).getAllByRole("row")[1]!);
    expect(screen.getByTestId("location")).toHaveTextContent("/approvals?request=req-t2-overdue");
  });

  it("opens a review on Improvement, with that proposal", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));

    await user.click(within(queueTable()).getAllByRole("row")[5]!);
    expect(screen.getByTestId("location")).toHaveTextContent("/improvement?proposal=prop-policy-2");
  });

  it("renders the empty copy when both granted queues are empty", async () => {
    server.use(http.get(APPROVALS, () => HttpResponse.json({ approvals: [] })));
    server.use(http.get(PROPOSALS, () => HttpResponse.json({ proposals: [] })));
    renderHome();

    expect(await screen.findByText("Nothing is waiting on you.")).toBeInTheDocument();
    // An empty queue this key CAN read is a real zero and is shown as one.
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Approvals/ })).toHaveTextContent("0 pending"),
    );
  });

  it("keeps an empty served lane non-terminal while another granted lane is loading", async () => {
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(APPROVALS, async () => {
        await blocked;
        return HttpResponse.json({ approvals: [] });
      }),
      http.get(PROPOSALS, () => HttpResponse.json({ proposals: [] })),
    );
    renderHome();

    const queue = await screen.findByRole("region", { name: "Waiting on you" });
    expect(await within(queue).findByRole("status")).toHaveTextContent("Loading your queue");
    expect(screen.queryByText("Nothing is waiting on you.")).not.toBeInTheDocument();

    release();
    expect(await screen.findByText("Nothing is waiting on you.")).toBeInTheDocument();
  });

  it("keeps served rows visible and names a partial queue until the other lane arrives", async () => {
    const proposalTitle = "PARTIAL-PROPOSAL-SENTINEL";
    const approvalAction = "partial.approval.sentinel";
    const proposal = {
      ...STEWARD_PROPOSALS[0]!,
      proposal_id: "proposal-partial-sentinel",
      artifact: {
        ...STEWARD_PROPOSALS[0]!.artifact,
        proposal_id: "proposal-partial-sentinel",
        title: proposalTitle,
      },
    };
    const approval = {
      ...PENDING_APPROVALS[0]!,
      request_id: "approval-partial-sentinel",
      action: approvalAction,
    };
    let release!: (response: Response) => void;
    const blocked = new Promise<Response>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(APPROVALS, () => blocked),
      http.get(PROPOSALS, () => HttpResponse.json({ proposals: [proposal] })),
    );
    renderHome();

    expect(await screen.findByText(proposalTitle)).toBeInTheDocument();
    const queue = screen.getByRole("region", { name: "Waiting on you" });
    expect(within(queue).getByRole("status")).toHaveTextContent("Loading the rest of your queue…");
    expect(screen.queryByText("Nothing is waiting on you.")).not.toBeInTheDocument();

    release(HttpResponse.json({ approvals: [approval] }));
    expect(await screen.findByText(approvalAction)).toBeInTheDocument();
    expect(screen.getByText(proposalTitle)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText("Loading the rest of your queue…")).not.toBeInTheDocument(),
    );
  });

  it("renders an error, never terminal empty, when one empty lane cannot cover a failed lane", async () => {
    server.use(
      http.get(APPROVALS, () => new HttpResponse(null, { status: 503 })),
      http.get(PROPOSALS, () => HttpResponse.json({ proposals: [] })),
    );
    renderHome();

    expect(await screen.findByText("Couldn’t load your queue.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing is waiting on you.")).not.toBeInTheDocument();
  });

  it("retains a served sentinel and names the failed sibling lane as partial", async () => {
    const proposalTitle = "PARTIAL-FAILURE-PROPOSAL-SENTINEL";
    server.use(
      http.get(APPROVALS, () => new HttpResponse(null, { status: 503 })),
      http.get(PROPOSALS, () =>
        HttpResponse.json({
          proposals: [
            {
              ...STEWARD_PROPOSALS[0]!,
              proposal_id: "proposal-partial-failure-sentinel",
              artifact: {
                ...STEWARD_PROPOSALS[0]!.artifact,
                proposal_id: "proposal-partial-failure-sentinel",
                title: proposalTitle,
              },
            },
          ],
        }),
      ),
    );
    renderHome();

    expect(await screen.findByText(proposalTitle)).toBeInTheDocument();
    expect(await screen.findByText("Couldn’t load your queue.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing is waiting on you.")).not.toBeInTheDocument();
  });

  it("omits the whole section when the server grants this key neither queue", async () => {
    refuse(APPROVALS);
    refuse(PROPOSALS);
    renderHome();

    // The control: the ops section still renders, so the page did load.
    await screen.findByRole("table", { name: "Recent activity" });
    await waitFor(() =>
      expect(screen.queryByRole("table", { name: "Waiting on you" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("Waiting on you")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing is waiting on you.")).not.toBeInTheDocument();
    // Nothing from either queue leaked in behind the refusal.
    expect(screen.queryByText("payments.wire_transfer")).not.toBeInTheDocument();
  });

  it("keeps the section when ONE queue is granted, with only that queue's rows", async () => {
    refuse(PROPOSALS);
    renderHome();

    await waitFor(() =>
      expect(queueRows()).toEqual(URGENCY_ORDER.filter((line) => line.startsWith("Approval"))),
    );
  });

  it("states a failed read and re-reads on Retry", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    let attempts = 0;
    server.use(
      http.get(APPROVALS, () => {
        attempts += 1;
        return attempts === 1
          ? new HttpResponse(null, { status: 503 })
          : HttpResponse.json({ approvals: PENDING_APPROVALS });
      }),
    );
    server.use(http.get(PROPOSALS, () => new HttpResponse(null, { status: 503 })));
    renderHome();

    expect(await screen.findByText("Couldn’t load your queue.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(queueRows()).toEqual(URGENCY_ORDER.filter((line) => line.startsWith("Approval"))),
    );
  });
});

describe("Home — recent activity", () => {
  it("names human queue and activity actors through their own served references", async () => {
    const human = "c21456b9-c8dc-4458-b393-bc3230db0081";
    const bodies: Array<{ scope: string; references: string[] }> = [];
    server.use(
      http.get(APPROVALS, () =>
        HttpResponse.json({
          approvals: [{ ...PENDING_APPROVALS[0]!, request_id: "req-human", principal_id: human }],
        }),
      ),
      http.get(AUDIT, () =>
        HttpResponse.json({
          events: [{ ...AUDIT_EVENTS[0]!, event_id: "evt-human", actor: human }],
          verification: VERIFICATION_OK,
        }),
      ),
      http.post("*/api/v1/principals/resolve", async ({ request }) => {
        bodies.push((await request.json()) as { scope: string; references: string[] });
        return HttpResponse.json({
          resolved: [{ id: human, display_name: "Ada Lovelace", removed: false }],
        });
      }),
    );
    renderHome();

    await waitFor(() => expect(screen.getAllByText("Ada Lovelace")).toHaveLength(2));
    expect(bodies).toContainEqual({ scope: "approvals", references: ["req-human"] });
    expect(bodies).toContainEqual({ scope: "audit", references: ["evt-human"] });
  });

  it("lists the chain's last decisions, newest first, refusals excluded", async () => {
    server.use(
      http.get(AUDIT, () =>
        HttpResponse.json({
          events: AUDIT_EVENTS_PDP_MIX,
          verification: VERIFICATION_PDP_MIX,
        }),
      ),
    );
    renderHome();

    const table = await screen.findByRole("table", { name: "Recent activity" });
    await waitFor(() =>
      expect(
        within(table)
          .getAllByRole("row")
          .slice(1)
          .map((row) => within(row).getAllByRole("cell")[0]?.textContent),
      ).toEqual(["5", "4", "2", "1"]),
    );
    // seq 3 is `api.chat.unauthorized` — a read the server declined, which
    // decided nothing about an action. Neither its code nor its reading is here.
    expect(within(table).queryByText("api.chat.unauthorized")).not.toBeInTheDocument();
    expect(within(table).queryByText("Chat access refused")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open audit log" })).toHaveAttribute("href", "/audit");
  });

  it("renders the served actor, action and outcome of each record", async () => {
    renderHome();
    const table = await screen.findByRole("table", { name: "Recent activity" });
    // The Action cell reads in words; `approval.decide` is on the cell's title.
    await waitFor(() => expect(within(table).getByText("Approval decision")).toBeInTheDocument());
    const row = within(table).getByText("Approval decision").closest("tr")!;
    expect(within(row).getByTitle("approval.decide")).toBeInTheDocument();
    expect(within(row).getByText("user:alice@example.com")).toBeInTheDocument();
    expect(within(row).getByText("denied")).toBeInTheDocument();
  });

  // HOME IS THE FIRST SCREEN OF THE PRODUCT, and it was printing the audit
  // vocabulary raw: `api.audit.unauthorized`, `tool.toolset_denied`. The code
  // is the record and stays on the cell; the cell reads in words.
  it("prints no dotted audit code in Recent activity", async () => {
    renderHome();
    const table = await screen.findByRole("table", { name: "Recent activity" });
    await waitFor(() => expect(within(table).getAllByRole("row").length).toBeGreaterThan(1));
    for (const row of within(table).getAllByRole("row").slice(1)) {
      const action = within(row).getAllByRole("cell")[3]?.textContent ?? "";
      expect(action).not.toBe("");
      // A lowercase word, a dot, a lowercase word — the shape of a code.
      expect(action).not.toMatch(/[a-z]\.[a-z]/);
    }
  });

  it("reads an action nobody enumerated as its own words, never as a blank", async () => {
    // The vocabulary is open-ended (`read.kg.*` is generated per deployment), so
    // the fallback is the common case, not the exotic one.
    server.use(
      http.get(AUDIT, () =>
        HttpResponse.json({
          events: [{ ...AUDIT_EVENTS[0]!, event_id: "evt-novel", action: "zephyr.recalibrated" }],
          verification: VERIFICATION_OK,
        }),
      ),
    );
    renderHome();
    const table = await screen.findByRole("table", { name: "Recent activity" });
    const cell = await within(table).findByTitle("zephyr.recalibrated");
    expect(cell).toHaveTextContent("Zephyr recalibrated");
  });

  it("has no sortable header — the ledger that sorts is one click away", async () => {
    renderHome();
    const table = await screen.findByRole("table", { name: "Recent activity" });
    await waitFor(() => expect(within(table).getAllByRole("row").length).toBeGreaterThan(1));
    expect(within(table).queryAllByRole("button")).toHaveLength(0);
  });

  it("is omitted entirely for a key the server refuses the chain to", async () => {
    refuse(AUDIT);
    renderHome();

    // The control: the queue section still renders, so the page did load.
    await screen.findByRole("table", { name: "Waiting on you" });
    await waitFor(() =>
      expect(screen.queryByRole("table", { name: "Recent activity" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("Recent activity")).not.toBeInTheDocument();
    // …and so is the tile that reads the same chain.
    expect(screen.queryByRole("link", { name: /Audit log/ })).not.toBeInTheDocument();
  });

  it("says an empty chain is empty", async () => {
    server.use(
      http.get(AUDIT, () => HttpResponse.json({ events: [], verification: VERIFICATION_OK })),
    );
    renderHome();
    expect(await screen.findByText("No recent activity.")).toBeInTheDocument();
  });
});

describe("Home — what may never appear on it", () => {
  it("names no endpoint, no ticket, no source file and no HTTP verb", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\/v1\//);
    expect(text).not.toMatch(/NE-\d{2,}/);
    expect(text).not.toMatch(/\.(py|tsx?)\b/);
    expect(text).not.toMatch(/\b(GET|POST|PATCH|DELETE) \//);
    expect(text).not.toMatch(/HTTP \d{3}/);
  });

  it("carries no browser-local journal and no visit diff", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));
    const text = (document.body.textContent ?? "").toLowerCase();
    expect(text).not.toContain("since your last visit");
    expect(text).not.toContain("your recent work");
    expect(text).not.toContain("recorded in this browser");
    expect(text).not.toContain("ask again");
  });

  it("argues nothing: no banned vocabulary, no pitch, no full hash", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));
    const text = (document.body.textContent ?? "").toLowerCase();
    for (const word of ["proven", "governed", "sovereign", "verbatim", "fabricated", "trimmed"]) {
      expect(text).not.toContain(word);
    }
    expect(document.body.textContent ?? "").not.toMatch(/[0-9a-f]{32,}/);
  });

  it("renders the served rows and only the served rows", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));
    // Four approvals + three proposals is exactly the fixture, so no row on this
    // screen came from anywhere but the two reads that produced it.
    expect(queueRows()).toHaveLength(PENDING_APPROVALS.length + STEWARD_PROPOSALS.length);
    expect(AUDIT_EVENTS).toHaveLength(3);
  });
});

// The recomposition (CR-B5): the queue LEADS and the four standing facts are
// demoted to a margin column beside it. Each assertion below is paired with the
// control that fails if the layout regresses to the opening-band shape.
describe("Home — the recomposition (CR-B5)", () => {
  it("leads with the queue, follows with activity, and keeps the facts in the margin", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));

    // The two work sections sit in the main column, the queue first.
    const main = document.querySelector<HTMLElement>(".ne-home__main")!;
    expect(main).not.toBeNull();
    expect(
      within(main)
        .getAllByRole("region")
        .map((c) => c.getAttribute("aria-label")),
    ).toEqual(["Waiting on you", "Recent activity"]);

    // The facts are a sibling margin — NOT the opening band, and NOT inside main.
    const facts = document.querySelector(".ne-facts");
    expect(facts).not.toBeNull();
    expect(main.contains(facts)).toBe(false);
    // The control: the first child of the page floor is the queue's main column,
    // so a fact can never be the first thing the eye lands on again.
    const home = document.querySelector(".ne-home")!;
    expect(home.firstElementChild).toBe(main);
    expect(
      within(facts as HTMLElement).getByRole("link", { name: /Approvals/ }),
    ).toBeInTheDocument();
  });

  it("marks only the breached row with the urgent edge", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));
    const rows = within(queueTable()).getAllByRole("row").slice(1);
    // Row 0 is the overdue crm.create_note; it wears the amber inset edge.
    expect(rows[0]).toHaveClass("ne-tr--urgent");
    // The control: a row inside its deadline does NOT — the edge is the breach,
    // not the table. Without this, a modifier applied to every row would pass.
    expect(rows[1]).not.toHaveClass("ne-tr--urgent");
  });

  it("counts the rows it drew and the overdue among them, omitting the zero segment", async () => {
    renderHome();
    await waitFor(() => expect(queueRows()).toEqual(URGENCY_ORDER));
    const queue = screen.getByRole("region", { name: "Waiting on you" });
    const count = queue.querySelector(".ne-cardhead__count")!;
    // Seven served rows; one breached; none escalated-but-not-breached.
    expect(count).toHaveTextContent("7 items · 1 overdue");
    // The escalated segment is absent at zero — never "0 escalated".
    expect(count.textContent).not.toContain("escalated");
  });

  it("shows no count while the queue is still empty or loading", async () => {
    server.use(http.get(APPROVALS, () => HttpResponse.json({ approvals: [] })));
    server.use(http.get(PROPOSALS, () => HttpResponse.json({ proposals: [] })));
    renderHome();

    // A queue that resolved to zero rows shows its empty copy, not "0 items".
    expect(await screen.findByText("Nothing is waiting on you.")).toBeInTheDocument();
    const queue = screen.getByRole("region", { name: "Waiting on you" });
    expect(queue.querySelector(".ne-cardhead__count")).toBeNull();
  });

  it("draws neither section for a reader key granted no queue and no chain", async () => {
    refuse(APPROVALS);
    refuse(PROPOSALS);
    refuse(AUDIT);
    renderHome();

    // A fact this key CAN read still renders, so the page itself did load…
    await screen.findByRole("link", { name: /Controls/ });
    // …but neither work section is drawn, and no empty copy stands in for one.
    await waitFor(() =>
      expect(screen.queryByRole("table", { name: "Waiting on you" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("table", { name: "Recent activity" })).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing is waiting on you.")).not.toBeInTheDocument();
    expect(screen.queryByText("No recent activity.")).not.toBeInTheDocument();
  });
});
