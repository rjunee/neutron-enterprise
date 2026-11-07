// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §3 rank 3 — THE ACTIONS, RUN.
//
// Four of the six palette actions used to emit a query string or a hash that no
// route in the package read: `?export=1`, `?verify=1`, `#appearance`, `?tour=1`.
// Every one of them "worked" in the sense that the URL changed, and none of them
// did the thing on the row. A palette that lists a capability it does not have is
// a fabricated capability, so the assertions here are deliberately NOT about the
// URL:
//
//   Export audit log   → a CSV blob exists, with the ledger's columns in it.
//   Re-verify chain    → the SERVER answered again and the new verdict is on screen.
//   Switch theme       → <html data-theme> flipped, and the choice was stored.
//   Open guided tour   → the tour panel is open.
//
// They run through the real App — the production router, providers and screens —
// because the defect being fixed is precisely that the palette and the screens
// were never actually connected.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { RuntimeConfigContext } from "../AppBootstrap";
import { dismissTour } from "../demo/tourStorage";
import { AuthStub } from "../test/authStub";
import { AUDIT_EVENTS, RUNTIME_CONFIG, VERIFICATION_OK } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { runPaletteCommand } from "./paletteCommands";
import { PALETTE_ACTIONS } from "./paletteIndex";

function renderAt(path: string) {
  window.history.pushState({}, "", path);
  render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <App />
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
  return userEvent.setup();
}

/** Open the palette, put the caller's query in it, and Enter the row that comes
 * out on top — the exact keystrokes §3 documents, never a direct call. */
async function runAction(user: ReturnType<typeof userEvent.setup>, query: string, label: string) {
  await user.keyboard("{Meta>}k{/Meta}");
  // Scoped to the dialog: a screen underneath may own a <select>, whose <option>
  // children carry the same ARIA role as the palette's rows.
  const palette = screen.getByRole("dialog", { name: "Search or jump to" });
  const input = within(palette).getByRole("combobox", { name: "Search or jump to" });
  await user.type(input, query);
  // First result preselected (§3) — assert it IS the action before Enter, so a
  // ranking change can never turn this into a proof about some other row.
  const first = within(palette).getAllByRole("option")[0]!;
  expect(within(first).getByText(label)).toBeInTheDocument();
  expect(within(first).getByText("Action")).toBeInTheDocument();
  await user.keyboard("{Enter}");
}

const objectUrl = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };

afterEach(() => {
  URL.createObjectURL = objectUrl.create;
  URL.revokeObjectURL = objectUrl.revoke;
  vi.restoreAllMocks();
  // <html> survives cleanup(); a theme left on it would decide the next test's
  // starting state.
  document.documentElement.removeAttribute("data-theme");
  window.history.pushState({}, "", "/");
});

/* ------------------------------ Export audit log --------------------------- */

describe('§3 — "Export audit log"', () => {
  it("writes the server-authored CSV bytes, from a palette keystroke on another screen", async () => {
    const serverCsv = [
      "# neutron-audit-export v1",
      "# verification_valid: true",
      "seq,event_id,record_hash",
      `7,evt-palette,${"a1".repeat(32)}`,
      "# export_complete: true",
      "",
    ].join("\r\n");
    const blobs: Blob[] = [];
    const names: string[] = [];
    const exportRequests: string[] = [];
    URL.createObjectURL = ((blob: Blob) => {
      blobs.push(blob);
      return "blob:stub";
    }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      names.push(this.download);
    });
    server.use(
      http.get("*/api/v1/audit/export", ({ request }) => {
        exportRequests.push(request.url);
        return new HttpResponse(serverCsv, {
          headers: { "Content-Type": "text/csv; charset=utf-8" },
        });
      }),
    );

    const user = renderAt("/");
    await screen.findByRole("button", { name: "Account" });
    await runAction(user, "export audit", "Export audit log");

    // THE FILE, not the URL. The Blob must carry the API response byte-for-byte;
    // an "Audit log" preamble would prove the removed client-built path returned.
    await waitFor(() => expect(blobs).toHaveLength(1));
    const text = await blobs[0]!.text();
    expect(text).toBe(serverCsv);
    expect(text).not.toContain("Audit log");
    expect(new URL(exportRequests[0]!).pathname).toBe("/api/v1/audit/export");
    expect(names[0]).toMatch(/^audit-.*\.csv$/);
  });

  it("leaves the reader ON the ledger with the export control focused", async () => {
    URL.createObjectURL = (() => "blob:stub") as typeof URL.createObjectURL;
    URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    const user = renderAt("/");
    await screen.findByRole("button", { name: "Account" });
    await runAction(user, "export audit", "Export audit log");

    // §3 calls an action a verb that JUMPS AND FOCUSES. The jump is the audit
    // screen; the focus is the control the row names, so a keyboard reader is
    // left standing where the thing they asked for lives.
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Export CSV" })),
    );
  });
});

