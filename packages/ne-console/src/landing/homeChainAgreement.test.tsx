// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// I3, RENDERED — the rail chip and Home's "Audit log" tile, in ONE VIEWPORT,
// about ONE chain.
//
// This is not a hypothetical pairing. `App.tsx` renders `<StatusChip/>` in the
// TopBar of `AuthedLayout` and routes `/` (LandingPage) inside that same layout,
// so the two are always on screen together and always an inch apart.
//
// The tile resolved its word from ONE read's raw `verification.valid` while the
// chip resolved its word from the LATCH, and the two failures that follow from
// that are both reproduced below, read by read, against the real provider:
//
//   1. A prefix row tampered after the anchor was blessed. /trace's unbounded
//      walk (here: the poll's own full-chain reads) disagrees three times and
//      LATCHES. The shared poll then answers `valid` off its
//      `anchored-extension` proof — the one read that STRUCTURALLY CANNOT see
//      that tamper — and the tile printed "Valid · 12,480 records" underneath a
//      chip reading "Chain break at entry 4812".
//   2. A SINGLE disagreeing read printed "Break · at entry 4812" on the tile
//      with zero confirmations behind it, while the chip beside it correctly
//      said it was still checking.
//
// The unit rules live in homeTiles.test.ts; this file asks the one question a
// unit test cannot — what the two surfaces SAY AT THE SAME INSTANT.

