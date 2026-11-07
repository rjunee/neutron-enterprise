// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §1.3 — THE SYSTEM STATUS CHIP AND THE CHAIN-VERDICT LATCH.
//
// The chip's other claims — quiet when healthy, the three-way absence split,
// one popover for all of global state — are asserted through the real App in
// rail/shell.test.tsx. This file asks the one question that needs a poll it can
// drive read by read: WHICH READING the chip rests on.
//
// It used to render one read's raw `verification.valid`. The server has a
// reproduced read-during-append race (audit/chainVerdict.ts) that answers
// invalid on roughly half of the reads taken while the chain is being appended
// to, and the console's own polling is one of the things appending — so the
// chip named a break at an entry and withdrew it on the next poll. With /audit
// routed through the latch, the chip and the strip beneath it stated opposite
// things about one chain at one instant, which is worse than either alone.
//
// Both directions are load-bearing and both are asserted here: a transient
// disagreement must NOT reach the chip as a break, and a confirmed break must
// reach it AND must not be withdrawn by a later passing read.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { ChainVerificationView } from "../api/types";
import { RuntimeConfigContext } from "../AppBootstrap";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { BREAK_CONFIRMATIONS } from "../audit/chainVerdict";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { AuthStub } from "../test/authStub";
import { AUDIT_EVENTS, RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { StatusChip } from "./StatusChip";

const VALID: ChainVerificationView = {
  valid: true,
  basis: "anchored-extension",
  contradiction: false,
  checked: 4812,
  first_break_seq: null,
  first_break_event_id: null,
  reason: null,
  detail: "chain intact",
};

/** The race's own answer, verbatim: the reason field is the server's. */
const BROKEN: ChainVerificationView = {
  valid: false,
  basis: "full-chain",
  contradiction: false,
  checked: 4812,
  first_break_seq: 4812,
  first_break_event_id: "evt-4812",
  reason: "head mismatch (truncated or forged tail)",
  detail: "chain broken",
};

/** Real timers and a small interval, per the AuditPage precedent: MSW and fake
 * timers interact badly. */
const POLL_MS = 15;

/** An interval no test waits out, so the chip is asked what it says after
 * EXACTLY ONE read. A question about a single disagreeing read cannot be asked
 * of a poll that promotes it to a confirmed break mid-assertion. */
const ONE_POLL_MS = 60_000;

/** Long enough for BREAK_CONFIRMATIONS polls at POLL_MS, generously: a shared
 * box under load must not manufacture a failure out of a timer. */
const LATCH_WAIT = 5000;

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

function renderChip(intervalMs: number) {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={RUNTIME_CONFIG}>
        <MemoryRouter initialEntries={["/"]}>
          <PrincipalProvider>
            <AuditPollProvider intervalMs={intervalMs}>
              <StatusChip />
            </AuditPollProvider>
          </PrincipalProvider>
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
  return user;
}

/** The Audit chain row of the open popover. */
function chainRow(): HTMLAnchorElement {
  const popover = screen.getByRole("group", { name: "System status" });
  return within(popover).getByText("Audit chain").closest("a") as HTMLAnchorElement;
}

describe("the system status chip's chain verdict (§1.3)", () => {
  it("a single disagreeing read is RE-CHECKED, never announced as a break", async () => {
    // One read, and the latch has not promoted it. The chip may not say
    // "Operational" — nothing has confirmed this chain — and may not say
    // "Chain break" either, which is the accusation the latch withholds until
    // the verdict agrees with itself.
    respondPolled([BROKEN]);
    const user = renderChip(ONE_POLL_MS);

    const chip = await screen.findByRole("button", { name: "Re-checking the audit chain" });
    // Not the fault colour: an unconfirmed disagreement is not yet a fault, and
    // spending the alarm on it is what made the indicator worth ignoring.
    expect(chip.className).not.toContain("ne-status__chip--fault");
    expect(screen.queryByRole("button", { name: /Chain break/ })).toBeNull();

    await user.click(chip);
    // The same sentence /audit's strip rests on, so the two screens cannot
    // contradict each other while the reading is unconfirmed.
    expect(chainRow().textContent).toContain(
      "Checking again — this reading has not been confirmed yet",
    );
    // No confirmed break, so no entry to point at: a deep link at seq 4812
    // would assert the location of a break nothing has confirmed.
    expect(chainRow()).toHaveAttribute("href", "/audit");
    expect(chainRow().textContent).not.toContain("Break at entry");
  });

  it("a GENUINE break still reaches the chip, named at the entry the server gave", async () => {
    // Every read disagrees, so the latch promotes and the chip NAMES THE WORST
    // FACT. Withholding a confirmed break would be the opposite failure to the
    // one above, and a worse one.
    respondPolled([BROKEN]);
    const user = renderChip(POLL_MS);

    const chip = await screen.findByRole(
      "button",
      { name: "Chain break at entry 4812" },
      { timeout: LATCH_WAIT },
    );
    expect(chip.className).toContain("ne-status__chip--fault");

    await user.click(chip);
    expect(chainRow().textContent).toContain("Break at entry 4812");
    expect(chainRow()).toHaveAttribute("href", "/audit?seq=4812");
  });

  it("a passing read followed by a disagreeing one does not withdraw the break", async () => {
    // THE PAIR. The reducer's failing branch used to compute its verdict from
    // the CURRENT run alone, so one passing poll reset the run and the very
    // next disagreeing poll rendered at run 1 — as RE-VERIFYING. `latched`
    // stayed true and nothing renders `latched`, so the chip's fault colour,
    // the named entry and the `/audit?seq=` deep link all went with the word.
    // Under the reproduced ~50/50 race this pair happens within two polls, and
    // the run never recovers: every later passing read holds the withdrawn word
    // in place.
    const { polls } = respondPolled([
      ...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(BROKEN),
      VALID,
      BROKEN,
      VALID,
    ]);
    const user = renderChip(POLL_MS);

    const chip = await screen.findByRole(
      "button",
      { name: "Chain break at entry 4812" },
      { timeout: LATCH_WAIT },
    );
    await waitFor(() => expect(polls()).toBeGreaterThan(BREAK_CONFIRMATIONS + 4), {
      timeout: LATCH_WAIT,
    });

    expect(chip.textContent).toBe("Chain break at entry 4812");
    expect(chip.className).toContain("ne-status__chip--fault");
    expect(screen.queryByRole("button", { name: /Re-checking/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Operational/ })).toBeNull();

    await user.click(chip);
    expect(chainRow().textContent).toContain("Break at entry 4812");
    expect(chainRow()).toHaveAttribute("href", "/audit?seq=4812");
  });

  it("a FAILED poll does not withdraw the break — it stops adding, it subtracts nothing", async () => {
    // The verdict used to be forced to "unknown" whenever the poll was not
    // `ok`, and `verification` is nulled on that arm — so the chip resolved to
    // no word at all and fell through to "Audit unavailable", while /audit's
    // strip fell all the way back to its own passing read and said `● VALID`.
    // Between them, one 502 took a confirmed break off the whole console for
    // the duration of the error backoff.
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        polls += 1;
        if (polls <= BREAK_CONFIRMATIONS) {
          return HttpResponse.json({ events: AUDIT_EVENTS, verification: BROKEN });
        }
        return HttpResponse.json({ detail: "audit unavailable" }, { status: 502 });
      }),
    );
    const user = renderChip(POLL_MS);

    const chip = await screen.findByRole(
      "button",
      { name: "Chain break at entry 4812" },
      { timeout: LATCH_WAIT },
    );
    await waitFor(() => expect(polls).toBeGreaterThan(BREAK_CONFIRMATIONS), {
      timeout: LATCH_WAIT,
    });

    expect(chip.textContent).toBe("Chain break at entry 4812");
    expect(chip.className).toContain("ne-status__chip--fault");
    expect(screen.queryByRole("button", { name: "Audit unavailable" })).toBeNull();

    await user.click(chip);
    // …and the row states the break rather than the absence of the read. An
    // absence must not overwrite tamper-evidence the console already confirmed.
    expect(chainRow().textContent).toContain("Break at entry 4812");
    expect(chainRow().textContent).not.toContain("Unavailable");
    expect(chainRow()).toHaveAttribute("href", "/audit?seq=4812");
  });

  it("CONTROL: a failed poll with NOTHING confirmed still reads as unavailable", async () => {
    // The bidirectional half. The guard above must not be satisfied by a chip
    // that shouts BREAK at every failed read: with no confirmed observation
    // behind it, an unreachable audit service is exactly what it says it is.
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ detail: "audit unavailable" }, { status: 502 }),
      ),
    );
    const user = renderChip(POLL_MS);

    const chip = await screen.findByRole(
      "button",
      { name: "Audit unavailable" },
      { timeout: LATCH_WAIT },
    );
    expect(screen.queryByRole("button", { name: /Chain break/ })).toBeNull();

    await user.click(chip);
    expect(chainRow().textContent).toContain("Unavailable");
    expect(chainRow()).toHaveAttribute("href", "/audit");
  });

  it("a LATCHED break is not withdrawn by the passing reads that follow it", async () => {
    // Tamper-evidence, or a mood ring: a break that clears itself on the next
    // read is indistinguishable from the race. Only /audit's Re-verify may
    // clear it. The entry survives too — and it cannot come from the passing
    // read, which names none — so it is read off the confirmed observation.
    const { polls } = respondPolled([
      ...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(BROKEN),
      VALID,
    ]);
    const user = renderChip(POLL_MS);

    const chip = await screen.findByRole(
      "button",
      { name: "Chain break at entry 4812" },
      { timeout: LATCH_WAIT },
    );
    await waitFor(() => expect(polls()).toBeGreaterThan(BREAK_CONFIRMATIONS + 2), {
      timeout: LATCH_WAIT,
    });

    expect(chip.textContent).toBe("Chain break at entry 4812");
    expect(chip.className).toContain("ne-status__chip--fault");
    expect(screen.queryByRole("button", { name: /Operational/ })).toBeNull();

    await user.click(chip);
    expect(chainRow().textContent).toContain("Break at entry 4812");
    expect(chainRow()).toHaveAttribute("href", "/audit?seq=4812");
  });

  it("a read that NAMES NO ENTRY does not de-locate the confirmed break", async () => {
    // The other read that names no entry — DEFENSIVE, unlike the passing read
    // the test above covers. It was called "the race's OWN answer"; `_break`
    // (verify.py:180) takes `seq: int`, so an invalid verdict with a null
    // `first_break_seq` is not a wire the server emits. Pinned because the
    // field is `int | None` all the way to this type. Were one to arrive after
    // the break is confirmed, the reducer would mark it `confirmed` too and
    // "the newest confirmed observation" would be one with no entry in it: the
    // chip's headline falls back to a bare "Chain break", the row loses the
    // entry, and the deep link degrades from `/audit?seq=4812` to `/audit` —
    // the operator's one route to the evidence, withdrawn by a poll that
    // CONFIRMED the break again.
    const { polls } = respondPolled([
      ...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(BROKEN),
      {
        valid: false,
        basis: "full-chain",
        contradiction: false,
        checked: 4812,
        first_break_seq: null,
        first_break_event_id: null,
        reason: "head mismatch (truncated or forged tail)",
        detail: "chain broken",
      },
    ]);
    const user = renderChip(POLL_MS);

    const chip = await screen.findByRole(
      "button",
      { name: "Chain break at entry 4812" },
      { timeout: LATCH_WAIT },
    );
    await waitFor(() => expect(polls()).toBeGreaterThan(BREAK_CONFIRMATIONS + 2), {
      timeout: LATCH_WAIT,
    });

    expect(chip.textContent).toBe("Chain break at entry 4812");
    expect(chip.className).toContain("ne-status__chip--fault");

    await user.click(chip);
    expect(chainRow().textContent).toContain("Break at entry 4812");
    expect(chainRow().textContent).not.toContain("did not name the entry");
    expect(chainRow()).toHaveAttribute("href", "/audit?seq=4812");
  });

  it("a CONTRADICTION reaches this chip with no entry to name — the arm is real", async () => {
    // THE SEQ-LESS BREAK ARM, DRIVEN BY A SHAPE THE SERVER ACTUALLY SENDS. The
    // test above it is deliberately DEFENSIVE (an ordinary break with a null
    // seq is not a wire `_break` can emit); this one is not. A CONTRADICTION is
    // `valid: false` carrying a PASSING `verify_chain` underneath it — the
    // ledger is internally consistent, it simply is not the ledger this process
    // verified — so it has no `first_break_seq` to give, and it latches on
    // SIGHT rather than after BREAK_CONFIRMATIONS, because the server emits it
    // exactly once before re-anchoring.
    //
    // The chip's comment called this arm "unreachable on today's wire". It is
    // reachable, on the FIRST poll, and this is what says so.
    const { polls } = respondPolled([
      {
        valid: false,
        basis: "full-chain",
        contradiction: true,
        checked: 4812,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail: "chain does not extend the head verified at seq 4800",
      },
      VALID,
    ]);
    const user = renderChip(POLL_MS);

    // ONE read, not three: the flap suppression does not apply to a verdict
    // that by construction never flaps.
    const chip = await screen.findByRole(
      "button",
      { name: "Chain break" },
      { timeout: LATCH_WAIT },
    );
    expect(chip.className).toContain("ne-status__chip--fault");

    // …and the passing reads that follow it — the server re-anchored — do not
    // take it back.
    await waitFor(() => expect(polls()).toBeGreaterThan(BREAK_CONFIRMATIONS + 2), {
      timeout: LATCH_WAIT,
    });
    expect(chip.textContent).toBe("Chain break");

    await user.click(chip);
    // The arm's own sentence: it says there is no entry rather than printing
    // "Break at entry —", and the deep link degrades to the ledger itself.
    expect(chainRow().textContent).toContain("did not name the entry");
    expect(chainRow().textContent).not.toContain("Break at entry");
    expect(chainRow()).toHaveAttribute("href", "/audit");
  });
});
