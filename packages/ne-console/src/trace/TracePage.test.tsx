// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §13b — the trace drill-down, as rendered.
//
// The two things this screen exists to prove: it renders the SERVER's rollup,
// aggregates and verdict verbatim (the impossible fixture — a run total that
// contradicts the aggregates beside it — must still render both as sent), and
// it reads as a run rather than as an emitter's vocabulary.
//
// And the three things that left with the drill-down ruling: the dispatch
// trigger, the recent-runs list and the paste-an-id form. A trace is opened
// about something.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import type {
  ChainVerificationView,
  PrincipalView,
  TraceAssemblyView,
  TraceNodeView,
} from "../api/types";
import { BREAK_CONFIRMATIONS } from "../audit/chainVerdict";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { StatusChip } from "../rail/StatusChip";
import { AuthStub } from "../test/authStub";
import { AUDIT_EVENTS, PRINCIPAL_APPROVER } from "../test/mocks/fixtures";
import {
  TRACE_ASSEMBLY,
  TRACE_ASSEMBLY_BROKEN,
  TRACE_ASSEMBLY_GROUNDED,
  TRACE_ASSEMBLY_IMPOSSIBLE,
  TRACE_ASSEMBLY_OLD_WIRE,
  TRACE_ID,
} from "../test/mocks/traceFixtures";
import { server } from "../test/mocks/server";
import { TracePage } from "./TracePage";

function serve(view: TraceAssemblyView) {
  server.use(http.get("*/api/v1/trace/:traceId", () => HttpResponse.json(view)));
}

function serveStatus(status: number) {
  server.use(
    http.get("*/api/v1/trace/:traceId", () => HttpResponse.json({ detail: "no" }, { status })),
  );
}

/**
 * A SEQUENCE of trace reads, one per request — the shape the chain-verdict
 * latch exists for. The server has a reproduced read-during-append race in
 * which roughly half of the reads taken while the chain is being appended to
 * come back invalid, so "what this screen shows" is a question about a run of
 * reads and cannot be asked of a single fixture. The last entry repeats once
 * the list runs out, and the call count is returned so a test can pin how many
 * re-reads a verdict actually cost.
 */
function serveSequence(views: readonly TraceAssemblyView[]): {
  calls: () => number;
  /** Milliseconds between consecutive reads — the SPACING the confirmations
   * rest on. Three reads that all land inside one append window confirm a break
   * that is not there, so how far apart they were taken is a fact about the
   * verdict, not about the test. */
  gaps: () => number[];
} {
  let n = 0;
  const at: number[] = [];
  server.use(
    http.get("*/api/v1/trace/:traceId", () => {
      const view = views[Math.min(n, views.length - 1)]!;
      n += 1;
      at.push(Date.now());
      return HttpResponse.json(view);
    }),
  );
  return {
    calls: () => n,
    gaps: () => at.slice(1).map((t, i) => t - at[i]!),
  };
}

/**
 * ONE READ, SWITCHABLE MID-TEST — for the questions a positional list cannot
 * ask any more.
 *
 * This screen folds into the SHELL's session-bound latch, so a read taken
 * before /v1/me resolves is not bound to a session and is taken again under the
 * one that can hold it. A fixture list indexed by REQUEST NUMBER therefore no
 * longer lines up with the OBSERVATIONS the verdict is made of. A test that
 * wants "and now the server starts passing" says exactly that, at the point the
 * screen has visibly reached the state the test is about.
 */
function serveSwitchable(initial: TraceAssemblyView): {
  calls: () => number;
  serveNow: (view: TraceAssemblyView) => void;
} {
  let n = 0;
  let current = initial;
  server.use(
    http.get("*/api/v1/trace/:traceId", () => {
      n += 1;
      return HttpResponse.json(current);
    }),
  );
  return {
    calls: () => n,
    serveNow: (view) => {
      current = view;
    },
  };
}

/** /v1/me, HELD until the test lets it answer — the cold deep link where the
 * session resolves after the screen has already read the run several times. */
function deferredMe(principal: PrincipalView): { resolve: () => void } {
  let release = () => {};
  const held = new Promise<void>((ok) => {
    release = ok;
  });
  server.use(
    http.get("*/api/v1/me", async () => {
      await held;
      return HttpResponse.json(principal);
    }),
  );
  return { resolve: () => release() };
}

/** Long enough for BREAK_CONFIRMATIONS reads spaced by the screen's re-verify
 * pause. Generous on purpose: a shared box under load must not manufacture a
 * failure out of a timer this test is not about. */
const LATCH_WAIT = 5000;

/**
 * THE SHELL'S AUDIT POLL, SILENCED — so a question about /trace's OWN reads is
 * a question about /trace's own reads.
 *
 * /trace no longer keeps a private latch: it folds its read into the ONE latch
 * the shell owns (hooks/useAuditPoll.tsx) and renders that. The provider must
 * therefore be mounted for any test about the verdict — it is in the app too,
 * on every route (App.tsx: every route is a child of AuthedLayout). But the
 * poll reads the SAME chain, so its verdicts land in the same run: left on the
 * default fixture it would fold a passing read at some unpredictable instant
 * and reset a run this file is counting. A poll that cannot read contributes
 * nothing — and a latch that survives that failed poll is itself the property
 * P1-3 is about.
 */
function silenceAuditPoll(): void {
  server.use(
    http.get("*/api/v1/audit/events", () =>
      HttpResponse.json({ detail: "audit unavailable" }, { status: 502 }),
    ),
  );
}

/** The poll's verdicts, consumed one per read, the last entry repeating — for
 * the tests that need the SHELL to be the observer rather than this screen. */
function pollVerdicts(verdicts: readonly ChainVerificationView[]): void {
  let n = 0;
  server.use(
    http.get("*/api/v1/audit/events", () => {
      const verification = verdicts[Math.min(n, verdicts.length - 1)]!;
      n += 1;
      return HttpResponse.json({ events: AUDIT_EVENTS, verification });
    }),
  );
}

/** Real timers and a small interval, per the AuditPage precedent: MSW and fake
 * timers interact badly. Only the tests that want the SHELL to observe use it. */
const POLL_MS = 15;

/** Long enough that exactly one poll is taken while a test about /trace's own
 * reads runs — and it is a 502, so it folds nothing. */
const NO_POLL_MS = 60_000;

function setup(path = `/trace/${TRACE_ID}`, intervalMs = NO_POLL_MS) {
  const user = userEvent.setup();
  if (intervalMs === NO_POLL_MS) silenceAuditPoll();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[path]}>
        <PrincipalProvider>
          <AuditPollProvider intervalMs={intervalMs}>
            <Routes>
              <Route path="/trace" element={<TracePage />} />
              <Route path="/trace/:traceId" element={<TracePage />} />
              <Route path="/audit" element={<h1>Audit log</h1>} />
            </Routes>
          </AuditPollProvider>
        </PrincipalProvider>
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/**
 * THE RAIL CHIP AND THE RUN, MOUNTED TOGETHER, UNDER ONE POLL — the arrangement
 * the app actually renders (/trace/:traceId is an outlet of the layout that
 * carries <StatusChip/>, both inside AuditPollProvider) and the ONLY arrangement
 * in which the two can be caught disagreeing.
 *
 * Rendering them one after another with `cleanup()` between, each from a fresh
 * latch, is structurally incapable of observing divergence: that is exactly how
 * two independent latches shipped.
 */
function setupWithChip(intervalMs = NO_POLL_MS) {
  const user = userEvent.setup();
  if (intervalMs === NO_POLL_MS) silenceAuditPoll();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={[`/trace/${TRACE_ID}`]}>
        <PrincipalProvider>
          <AuditPollProvider intervalMs={intervalMs}>
            <StatusChip />
            <Routes>
              <Route path="/trace/:traceId" element={<TracePage />} />
            </Routes>
          </AuditPollProvider>
        </PrincipalProvider>
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/**
 * THE READS STOPPED — a resting verdict is one this screen is no longer paying
 * for.
 *
 * It is no longer an exact count. This screen folds into the SHELL's latch,
 * which is session-bound, so a read that lands before /v1/me resolves cannot be
 * bound to a session and is taken again under the one that can hold it. How
 * many reads a verdict costs therefore depends on when the principal resolved —
 * which is a fact about the shell's startup, not about the verdict. What the
 * assertion is actually for survives verbatim: a confirmed break is a RESTING
 * state, not a screen that keeps hammering the audit service.
 */
async function readsSettle(calls: () => number): Promise<number> {
  let seen = calls();
  for (let i = 0; i < 6; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    const now = calls();
    if (now === seen) return seen;
    seen = now;
  }
  throw new Error(`reads never settled: still climbing at ${seen}`);
}

/** The revealed scope paragraph, or null when the strip offers no disclosure. */
function reachParagraph(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".ne-trace__reach");
}