import { render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import type { ChainVerificationView } from "../api/types";
import { RuntimeConfigContext } from "../AppBootstrap";
import { BREAK_CONFIRMATIONS } from "../audit/chainVerdict";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { DirectoryProvider } from "../directory/DirectoryContext";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { PendingApprovalsProvider } from "../hooks/usePendingApprovals";
import { PostureProvider } from "../posture/PostureContext";
import { StatusChip } from "../rail/StatusChip";
import { AuthStub } from "../test/authStub";
import { AUDIT_EVENTS, RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { LandingPage } from "./LandingPage";

/** Real timers and a small interval, per the StatusChip/AuditPage precedent:
 * MSW and fake timers interact badly. */
const POLL_MS = 15;

/** An interval no test waits out, so a surface is asked what it says after
 * EXACTLY ONE read. A question about a single disagreeing read cannot be asked
 * of a poll that promotes it to a confirmed break mid-assertion. */
const ONE_POLL_MS = 60_000;

/** Generous, because a shared box under load must not manufacture a failure out
 * of a timer. */
const LATCH_WAIT = 5000;

/** The proof an interval poll actually gets: the rows appended since a head a
 * full `verify_chain` blessed up to fifteen minutes ago. It does not re-read the
 * prefix at all, so a prefix row tampered after that blessing is invisible to it
 * BY CONSTRUCTION (chainVerdict.ts I4). */
const ANCHORED_PASS: ChainVerificationView = {
  valid: true,
  basis: "anchored-extension",
  contradiction: false,
  checked: 12480,
  first_break_seq: null,
  first_break_event_id: null,
  reason: null,
  detail: "extends anchor 12,455, verified 41s ago; the prefix is NOT re-checked by this call",
};

/** The unbounded walk, seeing the tampered prefix row the anchored proof cannot. */
const FULL_WALK_BREAK: ChainVerificationView = {
  valid: false,
  basis: "full-chain",
  contradiction: false,
  checked: 12479,
  first_break_seq: 4812,
  first_break_event_id: "evt-4812",
  reason: "record_hash mismatch",
  detail: "chain broken at seq 4812",
};

/** The poll's verdicts, consumed one per read, the last entry repeating. */
function respondPolled(verdicts: readonly ChainVerificationView[]): { polls: () => number } {
  let n = 0;
  server.use(
    http.get("*/api/v1/audit/events", () => {
      const verification = verdicts[Math.min(n, verdicts.length - 1)]!;
      n += 1;
      return HttpResponse.json({ events: AUDIT_EVENTS, verification });
    }),
  );
  return { polls: () => n };
}

/** The chip and the page, under ONE provider — which is the whole point: one
 * chain, one latch, one poll, two surfaces. */
function renderHomeWithChip(intervalMs: number) {
  render(
    <AuthStub>
      <MemoryRouter initialEntries={["/"]}>
        <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
          <PrincipalProvider>
            <DirectoryProvider>
              <PendingApprovalsProvider intervalMs={60_000}>
                <AuditPollProvider intervalMs={intervalMs}>
                  <PostureProvider>
                    <StatusChip />
                    <Routes>
                      <Route path="/" element={<LandingPage />} />
                    </Routes>
                  </PostureProvider>
                </AuditPollProvider>
              </PendingApprovalsProvider>
            </DirectoryProvider>
          </PrincipalProvider>
        </RuntimeConfigContext.Provider>
      </MemoryRouter>
    </AuthStub>,
  );
}

/** The "Audit log" standing-fact tile, as the words it actually renders. */
async function auditFact(): Promise<string> {
  const link = await screen.findByRole("link", { name: /Audit log/ });
  return link.textContent ?? "";
}

describe("I3 — Home's audit fact and the rail chip cannot disagree", () => {
  it("states a CONFIRMED break the poll's own proof cannot see, not that proof's Valid", async () => {
    // Three full-chain disagreements latch the break; every read after that is
    // the anchored extension, passing, blind to the tampered prefix.
    const { polls } = respondPolled([
      ...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(FULL_WALK_BREAK),
      ANCHORED_PASS,
    ]);
    renderHomeWithChip(POLL_MS);

    await screen.findByRole(
      "button",
      { name: "Chain break at entry 4812" },
      { timeout: LATCH_WAIT },
    );
    // Past the latch, so the read in hand is the PASSING anchored one — which is
    // exactly the read the tile used to publish.
    await waitFor(() => expect(polls()).toBeGreaterThan(BREAK_CONFIRMATIONS + 2), {
      timeout: LATCH_WAIT,
    });

    const fact = await auditFact();
    expect(fact).toContain("Break");
    expect(fact).toContain("at entry 4812");
    // The shipped sentence, verbatim. This is the assertion the defect fails.
    expect(fact).not.toContain("Valid");
    expect(fact).not.toContain("12,480 records");
    // And the chip, at the same instant, still says the same thing.
    expect(screen.getByRole("button", { name: "Chain break at entry 4812" })).toBeInTheDocument();
  });

  it("a SINGLE disagreeing read is Re-checking on BOTH surfaces, an accusation on neither", async () => {
    respondPolled([FULL_WALK_BREAK]);
    renderHomeWithChip(ONE_POLL_MS);

    await screen.findByRole("button", { name: "Re-checking the audit chain" });
    const fact = await auditFact();
    expect(fact).toContain("Re-checking");
    expect(fact).toContain("not confirmed yet");
    // The raw flicker `BREAK_CONFIRMATIONS` exists to suppress, which the tile
    // rendered unsuppressed on the first screen of the product.
    expect(fact).not.toContain("Break");
    expect(fact).not.toContain("at entry 4812");
    // And no record count: `checked` on a FAILING read is the reach of a verdict
    // that did not pass, so printing it anywhere on this tile is a false vouch.
    expect(fact).not.toContain("12,479");
    expect(screen.queryByRole("button", { name: /Chain break/ })).toBeNull();
  });

  it("CONTROL: an intact chain reads Valid on the tile and Operational on the chip", async () => {
    // Neither assertion above is vacuous: on a chain nothing disagrees about,
    // the tile still states the served count and the chip still goes quiet.
    respondPolled([ANCHORED_PASS]);
    renderHomeWithChip(POLL_MS);

    await screen.findByRole("button", { name: "Operational" });
    await waitFor(async () => {
      const fact = await auditFact();
      expect(fact).toContain("Valid");
      expect(fact).toContain("12,480 records");
    });
    expect(screen.queryByRole("button", { name: /Chain break|Re-checking/ })).toBeNull();
  });
});
