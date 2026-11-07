// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8 — APPROVALS, as rendered.
//
// The measured starting point for this rebuild was: no table, no filters, no
// count, no sortable column anywhere. So the assertions below are about the
// things whose absence was the defect — a real table with a stated default sort,
// filters that narrow the ROWS (asserted by identity, because a count assertion
// passes for the wrong rows), a live count, a decision in three interactions,
// and a receipt that survives on screen.
//
// And about the things whose PRESENCE would be the defect: no bulk-select
// control, no fabricated payload, no Approve offered on a Tier-3 request the
// screen cannot describe, no console-authored refusal standing in for the
// server's — and no payload read the operator did not ask for, because every
// one of those reads is an audit record with a name on it.

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { AuthContext } from "react-oidc-context";
import type { AuthContextProps } from "react-oidc-context";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApprovalPayloadView, PendingApprovalView } from "../api/types";
import type { PrincipalState } from "../auth/PrincipalContext";
import { DirectoryProvider } from "../directory/DirectoryContext";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { PendingApprovalsProvider } from "../hooks/usePendingApprovals";
import { AuthStub, makeAuthValue } from "../test/authStub";
import { server } from "../test/mocks/server";
import { ApprovalsPage } from "./ApprovalsPage";

const MIN = 60_000;
const HOUR = 60 * MIN;
const from = (deltaMs: number) => new Date(Date.now() + deltaMs).toISOString();

const principalState = vi.hoisted(() => ({
  current: {
    status: "ok" as const,
    principal: { id: "alice@example.com", groups: ["approvers"] },
  } as PrincipalState,
}));

vi.mock("../auth/PrincipalContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../auth/PrincipalContext")>()),
  usePrincipal: () => principalState.current,
}));

beforeEach(() => {
  principalState.current = {
    status: "ok",
    principal: { id: "alice@example.com", groups: ["approvers"] },
  };
  // A receipt resolves through the exact server-owned decision lookup, never
  // by searching the shared newest-25 page. Individual success cases override
  // this explicit-miss default.
  server.use(
    http.get("*/api/v1/audit/approval-decisions/:id", () =>
      HttpResponse.json({ detail: "approval decision audit entry not found" }, { status: 404 }),
    ),
  );
});

