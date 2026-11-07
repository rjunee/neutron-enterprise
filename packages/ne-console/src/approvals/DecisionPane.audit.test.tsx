// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, delay, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { ApprovalActionResult, PendingApprovalView } from "../api/types";
import { AuthStub } from "../test/authStub";
import { server } from "../test/mocks/server";
import { DecisionPane } from "./DecisionPane";

const approval: PendingApprovalView = {
  request_id: "req-1",
  action: "crm.create_note",
  tier: 2,
  principal_id: "agent://sales",
  requested_at: "2026-08-18T11:00:00Z",
  sla_deadline: "2026-08-18T13:00:00Z",
  expires_at: "2026-08-19T12:00:00Z",
  escalated: false,
};

const result: ApprovalActionResult = {
  status: "approved",
  approver: "user:approver",
  decided_at: "2026-08-18T12:00:00Z",
};

const principalState = vi.hoisted(() => ({
  status: "ok" as const,
  principal: { id: "alice@example.com", groups: ["approvers"] },
}));

vi.mock("../auth/PrincipalContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../auth/PrincipalContext")>()),
  usePrincipal: () => principalState,
}));

function renderReceipt(auditSeq = 9999) {
  return render(
    <AuthStub>
      <MemoryRouter>
        <DecisionPane
          approval={approval}
          now={Date.parse("2026-08-18T12:01:00Z")}
          state={{ result }}
          decidedElsewhere={false}
          auditSeq={auditSeq}
          chainReadable
          onClose={() => {}}
          onApprove={() => {}}
          onDeny={() => {}}
        />
      </MemoryRouter>
    </AuthStub>,
  );
}

describe("approval receipt audit truth", () => {
  it("ignores the legacy newest-page hint and links the exact server result", async () => {
    let requestPath = "";
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", ({ request }) => {
        requestPath = new URL(request.url).pathname;
        return HttpResponse.json({ seq: 4812 });
      }),
    );
    renderReceipt();
    const link = await screen.findByRole("link", { name: "View audit entry" });
    expect(requestPath).toBe("/api/v1/audit/approval-decisions/req-1");
    expect(link).toHaveAttribute("href", "/audit?seq=4812");
    expect(link).not.toHaveAttribute("href", "/audit?seq=9999");
  });

  it("renders an authoritative miss without claiming a write is happening", async () => {
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", () =>
        HttpResponse.json({ detail: "not found" }, { status: 404 }),
      ),
    );
    renderReceipt();
    expect(
      await screen.findByText("No decision audit entry was found for this request."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Writing to the audit log/i)).toBeNull();
    expect(screen.queryByRole("link", { name: "View audit entry" })).toBeNull();
  });

  it("keeps checking and lookup failure distinct, and neither says writing", async () => {
    server.use(
      http.get("*/api/v1/audit/approval-decisions/:id", async () => {
        await delay(30);
        return HttpResponse.json({}, { status: 500 });
      }),
    );
    renderReceipt();
    expect(await screen.findByText("Checking the decision audit entry…")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("Couldn’t check the decision audit entry.")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Writing to the audit log/i)).toBeNull();
  });
});

describe("approval decision ownership", () => {
  it("resets deny mode and private reason when the opaque request id changes", async () => {
    const user = userEvent.setup();
    const props = {
      now: Date.parse("2026-08-18T12:01:00Z"),
      state: {},
      decidedElsewhere: false,
      auditSeq: null,
      chainReadable: true,
      onClose: () => {},
      onApprove: () => {},
      onDeny: () => {},
    };
    const view = render(
      <AuthStub>
        <MemoryRouter>
          <DecisionPane approval={approval} {...props} />
        </MemoryRouter>
      </AuthStub>,
    );
    await user.click(screen.getByRole("button", { name: "Deny" }));
    await user.type(screen.getByLabelText("Why are you denying this?"), "PRIVATE REASON");

    view.rerender(
      <AuthStub>
        <MemoryRouter>
          <DecisionPane approval={{ ...approval, request_id: "req-2" }} {...props} />
        </MemoryRouter>
      </AuthStub>,
    );
    expect(screen.queryByLabelText("Why are you denying this?")).toBeNull();
    expect(screen.queryByDisplayValue("PRIVATE REASON")).toBeNull();
    expect(screen.getByRole("button", { name: "Deny" })).toBeInTheDocument();
  });
});

// APPROVE AND DENY MUST NEVER LEAVE THE VIEWPORT. Two clicks were lost to this
// in rehearsal: after Reveal the payload block grows to the target, one row per
// material parameter and the reversibility note, and on a laptop viewport the
// decision controls went below the fold of the pane's scrolling body. A control
// that is off-screen is not disabled and says nothing — the click lands on the
// payload rows and NOTHING HAPPENS.
//
// The fix is a sticky block, and a sticky block has exactly two preconditions.
// Both are pinned here because both are silently breakable by an ordinary edit:
// append one more section to `PendingBody` and the DOM half goes; drop the
// `position: sticky` and the CSS half goes. Neither would fail any other test.
describe("the decision controls stay in the viewport however long the payload is", () => {
  it("closes the pane body, so sticky has a scroll port to stick to", () => {
    render(
      <AuthStub>
        <MemoryRouter>
          <DecisionPane
            approval={approval}
            now={Date.parse("2026-08-18T12:01:00Z")}
            state={{}}
            decidedElsewhere={false}
            auditSeq={null}
            chainReadable
            onClose={() => {}}
            onApprove={() => {}}
            onDeny={() => {}}
          />
        </MemoryRouter>
      </AuthStub>,
    );

    const body = document.querySelector(".ne-detail__body");
    const actions = document.querySelector(".ne-approvals__actions");
    expect(actions).not.toBeNull();
    expect(screen.getByRole("button", { name: "Deny" }).closest(".ne-approvals__actions")).toBe(
      actions,
    );
    // LAST, not merely present: a section rendered after it would scroll the
    // decision away again, which is the whole defect returning.
    expect(body?.lastElementChild).toBe(actions);
  });

  it("sticks that block to the foot of the scroll port, full-bleed and opaque", () => {
    const css = readFileSync(resolve(__dirname, "./approvals.css"), "utf8");
    const block = css.split(".ne-approvals__actions {")[1]?.split("}")[0] ?? "";
    expect(block).toContain("position: sticky");
    expect(block).toContain("bottom: 0");
    // Opaque, or the payload scrolls THROUGH the decision controls.
    expect(block).toContain("background: var(--panel)");
    expect(block).toContain("border-top:");
    // The negative inline/bottom margins cancel `.ne-detail__body`'s padding so
    // the block spans the pane rather than floating inside it.
    expect(block).toContain("calc(-1 * var(--space-16))");
  });
});
