// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §16 — SETTINGS on a deployment whose governed client CAN serve §18 B7.
//
// §16 states the degradation exactly — read-only fields with one caption — and
// names what B7 changes: the two writable fields become editable "with the §2.5
// pending pattern". So the editable path is built, and these tests drive it,
// which is also the only way §2.5's hardest rule gets exercised: WHILE A CHANGE
// IS PARKED AT THE GATE, THE OLD VALUE REMAINS THE DISPLAYED VALUE. A console
// that showed the requested value would be stating a configuration no approver
// has agreed to.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { RuntimeConfigContext } from "../AppBootstrap";
import type { ConsoleRuntimeConfig } from "../api/runtimeConfig";
import { DensityProvider } from "../primitives/density";
import { ThemeProvider } from "../rail/theme";
import { AuthStub } from "../test/authStub";
import { SettingsPage } from "./SettingsPage";
import type { DeploymentSettingsView, SettingsPatch } from "./settings";

const stub = vi.hoisted(() => ({ client: {} as Record<string, unknown> }));
vi.mock("../auth/useApiClient", () => ({ useApiClient: () => stub.client }));
vi.mock("../demo/DemoContext", () => ({ useDemo: () => ({ summon: () => {} }) }));

const CONFIG: ConsoleRuntimeConfig = {
  issuer: "https://idp.example.com",
  client_id: "console",
  audience: "neutron",
  api_base: "/api",
};

const SERVED: DeploymentSettingsView = {
  deployment_name: "Fenwick Plant 3",
  search_deadline_ms: 75000,
  pending_change: null,
};

interface Serve {
  settings?: DeploymentSettingsView;
  readFail?: unknown;
  write?: (patch: SettingsPatch) => Promise<{
    status: "applied" | "pending";
    request_id: string | null;
  }>;
  /** A deployment that serves the read but no governed patch. */
  readOnly?: boolean;
}

function serve(over: Serve = {}) {
  const deploymentSettings = vi.fn(() =>
    over.readFail !== undefined
      ? Promise.reject(over.readFail)
      : Promise.resolve(over.settings ?? SERVED),
  );
  const updateDeploymentSettings = vi.fn(
    over.write ?? (() => Promise.resolve({ status: "applied" as const, request_id: null })),
  );
  stub.client = over.readOnly
    ? { deploymentSettings }
    : { deploymentSettings, updateDeploymentSettings };
  return { deploymentSettings, updateDeploymentSettings };
}

function setup() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <ThemeProvider>
        <DensityProvider>
          <RuntimeConfigContext.Provider value={CONFIG}>
            <MemoryRouter>
              <SettingsPage />
            </MemoryRouter>
          </RuntimeConfigContext.Provider>
        </DensityProvider>
      </ThemeProvider>
    </AuthStub>,
  );
  return user;
}

function fieldValue(label: string): string {
  const dt = screen.getByText(label);
  return dt.nextElementSibling?.textContent ?? "";
}

beforeEach(() => {
  stub.client = {};
});

