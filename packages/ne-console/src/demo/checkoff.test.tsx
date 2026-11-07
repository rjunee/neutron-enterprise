// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// CHECK-OFF. The tour audits itself: a station checks off ONLY when a genuinely
// NEW audit entry is observed on the shared poll while that station is armed, and
// the number it records is that real entry — the module holds no other source for
// one, so an invented check-off is impossible by construction. When the audit read
// is refused for this role the panel says so ONCE, and no station checks off.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router";
import { RuntimeConfigContext } from "../AppBootstrap";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuditPollProvider, useAuditPoll } from "../hooks/useAuditPoll";
import { PostureProvider } from "../posture/PostureContext";
import { AuthStub } from "../test/authStub";
import { AUDIT_EVENTS, RUNTIME_CONFIG, VERIFICATION_OK } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { DemoProvider, useDemo } from "./DemoContext";
import { STATIONS } from "./stations";
import { DemoOverlay } from "./DemoOverlay";

function Summoner() {
  const { summon } = useDemo();
  return (
    <button type="button" onClick={summon}>
      SUMMON
    </button>
  );
}

/** Surfaces the shared poll's latestSeq so a test can wait for a real baseline
 * before advancing it — deterministic, never timing-raced. */
function Probe() {
  const poll = useAuditPoll();
  return <span data-testid="probe">{String(poll.latestSeq)}</span>;
}

function renderCheckoff() {
  return render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <MemoryRouter>
          <PrincipalProvider>
            <AuditPollProvider intervalMs={15}>
              <PostureProvider>
                <DemoProvider>
                  <Summoner />
                  <Probe />
                  <DemoOverlay />
                </DemoProvider>
              </PostureProvider>
            </AuditPollProvider>
          </PrincipalProvider>
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
}

/** A seq-4 event mirroring the AUDIT_EVENTS shape — the "new" governed event. */
const SEQ4_EVENT = {
  ...AUDIT_EVENTS[2]!,
  id: 4,
  seq: 4,
  event_id: "evt-4",
  outcome: "success" as const,
};

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  sessionStorage.clear();
});

describe("guided tour — check-off", () => {
  it("checks off with the real seq the shared poll produced, and never before", async () => {
    renderCheckoff();
    await userEvent.click(screen.getByRole("button", { name: "SUMMON" }));

    // Wait for the baseline: the default handler serves seq 3 stably, so the poll
    // arms the station at seq 3 (and, staying at 3, never checks off).
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("3"));
    expect(screen.queryByText(/^Checked off/)).not.toBeInTheDocument();

    // Now advance the chain: every subsequent poll includes seq 4 (the new event).
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({
          events: [...AUDIT_EVENTS, SEQ4_EVENT],
          verification: { ...VERIFICATION_OK, checked: 4, detail: "chain intact: 4 record(s)" },
        }),
      ),
    );

    // The armed station checks off with that REAL entry number.
    expect(await screen.findByText("Checked off at entry 4.")).toBeInTheDocument();

    // …and it persisted under the resumable key (the first station's id).
    await waitFor(() => {
      const stored = JSON.parse(sessionStorage.getItem("ne-demo:v1") ?? "{}");
      expect(stored.checks?.[STATIONS[0]!.id]).toBe(4);
    });
  });

  it("states the role boundary ONCE under a refused audit read, never a fake check", async () => {
    // Check-off reads the audit log, which not every key can. This used to be
    // drawn on all nine stations — a control that existed only to display a
    // permission failure, nine times, on a tour nobody in the room can complete.
    // The reason is stated once, below the pager, where it is a fact about the
    // key rather than a failure about the station.
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ detail: "requires ops role" }, { status: 403 }),
      ),
    );

    renderCheckoff();
    await userEvent.click(screen.getByRole("button", { name: "SUMMON" }));

    const panel = await screen.findByRole("dialog", { name: "guided tour" });
    expect(
      await within(panel).findByText(
        "Stations check off against the audit log, which your role doesn’t include. No station will check off.",
      ),
    ).toBeInTheDocument();
    // Stated once — not once per station — and no invented check-off.
    expect(within(panel).getAllByText(/your role doesn’t include/i)).toHaveLength(1);
    expect(screen.queryByText(/^Checked off/)).not.toBeInTheDocument();
  });
});