/** The word on /trace's verdict strip, and the rail chip's headline. */
function stripWord(): string {
  return document.querySelector(".ne-trace__verdict .ne-pill__word")?.textContent ?? "none";
}

function chipHeadline(): string {
  const chip = document.querySelector(".ne-status__chip");
  return chip?.textContent ?? "none";
}

/** A stat tile's value, by its label. Scoped to the tile row: "Cost" is also a
 * column header, and a helper that cannot tell them apart proves nothing. */
function tile(label: string): string {
  const tiles = document.querySelector(".ne-trace__tiles");
  const found = [...(tiles?.querySelectorAll(".ne-trace__tile") ?? [])].find(
    (t) => t.querySelector(".ne-trace__tilelabel")?.textContent === label,
  );
  return found?.querySelector(".ne-trace__tilevalue")?.textContent ?? "";
}

/** The models table, row by row. */
function modelRows(): string[][] {
  const table = screen.getByRole("table", { name: "Models" });
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((r) => [...r.querySelectorAll("td")].map((c) => c.textContent?.trim() ?? ""));
}

/** The span rows, in render order, as "<kind> <name>". */
function spanRows(): string[] {
  return screen
    .getAllByRole("treeitem")
    .map(
      (r) =>
        `${r.querySelector(".ne-trace__spankind")?.textContent ?? ""} ${
          r.querySelector(".ne-trace__spanname")?.textContent ?? ""
        }`,
    );
}

function timelineFact(eventId: string, key: string): string | null {
  return (
    document
      .getElementById(`trace-event-${eventId}`)
      ?.querySelector(`[data-fact="${key}"]`)
      ?.textContent?.replace(/\s+/g, " ")
      .trim() ?? null
  );
}

