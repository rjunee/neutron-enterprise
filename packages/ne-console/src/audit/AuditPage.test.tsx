// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §13 — AUDIT LOG. What this file proves, in the order the spec states it:
//
//   * the verdict strip is the SERVER's verdict, rendered as sent — a fixture
//     whose hashes visibly do not chain but whose verdict says valid still
//     renders VALID, because a client that computed it would say otherwise;
//   * the DEFAULT SORT IS SEQ DESC, and Seq/Time/Actor/Action/Outcome are really
//     sortable — asserted BY ROW IDENTITY, because a count assertion is
//     satisfied by the wrong rows in the wrong order;
//   * every filter narrows to NAMED ROWS: the class chips, the search-within;
//   * the state copy, verbatim;
//   * a REFUSAL renders the SERVER's refusal and an ERROR NEVER DOES;
//   * the export preserves the server's filtered bytes, verdict and completion
//     evidence, and a failed export cannot offer a stale file;
//   * the pane's served fields, its payload, and the replay;
//   * no repo internals reach the screen.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RuntimeConfigContext } from "../AppBootstrap";
import type { AuditAnchorView, AuditRecordView, ChainVerificationView } from "../api/types";
import { PrincipalProvider } from "../auth/PrincipalContext";
import { DirectoryProvider } from "../directory/DirectoryContext";
import { AuditPollProvider } from "../hooks/useAuditPoll";
import { AuthStub } from "../test/authStub";
import { RUNTIME_CONFIG } from "../test/mocks/fixtures";
import { server } from "../test/mocks/server";
import { TRACE_ASSEMBLY, TRACE_ID } from "../test/mocks/traceFixtures";
import { TracePage } from "../trace/TracePage";
import { AuditPage } from "./AuditPage";
import { BREAK_CONFIRMATIONS } from "./chainVerdict";

/* ------------------------------- fixtures --------------------------------- */

function record(over: Partial<AuditRecordView>): AuditRecordView {
  return {
    id: 1,
    seq: 1,
    event_id: "evt",
    event_type: "runtime",
    config_hash: "a1".repeat(32),
    actor: "user:alice@example.com",
    occurred_at: "2026-07-20T09:00:00.000Z",
    kpi_tag: null,
    trace_id: null,
    action: null,
    resource: null,
    outcome: null,
    payload: {},
    prev_hash: "b2".repeat(32),
    record_hash: "c3".repeat(32),
    ...over,
  };
}

// Newest first, exactly as the ledger read answers. One record of each class,
// plus one with three absent fields, so every rule below is provable by naming
// the rows it keeps.
const RECORDS: AuditRecordView[] = [
  record({
    seq: 4021,
    event_id: "evt-decision",
    event_type: "govern",
    action: "pdp.decide",
    actor: "user:dana@example.com",
    resource: "payments.wire_transfer",
    outcome: "allow",
    trace_id: "trace-alpha",
    occurred_at: "2026-07-20T12:00:00.000Z",
    payload: { tier: 3, rule: "wire-limit" },
  }),
  record({
    seq: 4020,
    event_id: "evt-tool",
    event_type: "runtime",
    action: "tool.execute",
    actor: "agent://sales-drafts",
    resource: "crm.create_note",
    outcome: "success",
    trace_id: "trace-bravo",
    occurred_at: "2026-07-20T11:00:00.000Z",
    kpi_tag: "tool_latency_p50_s",
  }),
  record({
    seq: 4019,
    event_id: "evt-refusal",
    event_type: "api.audit",
    action: "api.audit.unauthorized",
    actor: "user:erin@example.com",
    resource: "api:audit",
    outcome: "deny",
    occurred_at: "2026-07-20T10:00:00.000Z",
  }),
  record({
    seq: 4018,
    event_id: "evt-other",
    event_type: "connector.sync",
    action: null,
    actor: "svc:connector",
    resource: null,
    outcome: null,
    occurred_at: "2026-07-20T09:30:00.000Z",
  }),
  record({
    seq: 4017,
    event_id: "evt-approval",
    event_type: "gate",
    action: "approval.approved",
    actor: "user:bob@example.com",
    resource: "payments.wire_transfer",
    outcome: "approved",
    trace_id: "trace-charlie",
    occurred_at: "2026-07-20T09:00:00.000Z",
  }),
];

const VALID: ChainVerificationView = {
  valid: true,
  basis: "anchored-extension",
  contradiction: false,
  checked: 12480,
  first_break_seq: null,
  first_break_event_id: null,
  reason: null,
  detail: "chain intact",
};

/** THE UNFILTERED PAGE'S VERDICT AS THE SERVER NOW COMPUTES IT — a COMPOSITE.
 * `checked` still counts every record the verdict COVERS (the whole chain), and
 * `detail` — the server's own sentence, served verbatim — states that only the
 * tail was re-hashed on this call and that the prefix rests on a full verify
 * that finished seconds ago. Shape copied from `ne.audit.anchor`. */
const VALID_ANCHORED: ChainVerificationView = {
  ...VALID,
  detail:
    "chain extends verified anchor: 2 record(s) verified forward from seq 12478 to head " +
    "seq 12480; the prefix at or before seq 12478 was verified in full 42.0s ago " +
    "(12478 record(s)) and is NOT re-checked by this call",
};

/** THE SAME LEDGER, PROVED THE STRONG WAY — a real unbounded `verify_chain`
 * walked every record on this request. It is what `?full_verify` (the console's
 * Re-verify sweep) asks the server for, and the only verdict strong enough to
 * withdraw a break another full walk confirmed. */
const VALID_FULL: ChainVerificationView = { ...VALID, basis: "full-chain" };

/** THE ONE-SHOT REFUSAL. `valid: false` with a PASSING `verify_chain` beneath
 * it: the ledger is internally consistent and simply is not the ledger this
 * process verified. It names no entry for that reason, and the server adopts
 * the freshly verified head before returning it — so it appears exactly once
 * and every read after it passes. Shape copied from `ne.audit.anchor`. */
const CONTRADICTION: ChainVerificationView = {
  valid: false,
  basis: "full-chain",
  contradiction: true,
  checked: 12480,
  first_break_seq: null,
  first_break_event_id: null,
  reason: null,
  detail:
    "chain does not extend the head verified at seq 12478: the ledger is internally " +
    "consistent but it is not an append-only continuation of the state this process verified",
};

const BROKEN: ChainVerificationView = {
  valid: false,
  basis: "full-chain",
  contradiction: false,
  checked: 12479,
  first_break_seq: 4020,
  first_break_event_id: "evt-tool",
  reason: "record_hash mismatch",
  detail: "chain broken at seq 4020",
};

/** A DEFENSIVE FIXTURE: invalid, and NAMING NO ENTRY. It was labelled here as
 * "the race's own answer … a read this server really returns"; it is not, and
 * the server cannot return it. `_break` (verify.py:180) takes `seq: int`, so
 * every invalid verdict names an entry — the reproduction in
 * audit/chainVerdict.ts shows the race naming `seq 7830` and `seq 7838`, a
 * MOVING head, not a missing one. It stays because `first_break_seq` is
 * `int | None` from the dataclass (verify.py:163) to ChainVerificationView, so
 * the rules that depend on a located break must still survive one arriving. */
const BROKEN_NO_SEQ: ChainVerificationView = {
  valid: false,
  basis: "full-chain",
  contradiction: false,
  checked: 12479,
  first_break_seq: null,
  first_break_event_id: null,
  reason: "head mismatch (truncated or forged tail)",
  detail: "chain broken",
};

function respond(
  events: AuditRecordView[] = RECORDS,
  verification: ChainVerificationView = VALID,
): void {
  server.use(http.get("*/api/v1/audit/events", () => HttpResponse.json({ events, verification })));
}

/**
 * THE LEDGER READ ANSWERED PER CALLER, and a SEQUENCE for the poll.
 *
 * Two callers read this endpoint on this screen: the page's own window
 * (`limit=100`) and the shared poll behind the verdict latch (`limit=25`).
 * They are separate observation streams, and the latch folds the POLL's — so a
 * question about the resting verdict is a question about a run of polls and
 * cannot be asked of one fixture. `pollVerdicts` is consumed one per poll, the
 * last entry repeating; the page's own window gets `pageVerification` every
 * time, which is how a screen can be asked what it renders when its own read
 * and the poll disagree.
 */
function respondPolled(
  pollVerdicts: readonly ChainVerificationView[],
  pageVerification: ChainVerificationView = VALID,
  events: AuditRecordView[] = RECORDS,
): { polls: () => number } {
  let n = 0;
  server.use(
    http.get("*/api/v1/audit/events", ({ request }) => {
      if (new URL(request.url).searchParams.get("limit") !== "25") {
        return HttpResponse.json({ events, verification: pageVerification });
      }
      const verification = pollVerdicts[Math.min(n, pollVerdicts.length - 1)]!;
      n += 1;
      return HttpResponse.json({ events, verification });
    }),
  );
  return { polls: () => n };
}

/* -------------------------------- harness --------------------------------- */

/** The deployment names its own export file (§13). */
const CONFIG = { ...RUNTIME_CONFIG, deployment_name: "Neutron Demo" };

/** The poll interval under test. Real timers and a small interval, per the
 * brief — MSW and fake timers interact badly (hooks/useAuditPoll.test.tsx). */
const POLL_MS = 15;

/** Long enough for BREAK_CONFIRMATIONS polls at POLL_MS, generously: a shared
 * box under load must not manufacture a failure out of a timer. */
const LATCH_WAIT = 5000;

/**
 * The screen — optionally under the SHARED AUDIT POLL.
 *
 * The verdict strip rests on the poll's LATCHED verdict (audit/chainVerdict.ts)
 * rather than on one read's raw `valid`, so any test about the word on the strip
 * has to mount the provider, and a BREAK appears only once BREAK_CONFIRMATIONS
 * polls have agreed. Everything else renders WITHOUT it, on the documented
 * honest default (loading, no verdict): the poll reads the same endpoint, and a
 * background read would land in the request counters half this file asserts on.
 */
function renderAudit(entry = "/audit", poll = false) {
  const user = userEvent.setup();
  const tree = poll ? (
    <AuditPollProvider intervalMs={POLL_MS}>
      <AuditPage />
    </AuditPollProvider>
  ) : (
    <AuditPage />
  );
  render(
    <AuthStub>
      <RuntimeConfigContext.Provider value={CONFIG}>
        <MemoryRouter initialEntries={[entry]}>
          <PrincipalProvider>
            <DirectoryProvider>{tree}</DirectoryProvider>
          </PrincipalProvider>
        </MemoryRouter>
      </RuntimeConfigContext.Provider>
    </AuthStub>,
  );
  return user;
}

function ReplayRouteSwitch() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate("/audit?trace_id=trace-next")}>
        Next trace
      </button>
      <AuditPage />
    </>
  );
}

function stubViewerZone(timeZone: string): void {
  const defaults = new Intl.DateTimeFormat().resolvedOptions();
  vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
    ...defaults,
    timeZone,
  });
}

function ledger(): HTMLElement {
  return screen.getByRole("table", { name: "Audit log" });
}

/** Every rendered data row's Seq — the row's identity. A count assertion is
 * satisfied by the wrong rows; this is not. */