/* ------------------------------ Re-verify chain ---------------------------- */

describe('§3 — "Re-verify chain"', () => {
  it("makes the SERVER walk the chain again and renders the fresh verdict", async () => {
    // ⚠️ TWICE-CORRECTED, AND THE SECOND CORRECTION WAS MINE.
    //
    // It first varied the LEDGER read (limit=100) and held the poll fixed. That
    // stopped working when the strip started rendering the LATCHED verdict:
    // `AuditPage` takes the word AND the count from the poll's read,
    // deliberately, because a word from one read beside a count from another is
    // a false sentence.
    //
    // I then re-aimed it at the poll and asserted `pollReads > before`. ⚠️ THAT
    // ASSERTION COULD NOT FAIL: `AuditPollProvider` polls on its own 5 s timer,
    // so on any run where the preceding query and typing take >4 s the interval
    // satisfies it by itself. An adversarial pass showed you could DELETE
    // `refetch()` from `reverify` and this test stayed green while the screen's
    // own window was never re-read — which is exactly what its title claims.
    // My comment then said "The CLAIM is unchanged", and it was not: I had
    // dropped the ledger claim while believing I had moved it.
    //
    // So it now asserts BOTH halves of what `reverify` does, and the ledger
    // count is the one the interval CANNOT produce — nothing but `refetch()`
    // issues a limit=100 read.
    let ledgerReads = 0;
    let pollReads = 0;
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        if (new URL(request.url).searchParams.get("limit") === "100") {
          ledgerReads += 1;
          return HttpResponse.json({ events: AUDIT_EVENTS, verification: VERIFICATION_OK });
        }
        pollReads += 1;
        return HttpResponse.json({
          events: AUDIT_EVENTS,
          verification: { ...VERIFICATION_OK, checked: 100 + pollReads },
        });
      }),
    );

    const user = renderAt("/audit");
    await screen.findByText("101 records verified");
    const ledgerBefore = ledgerReads;
    const pollBefore = pollReads;
    await runAction(user, "re-verify", "Re-verify chain");

    // (1) The SERVER walked the ledger window again — driven by `refetch()`
    // alone, so deleting it reds this line.
    await waitFor(() => expect(ledgerReads).toBeGreaterThan(ledgerBefore));
    // (2) And the strip is showing a LATER poll answer, not the first one with a
    // spinner over it — driven by `poll.reverify()`.
    await waitFor(() => expect(pollReads).toBeGreaterThan(pollBefore));
    await waitFor(() =>
      expect(screen.getByText(`${100 + pollReads} records verified`)).toBeInTheDocument(),
    );
  });
});

/* -------------------------------- Switch theme ----------------------------- */