function OwnerAuth({
  token,
  value,
  children,
}: {
  token: string;
  value?: AuthContextProps;
  children: ReactNode;
}) {
  const base = value ?? makeAuthValue();
  return (
    <AuthContext.Provider
      value={{ ...base, user: { ...base.user!, access_token: token } as typeof base.user }}
    >
      {children}
    </AuthContext.Provider>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function OwnerLocationProbe() {
  return <span data-testid="owner-url-search">{useLocation().search}</span>;
}

function ownedTree(
  token: string,
  entry = "/approvals",
  showLocation = false,
  authValue?: AuthContextProps,
) {
  return (
    <OwnerAuth token={token} value={authValue}>
      <MemoryRouter initialEntries={[entry]}>
        <DirectoryProvider>
          <PendingApprovalsProvider intervalMs={100_000}>
            <ApprovalsPage />
          </PendingApprovalsProvider>
        </DirectoryProvider>
        {showLocation ? <OwnerLocationProbe /> : null}
      </MemoryRouter>
    </OwnerAuth>
  );
}

function approval(over: Partial<PendingApprovalView> = {}): PendingApprovalView {
  return {
    request_id: "req-a",
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales",
    requested_at: from(-10 * MIN),
    sla_deadline: from(50 * MIN),
    expires_at: from(24 * HOUR),
    escalated: false,
    ...over,
  };
}

/** Three rows whose Due order (b → a → c) differs from their served order, so a
 * default sort that does nothing is visible immediately. */
function threeRows(): PendingApprovalView[] {
  return [
    approval(),
    approval({
      request_id: "req-b",
      action: "payments.wire_transfer",
      tier: 3,
      principal_id: "agent://finance",
      requested_at: from(-65 * MIN),
      sla_deadline: from(-5 * MIN),
      escalated: true,
    }),
    approval({
      request_id: "req-c",
      action: "billing.refund",
      tier: 2,
      principal_id: "agent://finance",
      requested_at: from(-3 * MIN),
      sla_deadline: from(2 * HOUR),
    }),
  ];
}

function servePending(rows: PendingApprovalView[]) {
  server.use(http.get("*/api/v1/approvals/pending", () => HttpResponse.json({ approvals: rows })));
}

function payload(over: Partial<ApprovalPayloadView> = {}): ApprovalPayloadView {
  return {
    // The gate states its disclosure decision explicitly (015A0-ENFORCEMENT);
    // the console fails closed on a body that does not.
    disclosure_status: "available",
    request_id: "req-a",
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales",
    target_resource: "ledger://acct/4021",
    description: "Move funds to a named counterparty.",
    material_parameters: { amount: "48200.00", counterparty: "Sable Ltd" },
    reversibility: "Reversible for 30 minutes by the same approver.",
    ...over,
  };
}

/** A refusal or failure from the governed disclosure read. */
interface PayloadRefusal {
  status: number;
  detail?: string;
}

/**
 * The governed per-request disclosure read, plus a record of EVERY call it
 * received. That record is the client-side shadow of the audit record each read
 * writes server-side — which is why the tests below assert on its length and
 * not merely on what is rendered.
 */
function servePayloads(bodies: Record<string, ApprovalPayloadView | PayloadRefusal>): string[] {
  const calls: string[] = [];
  server.use(
    http.get("*/api/v1/approvals/:id/payload", ({ params }) => {
      const id = params.id as string;
      calls.push(id);
      const body = bodies[id];
      if (body === undefined) {
        return HttpResponse.json({ detail: "approval request not found" }, { status: 404 });
      }
      if ("status" in body) {
        return HttpResponse.json({ detail: body.detail }, { status: body.status });
      }
      return HttpResponse.json(body);
    }),
  );
  return calls;
}

/** Let anything the screen started on its own — a read no operator asked for —
 * reach the handler before the absence of one is asserted. */
async function settle(ms = 60) {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

function setup({ withAuditPoll = false }: { withAuditPoll?: boolean } = {}) {
  const user = userEvent.setup();
  const page = (
    <PendingApprovalsProvider intervalMs={100_000}>
      <ApprovalsPage />
    </PendingApprovalsProvider>
  );
  render(
    <AuthStub>
      <MemoryRouter initialEntries={["/approvals"]}>
        <DirectoryProvider>
          {withAuditPoll ? (
            <AuditPollProvider intervalMs={100_000}>{page}</AuditPollProvider>
          ) : (
            page
          )}
        </DirectoryProvider>
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** The pending table's body rows, as their Action cell reads. */
function actions(): string[] {
  const table = screen.getByRole("table", { name: "Pending approvals" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");
}

async function openRow(user: ReturnType<typeof userEvent.setup>, action: string) {
  const table = screen.getByRole("table", { name: "Pending approvals" });
  const cell = await within(table).findByText(action);
  const row = cell.closest("tr");
  if (row === null) throw new Error(`no row for ${action}`);
  await user.click(row);
  return screen.getByRole("dialog");
}

describe("§8 — the pending table", () => {
  it("renders a REAL table, sorted Due ascending by default: the item about to breach is first", async () => {
    servePending(threeRows());
    setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    await waitFor(() => expect(actions()).toHaveLength(3));
    expect(actions()).toEqual(["payments.wire_transfer", "crm.create_note", "billing.refund"]);
  });

  it("names a human requester only through its approval record reference", async () => {
    const human = "c21456b9-c8dc-4458-b393-bc3230db0081";
    const bodies: unknown[] = [];
    servePending([approval({ request_id: "req-human", principal_id: human })]);
    server.use(
      http.post("*/api/v1/principals/resolve", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({
          resolved: [{ id: human, display_name: "Ada Lovelace", removed: false }],
        });
      }),
    );
    setup();

    expect(
      await within(screen.getByRole("table", { name: "Pending approvals" })).findByText(
        "Ada Lovelace",
      ),
    ).toBeInTheDocument();
    expect(bodies).toEqual([{ scope: "approvals", references: ["req-human"] }]);
  });

  it("states that default sort on the Due header, and marks no other header sorted", async () => {
    servePending(threeRows());
    setup();
    const due = await screen.findByRole("columnheader", { name: "Due" });
    expect(due).toHaveAttribute("aria-sort", "ascending");
    for (const name of ["Action", "Tier", "Requested by", "Requested", "Expires", "Status"]) {
      expect(screen.getByRole("columnheader", { name })).not.toHaveAttribute("aria-sort");
    }
  });

  it("every column sorts except Status — which has a header and no button", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    for (const name of ["Action", "Tier", "Requested by", "Requested", "Due", "Expires"]) {
      const header = screen.getByRole("columnheader", { name });
      expect(within(header).getByRole("button")).toBeInTheDocument();
    }
    const status = screen.getByRole("columnheader", { name: "Status" });
    expect(within(status).queryByRole("button")).toBeNull();

    await user.click(
      within(screen.getByRole("columnheader", { name: "Action" })).getByRole("button"),
    );
    expect(actions()).toEqual(["billing.refund", "crm.create_note", "payments.wire_transfer"]);
    expect(screen.getByRole("columnheader", { name: "Action" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });

  it("NO BULK SELECT, by ruling: the screen offers no checkbox and no select-all", async () => {
    servePending(threeRows());
    setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    expect(screen.queryAllByRole("checkbox")).toEqual([]);
  });

  it("derives the Status pill from the server's own timestamps and flag", async () => {
    servePending(threeRows());
    setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const table = screen.getByRole("table", { name: "Pending approvals" });
    // req-b is past its deadline AND escalated: OVERDUE wins, so the row that
    // survives "Overdue only" is never labelled something else.
    expect(within(table).getByText("OVERDUE")).toBeInTheDocument();
    expect(within(table).getAllByText("PENDING")).toHaveLength(2);
  });
});

describe("§8 — the toolbar", () => {
  it("counts the rendered rows and names the overdue tail", async () => {
    servePending(threeRows());
    setup();
    expect(await screen.findByText("3 pending · 1 overdue")).toBeInTheDocument();
  });

  it("drops the overdue clause when nothing is overdue", async () => {
    servePending([approval()]);
    setup();
    expect(await screen.findByText("1 pending")).toBeInTheDocument();
  });

  it("the tier filter narrows the RENDERED ROWS, by identity", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    await user.selectOptions(screen.getByLabelText("Tier"), "3");
    expect(actions()).toEqual(["payments.wire_transfer"]);
    expect(screen.queryByText("crm.create_note")).toBeNull();
    expect(screen.getByText("1 pending · 1 overdue")).toBeInTheDocument();
  });

  it("the requester filter narrows the RENDERED ROWS, by identity", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    await user.selectOptions(screen.getByLabelText("Requested by"), "agent://finance");
    expect(actions()).toEqual(["payments.wire_transfer", "billing.refund"]);
    expect(screen.queryByText("crm.create_note")).toBeNull();
  });

  it("Overdue only leaves exactly the breached row", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    await user.click(screen.getByRole("button", { name: "Overdue only" }));
    expect(actions()).toEqual(["payments.wire_transfer"]);
  });

  it("search-within narrows the RENDERED ROWS", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    await user.type(screen.getByLabelText("Filter approvals…"), "billing");
    expect(actions()).toEqual(["billing.refund"]);
  });

  it("an active filter posts a dismissible chip that restores the whole window", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    await user.selectOptions(screen.getByLabelText("Tier"), "3");
    await user.click(screen.getByRole("button", { name: "Clear Tier 3" }));
    expect(actions()).toHaveLength(3);
  });

  it("exports the filtered, sorted window with a preamble naming the filter", async () => {
    const blobs: Blob[] = [];
    vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
      blobs.push(blob as Blob);
      return "blob:approvals";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    await user.selectOptions(screen.getByLabelText("Tier"), "2");
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    const text = await blobs[0]?.text();
    expect(text).toContain("Approvals — Tier 2 — exported");
    const lines = (text ?? "").trim().split("\r\n");
    expect(lines[1]).toBe("Action,Tier,Requested by,Requested,Due,Expires,Status,Request id");
    // Exactly the rendered window, in the rendered (Due ascending) order.
    expect(lines).toHaveLength(4);
    expect(lines[2]).toContain("crm.create_note");
    expect(lines[3]).toContain("billing.refund");
  });
});

describe("§8 — the states", () => {
  it("empty is one line, verbatim — and NOTHING ELSE: no naked header row", async () => {
    servePending([]);
    setup();
    expect(await screen.findByText("No pending approvals.")).toBeInTheDocument();
    expect(screen.getByText("0 pending")).toBeInTheDocument();
    // The shipped defect on the flagship screen: seven column headers — six of
    // them sort buttons, Due carrying `aria-sort="ascending"` over an empty set
    // — standing above that one sentence. §8 says "one line, nothing else", so
    // the header row is not rendered and neither is any sort control.
    const table = screen.getByRole("table", { name: "Pending approvals" });
    expect(within(table).queryAllByRole("columnheader")).toEqual([]);
    expect(within(table).queryAllByRole("button")).toEqual([]);
    for (const name of [
      "Action",
      "Tier",
      "Requested by",
      "Requested",
      "Due",
      "Expires",
      "Status",
    ]) {
      expect(within(table).queryByText(name)).toBeNull();
    }
    // The sentence is the only row in the table.
    expect(within(table).getAllByRole("row")).toHaveLength(1);
  });

  it("the overdue filter's own empty state offers the way back, with a count", async () => {
    servePending([approval(), approval({ request_id: "req-c", action: "billing.refund" })]);
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    await user.click(screen.getByRole("button", { name: "Overdue only" }));
    expect(screen.getByText("Nothing is overdue.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show all 2" }));
    expect(actions()).toHaveLength(2);
  });

  it("a REFUSAL renders the server's refusal — the page mounted and asked", async () => {
    server.use(
      http.get("*/api/v1/approvals/pending", () =>
        HttpResponse.json({ detail: "not an approver" }, { status: 403 }),
      ),
    );
    setup();
    expect(await screen.findByRole("heading", { name: "Approvals" })).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Your role doesn’t include approvals. Contact your administrator for access.",
      ),
    ).toBeInTheDocument();
    // No queue is drawn for a key that was never shown one, and no toolbar
    // count — a refused read must never produce a confident "0 pending".
    expect(screen.queryByRole("table", { name: "Pending approvals" })).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("button", { name: "Export CSV" })).toBeNull();
  });

  it("a load error names the thing and offers Retry, which asks again", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/approvals/pending", () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({ detail: "upstream" }, { status: 500 })
          : HttpResponse.json({ approvals: threeRows() });
      }),
    );
    const user = setup();
    expect(await screen.findByText("Couldn’t load approvals.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(actions()).toHaveLength(3));
  });
});