function seqs(): string[] {
  return [...ledger().querySelectorAll("tbody tr.ne-tr")]
    .filter((row) => !row.classList.contains("ne-tr--state"))
    .map((row) => (row.querySelector("td")?.textContent ?? "").replace(/\D+/g, ""));
}

async function loaded(): Promise<void> {
  await waitFor(() => expect(seqs().length).toBeGreaterThan(0));
}

/** Rows on screen AND the chain verdict settled on a CONFIRMED break at `seq`.
 * A break is the latched verdict now (audit/chainVerdict.ts), which costs
 * BREAK_CONFIRMATIONS agreeing polls — it is never on screen the instant the
 * window's own first read lands, and the band and the spine wait for it too. */
async function latchedBreak(seq = "4020"): Promise<void> {
  await loaded();
  await screen.findByText(`Break at entry ${seq}`, {}, { timeout: LATCH_WAIT });
}

function countLine(): string {
  return document.querySelector(".ne-toolbar__count")?.textContent ?? "";
}

/** The paragraph holding the verdict's REACH — the server's own scope sentence,
 * below the toolbar, `hidden` until the verdict line is pressed. Queried by
 * class rather than by text so a test can assert it is ABSENT without the query
 * itself deciding what it would have said. */
function reachParagraph(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".ne-audit__reach");
}

/** §13 render: the scope form is a disclosure, collapsed by default — every
 * test that reaches into Actor/Action/From/To/Window/Apply opens it first. */
async function openScope(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(screen.getByRole("button", { name: /^Scope/ }));
}

/** §5 — the tier each rendered row's spine draws, in rendered order. "none"
 * means no spine was drawn at all. A tier list is the row-identity assertion
 * for the spine: a count of gaps is satisfied by a gap on the wrong row. */
function spineTiers(): string[] {
  return [...ledger().querySelectorAll("tbody tr.ne-tr")]
    .filter((row) => !row.classList.contains("ne-tr--state"))
    .map((row) => row.querySelector(".ne-audit__spine")?.getAttribute("data-spine") ?? "none");
}

/** The seq of every row carrying the break gap, and which edge it cuts. */
function spineCuts(): string[] {
  return [...ledger().querySelectorAll("tbody tr.ne-tr")]
    .filter((row) => row.querySelector(".ne-audit__spinecut") !== null)
    .map((row) => {
      const seq = (row.querySelector("td")?.textContent ?? "").replace(/\D+/g, "");
      const cut = row.querySelector(".ne-audit__spinecut")!;
      return `${seq} ${cut.classList.contains("ne-audit__spinecut--down") ? "down" : "up"}`;
    });
}

/** A viewer who has asked the OS for no motion. Nothing in src/audit reads this
 * (spine.test.ts asserts so); the stub is here to prove the rendered evidence
 * is the same either way rather than to be believed. */
function stubReducedMotion(): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: /prefers-reduced-motion/.test(query),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
}

const objectUrl = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };

