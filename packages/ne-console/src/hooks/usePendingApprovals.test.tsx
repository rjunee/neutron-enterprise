// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE SHARED PENDING-APPROVALS POLL — proved by counting REQUESTS, not renders.
//
// The defect these tests exist for: the rail badge, the Approvals toolbar count
// and home's attention lane each read `/v1/approvals/pending` on a timer of
// their own. Two of those timers ran at 15 s with no shared phase, so the badge
// and the toolbar were two answers to one question — and /approvals, the
// highest-frequency screen, paid double the request rate to disagree with
// itself.
//
// The invariant is therefore a request-count and a request-CADENCE one:
//   * however many consumers mount, and whichever view they read, the store
//     issues exactly one request;
//   * consecutive requests are at least one interval apart — including across an
//     off-cycle refresh, which must REPLACE the pending tick, not add one.
// Both are counted at the MSW boundary (the real request), so a second reader
// anywhere in the tree fails them.
//
// Real timers + a small intervalMs + waitFor, per the audit-poll precedent
// (MSW and fake timers interact badly).

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PendingApprovalView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import {
  PendingApprovalsProvider,
  usePendingApprovals,
  usePendingApprovalsQueue,
  usePendingApprovalsRead,
} from "./usePendingApprovals";

/** A served row, its SLA deadline stated relative to the REAL clock — these
 * tests run on real timers, so the shared fixtures' pinned epoch (long past)
 * would make every row read as breached and say nothing about the arithmetic. */
function pending(requestId: string, dueInMs: number): PendingApprovalView {
  const now = Date.now();
  return {
    request_id: requestId,
    action: "crm.create_note",
    tier: 2,
    principal_id: "agent://sales-drafts",
    requested_at: new Date(now - 10 * 60_000).toISOString(),
    sla_deadline: new Date(now + dueInMs).toISOString(),
    expires_at: new Date(now + 24 * 3_600_000).toISOString(),
    escalated: dueInMs < 0,
  };
}

/** Four pending rows, exactly one of them already past its deadline. */
const QUEUE: PendingApprovalView[] = [
  pending("req-fresh", 30 * 60_000),
  pending("req-overdue", -5 * 60_000),
  pending("req-wire", 2 * 3_600_000),
  pending("req-notes", 45 * 60_000),
];

/** Arrival time of every real request the tree made for the queue. */
let hits: number[] = [];

const principalState = vi.hoisted(() => ({
  status: "ok" as const,
  principal: { id: "alice@example.com", groups: ["approvers"] },
}));

vi.mock("../auth/PrincipalContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../auth/PrincipalContext")>()),
  usePrincipal: () => principalState,
}));

function countRequests(respond: () => Response) {
  server.use(
    http.get("*/api/v1/approvals/pending", () => {
      hits.push(performance.now());
      return respond();
    }),
  );
}

const served = () => HttpResponse.json({ approvals: QUEUE });

beforeEach(() => {
  hits = [];
});

/** The rail badge's view (§6 — `{count} pending, {overdue} overdue`). */
function BadgeProbe() {
  const { status, count, breached } = usePendingApprovals();
  return <span data-testid="badge">{`${status}/${count ?? "none"}/${breached ?? "none"}`}</span>;
}

/** The Approvals toolbar's view of the same store. */
function ToolbarProbe() {
  const { status, rows, refetch } = usePendingApprovalsQueue();
  return (
    <span>
      <span data-testid="toolbar">{`${status}/${rows.length}`}</span>
      <button type="button" onClick={() => void refetch()}>
        refetch
      </button>
    </span>
  );
}

/** Home's governed-read-shaped view of the same store. */
function HomeProbe() {
  const { status, data, reload } = usePendingApprovalsRead();
  return (
    <span>
      <span data-testid="home">{`${status}/${data === null ? "none" : data.approvals.length}`}</span>
      <button type="button" onClick={reload}>
        reload
      </button>
    </span>
  );
}

/** Nine consumers across all three views, plus five more that mount on demand —
 * a heavier tree than the shell ever renders. */
function Consumers() {
  const [extra, setExtra] = useState(false);
  return (
    <div>
      <BadgeProbe />
      <BadgeProbe />
      <BadgeProbe />
      <ToolbarProbe />
      <ToolbarProbe />
      <ToolbarProbe />
      <HomeProbe />
      <HomeProbe />
      <HomeProbe />
      <button type="button" onClick={() => setExtra(true)}>
        mount more
      </button>
      {extra ? (
        <>
          <BadgeProbe />
          <ToolbarProbe />
          <HomeProbe />
          <BadgeProbe />
          <ToolbarProbe />
        </>
      ) : null}
    </div>
  );
}

function renderStore(intervalMs: number) {
  return render(
    <AuthStub>
      <PendingApprovalsProvider intervalMs={intervalMs}>
        <Consumers />
      </PendingApprovalsProvider>
    </AuthStub>,
  );
}