describe("§8 — the decision pane", () => {
  it("opens on a row click with the action, its tier and its status", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    expect(within(pane).getByRole("heading", { name: "crm.create_note" })).toBeInTheDocument();
    expect(within(pane).getByText("TIER 2")).toBeInTheDocument();
    expect(within(pane).getByText("PENDING")).toBeInTheDocument();
  });

  it("carries the request facts, including the id with its own copy control", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    expect(within(pane).getByText("Requested by")).toBeInTheDocument();
    // The id is NOT truncated: it is the one string an approver quotes onward.
    expect(within(pane).getByText("req-a")).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Copy request id" })).toBeInTheDocument();
  });

  it("warns about unconfirmed facts only when the wire reports some, and links each ref", async () => {
    servePending([
      approval({
        provisional_source_count: 2,
        provisional_source_refs: ["llm:0123456789abcdef:doc-alpha#chunk-1", "not-parseable"],
      }),
    ]);
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    expect(
      within(pane).getByText(/2 unconfirmed extracted facts in this request’s context\./),
    ).toBeInTheDocument();
    expect(within(pane).getByRole("link", { name: "doc-alpha#chunk-1" })).toHaveAttribute(
      "href",
      "/improvement?tab=queue&proposal=0123456789abcdef",
    );
    expect(within(pane).getByText("not-parseable")).toBeInTheDocument();
  });

  it("renders NOTHING about unconfirmed facts when the field is absent", async () => {
    servePending([approval()]);
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    expect(within(pane).queryByText(/unconfirmed/i)).toBeNull();
  });
});

