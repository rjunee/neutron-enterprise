// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuthStub, TEST_ACCESS_TOKEN } from "../test/authStub";
import { server } from "../test/mocks/server";
import { AccountMenu } from "./AccountMenu";

vi.mock("../demo/DemoContext", () => ({ useDemo: () => ({ summon: vi.fn() }) }));

const SUBJECT = "c21456b9-c8dc-4458-b393-bc3230db0081";

function renderMenu(profile: Record<string, unknown>, groups: string[] = []) {
  server.use(http.get("*/api/v1/me", () => HttpResponse.json({ id: SUBJECT, groups })));
  render(
    <AuthStub value={{ user: { access_token: TEST_ACCESS_TOKEN, profile } } as never}>
      <PrincipalProvider>
        <AccountMenu />
      </PrincipalProvider>
    </AuthStub>,
  );
  return userEvent.setup();
}

async function openMenu(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(screen.getByRole("button", { name: "Account" }));
  return screen.getByRole("menu", { name: "Account" });
}

describe("the account menu names the signed-in reader", () => {
  it("leads with the already-held full-name claim and discloses exact governed ids on demand", async () => {
    const departmentGroup = "23c65bc3-a703-4fa2-9231-bdf92c2d3093";
    const menu = await openMenu(
      renderMenu({ sub: SUBJECT, preferred_username: "ops-pilot", name: "Ops Pilot" }, [
        departmentGroup,
      ]),
    );
    expect(await within(menu).findByText("Signed in as Ops Pilot")).toBeVisible();

    const disclosure = within(menu).getByText("Identity details").closest("details");
    expect(disclosure).not.toBeNull();
    expect(disclosure).not.toHaveAttribute("open");
    expect(within(menu).getByText(SUBJECT)).not.toBeVisible();
    expect(within(menu).getByText(departmentGroup)).not.toBeVisible();

    await userEvent.setup().click(within(menu).getByText("Identity details"));
    expect(disclosure).toHaveAttribute("open");
    expect(within(menu).getByText(SUBJECT)).toBeVisible();
    expect(within(menu).getByText(departmentGroup)).toBeVisible();
    expect(within(menu).getByText("Subject")).toBeVisible();
    expect(within(menu).getByText("Access groups")).toBeVisible();
  });

  it("falls back to preferred_username when the full name is absent", async () => {
    const menu = await openMenu(renderMenu({ sub: SUBJECT, preferred_username: "ada" }));
    expect(await within(menu).findByText("Signed in as ada")).toBeInTheDocument();
  });

  it("falls back to the subject id when name claims are blank", async () => {
    const menu = await openMenu(
      renderMenu({ sub: SUBJECT, name: "   ", preferred_username: "  " }),
    );
    expect(await within(menu).findByText(`Signed in as ${SUBJECT}`)).toBeInTheDocument();
  });
});
