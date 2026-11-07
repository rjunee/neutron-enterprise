// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §15 — ACCESS on the deployment this console actually ships against, whose
// governed client cannot serve §18 B6.
//
// THE MEASURED STARTING POINT was a title row over an empty body. That is not
// restraint, it is unintelligible: the reader cannot tell a screen whose data
// has not shipped from a screen that rendered and broke, and the two call for
// completely different actions.
//
// So these assertions are about the sentence that stands in its place — what
// appears here and what it waits on — and about the four things whose PRESENCE
// would be the defect: a table with a header row and no body (which states this
// deployment has no principals), a count of a window nobody fetched, a write
// control with no endpoint behind it, and a role-boundary claim over a read
// that was never refused.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { AuthStub } from "../test/authStub";
import { AccessPage } from "./AccessPage";

const PEOPLE_FALLBACK =
  "People, their groups and what each opens appear here once this deployment serves them.";
const ROLES_FALLBACK =
  "Roles, what each opens and the Tier-3 approvers appear here once this deployment serves them.";

function setup(path = "/access") {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[path]}>
        <AccessPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

describe("§15 — the screen states what it waits on", () => {
  it("renders the page, and something under the heading", async () => {
    setup();
    expect(screen.getByRole("heading", { level: 1, name: "Access" })).toBeInTheDocument();
    // The whole complaint, as one assertion: the panel is not empty.
    const panel = await screen.findByRole("tabpanel");
    expect(within(panel).getByText(PEOPLE_FALLBACK)).toBeInTheDocument();
  });

  it("names what appears on People and what it waits on", async () => {
    setup();
    expect(await screen.findByText(PEOPLE_FALLBACK)).toBeInTheDocument();
  });

  it("names what appears on Roles and what it waits on", async () => {
    const user = setup();
    await user.click(screen.getByRole("tab", { name: "Roles" }));
    expect(await screen.findByText(ROLES_FALLBACK)).toBeInTheDocument();
  });

  it("renders NO table — a header row over no body would count a zero", async () => {
    const user = setup();
    await screen.findByText(PEOPLE_FALLBACK);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText(ROLES_FALLBACK);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders NO count and NO filter over a window nobody fetched", async () => {
    setup();
    await screen.findByText(PEOPLE_FALLBACK);
    expect(screen.queryByText(/\d+ (people|person)/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Surface")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Filter people…")).not.toBeInTheDocument();
  });

  it("offers NO write the server has no endpoint for", async () => {
    const user = setup();
    await screen.findByText(PEOPLE_FALLBACK);
    expect(screen.queryByRole("button", { name: "Change roles…" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText(ROLES_FALLBACK);
    expect(
      screen.queryByRole("button", { name: "Edit Tier-3 approvers…" }),
    ).not.toBeInTheDocument();
  });

  it("does NOT claim a role boundary — nothing here was refused", async () => {
    // The bug this guards is a false statement about authorization: a screen
    // whose data has not shipped saying the reader's role is the reason.
    const user = setup();
    await screen.findByText(PEOPLE_FALLBACK);
    expect(screen.queryByText(/Your role doesn/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText(ROLES_FALLBACK);
    expect(screen.queryByText(/Your role doesn/)).not.toBeInTheDocument();
  });

  it("the two tabs are real tabs, and the deep link opens the one it names", async () => {
    setup("/access?tab=roles");
    expect(await screen.findByText(ROLES_FALLBACK)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Roles" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "People" })).toHaveAttribute("aria-selected", "false");
  });
});