describe("§13b Trace", () => {
  it("renders the rollup as three tiles and the server's verification line", async () => {
    serve({
      ...TRACE_ASSEMBLY,
      rollup: { ...TRACE_ASSEMBLY.rollup, total_events: 16 },
      verification: { ...TRACE_ASSEMBLY.verification, checked: 15_800 },
    });
    setup();
    expect(await screen.findByText("Events")).toBeInTheDocument();

    expect(tile("Events")).toBe("16");
    expect(tile("Duration")).toBe("8.0 s");
    expect(tile("Cost")).toBe("$0.02");
    expect(screen.getByText("Entire audit chain · 15,800 records verified")).toBeInTheDocument();
    expect(screen.queryByText("15,800 records verified")).toBeNull();
    expect(screen.getByText("VALID")).toBeInTheDocument();
    // Ask's provenance chip deep-link lands on the exact server event id.
    expect(document.getElementById("trace-event-evt-t-pdp")).toHaveAttribute("role", "treeitem");
    // The money format is two decimals, so the exact served figure rides in the
    // title — a sub-cent cost is rounded on screen but never lost.
    // Both the run tile and the model row that produced it carry it.
    expect(screen.getAllByTitle("0.018 USD")).toHaveLength(2);
  });

  it("renders a null run cost as Unpriced, never $0.00", async () => {
    serve({ ...TRACE_ASSEMBLY, rollup: { ...TRACE_ASSEMBLY.rollup, total_cost_usd: null } });
    setup();
    expect(await screen.findByText("Events")).toBeInTheDocument();
    expect(tile("Cost")).toBe("Unpriced");
  });

  it("renders the aggregates in the server's order, with sortable headers", async () => {
    const user = setup();
    expect(await screen.findByRole("table", { name: "Models" })).toBeInTheDocument();

    // Served order, unsorted until a header is clicked.
    expect(modelRows().map((r) => r[0])).toEqual(["qwen3-local", "claude-fable-5"]);
    const header = within(screen.getByRole("table", { name: "Models" })).getByRole("columnheader", {
      name: /Calls/,
    });
    expect(header).not.toHaveAttribute("aria-sort");

    await user.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(modelRows().map((r) => r[0])).toEqual(["qwen3-local", "claude-fable-5"]);
    await user.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "ascending");
    expect(modelRows().map((r) => r[0])).toEqual(["claude-fable-5", "qwen3-local"]);
  });

  it("renders a token total the server could not vouch for as absent, never zero", async () => {
    setup();
    expect(await screen.findByRole("table", { name: "Models" })).toBeInTheDocument();
    const frontier = modelRows().find((r) => r[0] === "claude-fable-5");
    // tasks, calls, input, output, cost — this fixture's aggregates name no
    // task (the em dash), the 0/0 totals are em dashes, the cost is real.
    expect(frontier?.slice(2)).toEqual(["—", "1", "—", "—", "$0.02"]);
    const local = modelRows().find((r) => r[0] === "qwen3-local");
    expect(local?.slice(2)).toEqual(["—", "3", "2400", "800", "Unpriced"]);
  });

  it("renders the served rollup even when the aggregates beside it disagree", async () => {
    // The console does not sum, reconcile or correct: a run total that
    // contradicts its own aggregates is the server's claim, rendered as sent.
    serve(TRACE_ASSEMBLY_IMPOSSIBLE);
    setup();
    expect(await screen.findByText("Events")).toBeInTheDocument();
    expect(tile("Cost")).toBe("$999.99");
    expect(screen.getByText("VALID")).toBeInTheDocument();
  });

  it("renders a break as the server states it, at the server's entry", async () => {
    // Every read disagrees, so the latch promotes and the break STANDS.
    serve(TRACE_ASSEMBLY_BROKEN);
    setup();
    expect(
      await screen.findByText("Entire audit chain · Break at entry 5", {}, { timeout: LATCH_WAIT }),
    ).toBeInTheDocument();
    expect(screen.getByText("BREAK")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("record_hash mismatch");
  });

  it("a break with NO seq says the server named no entry — never a trailing dash", async () => {
    // DEFENSIVE. `_break` (verify.py:180) takes `seq: int`, so this body is one
    // `assemble_trace` cannot produce — see `locates` in audit/chainVerdict.ts.
    // Pinned because `first_break_seq` is `int | None` on TraceVerificationView
    // (trace_assembly.py:360): if it ever arrives, the strip must not print a
    // sentence that lost its number.
    serve({
      ...TRACE_ASSEMBLY,
      verification: {
        valid: false,
        basis: "full-chain",
        contradiction: false,
        checked: 7,
        first_break_seq: null,
        first_break_event_id: null,
        reason: "head mismatch",
        detail: "the stored head does not match the recomputed chain",
      },
    });
    setup();
    expect(
      await screen.findByText(
        "Entire audit chain · Break confirmed — the server did not name the entry",
        {},
        { timeout: LATCH_WAIT },
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Entire audit chain · Break at entry —")).toBeNull();
  });

  // -------------------------------------------------------------------------
  // THE CHAIN-VERDICT LATCH ON /trace (audit/chainVerdict.ts).
  //
  // The defect these pin: this screen rendered `verification.valid` from ONE
  // response, so a run opened during the server's reproduced read-during-append
  // race flashed `⚠ BREAK` — and the server's word "forged" — over a chain
  // nothing was wrong with. The fix must cut BOTH ways: a single disagreeing
  // read may not reach the strip as BREAK, and a chain that really is broken
  // must still say so and keep saying so.
  // -------------------------------------------------------------------------

  it("a SINGLE transient false verdict never reaches the strip as BREAK", async () => {
    // Read 1 disagrees, read 2 passes — exactly the race, not a broken chain.
    const { calls } = serveSequence([TRACE_ASSEMBLY_BROKEN, TRACE_ASSEMBLY]);
    setup();

    // The honest third state while the server has disagreed with itself: not
    // VALID (nothing has been confirmed) and not BREAK (nothing has agreed).
    expect(await screen.findByText("RE-VERIFYING")).toBeInTheDocument();
    expect(screen.queryByText("BREAK")).toBeNull();
    // And no alert: the server's "record_hash mismatch" sentence is not
    // shouted over a reading the very next read withdraws.
    expect(screen.queryByRole("alert")).toBeNull();

    expect(await screen.findByText("VALID", {}, { timeout: LATCH_WAIT })).toBeInTheDocument();
    expect(screen.getByText("Entire audit chain · 42 records verified")).toBeInTheDocument();
    expect(screen.queryByText("BREAK")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    // It re-read rather than believing the first answer, and stopped as soon
    // as a read agreed — never a poll loop left running on a drill-down.
    expect(await readsSettle(calls)).toBeLessThanOrEqual(3);
  });

  it("a GENUINE break still latches: every read disagrees, so BREAK stands", async () => {
    const { calls } = serveSequence([TRACE_ASSEMBLY_BROKEN]);
    setup();

    expect(await screen.findByText("BREAK", {}, { timeout: LATCH_WAIT })).toBeInTheDocument();
    expect(screen.getByText("Entire audit chain · Break at entry 5")).toBeInTheDocument();
    // The server's own sentence, now that the verdict has agreed with itself.
    expect(screen.getByRole("alert")).toHaveTextContent("record_hash mismatch");
    expect(screen.queryByText("RE-VERIFYING")).toBeNull();
    // It cost at least BREAK_CONFIRMATIONS reads — nothing promotes on fewer —
    // and then it STOPS: a confirmed break is a resting state, not a screen
    // that keeps hammering the audit service.
    const settled = await readsSettle(calls);
    expect(settled).toBeGreaterThanOrEqual(BREAK_CONFIRMATIONS);
    expect(settled).toBeLessThanOrEqual(BREAK_CONFIRMATIONS + 1);
  });

  it("a run interrupted by a passing read is not a break — the count is consecutive AGREEMENT", async () => {
    // The server disagrees, the screen starts counting, and then the server
    // passes BEFORE the run reaches BREAK_CONFIRMATIONS. Nothing latches, and
    // nothing on the way there was ever allowed to say BREAK.
    const { calls, serveNow } = serveSwitchable(TRACE_ASSEMBLY_BROKEN);
    setup();

    await screen.findByText("RE-VERIFYING", {}, { timeout: LATCH_WAIT });
    expect(screen.queryByText("BREAK")).toBeNull();
    serveNow(TRACE_ASSEMBLY);

    expect(await screen.findByText("VALID", {}, { timeout: LATCH_WAIT })).toBeInTheDocument();
    expect(screen.queryByText("BREAK")).toBeNull();
    // …and it stopped asking as soon as a read agreed.
    await readsSettle(calls);
    expect(screen.queryByText("BREAK")).toBeNull();
  });

  it("the read that can promote a break is spaced further out than the one before it", async () => {
    // THE COUNT IS ONLY WORTH THE READS' INDEPENDENCE. Nothing bounds how wide
    // the server's append window is, so three reads taken 400ms apart can all
    // straddle ONE append and confirm a break over an intact chain — and on a
    // drill-down that verdict then rests for the whole visit. The re-reads
    // therefore widen: the third read, the only one that can promote, is taken
    // materially later than the second. A flat delay fails this.
    const { gaps } = serveSequence([TRACE_ASSEMBLY_BROKEN]);
    setup();
    await screen.findByText("BREAK", {}, { timeout: LATCH_WAIT });

    // The LAST two gaps: an unbound first read (taken before /v1/me resolved,
    // and retaken under the session that can hold it) is not one of the spaced
    // re-reads and its gap is not what this test is about.
    const [first, second] = gaps().slice(-2);
    // Lower bounds only: a timer fires late under load, never early, so these
    // cannot flake the way an upper bound would.
    expect(first).toBeGreaterThanOrEqual(300);
    expect(second).toBeGreaterThanOrEqual(1200);
  });

  it("the promoting read is spaced when the SHELL'S POLL opened the run, too", async () => {
    // THE SPACING WAS THIS SCREEN'S FOLD COUNT; PROMOTION IS THE SHARED RUN'S.
    //
    // The shell's poll reads the SAME chain and lands in the SAME run — that is
    // the whole point of one latch — so a single disagreeing poll read puts the
    // run at 1 before this screen has folded anything. This screen's first fold
    // then takes it to 2, one short of BREAK_CONFIRMATIONS, and the exponent
    // that decides how long to wait counted THIS SCREEN'S folds: one, so the
    // quick 400ms. The PROMOTING read was therefore taken 400ms after the one
    // before it — inside the per-request window the read-during-append race
    // lives in, which is exactly the independence the count is worth nothing
    // without. Three reads that all straddle one append then latch a GLOBAL
    // BREAK, on every screen, until someone clicks Re-verify.
    const at: number[] = [];
    let pollSeen = () => {};
    const polled = new Promise<void>((ok) => {
      pollSeen = ok;
    });
    server.use(
      http.get("*/api/v1/audit/events", () => {
        pollSeen();
        return HttpResponse.json({
          events: AUDIT_EVENTS,
          verification: {
            valid: false,
            basis: "full-chain",
            contradiction: false,
            checked: 4812,
            first_break_seq: 4812,
            first_break_event_id: "evt-4812",
            reason: "head mismatch (truncated or forged tail)",
            detail: "chain broken",
          } satisfies ChainVerificationView,
        });
      }),
      // Held until the poll's disagreeing read is in the run, so this test is
      // about the SPACING and not about which reader got there first.
      http.get("*/api/v1/trace/:traceId", async () => {
        await polled;
        await new Promise((ok) => setTimeout(ok, 30));
        at.push(Date.now());
        return HttpResponse.json(TRACE_ASSEMBLY_BROKEN);
      }),
    );
    // A poll interval long enough that the shell reads ONCE: one disagreeing
    // read, folded, and then nothing more from it for the rest of the test.
    setup(`/trace/${TRACE_ID}`, 30_000);

    await screen.findByText("BREAK", {}, { timeout: LATCH_WAIT });
    const gaps = at.slice(1).map((t, i) => t - at[i]!);
    // Lower bound only: a timer fires late under load, never early.
    expect(gaps.at(-1)).toBeGreaterThanOrEqual(1200);
  });

  it("a slow /v1/me does not spend the re-verify budget on reads the latch discards", async () => {
    // THE BUDGET COUNTS FOLDS, NOT ATTEMPTS. `observe` discards a read taken
    // before /v1/me resolves — the latch is session-bound, so a fold before the
    // session exists is dropped by the next render anyway — and this screen was
    // incrementing on every attempt. A cold deep link where /v1/me took over two
    // seconds therefore burned the reads at ~0s, 0.4s and 2.0s on observations
    // nothing could count, disabled the effect, and left the ONE read taken
    // under the resolved session standing alone. For this principal there is no
    // second reader: the entitlements say no `ops`, so the shell poll is never
    // made. The strip read "Checking again — this reading has not been confirmed
    // yet" for the whole visit, over a chain the server called broken on every
    // read, recoverable only by three manual Re-verify clicks.
    const me = deferredMe(PRINCIPAL_APPROVER);
    const { calls } = serveSequence([TRACE_ASSEMBLY_BROKEN]);
    setup();

    // The screen is already stating the disagreement it cannot confirm yet.
    await screen.findByText("RE-VERIFYING", {}, { timeout: LATCH_WAIT });
    expect(screen.queryByText("BREAK")).toBeNull();

    // Past the old schedule (400ms, then 1600ms) with the session still in
    // flight: long enough for an attempt-counting budget to be gone.
    await new Promise((done) => setTimeout(done, 2500));
    // A read the latch cannot hold is not worth re-taking on a timer either —
    // the session resolving re-runs the read effect on its own.
    expect(calls()).toBeLessThanOrEqual(2);

    me.resolve();

    // Three folded reads, spaced as always, and the break stands.
    expect(await screen.findByText("BREAK", {}, { timeout: LATCH_WAIT })).toBeInTheDocument();
    expect(screen.getByText("Entire audit chain · Break at entry 5")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("record_hash mismatch");
    // AND THE BOUND SURVIVED: it did not become an unbounded re-read loop on
    // the way to fixing the budget.
    const settled = await readsSettle(calls);
    expect(settled).toBeGreaterThanOrEqual(BREAK_CONFIRMATIONS);
    expect(settled).toBeLessThanOrEqual(BREAK_CONFIRMATIONS + 2);
    // ⏱️ AN EXPLICIT TIMEOUT, BECAUSE THIS TEST SPENDS REAL TIME ON PURPOSE.
    // It waits 2.5 s of wall clock to prove nothing is re-read while the
    // session is in flight — that IS the assertion, so it cannot be faked away
    // without testing something else. With `LATCH_WAIT` on either side it sits
    // just under vitest's 5 s default, which is fine on a quiet box and times
    // out under a full-suite run: it passed targeted and failed in the suite,
    // which is the signature of a test racing its own budget rather than a
    // flaky one. Raising the ceiling for THIS test is honest; raising the
    // global default would hide the next one.
  }, 20_000);

  it("Re-verify withdraws a latch this screen got wrong", async () => {
    // A latch is a resting accusation, and this screen could raise one and never
    // withdraw it: BREAK stood for the whole visit, because the only Re-verify
    // control was on /audit and nothing here calls `clearLatch`. Three straddled
    // reads confirm a break the chain does not have; the sweep then passes.
    const { calls, serveNow } = serveSwitchable(TRACE_ASSEMBLY_BROKEN);
    const user = setup();
    await screen.findByText("BREAK", {}, { timeout: LATCH_WAIT });
    const before = await readsSettle(calls);
    // The chain the sweep will find: the three straddled reads are behind us.
    serveNow(TRACE_ASSEMBLY);

    await user.click(screen.getByRole("button", { name: "Re-verify" }));

    expect(await screen.findByText("VALID", {}, { timeout: LATCH_WAIT })).toBeInTheDocument();
    expect(screen.getByText("Entire audit chain · 42 records verified")).toBeInTheDocument();
    expect(screen.queryByText("BREAK")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    // ONE sweep read, not a re-entered confirmation loop.
    expect(await readsSettle(calls)).toBe(before + 1);
  });

  it("Re-verify CANNOT wish away a break the server still reports", async () => {
    // The other half, and the one that matters: the control is a re-read, not an
    // acknowledgement. `clearLatch` returns the latch untouched on a failing
    // verdict, so a genuine break re-confirms on the spot and keeps standing.
    const { calls } = serveSequence([TRACE_ASSEMBLY_BROKEN]);
    const user = setup();
    await screen.findByText("BREAK", {}, { timeout: LATCH_WAIT });
    const before = await readsSettle(calls);

    await user.click(screen.getByRole("button", { name: "Re-verify" }));
    await waitFor(() => expect(calls()).toBe(before + 1));

    expect(screen.getByText("BREAK")).toBeInTheDocument();
    expect(screen.getByText("Entire audit chain · Break at entry 5")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("record_hash mismatch");
    expect(screen.queryByText("VALID")).toBeNull();
  });

  // -------------------------------------------------------------------------
  // ONE CHAIN, ONE LATCH — /trace AND THE RAIL CHIP, MOUNTED TOGETHER.
  //
  // The defect these pin: /trace held a PRIVATE latch (`useState(INITIAL_LATCH)`)
  // while the rail chip held the shell's, and `/trace/:traceId` is an outlet of
  // the layout that renders <StatusChip/> — both inside AuditPollProvider. So
  // the two rendered SIDE BY SIDE, two inches apart, off two independent
  // reducers: the chip could name "Chain break at entry 4812" in the fault
  // colour over a strip reading `● VALID`, or the reverse, and each screen's
  // Re-verify cleared only its own. A reader cannot tell which one to believe,
  // and the honest one is not identifiable from the screen.
  //
  // Both directions are load-bearing, so both are asserted: a break THIS SCREEN
  // confirms must reach the chip, and a break the SHELL confirms must reach this
  // screen. Neither is observable in a test that renders the two sequentially
  // with `cleanup()` between them, each from a fresh latch — which is how two
  // latches shipped.
  // -------------------------------------------------------------------------

  // -------------------------------------------------------------------------
  // I6 END-TO-END — THE WEAKER POLL MAY NOT ERASE THE PROGRESS THE FULL WALK
  // IS MAKING.
  //
  // This is the arrangement the rule was written for, and it is this screen's:
  // /trace's `assemble_trace` walks the chain UNBOUNDED on every read, while
  // the shell's 5s poll answers off an anchor a full verify blessed up to
  // fifteen minutes ago. A prefix row tampered after that blessing is invisible
  // to the poll BY CONSTRUCTION and named by every trace read.
  //
  // Both fold into ONE run, so before this rule the sequence was: trace read
  // disagrees (run 1) → poll passes (run 0) → trace read disagrees (run 1) →
  // poll passes (run 0) … forever. BREAK_CONFIRMATIONS was never reached, the
  // break NEVER LATCHED, and `latchedBasis` — the fix that was supposed to stop
  // a weak proof withdrawing a strong one's finding — never had a finding to
  // defend. The strongest instrument in the product saw the tamper on every
  // single read and the weakest one erased the evidence of it.
  // -------------------------------------------------------------------------

  const ANCHORED_PASS: ChainVerificationView = {
    valid: true,
    basis: "anchored-extension",
    contradiction: false,
    checked: 12_480,
    first_break_seq: null,
    first_break_event_id: null,
    reason: null,
    detail: "chain extends verified anchor; the prefix is NOT re-checked by this call",
  };

  it("a full-walk break still LATCHES while the anchored poll keeps passing", async () => {
    // Every trace read is a full walk and names the same entry. Every poll read
    // passes, at 15ms, so it folds many times more often than this screen reads
    // — and it still cannot erase what the full walk is accumulating.
    serve(TRACE_ASSEMBLY_BROKEN);
    pollVerdicts([ANCHORED_PASS]);
    setupWithChip(POLL_MS);

    await waitFor(() => expect(stripWord()).toBe("BREAK"), { timeout: 10_000 });
    // The entry the FULL walk named, not the passing poll's silence.
    expect(screen.getByText("Entire audit chain · Break at entry 5")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("record_hash mismatch");
    // ONE latch: the chip two inches above says the same thing at the same
    // instant, off the same poll that has been passing throughout.
    expect(chipHeadline()).toBe("Chain break at entry 5");

    // AND THE POLL CANNOT TAKE IT BACK. It keeps passing for another second;
    // the word does not move.
    await new Promise((done) => setTimeout(done, 500));
    expect(stripWord()).toBe("BREAK");
  }, 20_000);

  it("CONTROL: a FULL-chain poll pass does reset the run — this is strength, not stubbornness", async () => {
    // The same interleaving with ONE field changed: the poll's proof is now a
    // real unbounded walk, so it CAN answer what the trace read accused. That
    // is the read-during-append race, the count must stay consecutive, and
    // nothing may latch. If this went BREAK too, the test above would be
    // proving only that three reads eventually promote.
    serve(TRACE_ASSEMBLY_BROKEN);
    pollVerdicts([{ ...ANCHORED_PASS, basis: "full-chain", detail: "chain intact" }]);
    setupWithChip(POLL_MS);

    await waitFor(() => expect(stripWord()).toBe("VALID"), { timeout: LATCH_WAIT });
    await new Promise((done) => setTimeout(done, 1500));
    expect(stripWord()).toBe("VALID");
    expect(screen.queryByRole("alert")).toBeNull();
  }, 20_000);

  it("a break THIS SCREEN confirms is on the rail chip at the same instant", async () => {
    // The shell's poll cannot read the chain at all here (502), so this screen's
    // reads are the only observations there are — and they still reach the chip,
    // because there is one latch and this screen folds into it.
    serve(TRACE_ASSEMBLY_BROKEN);
    setupWithChip();

    await screen.findByText("BREAK", {}, { timeout: LATCH_WAIT });
    expect(stripWord()).toBe("BREAK");
    // Not "Audit unavailable": a confirmed break outranks a failed read.
    expect(chipHeadline()).toBe("Chain break at entry 5");
    expect(document.querySelector(".ne-status__chip")?.className).toContain(
      "ne-status__chip--fault",
    );
  });

  it("a break the SHELL confirms is on this screen — the run is never the last to know", async () => {
    // The other direction, and the one a private latch got wrong in the way that
    // hides evidence: every trace read here PASSES, so a screen with its own
    // latch rests on `● VALID` while the chip beside it names a confirmed break.
    serve(TRACE_ASSEMBLY);
    pollVerdicts([
      {
        valid: false,
        basis: "full-chain",
        contradiction: false,
        checked: 4812,
        first_break_seq: 4812,
        first_break_event_id: "evt-4812",
        reason: "head mismatch (truncated or forged tail)",
        detail: "chain broken",
      },
    ]);
    setupWithChip(POLL_MS);

    await waitFor(() => expect(chipHeadline()).toBe("Chain break at entry 4812"), {
      timeout: LATCH_WAIT,
    });
    expect(stripWord()).toBe("BREAK");
    expect(screen.getByText("Entire audit chain · Break at entry 4812")).toBeInTheDocument();
    // …and the strip does NOT print a record count off a passing read while the
    // chain stands confirmed broken.
    expect(screen.queryByText("Entire audit chain · 42 records verified")).toBeNull();
    // The red alert states the STANDING break's sentence. It printed the
    // sentence of whatever read THIS screen held, so a latch resting on a
    // passing trace read published that read's `detail` — "chain intact" — in
    // red, underneath the BREAK pill.
    expect(screen.getByRole("alert")).toHaveTextContent("head mismatch (truncated or forged tail)");
    expect(screen.queryByText("chain intact")).toBeNull();
  });

  it("the chip's word and this screen's are ONE word, even when their reads disagree", async () => {
    // THE ARM THE LATCH DOES NOT COVER, and the last place two screens could
    // come to rest on different words about one chain.
    //
    // A confirmed break and a re-verifying run are decided from the shared
    // latch, so they agree everywhere by construction. The REMAINING arm —
    // nothing latched, nothing being re-checked — is decided by THE READ IN
    // HAND, and this screen passed its own trace read while <StatusChip/>,
    // rendered by the layout this route is an outlet of, passed the poll's.
    //
    // So: the poll passes on every read, and this screen's own read disagrees
    // on every read. The disagreement IS folded into the shared latch (it
    // counts toward promotion, and this screen re-reads on it), but the poll's
    // passing reads keep resetting the run and this screen stops re-reading
    // after BREAK_CONFIRMATIONS reads per visit. Nothing is left to settle it:
    // `● VALID` on the chip, `RE-VERIFYING` on the strip two inches below it,
    // permanently, about one chain.
    const { calls } = serveSequence([TRACE_ASSEMBLY_BROKEN]);
    pollVerdicts([
      {
        valid: true,
        basis: "full-chain",
        contradiction: false,
        checked: 4812,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail: "chain intact",
      },
    ]);
    setupWithChip(POLL_MS);

    await screen.findByText("Events", {}, { timeout: LATCH_WAIT });
    // This screen has spent its re-reads, and the poll has come to rest passing.
    await readsSettle(calls);
    await waitFor(() => expect(chipHeadline()).toBe("Operational"), { timeout: LATCH_WAIT });

    expect(stripWord()).toBe("VALID");
    expect(screen.queryByText("RE-VERIFYING")).toBeNull();
    // And the sentence is the SAME read's as the word — never a record count
    // taken off the read that disagreed with it.
    expect(screen.getByText("Entire audit chain · 4812 records verified")).toBeInTheDocument();
    expect(screen.queryByText("Entire audit chain · 7 records verified")).toBeNull();
  });

  it("a served zero cost reads Unpriced AND its hover states no zero-dollar figure", async () => {
    serve({ ...TRACE_ASSEMBLY, rollup: { ...TRACE_ASSEMBLY.rollup, total_cost_usd: 0 } });
    setup();
    expect(await screen.findByText("Events")).toBeInTheDocument();
    expect(tile("Cost")).toBe("Unpriced");
    expect(screen.queryByTitle("0 USD")).toBeNull();
  });

  it("CONTROL: a real cost still carries the exact served figure in its hover", async () => {
    setup();
    expect(await screen.findByText("Events")).toBeInTheDocument();
    expect(screen.getAllByTitle("0.018 USD").length).toBeGreaterThan(0);
  });

  it("names each span in words, in the run's own order — no emitter codes", async () => {
    setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    expect(spanRows()).toEqual([
      "Policy decision pdp.decide",
      // A model-call row names the TASK it served beside the model.
      "Model call qwen3-localchat.answer",
      "Sub-agent reason-lane-analyst",
      "Routing subagent.route",
      "Knowledge knowledge.retrieve",
      "Redaction redact.pre_egress",
      "Model call claude-fable-5ap.adjudicate",
      "Approval approval.decide",
      "Tool execution tool.execute",
      "Guardrail guardrail.check",
      "Event future.unknown_action",
    ]);
    // The three-letter emitter codes this screen used to make a reader learn.
    for (const code of ["AGT", "RTE", "MDL", "RED", "KNW", "PDP", "APR", "GRD", "RAW"]) {
      expect(screen.queryByText(code)).toBeNull();
    }
  });

  it("surfaces typed governance facts on the settled timeline without synthetic progress", async () => {
    setup();
    const tree = await screen.findByRole("tree", { name: "Spans" });

    expect(timelineFact("evt-t-pdp", "pdp-effect")).toBe("Policy · allow");
    expect(timelineFact("evt-t-pdp", "pdp-tier")).toBe("Tier · 0");
    expect(timelineFact("evt-t-pdp", "pdp-action")).toBe("Action · agent.run");
    expect(timelineFact("evt-t-knowledge", "knowledge-candidates")).toBe("Candidates · 14");
    expect(timelineFact("evt-t-knowledge", "knowledge-returned")).toBe("Returned · 6");
    expect(timelineFact("evt-t-knowledge", "knowledge-withheld")).toBe("Permission withheld · 8");
    expect(timelineFact("evt-t-redaction", "redaction-count")).toBe("Redacted · 3");
    expect(timelineFact("evt-t-redaction", "redaction-categories")).toBe(
      "Categories · EMAIL_ADDRESS 2 · US_SSN 1",
    );
    expect(timelineFact("evt-t-approval", "approval-decision")).toBe("Approval · approved");
    expect(timelineFact("evt-t-approval", "approval-id")).toBe("Request · apr-1");
    expect(timelineFact("evt-t-tool", "tool-approval")).toBe("Approval · apr-1");
    expect(timelineFact("evt-t-guardrail", "guardrail")).toBe("Guardrail · spend-cap ok");

    expect(tree.querySelector(".ne-trace__track")).toBeNull();
    expect(tree.textContent).not.toMatch(/estimated|progress|remaining/i);
  });

  it("stacks the timeline detail pane and fact rows without motion at narrow widths", () => {
    const css = readFileSync(resolve(__dirname, "trace.css"), "utf8");
    const tablet = css.slice(css.indexOf("@media (max-width: 64rem)"));
    const mobile = css.slice(css.indexOf("@media (max-width: 48rem)"));

    expect(tablet).toContain("flex-direction: column");
    expect(tablet).toContain("width: 100% !important");
    expect(mobile).toContain("grid-template-columns: 24px minmax(0, 1fr)");
    expect(mobile).toContain("white-space: normal");
    expect(css).not.toMatch(/animation\s*:|transition\s*:/);
  });

  it("implements conventional roving tree navigation without extra disclosure tab stops", async () => {
    const user = setup();
    const tree = await screen.findByRole("tree", { name: "Spans" });
    let rows = within(tree).getAllByRole("treeitem");
    expect(rows.filter((row) => row.tabIndex === 0)).toHaveLength(1);
    expect(
      within(tree)
        .getAllByRole("button")
        .every((button) => button.tabIndex === -1),
    ).toBe(true);

    rows[0]?.focus();
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(rows[1]);
    await user.keyboard("{End}");
    expect(document.activeElement).toBe(rows.at(-1));
    await user.keyboard("{Home}");
    expect(document.activeElement).toBe(rows[0]);

    rows[2]?.focus();
    await user.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(rows[3]);
    await user.keyboard("{ArrowLeft}");
    expect(document.activeElement).toBe(rows[2]);

    await user.keyboard("{ArrowLeft}");
    expect(rows[2]).toHaveAttribute("aria-expanded", "false");
    rows = within(tree).getAllByRole("treeitem");
    expect(rows).toHaveLength(4);
    await user.keyboard("{ArrowRight}");
    expect(document.activeElement).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{ArrowRight}");
    expect(document.activeElement).toHaveAttribute("id", "trace-event-evt-t-route");
  });

  it("returns disclosure pointer focus to the owning row before tree navigation continues", async () => {
    const user = setup();
    const tree = await screen.findByRole("tree", { name: "Spans" });
    const disclosure = within(tree).getByRole("button", {
      name: "Collapse reason-lane-analyst",
    });
    const owner = disclosure.closest<HTMLElement>("[role='treeitem']");
    if (owner === null) throw new Error("disclosure owner missing");

    await user.click(disclosure);
    expect(document.activeElement).toBe(owner);
    expect(owner).toHaveAttribute("aria-expanded", "false");

    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toHaveAttribute("id", "trace-event-evt-t-raw");
    await user.keyboard("{Home}");
    expect(document.activeElement).toBe(within(tree).getAllByRole("treeitem")[0]);
  });

  it("collapses a parent span, and the child leaves the tree with it", async () => {
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();
    expect(spanRows()).toHaveLength(11);

    await user.click(screen.getByRole("button", { name: "Collapse reason-lane-analyst" }));
    expect(spanRows()).toEqual([
      "Policy decision pdp.decide",
      "Model call qwen3-localchat.answer",
      "Sub-agent reason-lane-analyst",
      "Event future.unknown_action",
    ]);
  });

  it("opens the kind-typed detail on selection, and Esc closes it", async () => {
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const trigger = screen
      .getByText("claude-fable-5", { selector: ".ne-trace__spanname" })
      .closest<HTMLElement>("[role='treeitem']");
    if (!trigger) throw new Error("model row missing");
    await user.click(trigger);
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Model call")).toBeInTheDocument();
    expect(within(pane).getByText("ap.adjudicate")).toBeInTheDocument();
    expect(within(pane).getByText("frontier-api")).toBeInTheDocument();
    expect(within(pane).getByText("$0.02")).toBeInTheDocument();
    // The frontier call's usage was never captured: absent, not zero.
    const tokens = within(pane).getByText("Input tokens").parentElement;
    expect(tokens?.textContent).toContain("—");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it("moves pane focus and close restoration to the current row when selection changes", async () => {
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const first = screen
      .getByText("qwen3-local", { selector: ".ne-trace__spanname" })
      .closest<HTMLElement>("[role='treeitem']");
    const second = screen
      .getByText("claude-fable-5", { selector: ".ne-trace__spanname" })
      .closest<HTMLElement>("[role='treeitem']");
    if (first === null || second === null) throw new Error("model rows missing");

    await user.click(first);
    let pane = screen.getByRole("dialog");
    expect(within(pane).getByRole("heading", { name: "qwen3-local" })).toBeInTheDocument();
    expect(pane).toContainElement(document.activeElement as HTMLElement);

    await user.click(second);
    pane = screen.getByRole("dialog");
    expect(within(pane).getByRole("heading", { name: "claude-fable-5" })).toBeInTheDocument();
    expect(pane).toContainElement(document.activeElement as HTMLElement);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(second);
  });

  it("renders a measured latency, and a missing measurement as absent", async () => {
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    // The local call carries a B12 measurement: latency renders in §2.7 form;
    // its First token is null (not a stream) and renders the dash.
    await user.click(screen.getByText("qwen3-local", { selector: ".ne-trace__spanname" }));
    let pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Latency").parentElement?.textContent).toContain("480 ms");
    expect(within(pane).getByText("First token").parentElement?.textContent).toContain("—");
    await user.keyboard("{Escape}");

    // The frontier call is a pre-measurement historical row: both rows absent —
    // never a fabricated zero for a measurement nobody made.
    await user.click(screen.getByText("claude-fable-5", { selector: ".ne-trace__spanname" }));
    pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Latency").parentElement?.textContent).toContain("—");
    expect(within(pane).getByText("Latency").parentElement?.textContent).not.toMatch(/0 ms/);
    expect(within(pane).getByText("First token").parentElement?.textContent).toContain("—");
  });

  it("renders a served zero latency as 0 ms — a measurement, not an absence", async () => {
    // The other direction of the absent-vs-zero rule: a sub-millisecond call
    // measured at 0 is a fact and must not collapse into the dash (the defect
    // that looks fine until someone asks why every call took no time).
    serve({
      ...TRACE_ASSEMBLY,
      nodes: TRACE_ASSEMBLY.nodes.map((n) =>
        n.event_id === "evt-t-local" && n.model_call
          ? { ...n, model_call: { ...n.model_call, latency_ms: 0, ttft_ms: 0 } }
          : n,
      ),
    });
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    await user.click(screen.getByText("qwen3-local", { selector: ".ne-trace__spanname" }));
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Latency").parentElement?.textContent).toContain("0 ms");
    expect(within(pane).getByText("Latency").parentElement?.textContent).not.toContain("—");
    expect(within(pane).getByText("First token").parentElement?.textContent).toContain("0 ms");
  });

  it("an executed write names the approval that authorised it — the server's id, copyable", async () => {
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    await user.click(screen.getByText("tool.execute", { selector: ".ne-trace__spanname" }));
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Authorised by approval")).toBeInTheDocument();
    // The id shown is the server's recorded join — the SAME id the approval
    // span in this run carries — with the full value on the copy affordance.
    expect(within(pane).getByText("apr-1")).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Copy approval id" })).toBeInTheDocument();
    expect(timelineFact("evt-t-tool", "tool-approval")).toBe("Approval · apr-1");
    // The executor's redacted detail still renders on the typed node.
    expect(within(pane).getByText('{"what_action": "write.erp"}')).toBeInTheDocument();
  });

  it("an execution recorded without the join renders NO authorised-by line — never an inferred one", async () => {
    // The pre-join record: same run, same adjacent approval span, but the
    // server recorded no approval_id on the execution. The one honest render
    // is absence — reaching for the adjacent approval would fabricate a join.
    const nodes = structuredClone(TRACE_ASSEMBLY.nodes);
    const tool = nodes[2]?.children.find((n) => n.kind === "tool");
    if (!tool || !tool.tool) throw new Error("fixture lost its tool node");
    tool.tool.approval_id = null;
    serve({ ...TRACE_ASSEMBLY, nodes });

    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();
    await user.click(screen.getByText("tool.execute", { selector: ".ne-trace__spanname" }));
    const pane = screen.getByRole("dialog");
    expect(within(pane).queryByText("Authorised by approval")).toBeNull();
    expect(within(pane).queryByText("apr-1")).toBeNull();
    expect(timelineFact("evt-t-tool", "tool-approval")).toBeNull();
    // The detail is still there — absence of the join hides nothing else.
    expect(within(pane).getByText('{"what_action": "write.erp"}')).toBeInTheDocument();
  });

  it("keeps an unrecognized span inert and withholds arbitrary raw detail", async () => {
    const secrets = ["<think>PRIVATE_CLOSED</think>", "<think>PRIVATE_OPEN", "PRIVATE_SENTINEL"];
    serve({
      ...TRACE_ASSEMBLY,
      nodes: TRACE_ASSEMBLY.nodes.map((node) =>
        node.event_id === "evt-t-raw" ? { ...node, detail: secrets.join(" · ") } : node,
      ),
    });
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    await user.click(
      screen.getByText("future.unknown_action", { selector: ".ne-trace__spanname" }),
    );
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Not displayed for an unclassified event.")).toBeInTheDocument();
    expect(within(pane).getAllByText("future.unknown_action").length).toBeGreaterThan(0);
    for (const secret of secrets) {
      expect(document.body.textContent).not.toContain(secret);
      expect(document.body.innerHTML).not.toContain(secret);
      expect(screen.queryByText(secret, { exact: false })).toBeNull();
    }
  });

  it("a refused read renders the SERVER's refusal, not a client-side denial", async () => {
    serveStatus(403);
    setup();
    expect(
      await screen.findByText(
        /Your role doesn’t include traces\. Contact your administrator for access\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Couldn’t load/)).toBeNull();
  });

  it("a run with no records is empty — not refused, not an error", async () => {
    serveStatus(404);
    setup();
    expect(await screen.findByText("No records for this run.")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn’t include/)).toBeNull();
  });

  it("an unreachable read errors, and the retry re-reads the run", async () => {
    serveStatus(503);
    const user = setup();
    expect(await screen.findByText("Couldn’t load this run.")).toBeInTheDocument();
    expect(screen.queryByText(/Your role doesn’t include/)).toBeNull();

    serve(TRACE_ASSEMBLY);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Events")).toBeInTheDocument();
  });

  it("the route with no run says where a trace comes from — and offers no dispatch", async () => {
    setup("/trace");
    expect(
      await screen.findByText(
        "No run selected. Open a trace from an audit record, an answer, or an approval.",
      ),
    ).toBeInTheDocument();
    // The three affordances the drill-down ruling deleted.
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /dispatch|delegation|run the/i })).toBeNull();
    expect(screen.queryByText(/recent runs/i)).toBeNull();
  });

  it("carries no repo internals on screen", async () => {
    setup();
    expect(await screen.findByText("Events")).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\/v1\/|GET |POST |NE-\d|\.py|\.tsx?\b/);
    // …and none of the words §0 bans from visible copy.
    expect(text.toLowerCase()).not.toMatch(/verbatim|governed|proven|fabricated/);
  });
});

/** Each span row's name cell and its result-fact chip, in render order. */
function factRows(): Array<{ name: string; fact: string | null; empty: boolean }> {
  return screen.getAllByRole("treeitem").map((r) => {
    const chip = r.querySelector(".ne-trace__spanfact");
    const nameCell = r.querySelector(".ne-trace__spanname");
    const chipText = chip?.textContent ?? null;
    const name = (nameCell?.textContent ?? "").replace(chipText ?? "", "").trim();
    return {
      name,
      fact: chipText,
      empty: chip?.classList.contains("ne-trace__spanfact--empty") ?? false,
    };
  });
}

describe("§13b Trace — a grounded chat turn reads as facts, not emitter codes", () => {
  it("states each step from served fields: template as words, row counts beside", async () => {
    serve(TRACE_ASSEMBLY_GROUNDED);
    setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const rows = factRows();
    // The template that matched: its served id as words, its served count.
    expect(rows[1]).toEqual({ name: "accounts directory", fact: "120 rows", empty: false });
    // The verified-read receipt beside it carries the same served count.
    expect(rows[0]).toEqual({ name: "", fact: "120 rows", empty: false });
    // The internal action strings have left the rows this screen can state.
    const tree = screen.getByRole("tree", { name: "Spans" });
    expect(within(tree).queryByText("knowledge.verified_read")).toBeNull();
    expect(within(tree).queryByText("grounding.assist")).toBeNull();
    expect(within(tree).queryByText("session.run")).toBeNull();
  });

  it("a zero-row fetch is visually distinct; a non-zero fetch is not", async () => {
    serve(TRACE_ASSEMBLY_GROUNDED);
    setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const rows = factRows();
    // The fetch that matched NOTHING — catchable at a glance, both directions:
    // the zero row carries the empty treatment, the 120 row does not.
    expect(rows[3]).toEqual({ name: "tickets by status", fact: "0 rows", empty: true });
    expect(rows[2]).toEqual({ name: "", fact: "0 rows", empty: true });
    expect(rows[1]?.empty).toBe(false);
    expect(rows[0]?.empty).toBe(false);
  });

  it("a tool row without lifted fields keeps the generic form and NO fact chip", async () => {
    serve(TRACE_ASSEMBLY_GROUNDED);
    setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const rows = factRows();
    // No served template, no served count: the server's own action string and
    // no chip — never a fabricated label, never a count nobody recorded.
    expect(rows[4]).toEqual({ name: "tool.execute", fact: null, empty: false });
  });

  it("the answer check and the conversation turn read in words", async () => {
    serve(TRACE_ASSEMBLY_GROUNDED);
    setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const rows = factRows();
    expect(rows[5]).toEqual({
      name: "",
      fact: "draft withheld — rewritten from fetched data",
      empty: false,
    });
    expect(rows[6]).toEqual({ name: "grounded-analyst", fact: null, empty: false });
    expect(screen.getByText("Answer check")).toBeInTheDocument();
    expect(screen.getByText("Conversation turn")).toBeInTheDocument();
  });

  it("the empty fetch's pane states the template, the argument NAMES, and the zero", async () => {
    serve(TRACE_ASSEMBLY_GROUNDED);
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    await user.click(screen.getByText("tickets by status", { selector: ".ne-trace__spanname" }));
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("tickets-by-status")).toBeInTheDocument();
    // Argument NAMES only — the chain never carries values, so none can render.
    expect(within(pane).getByText("Arguments").parentElement?.textContent).toContain("status");
    // A served zero renders "0" — a fact, never the absent dash.
    expect(within(pane).getByText("Rows returned").parentElement?.textContent).toContain("0");
    expect(within(pane).getByText("Rows returned").parentElement?.textContent).not.toContain("—");
  });

  it("the conversation pane carries the record's POINTER — an id, no text", async () => {
    serve(TRACE_ASSEMBLY_GROUNDED);
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    await user.click(screen.getByText("grounded-analyst", { selector: ".ne-trace__spanname" }));
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Conversation")).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Copy conversation id" })).toBeInTheDocument();
    expect(within(pane).getByText("engineering")).toBeInTheDocument();
  });
});

