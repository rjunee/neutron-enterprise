// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { ApprovalPayloadDisclosureView, PendingApprovalView } from "../api/types";
import type { DecisionPaneProps } from "../approvals/DecisionPane";
import type { AgentsClient } from "./types";
import { ApprovalOnlyRunPane, RunApprovalPane } from "./RunApprovalPane";

vi.mock("../approvals/DecisionPane", () => ({
  DecisionPane: ({ approval, state, onApprove, onDeny }: DecisionPaneProps) => (
    <div>
      <span>{approval.action}</span>
      <button type="button" onClick={onApprove}>
        Approve exact
      </button>
      <button type="button" onClick={() => onDeny("bounded reason")}>
        Deny exact
      </button>
      {state.error?.conflict ? <span>Already decided</span> : null}
    </div>
  ),
}));

const exactApproval: PendingApprovalView = {
  request_id: "approval-exact",
  action: "draft.email.create",
  tier: 2,
  principal_id: "user:owner",
  requested_at: "2026-08-23T00:00:00Z",
  sla_deadline: "2026-08-23T01:00:00Z",
  expires_at: "2026-08-23T02:00:00Z",
  escalated: false,
};

const unrelatedApproval: PendingApprovalView = {
  ...exactApproval,
  request_id: "approval-latest-but-wrong",
  action: "wrong.latest.action",
  requested_at: "2026-08-23T00:30:00Z",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((pass, fail) => {
    resolve = pass;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function approvalClient(overrides: Partial<AgentsClient> = {}): AgentsClient {
  return {
    listPendingApprovals: vi.fn(async () => ({
      approvals: [exactApproval, unrelatedApproval],
    })),
    approve: vi.fn(async () => ({ status: "approved", approver: "user:owner" })),
    reject: vi.fn(async () => ({ status: "rejected", approver: "user:owner" })),
    ...overrides,
  } as AgentsClient;
}

const exactPayload: ApprovalPayloadDisclosureView = {
  disclosure_status: "available",
  request_id: "approval-exact",
  action: "draft.email.create",
  tier: 2,
  principal_id: "user:owner",
  target_resource: "protected Run action",
  description: "Protected source-derived Run action",
  material_parameters: { subject: "protected" },
  reversibility: "Draft can be discarded.",
  run_context: {
    approval_id: "approval-exact",
    agent_id: "meridian",
    run_id: "run-1",
  },
};

function approvalRoute(
  client: AgentsClient,
  identityKey = "approver-a",
  factors: { approvalId?: string; agentId?: string; runId?: string } = {},
) {
  return (
    <MemoryRouter>
      <ApprovalOnlyRunPane
        client={client}
        approvalId={factors.approvalId ?? "approval-exact"}
        identityKey={identityKey}
        agentId={factors.agentId ?? "meridian"}
        runId={factors.runId ?? "run-1"}
      />
    </MemoryRouter>
  );
}

function pane(client: AgentsClient, onRefetchRun = vi.fn(), identityKey = "owner-a") {
  return (
    <MemoryRouter>
      <RunApprovalPane
        client={client}
        approvalId="approval-exact"
        identityKey={identityKey}
        agentId="meridian"
        runId="run-1"
        onRefetchRun={onRefetchRun}
      />
    </MemoryRouter>
  );
}

describe("RunApprovalPane", () => {
  it("selects only the exact pending approval and never the newest row", async () => {
    render(pane(approvalClient()));

    expect(await screen.findByText("draft.email.create")).toBeInTheDocument();
    expect(screen.queryByText("wrong.latest.action")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open approval queue" })).toHaveAttribute(
      "href",
      "/approvals",
    );
  });

  it("clears the prior approval immediately when the identity changes", async () => {
    const next = deferred<{ approvals: PendingApprovalView[] }>();
    const client = approvalClient();
    vi.mocked(client.listPendingApprovals!).mockResolvedValueOnce({ approvals: [exactApproval] });
    vi.mocked(client.listPendingApprovals!).mockReturnValueOnce(next.promise);
    const view = render(pane(client));
    expect(await screen.findByText("draft.email.create")).toBeInTheDocument();

    view.rerender(pane(client, vi.fn(), "owner-b"));
    expect(screen.queryByText("draft.email.create")).not.toBeInTheDocument();
    expect(screen.getByText("Loading exact approval…")).toBeInTheDocument();
    next.resolve({ approvals: [] });
    expect(await screen.findByText("Inline approval unavailable.")).toBeInTheDocument();
  });

  it("submits one decision for the exact id and refetches the same Run", async () => {
    const submitted = deferred<{ status: "approved"; approver: string }>();
    const client = approvalClient({
      approve: vi.fn(() => submitted.promise),
    });
    const refetch = vi.fn();
    const user = userEvent.setup();
    render(pane(client, refetch));
    const button = await screen.findByRole("button", { name: "Approve exact" });

    await user.click(button);
    await user.click(button);
    expect(client.approve).toHaveBeenCalledTimes(1);
    expect(client.approve).toHaveBeenCalledWith("approval-exact");
    submitted.resolve({ status: "approved", approver: "user:owner" });
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
  });

  it("refetches after an exact-id decision conflict", async () => {
    const client = approvalClient({
      approve: vi.fn(async () => {
        throw new ApiError("conflict", 409, "already decided");
      }),
    });
    const refetch = vi.fn();
    const user = userEvent.setup();
    render(pane(client, refetch));
    await user.click(await screen.findByRole("button", { name: "Approve exact" }));

    expect(await screen.findByText("Already decided")).toBeInTheDocument();
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("fails closed when the queue repeats the same approval id", async () => {
    const client = approvalClient({
      listPendingApprovals: vi.fn(async () => ({
        approvals: [exactApproval, { ...exactApproval }],
      })),
    });
    render(pane(client));

    expect(await screen.findByText("Inline approval unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("draft.email.create")).not.toBeInTheDocument();
  });
});

describe("ApprovalOnlyRunPane", () => {
  function routeClient(overrides: Partial<AgentsClient> = {}) {
    return approvalClient({
      readApprovalPayload: vi.fn(async () => exactPayload),
      getAgentRun: vi.fn(async () => {
        throw new Error("owner Run must never be read");
      }),
      listAgentRunEvents: vi.fn(async () => {
        throw new Error("Run events must never be read");
      }),
      getAgentRunEvidence: vi.fn(async () => {
        throw new Error("Run evidence must never be read");
      }),
      ...overrides,
    });
  }

  it("renders only after the governed tuple matches all route factors", async () => {
    const client = routeClient();
    const user = userEvent.setup();
    render(approvalRoute(client));

    await user.click(await screen.findByRole("button", { name: "Approve exact" }));
    expect(client.listPendingApprovals).toHaveBeenCalledTimes(1);
    expect(client.readApprovalPayload).toHaveBeenCalledWith("approval-exact");
    expect(client.approve).toHaveBeenCalledWith("approval-exact");
    expect(client.getAgentRun).not.toHaveBeenCalled();
    expect(client.listAgentRunEvents).not.toHaveBeenCalled();
    expect(client.getAgentRunEvidence).not.toHaveBeenCalled();
  });

  it.each([
    ["approval", { approvalId: "approval-other" }],
    ["agent", { agentId: "agent-other" }],
    ["run", { runId: "run-other" }],
  ])("refuses a one-factor %s substitution", async (_name, factors) => {
    render(approvalRoute(routeClient(), "approver-a", factors));
    expect(await screen.findByText("This approval is unavailable.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve exact" })).not.toBeInTheDocument();
  });

  it("refuses withheld or legacy disclosure without an exact context", async () => {
    const client = routeClient({
      readApprovalPayload: vi.fn(async (): Promise<ApprovalPayloadDisclosureView> => ({
        request_id: "approval-exact",
        disclosure_status: "content_withheld",
      })),
    });
    render(approvalRoute(client));

    expect(await screen.findByText("This approval is unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("draft.email.create")).not.toBeInTheDocument();
  });

  it("drops a late prior-principal response on generation change", async () => {
    const pending = deferred<{ approvals: PendingApprovalView[] }>();
    const payload = deferred<ApprovalPayloadDisclosureView>();
    const oldClient = routeClient({
      listPendingApprovals: vi.fn(() => pending.promise),
      readApprovalPayload: vi.fn(() => payload.promise),
    });
    const newClient = routeClient({
      listPendingApprovals: vi.fn(async () => ({ approvals: [] })),
    });
    const view = render(approvalRoute(oldClient));
    view.rerender(approvalRoute(newClient, "approver-b"));
    expect(await screen.findByText("This approval is unavailable.")).toBeInTheDocument();

    await act(async () => {
      pending.resolve({ approvals: [exactApproval] });
      payload.resolve(exactPayload);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("This approval is unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("draft.email.create")).not.toBeInTheDocument();
  });
});