afterEach(() => {
  URL.createObjectURL = objectUrl.create;
  URL.revokeObjectURL = objectUrl.revoke;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/* ------------------------------ the verdict ------------------------------- */

describe("§13 — the verdict strip", () => {
  it("renders the server's VALID verdict and its checked count", async () => {
    respond();
    renderAudit();
    await loaded();
    expect(screen.getByLabelText("VALID")).toBeInTheDocument();
    expect(screen.getByText("12,480 records covered")).toBeInTheDocument();
    expect(document.querySelector(".ne-audit__band")).toBeNull();
  });

  // -------------------------------------------------------------------------
  // THE STRIP MAY NOT CLAIM MORE THAN THE SERVER DID.
  //
  // The unfiltered page's verdict is a COMPOSITE: a prefix a full verify
  // blessed some seconds ago, plus the records appended since, re-hashed on
  // this request. `checked` is what the verdict COVERS — the server says so —
  // but "N records verified" read as the work THIS call did, which is a
  // stronger claim than the one served, on the one screen whose entire argument
  // is that it never overstates the server.
  // -------------------------------------------------------------------------

  it("states what the verdict covers and hands over the server's scope verbatim", async () => {
    respond(RECORDS, VALID_ANCHORED);
    renderAudit();
    await loaded();

    // The count is the server's and unchanged; the verb that over-read it is
    // gone, and nothing on the screen says this call re-hashed 12,480 records.
    const line = screen.getByText("12,480 records covered");
    expect(screen.queryByText(/records verified/)).toBeNull();
    // THE SCOPE IS THE SERVER'S OWN SENTENCE, not a paraphrase that can drift
    // from it — character for character, including "is NOT re-checked by this
    // call". It is the line's accessible DESCRIPTION, which is announced on
    // focus, and the text of the paragraph the line reveals.
    expect(line).toHaveAccessibleDescription(VALID_ANCHORED.detail);
    expect(reachParagraph()?.textContent).toBe(VALID_ANCHORED.detail);
    expect(line.className).toContain("ne-audit__verdictline--scoped");
    // AND IT IS NOT A TOOLTIP. `title` on a non-focusable span was the defect:
    // mouse-only, and announced inconsistently by screen readers.
    expect(line).not.toHaveAttribute("title");
  });

  it("degrades to the bare count when the server sent no scope sentence", async () => {
    // No sentence to render and none invented: no disclosure, no marker
    // promising one, and above all no dangling "verified 0.0s ago" assembled
    // here. A button that reveals nothing is a promise the server did not make.
    respond(RECORDS, { ...VALID, detail: "" });
    renderAudit();
    await loaded();

    const line = screen.getByText("12,480 records covered");
    expect(line.tagName).toBe("SPAN");
    expect(line).not.toHaveAttribute("title");
    expect(line).not.toHaveAttribute("aria-expanded");
    expect(line.className).not.toContain("ne-audit__verdictline--scoped");
    expect(reachParagraph()).toBeNull();
  });

  // -------------------------------------------------------------------------
  // THE SCOPE SENTENCE MUST BE REACHABLE WITHOUT A POINTER.
  //
  // It was a `title` attribute on a non-focusable <span>: visible on mouse
  // hover and nowhere else. No Tab reaches a `title`, no tap reveals one, and
  // screen readers announce them inconsistently. What it hid is the only thing
  // on this screen that explains why an anchored `valid=true` is not the same
  // claim as an unbounded verify — so a keyboard or touch reader was shown a
  // bare VALID with the caveat withheld.
  // -------------------------------------------------------------------------

  it("reveals the server's scope sentence to a keyboard with no pointer at all", async () => {
    respond(RECORDS, VALID_ANCHORED);
    const user = renderAudit();
    await loaded();

    // REACHABLE: it is a real control, so it is in the tab order and it is
    // found BY ROLE — the query a pointer cannot help with.
    const line = screen.getByRole("button", { name: "12,480 records covered" });
    expect(line).toHaveAttribute("aria-expanded", "false");
    // ANNOUNCED BEFORE IT IS OPENED. `aria-describedby` names the hidden
    // paragraph, so focus alone carries the caveat — nobody has to guess that
    // pressing helps.
    expect(line).toHaveAccessibleDescription(VALID_ANCHORED.detail);
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
    expect(revealed?.textContent).toBe(VALID_ANCHORED.detail);

    // And it closes again from the keyboard.
    await user.keyboard("{Enter}");
    expect(line).toHaveAttribute("aria-expanded", "false");
    expect(reachParagraph()).not.toBeVisible();
  });

  it("a FULL-CHAIN VALID reads VERIFIED and offers no disclosure at all", async () => {
    // THIS TEST USED TO ASSERT THE OPPOSITE OF ITS OWN NAME. It was called "a
    // full-walk VALID discloses that reading's own sentence" and it fed `VALID`
    // — whose basis is `anchored-extension` — so it exercised the anchored path
    // twice and pinned nothing about a full walk. Nothing in this file pinned
    // "full-chain ⇒ no marker" at all.
    //
    // The rule it should have been guarding: the marker exists to say the line
    // is NOT the whole account of the proof. On a real unbounded walk it IS —
    // every record was re-hashed on this request, the verb is "verified", and
    // `detail` only restates the count. A disclosure there would be a caret
    // promising something the sentence beneath it does not add, and a marker
    // that appears everywhere stops meaning anything where it matters.
    respond(RECORDS, VALID_FULL);
    renderAudit();
    await loaded();

    const line = screen.getByText("12,480 records verified");
    expect(line.tagName).toBe("SPAN");
    expect(line).not.toHaveAttribute("aria-expanded");
    expect(line).toHaveAccessibleDescription("");
    expect(line.className).not.toContain("ne-audit__verdictline--scoped");
    expect(reachParagraph()).toBeNull();
    // Nothing on the screen can be pressed to summon one either.
    expect(screen.queryByRole("button", { expanded: false })).toBeNull();
  });

  it("a latched BREAK carries no scope sentence off the passing read under it", async () => {
    // A latched break comes to rest on PASSING reads (chainVerdict.ts), whose
    // `detail` is "chain intact: …". Hanging that off the accusation is the
    // contradiction `breakReason` was already narrowed to keep out, so the
    // scope is VALID's alone. The accusation itself is untouched: same line,
    // same red band, same entry.
    respondPolled(
      [...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(BROKEN), VALID_ANCHORED],
      VALID_ANCHORED,
    );
    renderAudit("/audit", true);
    await latchedBreak();

    const line = screen.getByText("Break at entry 4020");
    expect(line).not.toHaveAttribute("title");
    expect(line.className).not.toContain("ne-audit__verdictline--scoped");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Chain break at entry 4020. Records after this point failed verification.",
    );
    expect(screen.getAllByLabelText("BREAK")).toHaveLength(2);
  });

  it("a BREAK offers no scope disclosure to reach, by keyboard or otherwise", async () => {
    // The disclosure made the scope sentence reachable without a pointer. That
    // must NOT hand a break a contradicting sentence: the read a latched break
    // rests on is a PASSING one whose `detail` says "chain intact", and an
    // accusation carrying "chain intact" in its accessible description — or one
    // Tab away from it — is the exact contradiction `breakReason` was narrowed
    // to keep out. The line is plain text under BREAK: no control, no
    // description, no paragraph, anywhere on the page.
    respondPolled(
      [...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(BROKEN), VALID_ANCHORED],
      VALID_ANCHORED,
    );
    renderAudit("/audit", true);
    await latchedBreak();

    const line = screen.getByText("Break at entry 4020");
    expect(line.tagName).toBe("SPAN");
    expect(line).not.toHaveAttribute("aria-expanded");
    expect(line).toHaveAccessibleDescription("");
    expect(reachParagraph()).toBeNull();
    // Not merely hidden from a mouse: the sentence is not on the page at all,
    // under any role, and nothing in the strip can be pressed to summon it.
    expect(screen.queryByText(/verified in full/)).toBeNull();
    expect(screen.queryByRole("button", { expanded: false })).toBeNull();

    // AND THE BREAK IS NO LESS PROMINENT THAN IT WAS. Same red band, same
    // entry, same pills — the one `role="alert"` on the screen.
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Chain break at entry 4020. Records after this point failed verification.",
    );
    expect(screen.getAllByLabelText("BREAK")).toHaveLength(2);
  });

  it("renders the server's BREAK verdict at the entry the server named", async () => {
    // Every poll disagrees, so the latch promotes and the break STANDS.
    respond(RECORDS, BROKEN);
    renderAudit("/audit", true);
    await latchedBreak();
    // Twice: once on the strip, once on the entry the server named.
    expect(screen.getAllByLabelText("BREAK")).toHaveLength(2);
    expect(screen.getByText("Break at entry 4020")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Chain break at entry 4020. Records after this point failed verification.",
    );
  });

  it("marks the broken entry and dims only the records after it", async () => {
    respond(RECORDS, BROKEN);
    renderAudit("/audit", true);
    await latchedBreak();
    const rows = [...ledger().querySelectorAll("tbody tr.ne-tr")] as HTMLElement[];
    const [after, broken, next] = rows;
    // Seq desc: 4021 sits above the break, 4020 IS the break, 4018 is below it.
    expect(within(after!).getByText("4021")).toBeInTheDocument();
    expect(within(broken!).getByLabelText("BREAK")).toBeInTheDocument();
    expect(after!.querySelectorAll(".ne-audit__after").length).toBeGreaterThan(0);
    expect(broken!.querySelectorAll(".ne-audit__after")).toHaveLength(0);
    expect(next!.querySelectorAll(".ne-audit__after")).toHaveLength(0);
  });

  it("renders the verdict the server sent even when the hashes do not chain", async () => {
    // prev_hash never equals the prior record_hash, and the server still says
    // valid. A console that verified anything itself would draw BREAK here.
    respond(
      RECORDS.map((r) => ({ ...r, prev_hash: "deadbeef", record_hash: "cafe" })),
      VALID,
    );
    renderAudit();
    await loaded();
    expect(screen.getByLabelText("VALID")).toBeInTheDocument();
    expect(screen.queryByLabelText("BREAK")).toBeNull();
  });

  // -------------------------------------------------------------------------
  // THE CHAIN-VERDICT LATCH ON /audit (audit/chainVerdict.ts).
  //
  // The defect these pin: this screen rendered `verification.valid` from ONE
  // read while /trace — the screen you reach by clicking a row here — rendered
  // the latched verdict of the SAME chain (`assemble_trace` calls the same
  // unbounded `verify_chain`). During the server's reproduced read-during-append
  // race that produced ⚠ BREAK here and ● VALID there, about one chain, at one
  // instant: a contradiction worse than either state alone, because a reader
  // cannot tell which screen to believe.
  //
  // The fix has to cut both ways. A transient false verdict may not reach this
  // screen as BREAK — not on the strip, not as the red band, not as the spine —
  // and a chain that really is broken must still say so and keep saying so.
  // -------------------------------------------------------------------------

  it("a single transient false verdict never reaches the strip as BREAK", async () => {
    // Poll 1 disagrees and every later poll passes — the race, not a break. The
    // page's OWN window read the same false verdict, which is precisely the
    // read this screen used to render verbatim.
    //
    // BOTH READS ARE FULL WALKS, which is what this race actually produces and
    // what the fixture pair used to get wrong. A full-chain INVALID clears the
    // process's anchor (`AnchoredChainVerifier._full_verify` on a failing walk),
    // so the next request-path read has no anchor to serve off and walks the
    // whole chain inline as well. The pair is same-instrument, and an
    // equal-strength passing read resets the run exactly as it always did.
    // Pairing it with an ANCHORED pass asked a different question — see
    // "an ANCHORED pass cannot erase the confirmations a FULL walk accumulated".
    respondPolled([BROKEN, VALID_FULL], BROKEN);
    renderAudit("/audit", true);
    await loaded();

    // The honest third state: not VALID (nothing confirmed it) and not BREAK
    // (nothing agreed with it).
    expect(
      await screen.findByText("Checking again — this reading has not been confirmed yet"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("BREAK")).toBeNull();

    // …and then the chain is simply valid, with the passing read's own count.
    expect(await screen.findByText("12,480 records verified")).toBeInTheDocument();
    expect(screen.getByLabelText("VALID")).toBeInTheDocument();
    expect(screen.queryByLabelText("BREAK")).toBeNull();
    // No red band and no severed spine over a verdict nothing confirmed: the
    // accusation is not allowed to reach the table ahead of the strip.
    expect(document.querySelector(".ne-audit__band")).toBeNull();
    expect(spineCuts()).toEqual([]);
  });

  it("a GENUINE break still latches here, and a later passing poll cannot withdraw it", async () => {
    // BREAK_CONFIRMATIONS disagreeing polls, then the server starts answering
    // valid. A latched break is tamper-evidence: only an explicit re-verify
    // sweep may clear it, never a poll that happens to pass.
    const passing = [...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(BROKEN), VALID];
    const { polls } = respondPolled(passing, BROKEN);
    renderAudit("/audit", true);
    await latchedBreak();

    expect(screen.getAllByLabelText("BREAK")).toHaveLength(2);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Chain break at entry 4020. Records after this point failed verification.",
    );
    expect(spineCuts()).toEqual(["4020 down"]);

    // Several passing polls later it is still standing.
    await waitFor(() => expect(polls()).toBeGreaterThan(BREAK_CONFIRMATIONS + 2));
    expect(screen.getByText("Break at entry 4020")).toBeInTheDocument();
    expect(screen.queryByLabelText("VALID")).toBeNull();
  });

  // -------------------------------------------------------------------------
  // A CONFIRMED BREAK CANNOT LEAVE THE SCREEN.
  //
  // Once the verdict has agreed with itself, the BREAK word is tamper-evidence
  // and nothing short of a passing re-verification sweep may take it off the
  // strip. Two ways it left anyway, both reproduced below, both worse than the
  // flicker the latch was built to stop — because a break that disappears is
  // indistinguishable from a chain that was never broken.
  // -------------------------------------------------------------------------

  it("a passing poll followed by a disagreeing one does not withdraw a confirmed break", async () => {
    // THE PAIR: the failing branch of the reducer used to compute its verdict
    // from the CURRENT run alone. So one passing poll reset the run to 0, the
    // next disagreeing poll came back at run 1 — and the screen rendered
    // RE-VERIFYING over a break this console had confirmed. `latched` stayed
    // true the whole time and NOTHING renders `latched`: the word, the red
    // band, the entry's pill and the severed spine all left together. Under the
    // reproduced ~50/50 race this pair happens within two polls.
    const { polls } = respondPolled(
      [...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(BROKEN), VALID, BROKEN, VALID],
      // The window's OWN read passes, which is the fallback a withdrawn latch
      // drops through to: it is what published `● VALID` over a broken chain.
      VALID,
    );
    renderAudit("/audit", true);
    await latchedBreak();

    // Well past the valid→invalid pair, and past the polls that follow it.
    await waitFor(() => expect(polls()).toBeGreaterThan(BREAK_CONFIRMATIONS + 4), {
      timeout: LATCH_WAIT,
    });

    expect(screen.getByText("Break at entry 4020")).toBeInTheDocument();
    expect(
      screen.queryByText("Checking again — this reading has not been confirmed yet"),
    ).toBeNull();
    expect(screen.queryByLabelText("VALID")).toBeNull();
    expect(screen.queryByText("12,480 records covered")).toBeNull();
    // The whole accusation, not just the word: the band, the entry's pill and
    // the severed spine are the same fact and leave the screen together.
    expect(screen.getAllByLabelText("BREAK")).toHaveLength(2);
    expect(screen.getByRole("alert")).toHaveTextContent("Chain break at entry 4020.");
    expect(spineCuts()).toEqual(["4020 down"]);
  });

  it("a FAILED poll does not withdraw a confirmed break either", async () => {
    // The verdict used to be forced to "unknown" whenever the poll was not
    // `ok`, and this screen falls back to its OWN read on "unknown" — so one
    // 502 published `● VALID · 12,480 records verified` for the whole error
    // backoff (up to ~60s), beside a chip reading "Audit unavailable". A poll
    // that did not answer subtracts nothing; it only stops adding.
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        if (new URL(request.url).searchParams.get("limit") !== "25") {
          // This window's own read PASSES throughout: the fallback is live and
          // would say VALID the moment the latch stopped being rendered.
          return HttpResponse.json({ events: RECORDS, verification: VALID });
        }
        polls += 1;
        if (polls <= BREAK_CONFIRMATIONS) {
          return HttpResponse.json({ events: RECORDS, verification: BROKEN });
        }
        return HttpResponse.json({ detail: "audit unavailable" }, { status: 502 });
      }),
    );
    renderAudit("/audit", true);
    await latchedBreak();

    await waitFor(() => expect(polls).toBeGreaterThan(BREAK_CONFIRMATIONS + 1), {
      timeout: LATCH_WAIT,
    });

    expect(screen.getByText("Break at entry 4020")).toBeInTheDocument();
    expect(screen.queryByLabelText("VALID")).toBeNull();
    expect(screen.queryByText("12,480 records covered")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Chain break at entry 4020.");
    expect(spineCuts()).toEqual(["4020 down"]);
  });

  it("a CONFIRMED break keeps its entry when the next read names none", async () => {
    // THE FOURTH READ — DEFENSIVE (see BROKEN_NO_SEQ). Three polls locate the
    // break at 4020 and confirm it — red band, pilled entry, dimmed tail, cut
    // spine — and the fourth is invalid naming NO entry, which the server
    // cannot send. Were it to: the reducer marks it `confirmed` too (the run is
    // past BREAK_CONFIRMATIONS), so "the newest confirmed observation" would be
    // one whose seq is null and the entry would vanish from a break still
    // standing. What would leave with it:
    // the band — the ONLY `role="alert"` on this screen — the entry's BREAK
    // pill, the dimmed tail, and the severed spine, which reverts to drawing an
    // UNBROKEN, VERIFIED CHAIN down every row. One extra poll withdrew the
    // whole accusation except the word.
    const { polls } = respondPolled(
      [...Array<ChainVerificationView>(BREAK_CONFIRMATIONS).fill(BROKEN), BROKEN_NO_SEQ],
      // The window's own read PASSES throughout, so any fallback to it is
      // immediately visible as `● VALID`.
      VALID,
    );
    renderAudit("/audit", true);
    await latchedBreak();

    // Well past the seq-less read, which then repeats for every later poll.
    await waitFor(() => expect(polls()).toBeGreaterThan(BREAK_CONFIRMATIONS + 2), {
      timeout: LATCH_WAIT,
    });

    expect(screen.getByText("Break at entry 4020")).toBeInTheDocument();
    // Twice: the strip's pill and the entry's own.
    expect(screen.getAllByLabelText("BREAK")).toHaveLength(2);
    expect(screen.getByRole("alert")).toHaveTextContent("Chain break at entry 4020.");
    expect(screen.queryByLabelText("VALID")).toBeNull();
    // …and the spine is still CUT at the entry, rather than drawing the chain
    // whole underneath a standing break.
    expect(spineCuts()).toEqual(["4020 down"]);
    expect(spineTiers()).toContain("after");
  });

  it("a break the server NEVER located does not draw a whole, verified chain", async () => {
    // The residual half — also DEFENSIVE, and previously called "the reachable
    // one", which it is not. A break confirmed from seq-less reads would have
    // no entry to name at all. That is not "no break", but the table reads it
    // as one if `broken` is not a second input: both states arrive as
    // `breakSeq === null`, every row draws a full-strength hairline and node,
    // and the band (the only alert here) never renders. A whole, verified
    // chain, drawn under the word BREAK.
    respondPolled([BROKEN_NO_SEQ], VALID);
    renderAudit("/audit", true);
    await loaded();
    await screen.findByText(
      "Break confirmed — the server did not name the entry",
      {},
      { timeout: LATCH_WAIT },
    );

    // Not one row may claim the server verified it.
    const tiers = spineTiers();
    expect(tiers.length).toBeGreaterThan(0);
    expect(tiers.every((tier) => tier === "none")).toBe(true);
    expect(spineCuts()).toEqual([]);
    // The break is still ANNOUNCED — in the only words the console honestly has.
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Chain break confirmed. The server did not name the entry",
    );
    expect(screen.queryByLabelText("VALID")).toBeNull();
  });

  it("an UNCONFIRMED read at the moving head does not MOVE the confirmed break", async () => {
    // DE-LOCATION'S OTHER HALF, and unlike the two above it needs nothing but
    // reads this server emits. Three polls confirm the break at 4020 and it
    // LATCHES. The poll then stops answering, so the strip falls through to
    // THIS WINDOW'S OWN read — and that read disagrees, at the MOVING head the
    // head-mismatch arm names (`seq=head_seq`, verify.py:509). It confirmed
    // nothing: it was never folded into the latch at all, this screen does not
    // `observe`. Reading the location off the read in hand moved the whole
    // accusation to 7838 — the red band (the only `role="alert"` here), the
    // entry's BREAK pill, the dimmed tail, the severed spine and the strip's
    // own sentence — and pointed the reader at a record no confirmed read ever
    // named.
    const moved: ChainVerificationView = {
      valid: false,
      basis: "full-chain",
      contradiction: false,
      checked: 12479,
      first_break_seq: 7838,
      first_break_event_id: "evt-7838",
      reason: "head mismatch (records beyond head)",
      detail: "chain broken at seq 7838",
    };
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        if (new URL(request.url).searchParams.get("limit") !== "25") {
          return HttpResponse.json({ events: RECORDS, verification: moved });
        }
        polls += 1;
        if (polls <= BREAK_CONFIRMATIONS) {
          return HttpResponse.json({ events: RECORDS, verification: BROKEN });
        }
        return HttpResponse.json({ detail: "audit unavailable" }, { status: 502 });
      }),
    );
    renderAudit("/audit", true);
    await latchedBreak();

    await waitFor(() => expect(polls).toBeGreaterThan(BREAK_CONFIRMATIONS + 1), {
      timeout: LATCH_WAIT,
    });

    // The CONFIRMED entry, everywhere it is drawn — and nowhere the moving head.
    expect(screen.getByText("Break at entry 4020")).toBeInTheDocument();
    expect(screen.queryByText("Break at entry 7838")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Chain break at entry 4020.");
    expect(spineCuts()).toEqual(["4020 down"]);
    expect(screen.getAllByLabelText("BREAK")).toHaveLength(2);
  });

  it("folds a WINDOW-SCOPED read that detected a break into the shared latch", async () => {
    // THE OTHER HALF OF THE RULE ABOVE, AND IT PULLS THE OPPOSITE WAY.
    //
    // A time-scoped read may not VOUCH — it walked a window and is blind to
    // every row outside it. But it is exactly as strong as the full walk about
    // the rows it DID read, so when it comes back INVALID it has made a real
    // finding, on a real walk, that the server ran and shipped to this browser.
    // This screen used to discard it: `chain` is nulled for a window-scoped
    // read (that is the rule above), the page never folded its own read into
    // the latch, and the break went nowhere. The console then went on resting
    // on whatever the shared poll said about a chain it had just been told was
    // broken.
    //
    // The poll 502s here precisely so it contributes nothing: what reaches the
    // latch can only have come from this screen's own read.
    //
    // MUTATION: delete the `observe(settled.value.verification)` call from the
    // fetch effect → RED (no strip at all: `chainReading` writes no word for an
    // "unknown" latch with no read in hand).
    stubViewerZone("America/New_York");
    const brokenWindow: ChainVerificationView = {
      ...BROKEN,
      basis: "windowed",
      checked: 300,
    };
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("limit") === "25") {
          return HttpResponse.json({ detail: "audit unavailable" }, { status: 502 });
        }
        return HttpResponse.json({
          events: RECORDS,
          verification: url.searchParams.has("from") ? brokenWindow : VALID,
        });
      }),
    );
    const user = renderAudit("/audit", true);
    await loaded();

    await openScope(user);
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-07-20T09:15" } });
    await user.click(screen.getByRole("button", { name: "Apply" }));

    // RE-VERIFYING, not VALID and not silence: one disagreeing read is a
    // disagreement, and the honest third state is what says so.
    await screen.findByLabelText("RE-VERIFYING", {}, { timeout: LATCH_WAIT });
    expect(
      screen.getByText("Checking again — this reading has not been confirmed yet"),
    ).toBeInTheDocument();
    // …and the vouching is STILL withheld: folding the read did not promote a
    // windowed verdict into a whole-chain reading.
    expect(screen.queryByLabelText("VALID")).toBeNull();
    expect(screen.queryByText("300 records covered in the selected window")).toBeNull();
  });

  it("LATCHES a break its own window-scoped reads confirmed, with no poll at all", async () => {
    // The fold is not cosmetic: a read that reaches the latch COUNTS toward
    // promotion, so a screen whose poll is down can still confirm a break on
    // its own reads and state it with the entry, the band and the severed
    // spine — the same evidence a polled break gets.
    //
    // THREE reads, because `BREAK_CONFIRMATIONS` is 3 and a windowed verdict is
    // not a contradiction: the Apply plus two Re-verify presses, each one an
    // operator-spaced read rather than a timer, which is exactly the spacing
    // the count assumes.
    //
    // MUTATION: delete the `observe(...)` call from the fetch effect → RED.
    stubViewerZone("America/New_York");
    const brokenWindow: ChainVerificationView = {
      ...BROKEN,
      basis: "windowed",
      checked: 300,
    };
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("limit") === "25") {
          return HttpResponse.json({ detail: "audit unavailable" }, { status: 502 });
        }
        return HttpResponse.json({
          events: RECORDS,
          verification: url.searchParams.has("from") ? brokenWindow : VALID,
        });
      }),
    );
    const user = renderAudit("/audit", true);
    await loaded();

    await openScope(user);
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-07-20T09:15" } });
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await screen.findByLabelText("RE-VERIFYING", {}, { timeout: LATCH_WAIT });

    for (let i = 1; i < BREAK_CONFIRMATIONS; i += 1) {
      await user.click(screen.getByRole("button", { name: "Re-verify" }));
    }

    await latchedBreak();
    expect(screen.getByRole("alert")).toHaveTextContent("Chain break at entry 4020.");
    expect(spineCuts()).toEqual(["4020 down"]);
  });

  it("does not vouch for the ledger from a read that only walked a time window", async () => {
    // `verify_chain(engine, start=start, end=end)` (routers/audit.py:130) is
    // scoped by from/to AND NOTHING ELSE, and `_verify_bounded` walks only the
    // rows inside that window — a row tampered outside it is invisible to that
    // read by construction. The unscoped poll answers the strip whenever it
    // can; with no poll read in hand this screen fell through to its OWN, and
    // once a time bound was applied that read was a window read. `● VALID`, and
    // a count of the window's rows, published as the whole ledger's verdict.
    stubViewerZone("America/New_York");
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        const scoped = new URL(request.url).searchParams.has("from");
        return HttpResponse.json({
          events: RECORDS,
          verification: scoped ? { ...VALID, checked: 300 } : VALID,
        });
      }),
    );
    // No provider: the poll has no read at all, which is the state a 502'd poll
    // leaves this screen in for the whole error backoff.
    const user = renderAudit();
    await loaded();
    // CONTROL: unscoped, this window's own read IS a whole-chain read and the
    // strip rests on it.
    expect(screen.getByLabelText("VALID")).toBeInTheDocument();
    expect(screen.getByText("12,480 records covered")).toBeInTheDocument();

    await openScope(user);
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-07-20T09:15" } });
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(screen.queryByText("12,480 records covered")).toBeNull());

    // The rows are still served; what is withheld is the VOUCHING — no word,
    // and above all no count presented as the whole ledger's.
    expect(seqs().length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("VALID")).toBeNull();
    expect(screen.queryByText("300 records covered")).toBeNull();
  });

  it("CONTROL: a passing SWEEP still clears the latch, and the strip returns to VALID", async () => {
    // The other direction, and the reason this is a latch and not a permanent
    // stain: Re-verify is an explicit re-verification, and a chain that comes
    // back valid to it is valid. Neither guard above may reach this outcome.
    //
    // THE SWEEP ASKS FOR THE REAL WALK, and that is what makes this reachable
    // at all. A break is confirmed off a `full-chain` verdict and a weaker proof
    // may not withdraw it (chainVerdict.ts, I4); the poll's ordinary read is an
    // `anchored-extension` proof, which does not re-read the prefix. So the
    // sweep carries `full_verify` and the server answers with the full walk —
    // asserted below, because without it this control is unreachable and the
    // operator's one route back from a latch is a button that does nothing.
    let polls = 0;
    let sweeps = 0;
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.get("limit") !== "25") {
          return HttpResponse.json({ events: RECORDS, verification: VALID });
        }
        polls += 1;
        if (params.get("full_verify") === "true") sweeps += 1;
        const verification = polls <= BREAK_CONFIRMATIONS ? BROKEN : VALID_FULL;
        return HttpResponse.json({ events: RECORDS, verification });
      }),
    );
    const user = renderAudit("/audit", true);
    await latchedBreak();
    expect(sweeps).toBe(0);

    await user.click(screen.getByRole("button", { name: "Re-verify" }));

    expect(
      await screen.findByText("12,480 records verified", {}, { timeout: LATCH_WAIT }),
    ).toBeInTheDocument();
    expect(sweeps).toBeGreaterThan(0);
    expect(screen.getByLabelText("VALID")).toBeInTheDocument();
    expect(screen.queryByLabelText("BREAK")).toBeNull();
    expect(document.querySelector(".ne-audit__band")).toBeNull();
    expect(spineCuts()).toEqual([]);
  });

  it("Re-verify reads the same window again", async () => {
    let reads = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        reads += 1;
        return HttpResponse.json({ events: RECORDS, verification: VALID });
      }),
    );
    const user = renderAudit();
    await loaded();
    expect(reads).toBe(1);
    await user.click(screen.getByRole("button", { name: "Re-verify" }));
    await waitFor(() => expect(reads).toBe(2));
  });
});