/** TRACE_ASSEMBLY_GROUNDED with its assist span's grounding fields replaced. */
function serveAssist(grounding: NonNullable<TraceNodeView["grounding"]>) {
  const view = structuredClone(TRACE_ASSEMBLY_GROUNDED);
  const assist = view.nodes.find((n) => n.kind === "grounding");
  if (assist) assist.grounding = grounding;
  serve(view);
}

describe("§13b Trace — the grounding row states the server's adopted verdict", () => {
  it("a rejected redraft reads as the fallback tables going out, and agrees with its own pane", async () => {
    // The served record: the draft was withheld, the redraft was NOT adopted.
    serveAssist({ phase: "fail-redraft", adopted: false, error_class: "TimeoutError" });
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const rows = factRows();
    expect(rows[5]).toEqual({
      name: "",
      fact: "draft withheld — fetched data sent as tables",
      empty: false,
    });
    // The row and the pane describe the SAME served record: the fact line may
    // never claim a rewrite while "Rewrite used" says No three inches away.
    await user.click(screen.getByText("draft withheld — fetched data sent as tables"));
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Rewrite used").parentElement?.textContent).toContain("No");
    // The class name is a fact and stays — as DETAIL behind a sentence, never
    // as the whole field. "TimeoutError" alone is the wire, not an account.
    const error = within(pane).getByText("Error").parentElement?.textContent ?? "";
    expect(error).toContain("The check did not complete (TimeoutError)");
  });

  it("a polish that errored reads as the original draft going out unchanged", async () => {
    serveAssist({ phase: "pass-polish", adopted: false, error_class: "TimeoutError" });
    setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const rows = factRows();
    expect(rows[5]).toEqual({
      name: "",
      fact: "wording unchanged — original draft sent",
      empty: false,
    });
  });
});