describe("§8 step 2 — the governed payload disclosure", () => {
  it("offers the exact inline Run shell only after governed context disclosure", async () => {
    servePending(threeRows());
    servePayloads({
      "req-a": payload({
        run_context: {
          approval_id: "req-a",
          agent_id: "agt-meridian",
          run_id: "run-exact",
        },
      }),
    });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    expect(within(pane).queryByRole("link", { name: "Open inline Run approval" })).toBeNull();

    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    expect(
      await within(pane).findByRole("link", { name: "Open inline Run approval" }),
    ).toHaveAttribute("href", "/agents/agt-meridian/runs/run-exact/approval/req-a");
  });

  it("fetches NOTHING until the operator asks — the read is audited, a glance is not", async () => {
    servePending(threeRows());
    const calls = servePayloads({ "req-a": payload() });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");

    await settle();
    // THE assertion, and it comes first: opening a row wrote no audit record,
    // because it issued no read.
    expect(calls).toEqual([]);
    // Nothing a read would have put on screen is on screen either.
    expect(within(pane).queryByText("ledger://acct/4021")).toBeNull();
    expect(within(pane).getByText("What happens if approved")).toBeInTheDocument();
    expect(
      within(pane).getByText("Request details aren’t shown until you ask. Every read is audited."),
    ).toBeInTheDocument();

    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    expect(await within(pane).findByText("ledger://acct/4021")).toBeInTheDocument();
    // Exactly one read: the one the operator asked for.
    expect(calls).toEqual(["req-a"]);
    expect(within(pane).queryByRole("button", { name: "Reveal" })).toBeNull();
  });

  it("shows §2.4's loading state while the read is in flight", async () => {
    servePending(threeRows());
    let release: (() => void) | undefined;
    server.use(
      http.get("*/api/v1/approvals/:id/payload", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(payload());
      }),
    );
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(await within(pane).findByText("Loading…")).toBeInTheDocument();
    await act(async () => {
      release?.();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(await within(pane).findByText("ledger://acct/4021")).toBeInTheDocument();
  });

  it("renders Target, one row per parameter and Reversibility — values EXACTLY as sent", async () => {
    servePending(threeRows());
    servePayloads({
      "req-a": payload({
        target_resource: "ledger://acct/4021",
        material_parameters: { amount: 48200.5, counterparty: "Sable Ltd" },
        reversibility: "Reversible for 30 minutes by the same approver.",
      }),
    });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(await within(pane).findByText("Target")).toBeInTheDocument();
    expect(within(pane).getByText("ledger://acct/4021")).toBeInTheDocument();
    expect(within(pane).getByText("amount")).toBeInTheDocument();
    expect(within(pane).getByText("counterparty")).toBeInTheDocument();
    expect(within(pane).getByText("Sable Ltd")).toBeInTheDocument();
    expect(
      within(pane).getByText("Reversible for 30 minutes by the same approver."),
    ).toBeInTheDocument();
    // No currency, no thousands separator, no rounding, no unit: the number is
    // the one the gate holds, or the approver is reading a different request.
    expect(within(pane).getByText("48200.5")).toBeInTheDocument();
    expect(within(pane).queryByText(/48,200|\$|48200\.50/)).toBeNull();
  });

  it("renders NO row for a field the response did not carry", async () => {
    servePending(threeRows());
    servePayloads({
      "req-a": payload({ description: "", material_parameters: null, reversibility: "" }),
    });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(await within(pane).findByText("Target")).toBeInTheDocument();
    expect(within(pane).queryByText("Reversibility")).toBeNull();
    expect(within(pane).queryByText("—")).toBeNull();
  });

  it("renders the SERVER's refusal, verbatim — never a refusal this console wrote", async () => {
    const detail =
      "payload withheld: not a named approver for this request (Tier-3 payload is " +
      "disclosed only to the configured named-approver list)";
    servePending(threeRows());
    servePayloads({ "req-a": { status: 403, detail } });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(await within(pane).findByText(detail)).toBeInTheDocument();
    // The console's own refusal template is NOT what a role-holder sees when the
    // server has already said why.
    expect(within(pane).queryByText(/Your role doesn’t include/)).toBeNull();
    expect(within(pane).queryByText("ledger://acct/4021")).toBeNull();
  });

  it("falls back to §2.4's refusal only when the server sent no words of its own", async () => {
    servePending(threeRows());
    servePayloads({ "req-a": { status: 403 } });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(
      await within(pane).findByText(
        "Your role doesn’t include these request details. Contact your administrator for access.",
      ),
    ).toBeInTheDocument();
  });

  it("offers a Retry on a failed read, and the retry re-runs the same read", async () => {
    servePending(threeRows());
    let attempts = 0;
    server.use(
      http.get("*/api/v1/approvals/:id/payload", () => {
        attempts += 1;
        if (attempts === 1) return HttpResponse.json({ detail: "upstream" }, { status: 500 });
        return HttpResponse.json(payload());
      }),
    );
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(await within(pane).findByText("Couldn’t load request details.")).toBeInTheDocument();
    await user.click(within(pane).getByRole("button", { name: "Retry" }));
    expect(await within(pane).findByText("ledger://acct/4021")).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it("never shows one request's payload on another — not on a switch, not on a reopen", async () => {
    servePending(threeRows());
    const calls = servePayloads({
      "req-a": payload({ target_resource: "ledger://acct/4021" }),
      "req-c": payload({
        request_id: "req-c",
        action: "billing.refund",
        target_resource: "ledger://acct/9999",
        material_parameters: { amount: "12.00" },
      }),
    });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });

    // Row A, revealed.
    let pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    expect(await within(pane).findByText("ledger://acct/4021")).toBeInTheDocument();

    // Row B, opened by clicking straight through — the pane never unmounts.
    pane = await openRow(user, "billing.refund");
    await settle();
    expect(within(pane).queryByText("ledger://acct/4021")).toBeNull();
    expect(within(pane).queryByText("48200.00")).toBeNull();
    expect(within(pane).getByRole("button", { name: "Reveal" })).toBeInTheDocument();
    expect(calls).toEqual(["req-a"]);

    // Closed, then row B again — still nothing, and still nothing fetched.
    await user.click(within(pane).getByRole("button", { name: "Close" }));
    pane = await openRow(user, "billing.refund");
    await settle();
    expect(within(pane).queryByText("ledger://acct/4021")).toBeNull();
    expect(calls).toEqual(["req-a"]);

    // CONTROL: row B's own reveal shows row B's payload, and only that.
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    expect(await within(pane).findByText("ledger://acct/9999")).toBeInTheDocument();
    expect(within(pane).getByText("12.00")).toBeInTheDocument();
    expect(within(pane).queryByText("ledger://acct/4021")).toBeNull();
    expect(calls).toEqual(["req-a", "req-c"]);
  });
});

describe("§8 — deciding", () => {
  it("APPROVES in three interactions and converts the pane into a receipt", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });

    // 1: open. 2: Approve. 3: confirm.
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    expect(
      within(pane).getByText("Approve crm.create_note? This will execute."),
    ).toBeInTheDocument();
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);

    expect(await within(pane).findAllByText("APPROVED")).not.toHaveLength(0);
    expect(pane.textContent).toContain("by alice@example.com at");
    expect(within(pane).getByRole("button", { name: "Copy receipt" })).toBeInTheDocument();
  });

  it("a Tier-3 first approval names the second approver BEFORE the confirm", async () => {
    servePending([approval({ request_id: "req-t3", tier: 3, action: "payments.wire_transfer" })]);
    servePayloads({ "req-t3": payload({ request_id: "req-t3" }) });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "payments.wire_transfer");
    // Revealed, so the withhold rule is not what is under test here.
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    await within(pane).findByText("ledger://acct/4021");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    expect(
      within(pane).getByText("Tier 3 — a second, different approver must also approve."),
    ).toBeInTheDocument();
  });

  it("WITHHOLDS Approve on a Tier-3 request nobody has revealed, and keeps Deny", async () => {
    servePending(threeRows());
    servePayloads({ "req-b": payload({ request_id: "req-b", action: "payments.wire_transfer" }) });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "payments.wire_transfer");
    expect(within(pane).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(
      within(pane).getByText("Approve is unavailable while the request details can’t be shown."),
    ).toBeInTheDocument();
    // Denying what you cannot see is a defensible verdict; approving it is not.
    expect(within(pane).getByRole("button", { name: "Deny" })).toBeEnabled();

    // …and the reveal is what lifts it — for THIS request, on the server's word.
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    await within(pane).findByText("ledger://acct/4021");
    expect(within(pane).getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(within(pane).queryByText(/Approve is unavailable/)).toBeNull();
  });

  it("keeps Approve WITHHELD when the gate refuses this approver the payload", async () => {
    servePending(threeRows());
    servePayloads({ "req-b": { status: 403, detail: "not a named approver for this request" } });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "payments.wire_transfer");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    expect(
      await within(pane).findByText("not a named approver for this request"),
    ).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(pane).getByRole("button", { name: "Deny" })).toBeEnabled();
  });

  it("CONTROL: a Tier-2 request is decidable without a reveal", async () => {
    servePending(threeRows());
    servePayloads({});
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    expect(within(pane).getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(within(pane).queryByText(/Approve is unavailable/)).toBeNull();
  });

  it("a DENY cannot land without a reason", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Deny" }));
    const confirm = within(pane).getByRole("button", { name: "Confirm deny" });
    expect(confirm).toBeDisabled();
    await user.type(within(pane).getByLabelText("Why are you denying this?"), "wrong account");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(await within(pane).findAllByText("DENIED")).not.toHaveLength(0);
  });

  it("an EXPIRED request offers neither decision and says why", async () => {
    servePending([approval({ expires_at: from(-MIN), sla_deadline: from(-2 * HOUR) })]);
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    expect(within(pane).getByText("This request expired before a decision.")).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(pane).getByRole("button", { name: "Deny" })).toBeDisabled();
  });

  it("a Tier-3 first approval renders the gate's AWAITING SECOND, and the row stays PENDING", async () => {
    servePending([
      approval({ request_id: "req-t3-wire", tier: 3, action: "payments.wire_transfer" }),
    ]);
    servePayloads({ "req-t3-wire": payload({ request_id: "req-t3-wire" }) });
    server.use(
      http.post("*/api/v1/approvals/:id/approve", () =>
        HttpResponse.json({ status: "pending_second", approver: null, decided_at: null }),
      ),
    );
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "payments.wire_transfer");
    // Revealed, so Approve is offered — the withhold rule is tested above and
    // must not be what makes this case pass.
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    await within(pane).findByText("ledger://acct/4021");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);

    expect(
      await within(pane).findByText("Approved by you. A second, different approver must decide."),
    ).toBeInTheDocument();
    // It still IS pending, so it is still in the pending table — carrying the
    // pill that says what it is waiting for.
    const table = screen.getByRole("table", { name: "Pending approvals" });
    expect(within(table).getByText("AWAITING SECOND")).toBeInTheDocument();
    expect(actions()).toEqual(["payments.wire_transfer"]);
  });

  it("a 409 says the gate already holds a verdict, in the gate's own words", async () => {
    servePending(threeRows());
    server.use(
      http.post("*/api/v1/approvals/:id/approve", () =>
        HttpResponse.json({ detail: "request already decided" }, { status: 409 }),
      ),
    );
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    expect(
      await within(pane).findByText(
        (_, el) => el?.textContent === "Already decided — request already decided.",
      ),
    ).toBeInTheDocument();
  });

  it("a 409 the gate said nothing about reads 'Already decided.' and stops there", async () => {
    // Two defects in one line. The console's §2.4 sentence for a 409 is
    // "Someone changed this first." — which, after the dash, both restates the
    // line it is attached to and reads as the GATE's account of the conflict,
    // and which ends in a full stop the line then adds a second copy of:
    // "Already decided — Someone changed this first..".
    servePending(threeRows());
    server.use(
      http.post("*/api/v1/approvals/:id/approve", () => new HttpResponse(null, { status: 409 })),
    );
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    expect(
      await within(pane).findByText((_, el) => el?.textContent === "Already decided."),
    ).toBeInTheDocument();
    expect(within(pane).queryByText(/Someone changed this first/)).toBeNull();
    expect(pane.textContent).not.toContain("..");
  });

  it("punctuates a served sentence that brought its own full stop exactly once", async () => {
    // The line owns its stop. A gate that writes in sentences must not produce
    // a double one, and its words are otherwise untouched.
    servePending(threeRows());
    server.use(
      http.post("*/api/v1/approvals/:id/approve", () =>
        HttpResponse.json({ detail: "Another approver decided this one." }, { status: 409 }),
      ),
    );
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    expect(
      await within(pane).findByText(
        (_, el) => el?.textContent === "Already decided — Another approver decided this one.",
      ),
    ).toBeInTheDocument();
    expect(pane.textContent).not.toContain("..");
  });

  it("a write failure names the failure and carries the server's detail verbatim", async () => {
    servePending(threeRows());
    server.use(
      http.post("*/api/v1/approvals/:id/approve", () =>
        HttpResponse.json({ detail: "gate unreachable" }, { status: 500 }),
      ),
    );
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    expect(
      await within(pane).findByText("Couldn’t submit the decision. Try again."),
    ).toBeInTheDocument();
    expect(within(pane).getByText("gate unreachable")).toBeInTheDocument();
  });

  it("moves the decided row into the Decided section", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    const decided = await screen.findByRole("table", { name: "Decided approvals" });
    const row = within(decided).getAllByRole("row")[1];
    expect(row?.textContent).toContain("crm.create_note");
    expect(row?.textContent).toContain("APPROVED");
  });
});

