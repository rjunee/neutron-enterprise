// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §16 — SETTINGS on the deployment this console ships against, whose governed
// client cannot serve §18 B7.
//
// THE MEASURED STARTING POINT was a title row over an empty body. Unlike
// Access, most of §16 needs no new endpoint at all: Your preferences is local
// and instant, and the Deployment values are already served by the
// configuration this deployment hands the console at boot. So the screen is
// whole today, and the ONE degraded part — the deployment fields are read-only
// — says so in §16's own caption rather than by rendering disabled controls
// that look broken.
//
// The traps these hold to account: a preference control that writes its own
// state instead of the one the account menu writes (the two would then
// disagree), and an unset configured value rendered as something other than
// absent — the host standing in for a name nobody set, or a zero standing in
// for a limit.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimeConfigContext } from "../AppBootstrap";
import type { ConsoleRuntimeConfig } from "../api/runtimeConfig";
import { DensityProvider, readDensity } from "../primitives/density";
import { ThemeProvider, readTheme } from "../rail/theme";
import { AuthStub } from "../test/authStub";
import { SettingsPage } from "./SettingsPage";

/** The guided tour is summoned through the SAME control the account menu uses;
 * the spy proves the button is wired to it rather than to a local no-op. */
const tour = vi.hoisted(() => ({ summon: vi.fn() }));
vi.mock("../demo/DemoContext", () => ({
  useDemo: () => ({ summon: tour.summon }),
}));

const CONFIG: ConsoleRuntimeConfig = {
  issuer: "https://idp.example.com",
  client_id: "console",
  audience: "neutron",
  api_base: "/api",
  deployment_name: "Fenwick Plant 3",
  search_deadline_ms: 75000,
};

function setup(config: Partial<ConsoleRuntimeConfig> | null = {}) {
  const user = userEvent.setup();
  const value = config === null ? null : { ...CONFIG, ...config };
  render(
    <AuthStub>
      <ThemeProvider>
        <DensityProvider>
          <RuntimeConfigContext.Provider value={value}>
            <SettingsPage />
          </RuntimeConfigContext.Provider>
        </DensityProvider>
      </ThemeProvider>
    </AuthStub>,
  );
  return user;
}

/** The value beside a labelled field. */
function fieldValue(label: string): string {
  const dt = screen.getByText(label);
  return dt.nextElementSibling?.textContent ?? "";
}

beforeEach(() => {
  tour.summon.mockClear();
  localStorage.clear();
});

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-density");
  localStorage.clear();
});

describe("§16 — the two kinds of setting are kept visibly apart", () => {
  it("renders both section headings, in §16's words", () => {
    setup();
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Your preferences" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Deployment" })).toBeInTheDocument();
  });
});

describe("§16 — your preferences, local and instant", () => {
  it("offers the three theme choices and applies the chosen one at once", async () => {
    const user = setup();
    expect(screen.getByRole("radio", { name: "System" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("writes the SAME theme state the account menu writes", async () => {
    // Two surfaces, one store: if this control held its own state the account
    // menu and Settings would disagree about what the theme is.
    const user = setup();
    await user.click(screen.getByRole("radio", { name: "Dark" }));
    expect(readTheme()).toBe("dark");
  });

  it("offers the two densities and applies the chosen one product-wide", async () => {
    const user = setup();
    expect(screen.getByRole("radio", { name: "Compact" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Comfortable" }));
    expect(document.documentElement.getAttribute("data-density")).toBe("comfortable");
    expect(readDensity()).toBe("comfortable");
  });

  it("reopens the guided tour through the control the account menu calls", async () => {
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Reopen the guided tour" }));
    expect(tour.summon).toHaveBeenCalledTimes(1);
  });
});

describe("§16 — the deployment section, read-only", () => {
  it("renders the three fields from the configuration this deployment serves", () => {
    setup();
    expect(fieldValue("Deployment name")).toBe("Fenwick Plant 3");
    expect(fieldValue("Search time limit")).toBe("75.0 s");
    expect(fieldValue("Service address")).toBe("/api");
  });

  it("carries §16's caption, and offers no control it cannot honour", () => {
    setup();
    expect(screen.getByText(/Managed in this deployment.s configuration\./)).toBeInTheDocument();
    // No submit with no endpoint behind it, and no disabled inputs pretending
    // to be one.
    expect(screen.queryByRole("button", { name: "Request change" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("an unset name renders as ABSENT — never the host standing in for it", () => {
    setup({ deployment_name: undefined });
    expect(fieldValue("Deployment name")).toBe("— Not set");
    // The rail falls back to the served host; this field is the CONFIGURED
    // name, and a host printed here would state that somebody set it.
    expect(fieldValue("Deployment name")).not.toContain(window.location.host);
  });

  it("an unset time limit renders as ABSENT — never a zero and never a default", () => {
    setup({ search_deadline_ms: undefined });
    expect(fieldValue("Search time limit")).toBe("— Not set");
  });

  it("surfaces nothing else from the configuration", () => {
    setup();
    // §16: Settings is not a viewer for values with no governed write behind
    // them. The three fields it names are the three that render.
    const labels = [...document.querySelectorAll(".ne-settings__label")].map((n) => n.textContent);
    expect(labels).toEqual(["Deployment name", "Search time limit", "Service address"]);
    for (const secret of [CONFIG.issuer, CONFIG.client_id, CONFIG.audience]) {
      expect(screen.queryByText(secret)).not.toBeInTheDocument();
    }
  });

  it("survives a shell that has not resolved its configuration", () => {
    // Every field absent, every field an em dash. Never a blank section, and
    // never a value invented to fill one.
    setup(null);
    expect(fieldValue("Deployment name")).toBe("— Not set");
    expect(fieldValue("Search time limit")).toBe("— Not set");
    expect(fieldValue("Service address")).toBe("— Not set");
  });

  it("does NOT claim a role boundary — nothing here was refused", () => {
    setup();
    expect(screen.queryByText(/Your role doesn/)).not.toBeInTheDocument();
  });
});