/* ------------------------------ the spine (§5) ---------------------------- */

describe("§5 — the ledger spine", () => {
  it("a valid page draws one unbroken chain and not a single gap", async () => {
    respond();
    renderAudit();
    await loaded();
    expect(spineTiers()).toEqual(["chain", "chain", "chain", "chain"]);
    expect(spineCuts()).toEqual([]);
  });

  it("a broken page cuts the spine at the entry the server named, and only there", async () => {
    respond(RECORDS, BROKEN);
    renderAudit("/audit", true);
    await latchedBreak();
    // Seq desc: 4021 is past the break, 4020 IS the break, 4018 and 4017 are
    // entries the server verified. 4019 is hidden by the refusals chip and the
    // hairline still runs 4020 → 4018 across it: the segment says the SERVER
    // verified the chain between these entries, not that nothing lies between
    // them — and the count line states the narrowing.
    expect(seqs()).toEqual(["4021", "4020", "4018", "4017"]);
    expect(spineTiers()).toEqual(["after", "break", "chain", "chain"]);
    expect(spineCuts()).toEqual(["4020 down"]);
    expect(ledger().querySelectorAll(".ne-audit__spinecut")).toHaveLength(1);
  });

  it("the same page draws differently under the two verdicts", async () => {
    // The bidirectional pair, on the DOM: neither assertion above is satisfied
    // by a spine that is always drawn the same way.
    respond();
    renderAudit();
    await loaded();
    const valid = spineTiers().join(",");
    cleanup();
    respond(RECORDS, BROKEN);
    renderAudit("/audit", true);
    await latchedBreak();
    expect(spineTiers().join(",")).not.toBe(valid);
  });

  it("the cut follows the sort: it faces the last verified entry in both directions", async () => {
    respond(RECORDS, BROKEN);
    const user = renderAudit("/audit", true);
    await latchedBreak();
    expect(spineCuts()).toEqual(["4020 down"]);
    // One click on Seq flips to ascending: the older neighbour is now ABOVE the
    // break row, so the severed edge moves with it. A hardcoded edge fails here.
    await user.click(within(ledger()).getByRole("button", { name: /Seq/ }));
    expect(seqs()).toEqual(["4017", "4018", "4020", "4021"]);
    expect(spineTiers()).toEqual(["chain", "chain", "break", "after"]);
    expect(spineCuts()).toEqual(["4020 up"]);
  });

  it("no chain is drawn down rows that are not in chain order", async () => {
    respond(RECORDS, BROKEN);
    const user = renderAudit("/audit", true);
    await latchedBreak();
    await user.click(within(ledger()).getByRole("button", { name: /Actor/ }));
    // Sorted by Actor the rows are not the chain's order, so a line joining
    // them would draw a chain that runs through them in that order.
    expect(spineTiers()).toEqual(["none", "none", "none", "none"]);
    expect(spineCuts()).toEqual([]);
    // …and the break is stated in full without it: the strip, the entry's own
    // pill, the band, and the dimmed tail. The spine was the redundant channel.
    expect(screen.getAllByLabelText("BREAK")).toHaveLength(2);
    expect(screen.getByRole("alert")).toHaveTextContent("Chain break at entry 4020.");
    expect(ledger().querySelectorAll(".ne-audit__after").length).toBeGreaterThan(0);
  });

  it("a break outside this window draws no gap in it", async () => {
    // The verdict still fails — at an entry this window does not contain. Every
    // record on screen is one the server verified, so the chain on screen is
    // whole, and the strip carries the verdict.
    respond(RECORDS, { ...BROKEN, first_break_seq: 9001, first_break_event_id: "evt-far" });
    renderAudit("/audit", true);
    await latchedBreak("9001");
    expect(spineTiers()).toEqual(["chain", "chain", "chain", "chain"]);
    expect(spineCuts()).toEqual([]);
    expect(screen.getAllByLabelText("BREAK")).toHaveLength(1);
    expect(screen.getByRole("alert")).toHaveTextContent("Chain break at entry 9001.");
  });

  it("draws nothing on a state with no records", async () => {
    respond([], { ...VALID, checked: 0 });
    renderAudit();
    expect(await screen.findByText("No records match these filters.")).toBeInTheDocument();
    expect(document.querySelectorAll(".ne-audit__spine")).toHaveLength(0);
  });

  it("draws nothing over a ledger nobody was shown", async () => {
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );
    renderAudit();
    await screen.findByText(/Your role doesn.t include the audit log/);
    expect(document.querySelectorAll(".ne-audit__spine")).toHaveLength(0);
  });

  it("renders identically for a reader who has asked for no motion", async () => {
    // §5 ships no animation, so reduced motion removes nothing: the gap, its
    // tick, the tiers and the markup are the same evidence either way. (What
    // this proves is that the RENDER does not branch on the preference — jsdom
    // applies no stylesheet, so no pixel is measured here.)
    respond(RECORDS, BROKEN);
    renderAudit("/audit", true);
    await latchedBreak();
    const gutter = ledger().querySelector(".ne-audit__spine--break")?.outerHTML ?? "";
    const tiers = spineTiers().join(",");
    expect(gutter).not.toBe("");

    cleanup();
    stubReducedMotion();
    respond(RECORDS, BROKEN);
    renderAudit("/audit", true);
    await latchedBreak();
    expect(spineTiers().join(",")).toBe(tiers);
    expect(spineCuts()).toEqual(["4020 down"]);
    expect(ledger().querySelector(".ne-audit__spine--break")?.outerHTML).toBe(gutter);
  });

  it("carries no accessible text of its own", async () => {
    // Every fact it draws is already spoken: the BREAK pill on the entry, the
    // verdict strip, and the alert band. A second, silent copy in the gutter
    // would be an announcement of a hairline.
    respond(RECORDS, BROKEN);
    renderAudit("/audit", true);
    await latchedBreak();
    for (const spine of ledger().querySelectorAll(".ne-audit__spine")) {
      expect(spine.getAttribute("aria-hidden")).toBe("true");
      expect(spine.textContent).toBe("");
    }
  });
});