// The grammar itself — every verdict word, every refusal surface, the arrival
// gate and the stylesheet — is guarded in verdictMotion.test.tsx. What is here
// is the WIRING: that the real three-interaction decision, driven through the
// real screen, reaches the pane with the grammar attached. A contract held by a
// module and dropped by its caller is a contract nobody ships.
describe("§4.5 — an allow and a deny feel different, through the real flow", () => {
  async function decideThrough(user: ReturnType<typeof userEvent.setup>, deny: boolean) {
    const pane = await openRow(user, "crm.create_note");
    if (deny) {
      await user.click(within(pane).getByRole("button", { name: "Deny" }));
      await user.type(within(pane).getByLabelText("Why are you denying this?"), "wrong account");
      await user.click(within(pane).getByRole("button", { name: "Confirm deny" }));
    } else {
      await user.click(within(pane).getByRole("button", { name: "Approve" }));
      await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    }
    return pane;
  }

  it("an APPROVAL settles and a DENIAL arrests — and neither is the other", async () => {
    servePending(threeRows());
    let user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    let pane = await decideThrough(user, false);
    await within(pane).findAllByText("APPROVED");
    const approved = pane.querySelector(".ne-approvals__verdict");
    expect(approved?.classList.contains("ne-approvals__settle")).toBe(true);
    expect(approved?.classList.contains("ne-approvals__arrest")).toBe(false);

    cleanup();
    servePending(threeRows());
    user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    pane = await decideThrough(user, true);
    await within(pane).findAllByText("DENIED");
    const denied = pane.querySelector(".ne-approvals__verdict");
    expect(denied?.classList.contains("ne-approvals__arrest")).toBe(true);
    // The clause, on the whole surface: nothing in a refusal eases.
    expect(pane.querySelectorAll(".ne-approvals__settle")).toHaveLength(0);
  });
});

describe("§8 — the audit link on a receipt", () => {
  it("renders the server's exact 404 as a miss, never as an inferred write", async () => {
    servePending(threeRows());
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({
          events: [],
          verification: {
            valid: true,
            checked: 0,
            first_break_seq: null,
            first_break_event_id: null,
            reason: null,
            detail: "ok",
          },
        }),
      ),
    );
    const user = setup({ withAuditPoll: true });
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    expect(
      await within(pane).findByText("No decision audit entry was found for this request."),
    ).toBeInTheDocument();
    expect(within(pane).queryByText(/Writing to the audit log/i)).toBeNull();
    expect(within(pane).queryByRole("link", { name: "View audit entry" })).toBeNull();
  });

  it("links the authoritative exact-request result even when the shared page is unrelated", async () => {
    servePending(threeRows());
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", () =>
        HttpResponse.json({
          id: 1,
          seq: 4812,
          event_id: "evt-1",
          event_type: "approval",
          config_hash: "a".repeat(64),
          actor: "alice@example.com",
          occurred_at: new Date().toISOString(),
          kpi_tag: null,
          trace_id: null,
          action: "approval.granted",
          resource: "tool:crm.create_note",
          outcome: "success",
          payload: { provenance: { approval_id: "req-a" } },
          prev_hash: "b".repeat(64),
          record_hash: "c".repeat(64),
        }),
      ),
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({
          events: [],
          verification: {
            valid: true,
            checked: 0,
            first_break_seq: null,
            first_break_event_id: null,
            reason: null,
            detail: "ok",
          },
        }),
      ),
    );
    const user = setup({ withAuditPoll: true });
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    const link = await within(pane).findByRole("link", { name: "View audit entry" });
    expect(link).toHaveAttribute("href", "/audit?seq=4812");
  });

  it("a key that cannot read the chain gets NO sequence line at all — not a line about the gap", async () => {
    servePending(threeRows());
    // No AuditPollProvider ⇒ the shared poll is not "ok" for this key.
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    expect(await within(pane).findAllByText("APPROVED")).not.toHaveLength(0);
    expect(within(pane).queryByText(/decision audit entry/i)).toBeNull();
    expect(within(pane).queryByRole("link", { name: "View audit entry" })).toBeNull();
  });
});

describe("§8 — the keyboard", () => {
  it("`a` opens the approve confirm, and a second Enter confirms it", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.keyboard("a");
    expect(
      await within(pane).findByText("Approve crm.create_note? This will execute."),
    ).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(await within(pane).findAllByText("APPROVED")).not.toHaveLength(0);
  });

  it("stands down when the shell has already consumed the key — `g a` navigates, it does not arm an approval", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    // §2.6's chord handler lives on the document and consumes the completing
    // key. Stand in for it exactly: same phase, same signal.
    const shell = (event: KeyboardEvent) => {
      if (event.key === "a") event.preventDefault();
    };
    document.addEventListener("keydown", shell);
    await user.keyboard("a");
    document.removeEventListener("keydown", shell);
    expect(within(pane).queryByText("Approve crm.create_note? This will execute.")).toBeNull();
    // CONTROL: with nothing consuming it, the same key still works.
    await user.keyboard("a");
    expect(
      await within(pane).findByText("Approve crm.create_note? This will execute."),
    ).toBeInTheDocument();
  });

  it("`d` opens the deny reason and puts the cursor in it", async () => {
    servePending(threeRows());
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.keyboard("d");
    const reason = await within(pane).findByLabelText("Why are you denying this?");
    expect(reason).toHaveFocus();
    // …and the letter keys stop firing the moment focus is in it: typing "a"
    // must reach the justification, not arm an approval.
    await user.keyboard("account closed");
    expect(reason).toHaveValue("account closed");
  });
});

