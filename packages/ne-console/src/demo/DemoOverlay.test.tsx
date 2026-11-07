// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE TOUR PANEL, SUMMONED. These cases start from an already-dismissed tour, so
// what they exercise is the panel someone ASKED for. The first-run presentation
// — raised once, killed by one action — is proved in firstRun.test.tsx.
//
// The case that matters most is (3): a station link is clicked and the screen it
// names actually arrives. A tour that misses is worse than no tour.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { App } from "../App";
import { RuntimeConfigContext } from "../AppBootstrap";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { PostureProvider } from "../posture/PostureContext";
import { AuthStub } from "../test/authStub";
import { RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { DemoProvider, useDemo } from "./DemoContext";
import { DemoOverlay } from "./DemoOverlay";
import { STATIONS } from "./stations";
import { dismissTour } from "./tourStorage";

/** The tour left the rail: it is a preference-shaped thing most people open
 * once, and it held a permanent rail row on every route. It lives in the account
 * menu now, beside the theme and density radios. */
async function summonFromAccountMenu() {
  await userEvent.click(screen.getByRole("button", { name: "Account" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Guided tour" }));
}

/** A summon control + a probe input, so a test can open the panel and dispatch
 * keys from inside a text field. */
function Summoner() {
  const { summon } = useDemo();
  return (
    <>
      <button type="button" onClick={summon}>
        SUMMON
      </button>
      <input aria-label="probe" />
    </>
  );
}

function renderOverlay() {
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <MemoryRouter>
          <AuditPollProvider intervalMs={50}>
            <PostureProvider>
              <DemoProvider>
                <Summoner />
                <DemoOverlay />
              </DemoProvider>
            </PostureProvider>
          </AuditPollProvider>
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

async function summon() {
  await userEvent.click(screen.getByRole("button", { name: "SUMMON" }));
}

const FIRST = STATIONS[0]!;
const SECOND = STATIONS[1]!;
const THIRD = STATIONS[2]!;

beforeEach(() => {
  dismissTour();
});

afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the guided tour panel", () => {
  it("(1) renders every station, with the current one on the placard", async () => {
    renderOverlay();
    await summon();
    expect(screen.getByRole("dialog", { name: "guided tour" })).toBeInTheDocument();

    for (const station of STATIONS) {
      expect(
        screen.getByRole("button", { name: `station ${station.numeral} ${station.title}` }),
      ).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { name: FIRST.title })).toBeInTheDocument();
    // A tour someone asked for does not wear the first-run introduction.
    expect(screen.getByText(/^Guided tour · Station 1 of/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "close the guided tour" })).toBeInTheDocument();
  });

  it("(2) renders this deployment's own egress figure, not a scripted one", async () => {
    renderOverlay();
    await summon();
    // The figure belongs to the station about what this deployment may reach,
    // which is the last job in the tour and no longer the first station.
    const reach = STATIONS.find((s) => s.liveReadout === "egress")!;
    await userEvent.click(
      screen.getByRole("button", { name: `station ${reach.numeral} ${reach.title}` }),
    );
    expect(
      await screen.findByText("External hosts allowed on this deployment"),
    ).toBeInTheDocument();
    // The served allowlist, counted — NO station's copy carries a figure of its
    // own for the product's screens to contradict.
    for (const station of STATIONS) {
      let residue = `${station.role} ${station.job} ${station.path} ${station.see}`;
      for (const name of station.tenantRecords) residue = residue.split(name).join(" ");
      expect(residue, station.id).not.toMatch(/\d/);
    }
  });

  it("(3) a station link lands on the screen it names", async () => {
    // The full App, so the route and its providers are the real ones.
    window.history.pushState({}, "", "/");
    render(
      <AuthStub>
        <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
          <App />
        </RuntimeConfigContext.Provider>
      </AuthStub>,
    );
    await summonFromAccountMenu();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("heading", { name: SECOND.title })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("link", { name: "Open Search" }));
    expect(await screen.findByRole("heading", { name: "Search", level: 1 })).toBeInTheDocument();
    window.history.pushState({}, "", "/");
  });

  it("(4) arrow keys advance and retreat, clamped at the first station", async () => {
    renderOverlay();
    await summon();
    expect(screen.getByRole("heading", { name: FIRST.title })).toBeInTheDocument();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("heading", { name: SECOND.title })).toBeInTheDocument();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("heading", { name: FIRST.title })).toBeInTheDocument();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("heading", { name: FIRST.title })).toBeInTheDocument();
  });

  it("(5) arrow keys are ignored while typing in a field", async () => {
    renderOverlay();
    await summon();
    screen.getByLabelText("probe").focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("heading", { name: FIRST.title })).toBeInTheDocument();
  });

  it("(6) position persists for the sitting and resumes on remount", async () => {
    const { unmount } = renderOverlay();
    await summon();
    await userEvent.keyboard("{ArrowRight}");
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("heading", { name: THIRD.title })).toBeInTheDocument();
    await waitFor(() => {
      const stored = JSON.parse(sessionStorage.getItem("ne-demo:v1") ?? "{}");
      expect(stored.station).toBe(2);
    });
    unmount();

    // A dismissed tour does not raise itself; summoning resumes where it stopped.
    renderOverlay();
    expect(screen.queryByRole("dialog", { name: "guided tour" })).not.toBeInTheDocument();
    await summon();
    expect(screen.getByRole("heading", { name: THIRD.title })).toBeInTheDocument();
  });

  it("(7) reduced motion collapses the motion class (and the inverse restores it)", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }));
    const first = renderOverlay();
    await summon();
    expect(screen.getByRole("dialog", { name: "guided tour" })).toHaveClass("ne-demo--reduced");
    first.unmount();

    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }));
    renderOverlay();
    await summon();
    expect(screen.getByRole("dialog", { name: "guided tour" })).toHaveClass("ne-demo--motion");
  });

  it("(8) carries no repo internals and no signage shouting", async () => {
    renderOverlay();
    await summon();
    const panel = screen.getByRole("dialog", { name: "guided tour" });
    const text = panel.textContent ?? "";
    expect(text).not.toMatch(/\/v1\/|GET |POST |NE-\d|\.py|\.tsx?\b/);
    expect(text.toLowerCase()).not.toMatch(/verbatim|governed|proven|fabricated|authored/);
  });
});