/* -------------------------------- the table ------------------------------- */

describe("§13 — the ledger table", () => {
  it("defaults to Seq descending, and says so on the header", async () => {
    respond();
    renderAudit();
    await loaded();
    // Access refusals are off by default, so 4019 is not among them.
    expect(seqs()).toEqual(["4021", "4020", "4018", "4017"]);
    expect(within(ledger()).getByRole("columnheader", { name: /Seq/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  // THE ACTION COLUMN READS, IT DOES NOT SPELL. `pdp.decide` and
  // `api.audit.unauthorized` are the server's identifiers; a compliance reader
  // needs both the reading and the identifier, so the cell carries the reading
  // and the `title` carries the code.
  it("reads each action in words and keeps the code on the cell", async () => {
    respond();
    renderAudit();
    await loaded();
    const rows = within(ledger()).getAllByRole("row").slice(1);
    const actions = rows.map((row) => within(row).getAllByRole("cell")[3]?.textContent);
    expect(actions).toEqual([
      "Policy decision",
      "Tool run",
      // seq 4018 carries no action at all; the event type is read the same way.
      "Connector sync",
      "Approval granted",
    ]);
    expect(within(rows[0]!).getByTitle("pdp.decide")).toBeInTheDocument();
    expect(within(rows[2]!).getByTitle("connector.sync")).toBeInTheDocument();
  });

  it("reads a refused read as a refused read, with the surface it asked for", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    // Access refusals are off by default; the chip brings seq 4019 in.
    await user.click(screen.getByRole("button", { name: /Access refusals/ }));
    const cell = await within(ledger()).findByTitle("api.audit.unauthorized");
    expect(cell).toHaveTextContent("Audit access refused");
  });

  it("still finds a row by the code a reader was handed, not only by its reading", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    await user.type(screen.getByRole("searchbox"), "pdp.decide");
    await waitFor(() => expect(seqs()).toEqual(["4021"]));
  });

  it("names a human actor only through the served audit event reference", async () => {
    const human = "c21456b9-c8dc-4458-b393-bc3230db0081";
    const bodies: unknown[] = [];
    respond([record({ seq: 1, event_id: "evt-human", actor: human, action: "human.action" })]);
    server.use(
      http.post("*/api/v1/principals/resolve", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({
          resolved: [{ id: human, display_name: "Ada Lovelace", removed: false }],
        });
      }),
    );
    renderAudit();

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(bodies).toEqual([{ scope: "audit", references: ["evt-human"] }]);
  });

  it("makes exactly Seq, Time, Actor, Action and Outcome sortable", async () => {
    respond();
    renderAudit();
    await loaded();
    const headers = [...ledger().querySelectorAll("thead .ne-th__sort")].map(
      (b) => b.textContent?.replace(/[▲▼]/g, "").trim() ?? "",
    );
    expect(headers).toEqual(["Seq", "Time", "Actor", "Action", "Outcome"]);
  });

  it("sorts by Actor, by row identity", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    await user.click(within(ledger()).getByRole("button", { name: /Actor/ }));
    // agent://sales-drafts · svc:connector · user:bob · user:dana
    expect(seqs()).toEqual(["4020", "4018", "4017", "4021"]);
    await user.click(within(ledger()).getByRole("button", { name: /Actor/ }));
    expect(seqs()).toEqual(["4021", "4017", "4018", "4020"]);
  });

  it("sorts by Outcome and leaves the record with no outcome last in both directions", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    await user.click(within(ledger()).getByRole("button", { name: /Outcome/ }));
    // allow · approved · success, then the record with no outcome at all.
    expect(seqs()).toEqual(["4021", "4017", "4020", "4018"]);
    await user.click(within(ledger()).getByRole("button", { name: /Outcome/ }));
    expect(seqs()).toEqual(["4020", "4017", "4021", "4018"]);
  });

  it("renders the em dash for an absent field and never a substitute", async () => {
    respond();
    renderAudit();
    await loaded();
    const row = [...ledger().querySelectorAll("tbody tr.ne-tr")].find((r) =>
      r.textContent?.includes("svc:connector"),
    ) as HTMLElement;
    // Resource, Outcome and Trace are all absent on that record.
    expect(row.querySelectorAll(".ne-absent")).toHaveLength(3);
    // …and Action falls back to the served event_type, never to a blank — read
    // in words, with the served code on the cell.
    expect(within(row).getByText("Connector sync")).toBeInTheDocument();
    expect(within(row).getByTitle("connector.sync")).toBeInTheDocument();
  });

  it("links a record with a trace to its run", async () => {
    respond();
    renderAudit();
    await loaded();
    const links = within(ledger()).getAllByRole("link", { name: "View" });
    expect(links[0]).toHaveAttribute("href", "/trace/trace-alpha");
  });

  it("states the window under the table", async () => {
    respond();
    renderAudit();
    await loaded();
    expect(
      screen.getByText(
        "Showing the most recent 5 records. Narrow with filters or raise the window.",
      ),
    ).toBeInTheDocument();
  });
});

/* ------------------------------- the filters ------------------------------ */