describe("approval owner generations", () => {
  it("preserves the initial deep link across same-client principal loading to ready", async () => {
    principalState.current = { status: "loading", principal: null };
    const authValue = makeAuthValue();
    authValue.user = {
      ...authValue.user!,
      access_token: "bob-token",
    } as typeof authValue.user;
    server.use(
      http.get("*/api/v1/approvals/pending", () =>
        HttpResponse.json({
          approvals: [approval({ request_id: "req-b", action: "bob.safe.action" })],
        }),
      ),
    );
    const view = render(ownedTree("bob-token", "/approvals?request=req-b", true, authValue));
    expect(screen.getByTestId("owner-url-search")).toHaveTextContent("request=req-b");

    principalState.current = {
      status: "ok",
      principal: { id: "bob@example.com", groups: ["approvers"] },
    };
    view.rerender(ownedTree("bob-token", "/approvals?request=req-b", true, authValue));
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("bob.safe.action")).toBeInTheDocument();
    expect(screen.getByTestId("owner-url-search")).toHaveTextContent("request=req-b");
  });

  it("clears client A's distinct request deep link before client B settles", async () => {
    const bobQueue = deferred<void>();
    server.use(
      http.get("*/api/v1/approvals/pending", async ({ request }) => {
        if (request.headers.get("authorization")?.endsWith("alice-token")) {
          return HttpResponse.json({
            approvals: [approval({ request_id: "req-a", action: "alice.private.action" })],
          });
        }
        await bobQueue.promise;
        return HttpResponse.json({
          approvals: [approval({ request_id: "req-b", action: "bob.safe.action" })],
        });
      }),
    );
    const view = render(ownedTree("alice-token", "/approvals?request=req-a", true));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByTestId("owner-url-search")).toHaveTextContent("request=req-a");

    view.rerender(ownedTree("bob-token", "/approvals?request=req-a", true));
    expect(document.body.textContent).not.toContain("req-a");
    expect(screen.getByTestId("owner-url-search")).not.toHaveTextContent("request=");

    bobQueue.resolve();
    expect(await screen.findByText("bob.safe.action")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("req-a");
  });

  it("preserves client B's initial authenticated request deep link", async () => {
    server.use(
      http.get("*/api/v1/approvals/pending", () =>
        HttpResponse.json({
          approvals: [approval({ request_id: "req-b", action: "bob.safe.action" })],
        }),
      ),
    );
    render(ownedTree("bob-token", "/approvals?request=req-b", true));
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByText("bob.safe.action")).toBeInTheDocument();
    expect(screen.getByTestId("owner-url-search")).toHaveTextContent("request=req-b");
  });

  it("hides Ada's same-id row/disclosure before Bob settles and drops Ada's late payload", async () => {
    const bobQueue = deferred<void>();
    const alicePayload = deferred<Response>();
    const calls = { queue: [] as string[], payload: [] as string[] };
    server.use(
      http.get("*/api/v1/approvals/pending", async ({ request }) => {
        const token = request.headers.get("authorization") ?? "";
        calls.queue.push(token);
        if (token.endsWith("alice-token")) {
          return HttpResponse.json({
            approvals: [approval({ request_id: "shared-r", action: "alice.private.action" })],
          });
        }
        await bobQueue.promise;
        return HttpResponse.json({
          approvals: [approval({ request_id: "shared-r", action: "bob.safe.action" })],
        });
      }),
      http.get("*/api/v1/approvals/:id/payload", async ({ request }) => {
        const token = request.headers.get("authorization") ?? "";
        calls.payload.push(token);
        return token.endsWith("alice-token")
          ? alicePayload.promise
          : HttpResponse.json(payload({ request_id: "shared-r", description: "BOB ONLY" }));
      }),
    );
    const user = userEvent.setup();
    const view = render(ownedTree("alice-token"));
    const alicePane = await openRow(user, "alice.private.action");
    await user.click(within(alicePane).getByRole("button", { name: "Reveal" }));

    principalState.current = {
      status: "ok",
      principal: { id: "bob@example.com", groups: ["approvers"] },
    };
    view.rerender(ownedTree("bob-token"));
    expect(screen.queryByText("alice.private.action")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();

    alicePayload.resolve(
      HttpResponse.json(payload({ request_id: "shared-r", description: "ALICE PAYLOAD SENTINEL" })),
    );
    await settle();
    expect(screen.queryByText("ALICE PAYLOAD SENTINEL")).toBeNull();

    bobQueue.resolve();
    const bobPane = await openRow(user, "bob.safe.action");
    expect(within(bobPane).getByText("bob.safe.action")).toBeInTheDocument();
    expect(within(bobPane).getByRole("button", { name: "Reveal" })).toBeInTheDocument();
    expect(calls).toEqual({
      queue: ["Bearer alice-token", "Bearer bob-token"],
      payload: ["Bearer alice-token"],
    });
  });

  it("resets Ada's private deny reason for Bob on the same request id", async () => {
    const bobQueue = deferred<void>();
    server.use(
      http.get("*/api/v1/approvals/pending", async ({ request }) => {
        if (request.headers.get("authorization")?.endsWith("alice-token")) {
          return HttpResponse.json({
            approvals: [approval({ request_id: "shared-r", action: "alice.private.action" })],
          });
        }
        await bobQueue.promise;
        return HttpResponse.json({
          approvals: [approval({ request_id: "shared-r", action: "bob.safe.action" })],
        });
      }),
    );
    const user = userEvent.setup();
    const view = render(ownedTree("alice-token"));
    const alicePane = await openRow(user, "alice.private.action");
    await user.click(within(alicePane).getByRole("button", { name: "Deny" }));
    await user.type(
      within(alicePane).getByLabelText("Why are you denying this?"),
      "ADA PRIVATE REASON",
    );

    principalState.current = {
      status: "ok",
      principal: { id: "bob@example.com", groups: ["approvers"] },
    };
    view.rerender(ownedTree("bob-token"));
    expect(screen.queryByLabelText("Why are you denying this?")).toBeNull();
    bobQueue.resolve();
    const bobPane = await openRow(user, "bob.safe.action");
    expect(within(bobPane).queryByLabelText("Why are you denying this?")).toBeNull();
    expect(within(bobPane).getByRole("button", { name: "Deny" })).toBeInTheDocument();
  });

  it("suppresses Ada's stale decision/refetch and makes Bob's double click one write", async () => {
    const aliceDecision = deferred<Response>();
    const bobDecision = deferred<Response>();
    const bobQueue = deferred<void>();
    const queueCalls: string[] = [];
    const approveCalls: string[] = [];
    server.use(
      http.get("*/api/v1/approvals/pending", async ({ request }) => {
        const token = request.headers.get("authorization") ?? "";
        queueCalls.push(token);
        if (token.endsWith("alice-token")) {
          return HttpResponse.json({
            approvals: [approval({ request_id: "shared-r", action: "alice.private.action" })],
          });
        }
        await bobQueue.promise;
        return HttpResponse.json({
          approvals: [approval({ request_id: "shared-r", action: "bob.safe.action" })],
        });
      }),
      http.post("*/api/v1/approvals/:id/approve", async ({ request }) => {
        const token = request.headers.get("authorization") ?? "";
        approveCalls.push(token);
        return token.endsWith("alice-token") ? aliceDecision.promise : bobDecision.promise;
      }),
    );
    const user = userEvent.setup();
    const view = render(ownedTree("alice-token"));
    const alicePane = await openRow(user, "alice.private.action");
    await user.click(within(alicePane).getByRole("button", { name: "Approve" }));
    await user.click(within(alicePane).getByRole("button", { name: "Approve" }));

    principalState.current = {
      status: "ok",
      principal: { id: "bob@example.com", groups: ["approvers"] },
    };
    view.rerender(ownedTree("bob-token"));
    aliceDecision.resolve(
      HttpResponse.json(
        {
          detail: "ALICE PRIVATE FAILURE",
        },
        { status: 500 },
      ),
    );
    await settle();
    expect(screen.queryByText(/alice@example.com/)).toBeNull();
    expect(screen.queryByText("ALICE PRIVATE FAILURE")).toBeNull();
    expect(queueCalls).toEqual(["Bearer alice-token", "Bearer bob-token"]);
    expect(approveCalls).toEqual(["Bearer alice-token"]);

    bobQueue.resolve();
    const bobPane = await openRow(user, "bob.safe.action");
    await user.click(within(bobPane).getByRole("button", { name: "Approve" }));
    const confirm = within(bobPane).getByRole("button", { name: "Approve" });
    act(() => {
      confirm.click();
      confirm.click();
    });
    await waitFor(() => expect(approveCalls).toHaveLength(2));
    await settle();
    expect(approveCalls).toEqual(["Bearer alice-token", "Bearer bob-token"]);
    bobDecision.resolve(
      HttpResponse.json({
        status: "approved",
        approver: "bob@example.com",
        decided_at: "2026-08-21T12:01:00Z",
      }),
    );
    await waitFor(() => expect(within(bobPane).getByText(/bob@example.com/)).toBeInTheDocument());
  });

  it("drops Alice's rendered receipt before Bob's same-id queue and stale refetch settle", async () => {
    const aliceRefetch = deferred<Response>();
    const bobQueue = deferred<Response>();
    let aliceReads = 0;
    server.use(
      http.get("*/api/v1/approvals/pending", ({ request }) => {
        if (request.headers.get("authorization")?.endsWith("alice-token")) {
          aliceReads += 1;
          return aliceReads === 1
            ? HttpResponse.json({
                approvals: [approval({ request_id: "shared-r", action: "alice.private.action" })],
              })
            : aliceRefetch.promise;
        }
        return bobQueue.promise;
      }),
      http.post("*/api/v1/approvals/shared-r/approve", () =>
        HttpResponse.json({
          status: "approved",
          approver: "alice-private-approver",
          decided_at: "2026-08-21T12:00:00Z",
        }),
      ),
    );
    const user = userEvent.setup();
    const view = render(ownedTree("alice-token"));
    const alicePane = await openRow(user, "alice.private.action");
    await user.click(within(alicePane).getByRole("button", { name: "Approve" }));
    await user.click(within(alicePane).getByRole("button", { name: "Approve" }));
    expect(await within(alicePane).findByText(/alice-private-approver/)).toBeInTheDocument();
    await waitFor(() => expect(aliceReads).toBe(2));

    view.rerender(ownedTree("bob-token"));
    expect(screen.queryByText("alice-private-approver")).toBeNull();
    expect(screen.queryByText("APPROVED")).toBeNull();

    aliceRefetch.resolve(
      HttpResponse.json({
        approvals: [approval({ request_id: "shared-r", action: "ALICE STALE ROW" })],
      }),
    );
    await settle();
    expect(document.body.textContent).not.toContain("ALICE");

    bobQueue.resolve(
      HttpResponse.json({
        approvals: [approval({ request_id: "shared-r", action: "bob.safe.action" })],
      }),
    );
    expect(await screen.findByText("bob.safe.action")).toBeInTheDocument();
    expect(screen.queryByText("alice-private-approver")).toBeNull();
  });
});

