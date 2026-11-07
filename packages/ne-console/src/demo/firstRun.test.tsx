// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE FIRST-RUN PANEL. The tour used to be the BODY of the home route — the
// product opened on its own walkthrough — and then an in-flow panel on that same
// route, which is the same mistake with fewer pixels. It is now what every
// product ships, and these cases are the guardrails on "never page content, and
// never the default view again":
//
//   (1) it opens by itself exactly once, on a browser that has never dismissed
//       it, and introduces itself;
//   (2) ONE action dismisses it — no confirmation step — and that action writes
//       localStorage, not sessionStorage;
//   (3) after that it does NOT open on a fresh load, INCLUDING a fresh load of a
//       different route (the failure this locks down is a panel that reappears
//       because the dismissal only lived for the session);
//   (4) it stays reachable: the account menu's "Guided tour" reopens the same
//       stations, now without the first-run introduction;
//   (5) it is an OVERLAY — it is never rendered inside the page.
//
// Escape dismisses on the same terms as the button (one action, permanent), so a
// keyboard user is not left with the only permanent dismissal behind a click.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { RuntimeConfigContext } from "../AppBootstrap";
import { AuthStub } from "../test/authStub";
import { RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { STATIONS } from "./stations";
import { isTourDismissed } from "./tourStorage";

/** The tour control left the rail (§1.2/§1.3): it is a preference-shaped thing
 * most people open once, and it held a permanent rail row on every route. It
 * lives in the account menu now, where the theme and density radios live. */
async function summonFromAccountMenu() {
  await userEvent.click(screen.getByRole("button", { name: "Account" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Guided tour" }));
}

/**
 * NOBODY WITH WORK WAITING GETS AMBUSHED BY A WALKTHROUGH.
 *
 * A first-run panel is for someone who has nothing else to do here. The approver
 * opens this console BECAUSE something is waiting for a decision, so an
 * unprompted tour interrupts the highest-frequency job in the product on the
 * very screen where that job starts. The default fixture principal HOLDS a
 * queue, so these cases sign in as one who does not — and the case below them
 * proves the queue-holder is left alone.
 */
function withoutAQueue() {
  server.use(
    http.get("*/api/v1/approvals/pending", () =>
      HttpResponse.json({ detail: "not an approver" }, { status: 403 }),
    ),
  );
}

// The dismissal is SCOPED TO THE PRINCIPAL (tourStorage.ts): one person
// dismissing the tour on a shared machine must not hide it from the next
// signed-in user. `fixtures.principal.id` is who /v1/me resolves to here.
const PRINCIPAL = "alice@example.com";

function renderAppAt(path: string) {
  withoutAQueue();
  window.history.pushState({}, "", path);
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

const TOUR = { name: "guided tour" } as const;
const FIRST = STATIONS[0]!;

describe("guided tour — the first-run panel", () => {
  it("(1) opens by itself on a first run and says what it is", async () => {
    renderAppAt("/");
    const panel = await screen.findByRole("dialog", TOUR);
    expect(panel).toBeInTheDocument();
    expect(screen.getByText(/^First run · Guided tour/)).toBeInTheDocument();
    // It says plainly that dismissing is permanent, and where to find it again.
    expect(panel.textContent).toContain("it will not open again");
    expect(panel.textContent).toContain("Guided tour");
    // Non-modal by construction: the work surface underneath is still there.
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  });

  it("(2) one action dismisses it, and the dismissal is written to localStorage", async () => {
    const { unmount } = renderAppAt("/");
    expect(isTourDismissed(PRINCIPAL)).toBe(false);

    await userEvent.click(
      await screen.findByRole("button", { name: "dismiss the guided tour for good" }),
    );

    // Gone, with no second confirmation step in between.
    expect(screen.queryByRole("dialog", TOUR)).not.toBeInTheDocument();
    expect(isTourDismissed(PRINCIPAL)).toBe(true);
    // localStorage, not sessionStorage: a dismissal that dies with the tab is
    // not a dismissal. This is the assertion that catches that regression.
    expect(localStorage.getItem(`ne-tour:v1:${PRINCIPAL}`)).toBe("dismissed");
    // …and NOT under the browser-global key, which would hide the tour from the
    // next person to sign in on this machine.
    expect(localStorage.getItem("ne-tour:v1")).toBeNull();
    expect(sessionStorage.getItem("ne-tour:v1")).toBeNull();
    unmount();
  });

  it("(3) never auto-opens again — not on '/', not on any other route", async () => {
    const first = renderAppAt("/");
    await userEvent.click(
      await screen.findByRole("button", { name: "dismiss the guided tour for good" }),
    );
    first.unmount();

    // A completely fresh load of the home route.
    const second = renderAppAt("/");
    expect(screen.queryByRole("dialog", TOUR)).not.toBeInTheDocument();
    second.unmount();

    // And of a different route — the panel lives in the shell, so a regression
    // here would ambush someone who never even goes to the home screen.
    renderAppAt("/approvals");
    expect(screen.queryByRole("dialog", TOUR)).not.toBeInTheDocument();
  });

  it("(4) stays reachable from the account menu, without the first-run introduction", async () => {
    const first = renderAppAt("/");
    await userEvent.click(
      await screen.findByRole("button", { name: "dismiss the guided tour for good" }),
    );
    first.unmount();

    renderAppAt("/");
    await summonFromAccountMenu();
    const panel = await screen.findByRole("dialog", TOUR);
    // The same content — the first station.
    expect(panel.textContent).toContain(FIRST.title);
    // …presented as a tour someone asked for, not as a first run.
    expect(screen.queryByText(/^First run · Guided tour/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "close the guided tour" })).toBeInTheDocument();
  });

  it("(5) Escape dismisses permanently too — the keyboard path is not weaker", async () => {
    renderAppAt("/");
    expect(await screen.findByRole("dialog", TOUR)).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", TOUR)).not.toBeInTheDocument();
    expect(isTourDismissed(PRINCIPAL)).toBe(true);
  });

  it("(6) the stations never render inside the page — the panel is an overlay", async () => {
    const { container } = renderAppAt("/");
    const panel = await screen.findByRole("dialog", TOUR);
    // §4: the tour is a dismissible overlay, never rendered in the page flow.
    const main = container.querySelector("#ne-main");
    expect(main).toBeTruthy();
    expect(main?.contains(panel)).toBe(false);
    expect(main?.textContent).not.toContain(FIRST.title);
    // The page reserves height beneath it, so it covers nothing.
    //
    // WAITED FOR, because the flag and the panel land on different ticks. The
    // dialog node is in the DOM at commit; `data-ne-tour` is written by
    // DemoOverlay's effect, which React runs as a PASSIVE effect afterwards.
    // `findByRole` above resolves the instant the node appears, so a synchronous
    // read here was racing the effect and lost about one run in fifteen —
    // `expected undefined to be 'open'`, with the attribute arriving immediately
    // after the assertion (traced: ASSERT undefined, then the effect's write).
    // Nothing is wrong with the product; the flag always arrives. This waits for
    // the effect exactly as the line above waits for the node.
    await waitFor(() => expect(document.documentElement.dataset.neTour).toBe("open"));
  });
});

// ===========================================================================
// The panel is scoped to the home screen, and never raised over working content
// ===========================================================================

describe("guided tour — scoped to the home screen, never over working content", () => {
  it("does NOT open unprompted on a working route", () => {
    // Verified defect, three reviewers: on a fresh session the panel rendered on
    // EVERY route — covering the composer on Ask (which could not be scrolled
    // clear of it), the Posture control table and the Approvals queue — while
    // its own copy read "this panel never blocks it".
    for (const route of [
      "/ask",
      "/approvals",
      "/posture",
      "/knowledge/documents",
      "/verified-queries",
      "/audit",
    ]) {
      localStorage.clear();
      const { unmount } = renderAppAt(route);
      expect(screen.queryByRole("dialog", TOUR), route).toBeNull();
      unmount();
    }
  });

  it("still opens unprompted on the home screen, where there is nothing to occlude", async () => {
    localStorage.clear();
    renderAppAt("/");
    expect(await screen.findByRole("dialog", TOUR)).toBeInTheDocument();
  });

  it("is still summonable on a working route — a panel you ASKED for is a different thing", async () => {
    localStorage.clear();
    renderAppAt("/ask");
    expect(screen.queryByRole("dialog", TOUR)).toBeNull();
    await summonFromAccountMenu();
    const panel = await screen.findByRole("dialog", TOUR);
    expect(panel).toBeInTheDocument();
    // Summoned, so it does NOT wear the first-run introduction.
    expect(screen.queryByText(/^First run · Guided tour/)).toBeNull();
  });

  it("does NOT open unprompted for a principal who holds a decision queue", async () => {
    // The default fixture principal is shown a queue, i.e. this key has work
    // waiting on it. No override here, deliberately.
    localStorage.clear();
    window.history.pushState({}, "", "/");
    render(
      <AuthStub>
        <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
          <App />
        </RuntimeConfigContext.Provider>
      </AuthStub>,
    );
    // The queue itself lands, which is what makes the absence meaningful rather
    // than a race: the page settled and the panel still did not open.
    await screen.findByRole("table", { name: "Waiting on you" });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("dialog", TOUR)).toBeNull();
    // …and it is NOT dismissed — it is simply not raised unprompted. Two clicks
    // in the account menu still open it.
    expect(isTourDismissed(PRINCIPAL)).toBe(false);
    await summonFromAccountMenu();
    expect(await screen.findByRole("dialog", TOUR)).toBeInTheDocument();
  });

  it("the reach station renders the deployment's LIVE egress figure, not a scripted one", async () => {
    // A station used to assert "Zero external requests — by construction" and
    // tell the presenter the egress readout would agree, then deep-link to a
    // screen reporting one allowed host. The figure is read from the deployment
    // now, so the script and the product cannot disagree. It sits on the
    // operator's job — what this deployment may run and reach — which is where
    // someone actually asks the question.
    localStorage.clear();
    renderAppAt("/");
    const panel = await screen.findByRole("dialog", TOUR);
    expect(panel.textContent).not.toContain("Zero external requests");

    const reach = STATIONS.find((s) => s.liveReadout === "egress")!;
    await userEvent.click(
      within(panel).getByRole("button", { name: `station ${reach.numeral} ${reach.title}` }),
    );
    expect(
      await within(panel).findByText("External hosts allowed on this deployment"),
    ).toBeInTheDocument();
  });
});

describe("the shell — a skip link is the first focusable element", () => {
  it("targets the main region and precedes the rail in the tab order", async () => {
    localStorage.setItem("ne-tour:v1", "dismissed");
    const { container } = renderAppAt("/");
    const skip = container.querySelector("a.ne-skip")!;
    expect(skip).toBeTruthy();
    expect(skip.getAttribute("href")).toBe("#ne-main");
    expect(container.querySelector("#ne-main")).toBeTruthy();
    // It is the FIRST focusable element in the document, ahead of the wordmark.
    const focusables = [
      ...container.querySelectorAll<HTMLElement>("a[href], button, input, [tabindex='0']"),
    ];
    expect(focusables[0]).toBe(skip);
  });
});