describe("§13 — the toolbar", () => {
  it("hides access refusals by default and carries the hidden count", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    expect(seqs()).not.toContain("4019");
    const chip = screen.getByRole("button", { name: "Access refusals (1)" });
    expect(chip).toHaveAttribute("aria-pressed", "false");
    await user.click(chip);
    expect(seqs()).toEqual(["4021", "4020", "4019", "4018", "4017"]);
  });

  it("each class chip narrows to named rows", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    await user.click(screen.getByRole("button", { name: "Tool calls (1)" }));
    expect(seqs()).toEqual(["4021", "4018", "4017"]);
    await user.click(screen.getByRole("button", { name: "Decisions" }));
    // Only the record no chip accounts for is left — it is never hidden.
    expect(seqs()).toEqual(["4018"]);
  });

  it("the search-within narrows to named rows and the count follows it", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    expect(countLine()).toBe("Showing 4 of 5");
    await user.type(screen.getByPlaceholderText("Filter records…"), "wire_transfer");
    expect(seqs()).toEqual(["4021", "4017"]);
    expect(countLine()).toBe("Showing 2 of 5");
  });

  it("sends the actor and window the reader applied", async () => {
    const urls: string[] = [];
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json({ events: RECORDS, verification: VALID });
      }),
    );
    const user = renderAudit();
    await loaded();
    await openScope(user);
    await user.type(screen.getByLabelText("Actor"), "user:dana@example.com");
    await user.selectOptions(screen.getByLabelText("Window"), "500");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(urls).toHaveLength(2));
    const applied = new URL(urls[1]!);
    expect(applied.searchParams.get("actor")).toBe("user:dana@example.com");
    expect(applied.searchParams.get("limit")).toBe("500");
    // An empty box is not a filter for the empty string.
    expect(applied.searchParams.has("action")).toBe(false);
  });

  it("sends viewer-local bounds as unambiguous instants and renders the mapping", async () => {
    stubViewerZone("America/New_York");
    const urls: string[] = [];
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json({ events: RECORDS, verification: VALID });
      }),
    );
    const user = renderAudit();
    await loaded();
    await openScope(user);
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-07-20T09:15" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-07-20T10:45" } });
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(urls).toHaveLength(2));
    const applied = new URL(urls[1]!);
    expect(applied.searchParams.get("from")).toBe("2026-07-20T13:15:00.000Z");
    expect(applied.searchParams.get("to")).toBe("2026-07-20T14:45:00.000Z");
    expect(screen.getByText(/Times use America\/New_York/)).toHaveTextContent(
      "2026-07-20T13:15:00.000Z to 2026-07-20T14:45:00.000Z",
    );
  });

  it("fails visibly on a DST fold and leaves the prior result window untouched", async () => {
    stubViewerZone("America/New_York");
    const urls: string[] = [];
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json({ events: RECORDS, verification: VALID });
      }),
    );
    const user = renderAudit();
    await loaded();
    await openScope(user);
    const before = seqs();
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-11-01T01:30" } });
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "From occurs twice in America/New_York because the clock changes at that time",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("loaded results were not changed");
    expect(urls).toHaveLength(1);
    expect(seqs()).toEqual(before);
  });

  it("fails visibly when From is later than To and does not issue a read", async () => {
    stubViewerZone("UTC");
    const urls: string[] = [];
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json({ events: RECORDS, verification: VALID });
      }),
    );
    const user = renderAudit();
    await loaded();
    await openScope(user);
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-07-21T00:00" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-07-20T00:00" } });
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByRole("alert")).toHaveTextContent("From must not be later than To");
    expect(urls).toHaveLength(1);
  });

  it("carries a deep-linked actor into the first read", async () => {
    const urls: string[] = [];
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json({ events: RECORDS, verification: VALID });
      }),
    );
    renderAudit("/audit?actor=user%3Adana%40example.com");
    await loaded();
    expect(new URL(urls[0]!).searchParams.get("actor")).toBe("user:dana@example.com");
  });

  it("highlights the entry a deep link names", async () => {
    respond();
    renderAudit("/audit?seq=4020");
    await loaded();
    const selected = ledger().querySelector("tr.ne-tr--selected") as HTMLElement;
    expect(within(selected).getByText("4020")).toBeInTheDocument();
  });

  /* --------------------------- §13 render: the scope -------------------------
   * "Scope collapsed by default, applied scope visible as removable tokens...
   * a narrowed window can never masquerade as the whole ledger." */

  it("the scope form is collapsed by default", async () => {
    respond();
    renderAudit();
    await loaded();
    // The paired control below opens it with the same button — collapsed is
    // not "missing the button", it is the fields being absent until it is used.
    expect(screen.queryByLabelText("Actor")).toBeNull();
    expect(screen.getByRole("button", { name: "Scope ▾" })).toBeInTheDocument();
  });

  it("Scope opens the form, and no scope applied renders no token", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    await openScope(user);
    expect(screen.getByLabelText("Actor")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Scope ▴" })).toBeInTheDocument();
    // The control for the tests below: an unnarrowed ledger carries no
    // dismissible token — a token with nothing behind it would be a lie about
    // what is applied.
    expect(screen.queryByRole("button", { name: /^Clear / })).toBeNull();
  });

  it("an applied scope renders a removable token even once the form collapses", async () => {
    const urls: string[] = [];
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json({ events: RECORDS, verification: VALID });
      }),
    );
    const user = renderAudit();
    await loaded();
    await openScope(user);
    await user.type(screen.getByLabelText("Actor"), "user:dana@example.com");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(urls).toHaveLength(2));
    // Collapse the form by hand — the token survives on its own.
    await user.click(screen.getByRole("button", { name: "Scope ▴" }));
    expect(screen.queryByLabelText("Actor")).toBeNull();
    const token = screen.getByText("Actor: user:dana@example.com");
    expect(token).toBeInTheDocument();
    // Removing the token clears the scope and re-reads — no need to re-open
    // the form to withdraw a narrowing it named.
    await user.click(screen.getByRole("button", { name: "Clear Actor: user:dana@example.com" }));
    await waitFor(() => expect(urls).toHaveLength(3));
    expect(new URL(urls[2]!).searchParams.has("actor")).toBe(false);
    expect(screen.queryByText("Actor: user:dana@example.com")).toBeNull();
  });
});

/* ------------------------- the anchored ?seq= link ------------------------- */

// A window AROUND entry 1300 — an entry ~14,400 records older than the head,
// which the most-recent page can never contain. Exactly what the anchored read
// returns for anchor_seq=1300.
const DEEP_WINDOW: AuditRecordView[] = [1302, 1301, 1300, 1299, 1298].map((seq) =>
  record({
    seq,
    event_id: `evt-${seq}`,
    event_type: "connector.sync",
    actor: "svc:connector",
    occurred_at: "2026-06-01T09:00:00.000Z",
  }),
);

/** The ledger read with a REAL anchor contract: the newest page unless the
 * request is anchored; an anchored request answers the window containing the
 * anchor (or the nearest one) plus the server's reachability verdict. Requests
 * are recorded so the tests can assert what was actually asked. */
function respondAnchorAware(
  verdictFor: (anchorSeq: string) => { events: AuditRecordView[]; anchor: AuditAnchorView },
): string[] {
  const urls: string[] = [];
  server.use(
    http.get("*/api/v1/audit/events", ({ request }) => {
      urls.push(request.url);
      const anchorSeq = new URL(request.url).searchParams.get("anchor_seq");
      if (anchorSeq === null) {
        return HttpResponse.json({ events: RECORDS, verification: VALID, anchor: null });
      }
      const { events, anchor } = verdictFor(anchorSeq);
      return HttpResponse.json({ events, verification: VALID, anchor });
    }),
  );
  return urls;
}