describe("§8 — the live refresh", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("a row decided elsewhere leaves on the refetch, and its open pane says so", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/approvals/pending", () => {
        calls += 1;
        const rows = threeRows();
        return HttpResponse.json({
          approvals: calls === 1 ? rows : rows.filter((r) => r.request_id !== "req-a"),
        });
      }),
    );
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTimeAsync });
    render(
      <AuthStub>
        <MemoryRouter initialEntries={["/approvals"]}>
          <PendingApprovalsProvider intervalMs={15_000}>
            <ApprovalsPage />
          </PendingApprovalsProvider>
        </MemoryRouter>
      </AuthStub>,
    );
    await screen.findByRole("table", { name: "Pending approvals" });
    await waitFor(() => expect(actions()).toHaveLength(3));
    const pane = await openRow(user, "crm.create_note");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    await waitFor(() => expect(actions()).toEqual(["payments.wire_transfer", "billing.refund"]));
    expect(pane.textContent).toContain("Already decided.");
    expect(within(pane).queryByRole("button", { name: "Approve" })).toBeNull();
  });
});

describe("§8 — the ?request= deep link that cannot land", () => {
  /** Surfaces the live URL search string so a test can assert the deep link is
   * actually gone from the address, not merely hidden from the screen. */
  function LocationProbe() {
    const { search } = useLocation();
    return <span data-testid="url-search">{search}</span>;
  }

  function renderAt(entry: string) {
    render(
      <AuthStub>
        <MemoryRouter initialEntries={[entry]}>
          <PendingApprovalsProvider intervalMs={100_000}>
            <ApprovalsPage />
          </PendingApprovalsProvider>
          <LocationProbe />
        </MemoryRouter>
      </AuthStub>,
    );
  }

  it("says so, loudly, when the linked request is not in the served queue", async () => {
    servePending(threeRows());
    renderAt("/approvals?request=req-ghost");
    await screen.findByRole("table", { name: "Pending approvals" });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("req-ghost");
    expect(alert.textContent).toContain("not in the pending queue");
    // The miss opens no pane — the notice, not a blank slide-in, is the answer.
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("dismiss clears the dead link from the URL and the notice from the screen", async () => {
    servePending(threeRows());
    const user = userEvent.setup();
    renderAt("/approvals?request=req-ghost");
    const alert = await screen.findByRole("alert");
    expect(screen.getByTestId("url-search").textContent).toContain("request=req-ghost");
    await user.click(within(alert).getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
    // The dead link is gone from the ADDRESS, not merely from the screen: a
    // reload or a bookmark must not resurrect the miss notice.
    expect(screen.getByTestId("url-search").textContent).not.toContain("request");
  });

  it("control: a deep link to a SERVED request opens its pane and shows no miss notice", async () => {
    servePending(threeRows());
    renderAt("/approvals?request=req-a");
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByRole("heading", { name: "crm.create_note" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("control: while the queue is still loading, the miss is not yet assertable — no notice", async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get("*/api/v1/approvals/pending", async () => {
        await gate;
        return HttpResponse.json({ approvals: threeRows() });
      }),
    );
    renderAt("/approvals?request=req-ghost");
    await settle(30);
    expect(screen.queryByRole("alert")).toBeNull();
    release?.();
    await screen.findByRole("alert");
  });
});

describe("§8 — the Decided section's chain rows (real wire vocabulary)", () => {
  /** A REAL chain record as `ne-runtime/approvals.py` emits it. */
  function chainRecord(over: Record<string, unknown> = {}) {
    return {
      id: 1,
      seq: 9001,
      event_id: "evt-9001",
      event_type: "audit",
      config_hash: "a".repeat(64),
      actor: "06a099a0-f835-49d8-b118-b05d9ede8bfa",
      occurred_at: new Date().toISOString(),
      kpi_tag: null,
      trace_id: null,
      action: "approval.granted",
      resource: "tool:write.internal.crm.note",
      outcome: "success",
      payload: {
        detail: { tier: 2, action: "write.internal.crm.note", status: "approved" },
        provenance: { approval_id: "req-elsewhere", approver: "06a099a0" },
      },
      prev_hash: "b".repeat(64),
      record_hash: "c".repeat(64),
      ...over,
    };
  }

  it("a grant decided elsewhere renders ONE row: the acted-on action with an APPROVED pill", async () => {
    servePending(threeRows());
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({
          events: [
            chainRecord(),
            chainRecord({
              id: 2,
              seq: 9002,
              event_id: "evt-9002",
              event_type: "api",
              action: "api.approval.submitted",
              resource: "req-elsewhere",
              outcome: "success",
              payload: {},
            }),
          ],
          verification: {
            valid: true,
            checked: 2,
            first_break_seq: null,
            first_break_event_id: null,
            reason: null,
            detail: "ok",
          },
        }),
      ),
    );
    setup({ withAuditPoll: true });
    const decided = await screen.findByRole("table", { name: "Decided approvals" });
    // Exactly one chain row — the verdict record; the API echo must not
    // double-list it as a second, verdict-less row.
    await waitFor(() => {
      expect(within(decided).getAllByRole("row")).toHaveLength(2); // header + 1
    });
    const row = within(decided).getAllByRole("row")[1]!;
    expect(row.textContent).toContain("write.internal.crm.note");
    expect(within(row).getByText("APPROVED")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "9001" })).toHaveAttribute(
      "href",
      "/audit?seq=9001",
    );
  });

  it("a verdict THIS SITTING holds as a receipt is claimed by request id — never listed twice", async () => {
    servePending(threeRows());
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({
          events: [
            // The API's own submission echo, NEWER than the verdict: the router
            // awaits `gate.approve()` and only then appends
            // `api.approval.submitted` (`routers/approvals.py`:165, :199), so it
            // always outranks the verdict record. `seqForRequest` resolves it,
            // which is why claiming by seq leaves the verdict at 4900 unclaimed
            // and lists this decision twice.
            chainRecord({
              id: 2,
              seq: 4901,
              event_id: "evt-4901",
              action: "api.approval.submitted",
              resource: "api:approval",
              outcome: "success",
              payload: {
                detail: JSON.stringify({
                  decision: "approve",
                  request_id: "req-a",
                  status: "approved",
                }),
                provenance: { source: "ne-api", component: "approval", surface: "console" },
              },
            }),
            chainRecord({
              seq: 4900,
              event_id: "evt-4900",
              payload: {
                detail: { tier: 2, action: "crm.create_note", status: "approved" },
                provenance: { approval_id: "req-a", approver: "06a099a0" },
              },
            }),
          ],
          verification: {
            valid: true,
            checked: 2,
            first_break_seq: null,
            first_break_event_id: null,
            reason: null,
            detail: "ok",
          },
        }),
      ),
    );
    const user = setup({ withAuditPoll: true });
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Approve" }));
    await user.click(within(pane).getAllByRole("button", { name: "Approve" })[0]!);
    const decided = await screen.findByRole("table", { name: "Decided approvals" });
    // One row: the session receipt. The chain's granted record for the SAME
    // request must be claimed by its request id, not re-listed at seq 4900.
    await waitFor(() => {
      expect(within(decided).getAllByRole("row")).toHaveLength(2); // header + 1
    });
    expect(within(decided).getAllByText("APPROVED")).toHaveLength(1);
  });

  it("a rejection decided elsewhere renders a DENIED pill read from the action name", async () => {
    servePending(threeRows());
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({
          events: [
            chainRecord({
              action: "approval.rejected",
              outcome: "denied",
              payload: {
                detail: { tier: 2, action: "write.internal.crm.note", status: "rejected" },
                provenance: { approval_id: "req-elsewhere", approver: "06a099a0" },
              },
            }),
          ],
          verification: {
            valid: true,
            checked: 1,
            first_break_seq: null,
            first_break_event_id: null,
            reason: null,
            detail: "ok",
          },
        }),
      ),
    );
    setup({ withAuditPoll: true });
    const decided = await screen.findByRole("table", { name: "Decided approvals" });
    await waitFor(() => {
      expect(within(decided).getByText("DENIED")).toBeInTheDocument();
    });
  });
});