/** Wall-clock wait; the poll under test runs on real timers. */
async function settle(ms: number) {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

describe("the shared pending-approvals store", () => {
  it("issues ONE request no matter how many consumers mount, and mounting more issues none", async () => {
    countRequests(served);
    // A long interval, so nothing here can be a second tick of the same loop:
    // every extra request would be a second READER.
    renderStore(60_000);

    await waitFor(() => expect(screen.getAllByTestId("badge")[0]).toHaveTextContent("ok/4/1"));
    expect(hits).toHaveLength(1);

    // Nine consumers, three different views of the store, one served payload.
    for (const el of screen.getAllByTestId("badge")) expect(el).toHaveTextContent("ok/4/1");
    for (const el of screen.getAllByTestId("toolbar")) expect(el).toHaveTextContent("ok/4");
    for (const el of screen.getAllByTestId("home")) expect(el).toHaveTextContent("ok/4");

    // Five MORE consumers arrive mid-session (a route change into /approvals is
    // exactly this). A consumer is a reader of the store, never a fetch.
    fireEvent.click(screen.getByText("mount more"));
    await settle(120);
    expect(hits).toHaveLength(1);
    expect(screen.getAllByTestId("toolbar")).toHaveLength(5);
    for (const el of screen.getAllByTestId("toolbar")) expect(el).toHaveTextContent("ok/4");
  });

  it("keeps every consecutive request at least one interval apart", async () => {
    countRequests(served);
    const intervalMs = 120;
    renderStore(intervalMs);

    // Four ticks of the ONE loop. With a reader per screen this window holds
    // three times as many requests, in near-simultaneous bursts.
    await waitFor(() => expect(hits.length).toBeGreaterThanOrEqual(4), { timeout: 4000 });
    for (let i = 1; i < hits.length; i += 1) {
      // setTimeout never fires early; 2 ms of slack for timer coarseness.
      expect(hits[i]! - hits[i - 1]!).toBeGreaterThanOrEqual(intervalMs - 2);
    }
  });

  it("serves an off-cycle refetch from the same loop — replacing the pending tick, not adding one", async () => {
    countRequests(served);
    const intervalMs = 400;
    renderStore(intervalMs);

    await waitFor(() => expect(hits).toHaveLength(1));

    // A decision refetch, part-way through the interval. The tick already
    // scheduled by the first poll must be cancelled by it: if it survives, the
    // next request lands a partial interval after this one and the queue has
    // quietly gained a second cadence.
    await settle(intervalMs / 4);
    fireEvent.click(screen.getAllByText("refetch")[0]!);
    await waitFor(() => expect(hits).toHaveLength(2));

    await waitFor(() => expect(hits).toHaveLength(3), { timeout: 4000 });
    expect(hits[2]! - hits[1]!).toBeGreaterThanOrEqual(intervalMs - 2);

    // Home's Retry is the same call on the same loop.
    fireEvent.click(screen.getAllByText("reload")[0]!);
    await waitFor(() => expect(hits).toHaveLength(4));
  });

  it("moves the badge count and the toolbar count together, off the one payload", async () => {
    countRequests(served);
    renderStore(150);
    await waitFor(() => expect(screen.getAllByTestId("badge")[0]).toHaveTextContent("ok/4/1"));

    // The server's queue changes: one row decided elsewhere.
    countRequests(() => HttpResponse.json({ approvals: QUEUE.slice(0, 2) }));

    await waitFor(() => expect(screen.getAllByTestId("badge")[0]).toHaveTextContent("ok/2/1"));
    // Same render pass, same store: no window in which the two disagree.
    for (const el of screen.getAllByTestId("toolbar")) expect(el).toHaveTextContent("ok/2");
    for (const el of screen.getAllByTestId("home")) expect(el).toHaveTextContent("ok/2");
  });

  it("reports a 403 as refused on every view — no count, no rows, never a zero", async () => {
    countRequests(() => HttpResponse.json({ detail: "requires approver role" }, { status: 403 }));
    renderStore(60_000);

    await waitFor(() =>
      expect(screen.getAllByTestId("badge")[0]).toHaveTextContent("refused/none/none"),
    );
    expect(hits).toHaveLength(1);
    for (const el of screen.getAllByTestId("toolbar")) expect(el).toHaveTextContent("refused/0");
    // Home's lane is null-data, not an empty queue: nothing was shown to this key.
    for (const el of screen.getAllByTestId("home")) expect(el).toHaveTextContent("refused/none");
  });

  it("reports an unreachable queue as error, still on one request", async () => {
    countRequests(() => HttpResponse.json({}, { status: 500 }));
    renderStore(60_000);

    await waitFor(() =>
      expect(screen.getAllByTestId("badge")[0]).toHaveTextContent("error/none/none"),
    );
    expect(hits).toHaveLength(1);
    for (const el of screen.getAllByTestId("home")) expect(el).toHaveTextContent("error/none");
  });
});