describe("rolling upgrade — an older API's nodes render absent, never crash or claim", () => {
  it("a pre-lift tool span opens with the dash for every field the server never sent", async () => {
    serve(TRACE_ASSEMBLY_OLD_WIRE);
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    const rows = factRows();
    // No lifted fields, no `adopted` verdict: the server's own action strings
    // and NO fact chips — never an invented label, count, or verdict.
    expect(rows[0]).toEqual({ name: "tool.execute", fact: null, empty: false });
    expect(rows[1]).toEqual({ name: "grounding.assist", fact: null, empty: false });

    await user.click(screen.getByText("tool.execute", { selector: ".ne-trace__spanname" }));
    const pane = screen.getByRole("dialog");
    expect(within(pane).getByText("Arguments").parentElement?.textContent).toContain("—");
    expect(within(pane).getByText("Rows returned").parentElement?.textContent).toContain("—");
  });

  it("a pre-`adopted` grounding span renders the dash for the verdict it lacks", async () => {
    serve(TRACE_ASSEMBLY_OLD_WIRE);
    const user = setup();
    expect(await screen.findByRole("tree", { name: "Spans" })).toBeInTheDocument();

    await user.click(screen.getByText("grounding.assist", { selector: ".ne-trace__spanname" }));
    const pane = screen.getByRole("dialog");
    const rewrite = within(pane).getByText("Rewrite used").parentElement?.textContent ?? "";
    // Absent is absent: never "No" (a verdict the server did not serve).
    expect(rewrite).toContain("—");
    expect(rewrite).not.toContain("No");
  });
});