describe("§13 — the ?seq= deep link is ANCHORED, and a miss is loud", () => {
  it("lands highlighted on an entry far outside the default window", async () => {
    respondAnchorAware(() => ({
      events: DEEP_WINDOW,
      anchor: { seq: 1300, status: "found", head_seq: 15700 },
    }));
    renderAudit("/audit?seq=1300");
    await loaded();
    // The fetched window is the page CONTAINING the entry, not the newest page.
    expect(seqs()).toEqual(["1302", "1301", "1300", "1299", "1298"]);
    // …and the named entry itself carries the highlight, by row identity.
    const selected = ledger().querySelector("tr.ne-tr--selected") as HTMLElement;
    expect(selected).not.toBeNull();
    expect(within(selected).getByText("1300")).toBeInTheDocument();
    // A landed jump shows NO failure notice — the pair that makes the loud
    // tests below mean something.
    expect(screen.queryByRole("alert")).toBeNull();
    // The window line states the anchored position, never "most recent".
    expect(
      screen.getByText(
        "Showing 5 records around entry 1300. Narrow with filters or raise the window.",
      ),
    ).toBeInTheDocument();
  });

  it("an unreachable entry names ITSELF and WHERE THE LOG ENDS — the server's verdict, on screen", async () => {
    respondAnchorAware(() => ({
      events: RECORDS,
      anchor: { seq: 13252, status: "not_found", head_seq: 12000 },
    }));
    renderAudit("/audit?seq=13252");
    await loaded();
    // The message is SPECIFIC: which entry, and why. A reader can tell this
    // apart from every other miss and from an ordinary unfiltered list.
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Entry 13,252 isn't in the audit log — the log ends at entry 12,000.",
    );
    // No row pretends to be the landing point…
    expect(ledger().querySelector("tr.ne-tr--selected")).toBeNull();
    // …and the ledger itself still renders — the miss degrades the JUMP, not
    // the screen.
    expect(seqs()).toEqual(["4021", "4020", "4018", "4017"]);
  });

  it("an entry the applied filters exclude says SO — not 'not found' — and Clear filters recovers it", async () => {
    // With ?actor= applied the anchor is excluded; unfiltered it is found.
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("actor") !== null) {
          return HttpResponse.json({
            events: RECORDS,
            verification: VALID,
            anchor: { seq: 1300, status: "filtered_out", head_seq: 15700 },
          });
        }
        return HttpResponse.json({
          events: DEEP_WINDOW,
          verification: VALID,
          anchor: { seq: 1300, status: "found", head_seq: 15700 },
        });
      }),
    );
    const user = renderAudit("/audit?seq=1300&actor=user%3Adana%40example.com");
    await loaded();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Entry 1300 exists but the applied filters exclude it.",
    );
    // The recovery the notice offers: drop the filters, keep the jump.
    await user.click(
      within(screen.getByRole("alert")).getByRole("button", { name: "Clear filters" }),
    );
    await waitFor(() => expect(seqs()).toEqual(["1302", "1301", "1300", "1299", "1298"]));
    const selected = ledger().querySelector("tr.ne-tr--selected") as HTMLElement;
    expect(within(selected).getByText("1300")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("an entry hidden by the view chips says SO and Show entry reveals it highlighted", async () => {
    // Entry 4019 is an access refusal — hidden by the default chips. It was
    // FETCHED; the console itself is what hid it, and it must say so.
    respondAnchorAware(() => ({
      events: RECORDS,
      anchor: { seq: 4019, status: "found", head_seq: 4021 },
    }));
    const user = renderAudit("/audit?seq=4019");
    await loaded();
    expect(seqs()).not.toContain("4019");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Entry 4019 is in the loaded window but hidden by the current view filters.",
    );
    await user.click(within(screen.getByRole("alert")).getByRole("button", { name: "Show entry" }));
    expect(seqs()).toContain("4019");
    const selected = ledger().querySelector("tr.ne-tr--selected") as HTMLElement;
    expect(within(selected).getByText("4019")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a link that names no entry is loud, and no anchor is sent for it", async () => {
    const urls = respondAnchorAware(() => {
      throw new Error("no anchored request should be made for a malformed seq");
    });
    renderAudit("/audit?seq=bananas");
    await loaded();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "“bananas” isn't an audit entry number, so there is no entry to jump to.",
    );
    expect(urls).toHaveLength(1);
    expect(new URL(urls[0]!).searchParams.has("anchor_seq")).toBe(false);
  });

  it("Apply re-scopes the window: the anchor is dropped from the request AND from the screen", async () => {
    const urls = respondAnchorAware(() => ({
      events: DEEP_WINDOW,
      anchor: { seq: 1300, status: "found", head_seq: 15700 },
    }));
    const user = renderAudit("/audit?seq=1300");
    await loaded();
    expect(seqs()).toEqual(["1302", "1301", "1300", "1299", "1298"]);
    await openScope(user);
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(urls).toHaveLength(2));
    expect(new URL(urls[1]!).searchParams.has("anchor_seq")).toBe(false);
    await waitFor(() => expect(seqs()).toEqual(["4021", "4020", "4018", "4017"]));
    expect(ledger().querySelector("tr.ne-tr--selected")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

/* -------------------------------- the states ------------------------------ */

describe("§13 — the four states", () => {
  it("empty: the spec's sentence and a way out", async () => {
    respond([], { ...VALID, checked: 0 });
    renderAudit();
    expect(await screen.findByText("No records match these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("loading: skeleton rows in the table's own geometry, announced", async () => {
    respond();
    renderAudit();
    expect(
      screen.getAllByRole("status").some((n) => n.textContent === "Loading the audit log"),
    ).toBe(true);
    expect(ledger().querySelectorAll("tr.ne-tr--skeleton")).toHaveLength(3);
    await loaded();
  });

  it("refused: the SERVER's refusal, and no controls over a ledger nobody was shown", async () => {
    server.use(
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ detail: "not authorized" }, { status: 403 }),
      ),
    );
    renderAudit();
    expect(await screen.findByText(/Your role doesn.t include the audit log/)).toBeInTheDocument();
    expect(screen.getByLabelText(/GATED/)).toBeInTheDocument();
    expect(document.querySelector(".ne-toolbar")).toBeNull();
    expect(document.querySelector(".ne-audit__verdict")).toBeNull();
    // §13 render: "no controls over a surface this key cannot read" — Export
    // CSV moved to the title row with CR-B6, and must be withheld with the
    // toolbar it used to live inside, not merely disabled.
    expect(screen.queryByRole("button", { name: "Export CSV" })).toBeNull();
  });

  it("Export CSV renders (disabled) once a read at least reaches a verdict, unlike a refusal", async () => {
    // The control for the assertion above: a fault still gets the control
    // (disabled, per the existing export tests) — only a ROLE BOUNDARY
    // withholds it outright.
    server.use(http.get("*/api/v1/audit/events", () => HttpResponse.json({}, { status: 500 })));
    renderAudit();
    await screen.findByText(/Couldn.t load the audit log/);
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled();
  });

  it("error: an outage is NEVER drawn as a role boundary", async () => {
    server.use(http.get("*/api/v1/audit/events", () => HttpResponse.json({}, { status: 500 })));
    renderAudit();
    expect(await screen.findByText(/Couldn.t load the audit log/)).toBeInTheDocument();
    // The one thing this product may not do: state that a key was refused when
    // the read never reached a verdict.
    expect(screen.queryByText(/Your role doesn.t include/)).toBeNull();
    expect(document.querySelector(".ne-audit__verdict")).toBeNull();
    expect(countLine()).toBe("");
  });

  it("a chip carries no count over a window nobody was shown", async () => {
    server.use(http.get("*/api/v1/audit/events", () => HttpResponse.json({}, { status: 500 })));
    renderAudit();
    await screen.findByText(/Couldn.t load the audit log/);
    expect(screen.getByRole("button", { name: "Access refusals" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Access refusals \(/ })).toBeNull();
  });

  it("Retry reads again", async () => {
    let reads = 0;
    server.use(
      http.get("*/api/v1/audit/events", () => {
        reads += 1;
        return reads === 1
          ? HttpResponse.json({}, { status: 500 })
          : HttpResponse.json({ events: RECORDS, verification: VALID });
      }),
    );
    const user = renderAudit();
    await screen.findByText(/Couldn.t load the audit log/);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await loaded();
    expect(seqs()).toEqual(["4021", "4020", "4018", "4017"]);
  });
});

/* -------------------------------- the export ------------------------------ */

describe("§13 — export", () => {
  it("downloads the authoritative server bytes with verdict and completion evidence", async () => {
    const serverCsv = [
      "# neutron-audit-export v1",
      "# filter_actor: user:dana@example.com",
      "# rows_matched: 1",
      "# rows_exported: 1",
      "# truncated: false",
      "# verification_valid: false",
      "# verification_checked: 12479",
      "# verification_first_break_seq: 4020",
      "seq,actor,event_id,record_hash",
      `4020,'=SUM(A1),evt-tool,${"c3".repeat(32)}`,
      "# rows_written: 1",
      "# truncated_confirmed: false",
      "# export_complete: true",
      "",
    ].join("\r\n");
    const blobs: Blob[] = [];
    const names: string[] = [];
    const exportRequests: string[] = [];
    let releaseExport: (() => void) | undefined;
    const heldExport = new Promise<void>((resolve) => {
      releaseExport = resolve;
    });
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

    respond(RECORDS, BROKEN);
    server.use(
      http.get("*/api/v1/audit/export", async ({ request }) => {
        exportRequests.push(request.url);
        await heldExport;
        return new HttpResponse(serverCsv, {
          headers: { "Content-Type": "text/csv; charset=utf-8" },
        });
      }),
    );
    const user = renderAudit();
    await loaded();
    await openScope(user);
    await user.type(screen.getByLabelText("Actor"), "user:dana@example.com");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await loaded();

    const button = screen.getByRole("button", { name: "Export CSV" });
    await user.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    releaseExport?.();
    await waitFor(() => expect(blobs).toHaveLength(1));

    expect(await blobs[0]!.text()).toBe(serverCsv);
    const exportRequest = new URL(exportRequests[0]!);
    expect(exportRequest.pathname).toBe("/api/v1/audit/export");
    expect(exportRequest.searchParams.get("actor")).toBe("user:dana@example.com");
    expect(exportRequest.searchParams.get("limit")).toBe("100");
    expect(await blobs[0]!.text()).toContain("# verification_valid: false");
    expect(await blobs[0]!.text()).toContain("# export_complete: true");
    expect(await blobs[0]!.text()).toContain("4020,'=SUM(A1),evt-tool");
    await waitFor(() => expect(button).toBeEnabled());
    expect(names[0]).toBe("audit-neutron-demo-2026-07-20-2026-07-20.csv");
  });

  it("shows an export failure and never downloads a stale blob", async () => {
    const blobs: Blob[] = [];
    URL.createObjectURL = ((blob: Blob) => {
      blobs.push(blob);
      return "blob:stub";
    }) as typeof URL.createObjectURL;
    server.use(
      http.get("*/api/v1/audit/export", () =>
        HttpResponse.json({ detail: "audit store unavailable" }, { status: 503 }),
      ),
    );
    respond();
    const user = renderAudit();
    await loaded();

    const button = screen.getByRole("button", { name: "Export CSV" });
    await user.click(button);

    expect(await screen.findByText("Couldn’t save. Try again.")).toBeInTheDocument();
    expect(screen.getByText("audit store unavailable")).toBeInTheDocument();
    expect(blobs).toHaveLength(0);
    expect(button).toBeEnabled();
  });

  it("offers no export over a window that was never read", async () => {
    server.use(http.get("*/api/v1/audit/events", () => HttpResponse.json({}, { status: 500 })));
    renderAudit();
    await screen.findByText(/Couldn.t load the audit log/);
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled();
  });
});

/* ------------------------------- the pane --------------------------------- */

describe("§13 — the record pane", () => {
  it("opens on the row and shows the record's own fields", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    await user.click(within(ledger()).getByText("agent://sales-drafts"));
    const pane = await screen.findByRole("dialog");
    expect(within(pane).getByRole("heading", { name: "Entry 4020" })).toBeInTheDocument();
    expect(within(pane).getByText("tool_latency_p50_s")).toBeInTheDocument();
    expect(within(pane).getByText("trace-bravo")).toBeInTheDocument();
    // Hashes: 8 characters on screen, the whole value in `title`.
    const configHash = within(pane).getByTitle("a1".repeat(32));
    expect(configHash).toHaveTextContent("a1a1a1a1");
    expect(within(pane).getByRole("button", { name: "Copy record hash" })).toBeInTheDocument();
    expect(within(pane).getByRole("link", { name: "Open trace" })).toHaveAttribute(
      "href",
      "/trace/trace-bravo",
    );
  });

  it("shows the payload as inert text, and the em dash when there is none", async () => {
    respond();
    const user = renderAudit();
    await loaded();
    await user.click(within(ledger()).getByText("user:dana@example.com"));
    const pane = await screen.findByRole("dialog");
    expect(pane.querySelector(".ne-audit__json")?.textContent).toContain('"rule": "wire-limit"');
    await user.click(within(pane).getByRole("button", { name: "Close" }));
    await user.click(within(ledger()).getByText("svc:connector"));
    const second = await screen.findByRole("dialog");
    expect(second.querySelector(".ne-audit__json")?.textContent).toBe("—");
  });

  it("replays the decision into the pane", async () => {
    respond();
    server.use(
      http.get("*/api/v1/audit/replay", ({ request }) => {
        expect(new URL(request.url).searchParams.get("event_id")).toBe("evt-decision");
        return HttpResponse.json({
          events: [RECORDS[0]!, RECORDS[4]!],
          config_hashes: ["a1".repeat(32)],
          context_ids: ["chunk-alpha", "chunk-bravo"],
        });
      }),
    );
    const user = renderAudit();
    await loaded();
    await user.click(within(ledger()).getByText("user:dana@example.com"));
    const pane = await screen.findByRole("dialog");
    await user.click(within(pane).getByRole("button", { name: "Replay decision" }));
    const replayed = await within(pane).findByRole("table", { name: "Replayed events" });
    expect(within(replayed).getByText("4021")).toBeInTheDocument();
    expect(within(replayed).getByText("4017")).toBeInTheDocument();
    expect(within(pane).getByText("Config hashes:")).toBeInTheDocument();
    expect(within(pane).getByText("chunk-alpha, chunk-bravo")).toBeInTheDocument();
  });

  it("a failed replay says so and offers the way back", async () => {
    respond();
    server.use(http.get("*/api/v1/audit/replay", () => HttpResponse.json({}, { status: 500 })));
    const user = renderAudit();
    await loaded();
    await user.click(within(ledger()).getByText("user:dana@example.com"));
    const pane = await screen.findByRole("dialog");
    await user.click(within(pane).getByRole("button", { name: "Replay decision" }));
    expect(await within(pane).findByText(/Couldn.t replay this decision/)).toBeInTheDocument();
    expect(within(pane).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // A server fault is not a role boundary.
    expect(within(pane).queryByText(/Your role doesn.t include/)).toBeNull();
  });
});

/* ------------------------------ no internals ------------------------------ */

describe("§0 — no repo internals on screen", () => {
  it("renders no endpoint, no verb, no ticket id, and no self-narration", async () => {
    respond(RECORDS, BROKEN);
    renderAudit();
    await loaded();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/\/v1\//);
    expect(text).not.toMatch(/\bGET\b|\bPOST\b/);
    expect(text).not.toMatch(/NE-\d{2,}/);
    expect(text).not.toMatch(/\.tsx?\b|\.py\b/);
    // …and no raw audit code either: `pdp.decide` / `api.audit.unauthorized`
    // are identifiers, and identifiers live on the `title` and in the record
    // pane, not in the running text of a screen a buyer reads.
    expect(text).not.toMatch(/pdp\.decide|approval\.approved|tool\.execute/);
    for (const word of ["governed", "proven", "verbatim", "recomputes", "server-side"]) {
      expect(text.toLowerCase()).not.toContain(word);
    }
  });
});

describe("Run → Audit trace replay deep link", () => {
  it("requests the one exact trace identity and preserves every server order", async () => {
    respond();
    const seen: string[] = [];
    server.use(
      http.get("*/api/v1/audit/replay", ({ request }) => {
        const url = new URL(request.url);
        seen.push(url.search);
        return HttpResponse.json({
          events: [RECORDS[4]!, RECORDS[0]!],
          config_hashes: ["b2".repeat(32), "a1".repeat(32)],
          context_ids: ["context-zulu", "context-alpha"],
        });
      }),
    );

    renderAudit("/audit?trace_id=trace%2Fexact-2");
    const replay = await screen.findByRole("region", { name: "Replay for trace trace/exact-2" });
    await within(replay).findByText("evt-approval");

    expect(seen).toEqual(["?trace_id=trace%2Fexact-2"]);
    expect(within(replay).getByRole("list", { name: "Replayed events" })).toHaveTextContent(
      /4017.*evt-approval.*Approval granted.*4021.*evt-decision.*Policy decision/,
    );
    expect(within(replay).getByRole("list", { name: "Replay config hashes" }).textContent).toMatch(
      /b2b2b2b2.*a1a1a1a1/,
    );
    expect(within(replay).getByRole("list", { name: "Replay context IDs" })).toHaveTextContent(
      /context-zulu.*context-alpha/,
    );
  });

  it("does not replay ambiguous, empty, or overlong trace parameters", async () => {
    respond();
    let calls = 0;
    server.use(
      http.get("*/api/v1/audit/replay", () => {
        calls += 1;
        return HttpResponse.json({ events: [], config_hashes: [], context_ids: [] });
      }),
    );

    for (const entry of [
      "/audit?trace_id=one&trace_id=two",
      "/audit?trace_id=%20%20",
      `/audit?trace_id=${"x".repeat(257)}`,
    ]) {
      const view = renderAudit(entry);
      await loaded();
      cleanup();
      void view;
    }
    expect(calls).toBe(0);
  });

  it.each([
    [403, /Your role doesn.t include replaying this trace/],
    [500, /Couldn.t load the replay/],
  ])("renders replay HTTP %s truthfully", async (status, message) => {
    respond();
    server.use(http.get("*/api/v1/audit/replay", () => HttpResponse.json({}, { status })));
    renderAudit("/audit?trace_id=trace-refused-or-error");
    const replay = await screen.findByRole("region", {
      name: "Replay for trace trace-refused-or-error",
    });
    expect(await within(replay).findByText(message)).toBeInTheDocument();
  });

  it("synchronously hides the old replay when the URL identity changes", async () => {
    respond();
    server.use(
      http.get("*/api/v1/audit/replay", ({ request }) => {
        const trace = new URL(request.url).searchParams.get("trace_id");
        if (trace === "trace-old") {
          return HttpResponse.json({
            events: [record({ seq: 88, event_id: "ONLY-OLD-EVENT" })],
            config_hashes: ["OLD-CONFIG-HASH"],
            context_ids: ["OLD-CONTEXT-ID"],
          });
        }
        return new Promise(() => undefined);
      }),
    );
    const user = userEvent.setup();
    render(
      <AuthStub>
        <RuntimeConfigContext.Provider value={CONFIG}>
          <MemoryRouter initialEntries={["/audit?trace_id=trace-old"]}>
            <PrincipalProvider>
              <DirectoryProvider>
                <ReplayRouteSwitch />
              </DirectoryProvider>
            </PrincipalProvider>
          </MemoryRouter>
        </RuntimeConfigContext.Provider>
      </AuthStub>,
    );
    expect(await screen.findByText("ONLY-OLD-EVENT")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next trace" }));
    expect(screen.queryByText("ONLY-OLD-EVENT")).toBeNull();
    expect(screen.queryByText("OLD-CONFIG-HASH")).toBeNull();
    expect(screen.queryByText("OLD-CONTEXT-ID")).toBeNull();
    expect(screen.getByRole("region", { name: "Replay for trace trace-next" })).toBeInTheDocument();
  });
});

/* -------------------- one chain, two screens, one word -------------------- */

describe("§13/§13b — /audit and /trace never contradict each other about the chain", () => {
  // `assemble_trace` calls the SAME unbounded `verify_chain` the audit list
  // calls, so these two screens render one verdict about ONE chain — and /trace
  // is reached by clicking a row on /audit, so a reader sees both within a
  // click of each other. When only one of them was latched, the console said
  // `⚠ BREAK · Break at entry 4020` on the ledger and `● VALID` on the run, at
  // one instant, about one chain. That is worse than either state alone: the
  // reader cannot tell which screen to believe, and the honest one is not
  // identifiable from the screen.
  //
  // Both now read the SAME latch and render through the same `chainReading`,
  // so the same run of server verdicts resolves to the same word on both.

  /** The word on whichever verdict strip is mounted. */
  function stripWord(): string {
    const pill = document.querySelector(
      ".ne-audit__verdict .ne-pill__word, .ne-trace__verdict .ne-pill__word",
    );
    return pill?.textContent ?? "none";
  }

  /** The word each screen COMES TO REST on. RE-VERIFYING is by construction not
   * a resting state — both screens go and ask again — so waiting past it is
   * what "what does this screen say" means for a verdict. */
  async function resting(): Promise<string> {
    await waitFor(() => expect(["VALID", "BREAK"]).toContain(stripWord()), { timeout: LATCH_WAIT });
    return stripWord();
  }

  async function auditSays(verdicts: readonly ChainVerificationView[]): Promise<string> {
    respondPolled(verdicts, verdicts[0]);
    renderAudit("/audit", true);
    await loaded();
    return resting();
  }

  async function traceSays(verdicts: readonly ChainVerificationView[]): Promise<string> {
    let n = 0;
    server.use(
      // The shell's poll cannot read the chain here, so /trace's own reads are
      // the only observations there are. /trace folds into the SHELL's latch now
      // (there is one chain, so there is one latch), which is why the provider is
      // mounted — as it is in the app, on every route.
      http.get("*/api/v1/audit/events", () =>
        HttpResponse.json({ detail: "audit unavailable" }, { status: 502 }),
      ),
      http.get("*/api/v1/trace/:traceId", () => {
        const verification = verdicts[Math.min(n, verdicts.length - 1)]!;
        n += 1;
        return HttpResponse.json({ ...TRACE_ASSEMBLY, verification });
      }),
    );
    render(
      <AuthStub>
        <MemoryRouter initialEntries={[`/trace/${TRACE_ID}`]}>
          <PrincipalProvider>
            <AuditPollProvider intervalMs={60_000}>
              <Routes>
                <Route path="/trace/:traceId" element={<TracePage />} />
              </Routes>
            </AuditPollProvider>
          </PrincipalProvider>
        </MemoryRouter>
      </AuthStub>,
    );
    return resting();
  }

  it("one straddled read rests as VALID on BOTH screens", async () => {
    // The reproduced race: one read disagrees, the next one withdraws it.
    //
    // SAME INSTRUMENT ON BOTH READS, which is what the race produces: a
    // full-chain invalid clears the anchor, so the next request-path read walks
    // the chain inline too, and /trace's `assemble_trace` is unbounded on every
    // read. A weaker passing read would not withdraw this one, and must not —
    // see "an ANCHORED pass cannot erase the confirmations a FULL walk
    // accumulated".
    const race = [BROKEN, VALID_FULL];
    expect(await auditSays(race)).toBe("VALID");
    cleanup();
    expect(await traceSays(race)).toBe("VALID");
  });

  it("a chain that is really broken rests as BREAK on BOTH screens", async () => {
    // The bidirectional half. Every read disagrees, so both screens promote and
    // stand on the break — neither one is quietly softening a real finding.
    const broken = [BROKEN];
    expect(await auditSays(broken)).toBe("BREAK");
    cleanup();
    expect(await traceSays(broken)).toBe("BREAK");
  });
});

/* ===========================================================================
 * I4 END-TO-END — A WEAKER PROOF MAY NOT WITHDRAW A STRONGER PROOF'S FINDING.
 *
 * The polled read is an `anchored-extension` proof: it does not re-read the
 * prefix at all, so a prefix row tampered after the anchor was blessed is
 * invisible to it BY CONSTRUCTION while a full walk names it. Before this rule,
 * the operator's Re-verify resolved through that poll and a passing anchored
 * verdict withdrew a break a full walk had confirmed — detected, then
 * UN-detected, which is worse than never detecting.
 * ======================================================================== */

describe("§13 — the sweep that clears a latch must be as strong as the one that made it", () => {
  it("an ANCHORED pass does not clear a break the FULL walk confirmed", async () => {
    // The server here answers every read — sweep included — with the anchored
    // proof, which is exactly the state a deployment is in while the anchor is
    // fresh and the tamper is below it. The break stands.
    let sweeps = 0;
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.get("limit") !== "25") {
          return HttpResponse.json({ events: RECORDS, verification: VALID });
        }
        if (params.get("full_verify") === "true") {
          sweeps += 1;
          return HttpResponse.json({ events: RECORDS, verification: VALID });
        }
        return HttpResponse.json({
          events: RECORDS,
          verification: sweeps === 0 ? BROKEN : VALID,
        });
      }),
    );
    const user = renderAudit("/audit", true);
    await latchedBreak();

    await user.click(screen.getByRole("button", { name: "Re-verify" }));
    await waitFor(() => expect(sweeps).toBeGreaterThan(0), { timeout: LATCH_WAIT });
    // Give the poll several more passing anchored reads to withdraw it with.
    await new Promise((ok) => setTimeout(ok, POLL_MS * 8));

    // Several pills carry the word once a break stands (the strip's and the
    // accused row's), so the strip is identified by its sentence.
    expect(screen.getAllByLabelText("BREAK").length).toBeGreaterThan(0);
    expect(screen.getByText("Break at entry 4020")).toBeInTheDocument();
    expect(screen.queryByLabelText("VALID")).toBeNull();
    expect(document.querySelector(".ne-audit__band")).not.toBeNull();
  });

  it("a ONE-SHOT contradiction latches BREAK from a single poll", async () => {
    // The verdict that survives exactly one response: the ledger is internally
    // perfect and no longer extends the head this process verified, and the
    // server re-anchors on its way out so every read after it PASSES. Three
    // consecutive disagreements are unobtainable, so nothing but the server's
    // own flag can put this on a screen.
    let polls = 0;
    server.use(
      http.get("*/api/v1/audit/events", ({ request }) => {
        if (new URL(request.url).searchParams.get("limit") !== "25") {
          return HttpResponse.json({ events: RECORDS, verification: VALID });
        }
        polls += 1;
        return HttpResponse.json({
          events: RECORDS,
          verification: polls === 1 ? CONTRADICTION : VALID,
        });
      }),
    );
    renderAudit("/audit", true);
    await loaded();

    await screen.findByText(
      "Break confirmed — the server did not name the entry",
      {},
      { timeout: LATCH_WAIT },
    );
    expect(screen.getAllByLabelText("BREAK").length).toBeGreaterThan(0);
    expect(spineCuts()).toEqual([]);

    // And it STANDS over the passing reads that follow.
    await new Promise((ok) => setTimeout(ok, POLL_MS * 8));
    expect(screen.getAllByLabelText("BREAK").length).toBeGreaterThan(0);
    expect(polls).toBeGreaterThan(BREAK_CONFIRMATIONS);
  });
});