// ===========================================================================
// 015A0-ENFORCEMENT — the fixed safe shell for a drifted source
// ===========================================================================

describe("§8 + 015A0-ENFORCEMENT — a request whose source authority changed", () => {
  /** The gate's status-discriminated answer for the one request under test. */
  function serveDisclosure(status: "content_withheld" | "retryable_unknown") {
    const calls: string[] = [];
    server.use(
      http.get("*/api/v1/approvals/:id/payload", ({ params }) => {
        calls.push(params.id as string);
        return HttpResponse.json({ request_id: params.id, disclosure_status: status });
      }),
    );
    return calls;
  }

  it("replaces a revealed payload with the safe shell when a later read is withheld", async () => {
    servePending(threeRows());
    servePayloads({ "req-a": payload({ target_resource: "ledger://acct/4021" }) });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    let pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    expect(await within(pane).findByText("ledger://acct/4021")).toBeInTheDocument();

    // The gate now answers the SAME request with the withheld shell.
    serveDisclosure("content_withheld");
    await user.click(within(pane).getByRole("button", { name: "Close" }));
    pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(await within(pane).findByText("Content withheld.")).toBeInTheDocument();
    // Not "still on screen from before": the exact served target is gone.
    expect(within(pane).queryByText("ledger://acct/4021")).toBeNull();
    expect(within(pane).queryByText("Sable Ltd")).toBeNull();
  });

  it("offers no Approve and no Deny on a withheld request, at a tier that normally has both", async () => {
    servePending(threeRows());
    serveDisclosure("content_withheld");
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    await within(pane).findByText("Content withheld.");

    expect(within(pane).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(pane).queryByRole("button", { name: "Deny" })).toBeNull();
    expect(
      within(pane).getByText("Content withheld — this request’s source access changed."),
    ).toBeInTheDocument();
  });

  it("arms neither decision key on a withheld request", async () => {
    servePending(threeRows());
    serveDisclosure("content_withheld");
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    await within(pane).findByText("Content withheld.");

    await user.keyboard("a");
    await user.keyboard("d");
    expect(within(pane).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(pane).queryByLabelText("Why are you denying this?")).toBeNull();
  });

  it("offers no copy affordance on a withheld request, though the opaque request id stays visible", async () => {
    servePending(threeRows());
    serveDisclosure("content_withheld");
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    expect(within(pane).getByRole("button", { name: "Copy request id" })).toBeInTheDocument();

    await user.click(within(pane).getByRole("button", { name: "Reveal" }));
    await within(pane).findByText("Content withheld.");
    expect(within(pane).queryByRole("button", { name: "Copy request id" })).toBeNull();
    expect(within(pane).getByText("req-a")).toBeInTheDocument();
  });

  it("says an unresolved source is unresolved, not that access was revoked, and offers a retry", async () => {
    servePending(threeRows());
    serveDisclosure("retryable_unknown");
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(
      await within(pane).findByText(
        "Request details couldn’t be resolved for your current access.",
      ),
    ).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(within(pane).queryByText("Content withheld.")).toBeNull();
    expect(within(pane).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(pane).queryByRole("button", { name: "Deny" })).toBeNull();
  });

  it("keeps Deny available when the 403 refusal is about the approver, not the source", async () => {
    servePending(threeRows());
    servePayloads({ "req-a": { status: 403, detail: "not a named approver for this request" } });
    const user = setup();
    await screen.findByRole("table", { name: "Pending approvals" });
    const pane = await openRow(user, "crm.create_note");
    await user.click(within(pane).getByRole("button", { name: "Reveal" }));

    expect(
      await within(pane).findByText("not a named approver for this request"),
    ).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Deny" })).toBeEnabled();
  });
});