describe('§3 — "Switch theme"', () => {
  it("flips the rendered theme and stores the choice", async () => {
    const user = renderAt("/");
    await screen.findByRole("button", { name: "Account" });
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);

    await runAction(user, "switch theme", "Switch theme");
    // The attribute is the whole contract with tokens.css, so it is the thing
    // asserted — and the preference is written, or the flip dies with the tab.
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
    expect(localStorage.getItem("ne-theme:v1")).toBe("dark");

    // It is a switch, not a one-way trip.
    await runAction(user, "switch theme", "Switch theme");
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(localStorage.getItem("ne-theme:v1")).toBe("light");
  });

  it("moves the account menu's radio with it — ONE piece of state, two surfaces", async () => {
    const user = renderAt("/");
    await screen.findByRole("button", { name: "Account" });
    await runAction(user, "switch theme", "Switch theme");
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));

    await user.click(screen.getByRole("button", { name: "Account" }));
    const menu = screen.getByRole("menu", { name: "Account" });
    expect(within(menu).getByRole("radio", { name: "Dark" })).toBeChecked();
    expect(within(menu).getByRole("radio", { name: "System" })).not.toBeChecked();
  });
});

/* ----------------------------- Open guided tour ---------------------------- */

describe('§3 — "Open guided tour"', () => {
  it("opens the tour panel", async () => {
    // Start from a browser that has already dismissed the tour, so the panel on
    // screen at the end can only have been summoned by the action.
    dismissTour();
    const user = renderAt("/");
    await screen.findByRole("button", { name: "Account" });
    expect(screen.queryByRole("dialog", { name: "guided tour" })).not.toBeInTheDocument();

    await runAction(user, "guided tour", "Open guided tour");

    const panel = await screen.findByRole("dialog", { name: "guided tour" });
    // The summoned presentation, identical to the account menu's — the palette
    // runs that control rather than owning a second one.
    expect(within(panel).getByText(/^Guided tour · Station 1 of/)).toBeInTheDocument();
  });
});

/* --------------------------- the registry, and the give-up ------------------ */

describe("§3 — the action registry cannot hold a dead target", () => {
  it("carries no query string and no hash: an action is a route plus a command", () => {
    // The exact defect signature. `?export=1`, `?verify=1`, `?tour=1` and
    // `#appearance` were parameters no route parsed — a promise made to nothing.
    for (const action of PALETTE_ACTIONS) {
      if (action.to === null) continue;
      expect(action.to).toMatch(/^\/[a-z]*$/);
    }
  });

  it("gives every non-navigating row something to run", () => {
    for (const action of PALETTE_ACTIONS) {
      expect(action.to !== null || action.command !== undefined).toBe(true);
    }
    // Named, so this cannot pass by the four rows quietly disappearing.
    const commanded = PALETTE_ACTIONS.filter((a) => a.command !== undefined).map((a) => a.label);
    expect(commanded).toEqual([
      "Export audit log",
      "Re-verify chain",
      "Switch theme",
      "Open guided tour",
    ]);
  });
});

describe("a screen command reports whether it actually ran", () => {
  const inert = { setTheme: () => {}, summonTour: () => {} };

  it("returns false, and does nothing, when the control never arrives", async () => {
    // Nothing rendered: no audit screen, so no export. The reader is left on the
    // screen that says why — the palette does not invent a second explanation.
    const clicked = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(clicked);
    await expect(runPaletteCommand("export-audit", { ...inert, deadlineMs: 40 })).resolves.toBe(
      false,
    );
    expect(clicked).not.toHaveBeenCalled();
  });

  it("returns true, and presses it, when the control is there", async () => {
    // The control case for the assertion above: the same call, the same
    // deadline, one difference — the button exists.
    const pressed = vi.fn();
    const button = document.createElement("button");
    button.textContent = "Export CSV";
    button.addEventListener("click", pressed);
    document.body.append(button);
    try {
      await expect(runPaletteCommand("export-audit", { ...inert, deadlineMs: 40 })).resolves.toBe(
        true,
      );
      expect(pressed).toHaveBeenCalledTimes(1);
    } finally {
      button.remove();
    }
  });

  it("treats a DISABLED control as absent rather than clicking into nothing", async () => {
    const pressed = vi.fn();
    const button = document.createElement("button");
    button.textContent = "Export CSV";
    button.disabled = true;
    button.addEventListener("click", pressed);
    document.body.append(button);
    try {
      await expect(runPaletteCommand("export-audit", { ...inert, deadlineMs: 40 })).resolves.toBe(
        false,
      );
      expect(pressed).not.toHaveBeenCalled();
    } finally {
      button.remove();
    }
  });
});