/* ===========================================================================
 * THE OVERCLAIM, AND WHERE IT ACTUALLY LIVES.
 *
 * This screen prefers the shared audit poll's read to its own — deliberately,
 * because the rail chip two inches above it renders the poll's and two screens
 * that resolve different reads come to rest on different words about one chain.
 * But the poll's verdict stopped being a full walk. It is an
 * `anchored-extension` proof: the rows appended since a head a full
 * `verify_chain` blessed up to fifteen minutes ago, and nothing else.
 *
 * The strip went on printing `Entire audit chain · N records verified` over it,
 * unqualified — the strongest sentence in the console, over the weaker of the
 * two verdicts in hand, on the screen that had the stronger one, and a stronger
 * claim than /audit makes about the very same read.
 * ======================================================================== */

describe("the verdict line never claims more than the read it renders", () => {
  const ANCHORED_DETAIL =
    "chain extends verified anchor: 2 record(s) verified forward from seq 12478 to head " +
    "seq 12480; the prefix at or before seq 12478 was verified in full 42.0s ago " +
    "(12478 record(s)) and is NOT re-checked by this call";

  it("an ANCHORED poll verdict is COVERED, with the server's scope verbatim", async () => {
    serve(TRACE_ASSEMBLY);
    pollVerdicts([
      {
        valid: true,
        basis: "anchored-extension",
        contradiction: false,
        checked: 12_480,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail: ANCHORED_DETAIL,
      },
    ]);
    setup(`/trace/${TRACE_ID}`, POLL_MS);

    const line = await screen.findByText("Entire audit chain · 12,480 records covered");
    // The word this screen was built on, and the one the server did not support.
    expect(screen.queryByText(/records verified/)).toBeNull();
    // The reach is the SERVER'S sentence, character for character — including
    // "is NOT re-checked by this call" — never a paraphrase assembled here. It
    // is the line's accessible DESCRIPTION, announced on focus, and the text of
    // the paragraph the line reveals.
    expect(line).toHaveAccessibleDescription(ANCHORED_DETAIL);
    expect(reachParagraph()?.textContent).toBe(ANCHORED_DETAIL);
    expect(line.className).toContain("ne-trace__verdictline--scoped");
    // AND IT IS NOT A TOOLTIP. `title` on a non-focusable span was the defect:
    // mouse-only, and announced inconsistently by screen readers. This
    // assertion USED TO PIN THE DEFECT — it read
    // `toHaveAttribute("title", ANCHORED_DETAIL)`.
    expect(line).not.toHaveAttribute("title");
  });

  // -------------------------------------------------------------------------
  // THE SCOPE SENTENCE MUST BE REACHABLE WITHOUT A POINTER — and on THIS screen
  // it matters more than on /audit, because this strip leads with the words
  // "Entire audit chain". The qualification is the only thing standing between
  // an `anchored-extension` verdict and an outright overclaim, and it was a
  // `title` on a non-focusable <span>: no Tab reaches one, no tap reveals one,
  // and screen readers announce them inconsistently. /audit had the identical
  // defect and was fixed; this screen kept it.
  // -------------------------------------------------------------------------

  it("reveals the server's scope sentence to a keyboard with no pointer at all", async () => {
    serve(TRACE_ASSEMBLY);
    pollVerdicts([
      {
        valid: true,
        basis: "anchored-extension",
        contradiction: false,
        checked: 12_480,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail: ANCHORED_DETAIL,
      },
    ]);
    const user = setup(`/trace/${TRACE_ID}`, POLL_MS);

    // REACHABLE: it is a real control, so it is in the tab order and it is
    // found BY ROLE — the query a pointer cannot help with.
    const line = await screen.findByRole("button", {
      name: "Entire audit chain · 12,480 records covered",
    });
    expect(line).toHaveAttribute("aria-expanded", "false");
    // ANNOUNCED BEFORE IT IS OPENED. `aria-describedby` names the hidden
    // paragraph, so focus alone carries the caveat — nobody has to guess that
    // pressing helps.
    expect(line).toHaveAccessibleDescription(ANCHORED_DETAIL);
    expect(reachParagraph()).not.toBeVisible();

    // OPERABLE FROM THE KEYBOARD: Tab to it, Enter to open. No click anywhere.
    line.focus();
    expect(line).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(line).toHaveAttribute("aria-expanded", "true");
    const revealed = reachParagraph();
    expect(revealed).toBeVisible();
    // ASSOCIATED, not merely present: the control points at THIS paragraph.
    expect(line.getAttribute("aria-controls")).toBe(revealed?.id);
    // VERBATIM. The revealed sentence is the server's `detail`, unedited.
    expect(revealed?.textContent).toBe(ANCHORED_DETAIL);

    // And it closes again from the keyboard.
    await user.keyboard("{Enter}");
    expect(line).toHaveAttribute("aria-expanded", "false");
    expect(reachParagraph()).not.toBeVisible();
  });

  it("a BREAK offers no scope disclosure to reach, by keyboard or otherwise", async () => {
    // The disclosure must NOT hand a break a contradicting sentence: the read a
    // latched break rests on is a PASSING one whose `detail` describes an
    // intact chain, and an accusation one Tab away from that sentence is the
    // contradiction `breakReason` was narrowed to keep out. The line is plain
    // text under BREAK — no control, no description, no paragraph.
    serve(TRACE_ASSEMBLY);
    pollVerdicts([
      {
        valid: false,
        basis: "full-chain",
        contradiction: true,
        checked: 12_480,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail: "chain does not extend the head verified at seq 12478",
      },
      {
        valid: true,
        basis: "anchored-extension",
        contradiction: false,
        checked: 12_480,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail: ANCHORED_DETAIL,
      },
    ]);
    setup(`/trace/${TRACE_ID}`, POLL_MS);

    await waitFor(() => expect(stripWord()).toBe("BREAK"), { timeout: LATCH_WAIT });
    // THE READ IN HAND MUST BE THE PASSING ANCHORED ONE, or this proves nothing:
    // the contradiction that latched is itself a `full-chain` verdict, which
    // `chainScope` excludes for a different reason entirely. At 15ms the poll
    // has re-read many times over by here, so the strip is resting a BREAK on a
    // read whose `detail` describes an intact chain — which is exactly the
    // sentence that must not be one Tab away from the accusation.
    await new Promise((done) => setTimeout(done, 200));
    expect(stripWord()).toBe("BREAK");
    const line = screen.getByText(/^Entire audit chain · Break/);
    expect(line.tagName).toBe("SPAN");
    expect(line).not.toHaveAttribute("title");
    expect(line).not.toHaveAttribute("aria-expanded");
    expect(line).toHaveAccessibleDescription("");
    expect(reachParagraph()).toBeNull();
    // Not merely hidden from a mouse: the sentence is not on the page at all.
    expect(screen.queryByText(/NOT re-checked by this call/)).toBeNull();
    expect(screen.queryByRole("button", { expanded: false })).toBeNull();
  });

  it("CONTROL: a FULL-chain verdict still reads VERIFIED, with no disclosure", async () => {
    // Nothing is softened for a proof that was actually taken, and the
    // disclosure marker must not appear where there is nothing left to disclose
    // — otherwise it stops meaning anything.
    serve(TRACE_ASSEMBLY);
    pollVerdicts([
      {
        valid: true,
        basis: "full-chain",
        contradiction: false,
        checked: 12_480,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail: "chain intact: 12480 record(s) verified",
      },
    ]);
    setup(`/trace/${TRACE_ID}`, POLL_MS);

    const line = await screen.findByText("Entire audit chain · 12,480 records verified");
    expect(line.tagName).toBe("SPAN");
    expect(line).not.toHaveAttribute("title");
    expect(line).not.toHaveAttribute("aria-expanded");
    expect(line).toHaveAccessibleDescription("");
    expect(line.className).not.toContain("ne-trace__verdictline--scoped");
    expect(reachParagraph()).toBeNull();
  });

  it("a ONE-SHOT contradiction from the poll stands as BREAK on this screen", async () => {
    // The verdict the server emits exactly once: the ledger is internally
    // perfect and no longer extends the head this process verified. It names no
    // entry (the walk under it passed), so the seq-less arm — long documented
    // here as unreachable — is the one that renders, and the server's own
    // sentence is all the operator has to go on.
    //
    // Every trace read PASSES throughout, so nothing but the flag can put this
    // word on the screen.
    serve(TRACE_ASSEMBLY);
    pollVerdicts([
      {
        valid: false,
        basis: "full-chain",
        contradiction: true,
        checked: 12_480,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail:
          "chain does not extend the head verified at seq 12478: the ledger is internally " +
          "consistent but it is not an append-only continuation of the state this process " +
          "verified",
      },
      {
        valid: true,
        basis: "anchored-extension",
        contradiction: false,
        checked: 12_480,
        first_break_seq: null,
        first_break_event_id: null,
        reason: null,
        detail: ANCHORED_DETAIL,
      },
    ]);
    setup(`/trace/${TRACE_ID}`, POLL_MS);

    expect(
      await screen.findByText(
        "Entire audit chain · Break confirmed — the server did not name the entry",
        {},
        { timeout: LATCH_WAIT },
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("BREAK")).toBeInTheDocument();
    // And it STAYS, over the passing reads that follow it — which is every read
    // after it, because the server re-anchored on its way out.
    await new Promise((ok) => setTimeout(ok, POLL_MS * 6));
    expect(screen.getByText("BREAK")).toBeInTheDocument();
    expect(
      screen.getByText(/not an append-only continuation of the state this process verified/),
    ).toBeInTheDocument();
  });
});