describe("§16 — the deployment section, once the server can serve it", () => {
  it("renders the SERVED values as editable fields and drops the read-only caption", async () => {
    serve();
    setup();
    const name = await screen.findByLabelText("Deployment name");
    expect(name).toHaveValue("Fenwick Plant 3");
    expect(screen.getByLabelText("Search time limit in seconds")).toHaveValue(75);
    // The caption would now be false, so it is gone.
    expect(
      screen.queryByText(/Managed in this deployment.s configuration\./),
    ).not.toBeInTheDocument();
    // The service address is not part of B7 and is never offered as a field.
    expect(fieldValue("Service address")).toBe("/api");
  });

  it("arms the submit only once something actually changed", async () => {
    serve();
    const user = setup();
    await screen.findByLabelText("Deployment name");
    expect(screen.getByRole("button", { name: "Request change" })).toBeDisabled();
    await user.type(screen.getByLabelText("Deployment name"), "!");
    expect(screen.getByRole("button", { name: "Request change" })).toBeEnabled();
  });

  it("submits ONLY the field that changed, then re-reads the server", async () => {
    const { deploymentSettings, updateDeploymentSettings } = serve();
    const user = setup();
    await screen.findByLabelText("Deployment name");
    await user.clear(screen.getByLabelText("Search time limit in seconds"));
    await user.type(screen.getByLabelText("Search time limit in seconds"), "90");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await waitFor(() =>
      expect(updateDeploymentSettings).toHaveBeenCalledWith({ search_deadline_ms: 90000 }),
    );
    // §2.5 step 5: the object reflects the server's new state on the NEXT READ.
    await waitFor(() => expect(deploymentSettings).toHaveBeenCalledTimes(2));
  });

  it("renders a parked change on the object, with the OLD value still displayed", async () => {
    serve({
      settings: {
        ...SERVED,
        pending_change: { request_id: "req-7", requested_by: "mira@example.com" },
      },
    });
    setup();
    expect(
      await screen.findByText(/Change requested by mira@example\.com · awaiting approval/),
    ).toBeInTheDocument();
    // The displayed name is the one in force, not the one somebody asked for.
    expect(screen.getByLabelText("Deployment name")).toHaveValue("Fenwick Plant 3");
  });

  it("renders a DENIAL where the submit happened, with the server's reason verbatim", async () => {
    serve({
      write: () =>
        Promise.reject(new ApiError("forbidden", 403, "deployment name is fixed by the operator")),
    });
    const user = setup();
    await screen.findByLabelText("Deployment name");
    await user.type(screen.getByLabelText("Deployment name"), "!");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    expect(await screen.findByText("Denied by policy.")).toBeInTheDocument();
    expect(screen.getByText("deployment name is fixed by the operator")).toBeInTheDocument();
  });

  it("a DENIAL with no served reason states the denial and nothing else", async () => {
    // The gate stated no reason (a proxy 403 carries no JSON body). The pane
    // used to print the console's own sentence under "Denied by policy.",
    // where it reads as the gate's account of the denial.
    serve({ write: () => Promise.reject(new ApiError("forbidden", 403)) });
    const user = setup();
    await screen.findByLabelText("Deployment name");
    await user.type(screen.getByLabelText("Deployment name"), "!");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    expect(await screen.findByText("Denied by policy.")).toBeInTheDocument();
    expect(screen.queryByText("Your role doesn’t include this action.")).toBeNull();
  });

  it("a save that FAILED says so, with the server's own words, and does not claim a denial", async () => {
    serve({ write: () => Promise.reject(new ApiError("boom", 500, "upstream unavailable")) });
    const user = setup();
    await screen.findByLabelText("Deployment name");
    await user.type(screen.getByLabelText("Deployment name"), "!");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    expect(await screen.findByText(/Couldn.t save\. Try again\./)).toBeInTheDocument();
    expect(screen.getByText("upstream unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Denied by policy.")).not.toBeInTheDocument();
  });

  it("a REFUSAL renders the SERVER's refusal — and the server was actually asked", async () => {
    const { deploymentSettings } = serve({
      readFail: new ApiError("forbidden", 403, "not an operator"),
    });
    setup();
    expect(
      await screen.findByText(/Your role doesn.t include deployment settings/),
    ).toBeInTheDocument();
    expect(deploymentSettings).toHaveBeenCalled();
    expect(screen.queryByLabelText("Deployment name")).not.toBeInTheDocument();
  });

  it("an UNREACHABLE read says so, and never claims a role boundary", async () => {
    serve({ readFail: new ApiError("boom", 500) });
    setup();
    expect(await screen.findByText(/Couldn.t load deployment settings\./)).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("a deployment that serves the read but no governed patch offers NO submit", async () => {
    serve({ readOnly: true });
    setup();
    // The served values still render — read-only, as values, not as disabled
    // inputs, which look like a broken form rather than a fact.
    await waitFor(() => expect(fieldValue("Deployment name")).toBe("Fenwick Plant 3"));
    expect(fieldValue("Search time limit")).toBe("75.0 s");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request change" })).not.toBeInTheDocument();
  });

  it("Your preferences is unaffected by any of it", async () => {
    serve({ readFail: new ApiError("forbidden", 403) });
    setup();
    await screen.findByText(/Your role doesn.t include deployment settings/);
    // A refusal on the deployment half must not take the local half with it:
    // the two are different kinds of setting and §16 keeps them apart.
    expect(screen.getByRole("radio", { name: "System" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reopen the guided tour" })).toBeInTheDocument();
  });
});
