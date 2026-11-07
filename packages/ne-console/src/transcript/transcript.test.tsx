// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE TRANSCRIPT, STATE BY STATE — the six states drawn in ask-states.html plus
// the Run variant from agent-run.html.
//
// The fixtures are built from the REAL wire shapes, through the little adapters
// below, so these tests fail if the shapes move: `ToolTransitEvent` (api/types,
// the governed transit Ask streams) and `AgentRunWire` (api/types, one canonical
// Run). Nothing here hand-writes a view model that the wire could not produce.
//
// WHAT EACH CONTROL IS FOR. The load-bearing claim of this unit is a CONTAINMENT
// claim — gates outside the fold, routine steps inside it — and a containment
// claim is provable only in both directions. Every "the gate is outside / is
// visible collapsed" assertion is therefore paired with the step-row assertion
// that would ALSO pass if the fold had simply been deleted.

import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentRunWire, ToolTransitEvent } from "../api/types";
import { classifyAnswer } from "../chat/answerFrames";
import { Transcript, TranscriptTurnView } from "./Transcript";
import type {
  TranscriptFault,
  TranscriptRefusal,
  TranscriptStep,
  TranscriptStopped,
  TranscriptTurn,
} from "./types";

/* --------------------------------------------------------------------------
 * Adapters — the shape CR-B3 and CR-B4 will each own. Kept here so the module
 * is exercised through the wire and not through a convenient invention.
 * ------------------------------------------------------------------------ */

/** One governed transit off the Ask stream, as one transcript step. The wire
 * carries NO duration for a transit (api/types, ToolTransitEvent) — so the
 * adapter passes null and the row draws no time slot at all. */
function stepFromTransit(event: ToolTransitEvent, key: string): TranscriptStep {
  const verdict =
    event.decision === "allow"
      ? "allowed"
      : event.decision === "pending"
        ? "pending"
        : event.decision === "deny" || event.decision === "rejected"
          ? "refused"
          : "failed";
  return {
    key,
    name: event.name,
    verdict,
    verdictLabel:
      verdict === "allowed"
        ? "Allowed"
        : verdict === "pending"
          ? "Awaiting approval"
          : verdict === "refused"
            ? "Refused"
            : "Error",
    tier: event.tier,
    durationSeconds: null,
    reference: event.approval_id ?? null,
  };
}

function transit(over: Partial<ToolTransitEvent> = {}): ToolTransitEvent {
  return {
    type: "tool",
    name: "read.knowledge.search",
    tier: null,
    decision: "allow",
    audit_seq: null,
    ...over,
  };
}

function runWire(over: Partial<AgentRunWire> = {}): AgentRunWire {
  return {
    run_id: "run-3",
    conversation_id: "conv-1",
    agent_id: "agent-1",
    agent_version: 1,
    sequence: 3,
    version: 4,
    predecessor_run_id: "run-2",
    root_run_id: "run-1",
    retry_ordinal: 1,
    display_label: "Run 3",
    strategy: "unavailable",
    status: "completed",
    output_verdict: "verified",
    model_policy_id: null,
    model_request_mode: "auto",
    context_snapshot_hash: null,
    requested_model: null,
    requested_model_lane: null,
    served_model: "qwen3-32b",
    route_position: "unavailable",
    fallback_from: null,
    output_preview: "The Master Contract for Beta LLC renews on 30 September.",
    config_hash: "cfg",
    capability_snapshot_hash: "cap",
    created_at: "2026-08-24T11:58:00Z",
    updated_at: "2026-08-24T11:58:08Z",
    terminal_at: "2026-08-24T11:58:08Z",
    ...over,
  };
}

/* ------------------------------------------------------------- fixtures -- */

const COMPLETE: TranscriptTurn = {
  id: "turn-complete",
  question: { text: "what is the Engineering incident response SLA?", meta: "12:02" },
  steps: [
    { ...stepFromTransit(transit(), "t1"), durationSeconds: 1.4 },
    {
      ...stepFromTransit(transit({ name: "read.graph.neighborhood" }), "t2"),
      durationSeconds: 0.7,
    },
  ],
  workElapsedSeconds: 2.1,
  answer: {
    label: "Answer",
    body: classifyAnswer("For urgent (P1) issues, the SLA is 15 minutes to acknowledgement.")
      .answer,
  },
  sources: [{ key: "c1", ordinal: 1, system: "sharepoint", document: "doc-alpha" }],
  receipt: { outcome: "Complete", tone: "ok", model: "qwen3-32b" },
};

const AWAITING: TranscriptTurn = {
  id: "turn-awaiting",
  question: { text: "Draft a CRM note for Bluepeak Dynamics", meta: "12:04" },
  steps: [
    { ...stepFromTransit(transit(), "t1"), durationSeconds: 1.7 },
    {
      ...stepFromTransit(
        transit({
          name: "write.internal.crm.note",
          tier: 2,
          decision: "pending",
          approval_id: "approval-7c19",
        }),
        "t2",
      ),
      sentence: "Tier 2 write, parked. It executes only after a decision is recorded in Approvals.",
    },
  ],
  workElapsedSeconds: 4.8,
  answer: {
    label: "Answer",
    body: "A note for Bluepeak Dynamics LLC is drafted and parked for approval.",
  },
  receipt: {
    outcome: "Paused",
    tone: "paused",
    facts: [{ key: "decision", value: "Awaiting a decision" }],
    model: "qwen3-32b",
  },
};

const REFUSED: TranscriptTurn = {
  id: "turn-refused",
  question: { text: "email the incident summary to the Beta LLC list", meta: "12:07" },
  steps: [
    { ...stepFromTransit(transit(), "t1"), durationSeconds: 0.9 },
    {
      ...stepFromTransit(
        transit({ name: "write.comms.email.send", tier: 3, decision: "deny", audit_seq: 4021 }),
        "t2",
      ),
      sentence:
        "Refused by policy: this action is not permitted for your role in this workspace. Nothing was sent.",
      reference: "decision-4b21",
    },
  ],
  workElapsedSeconds: 0.9,
  answer: {
    label: "Answer",
    body: "The send was refused, so no email went out. The summary is drafted below.",
  },
  receipt: { outcome: "Complete", tone: "ok", model: "qwen3-32b" },
};

/** The turn broke: nothing answered. A FAULT, and retrying it is sensible. */
const TURN_FAULT: TranscriptFault = {
  tone: "fault",
  state: "Turn failed",
  sentence: "The metrics source did not respond within the time limit. Nothing was written.",
};

const ERRORED: TranscriptTurn = {
  id: "turn-error",
  question: { text: "compare current uptime figures across regions", meta: "12:09" },
  steps: [
    { ...stepFromTransit(transit(), "t1"), durationSeconds: 1.1 },
    {
      ...stepFromTransit(transit({ name: "read.metrics.query", decision: "failed" }), "t2"),
      durationSeconds: 30,
    },
  ],
  workElapsedSeconds: 31.1,
  failure: TURN_FAULT,
  // The receipt takes the FAILURE'S OWN tone (types.ts), so this fixture cannot
  // be written with a footer that contradicts its card.
  receipt: { outcome: "Failed", tone: TURN_FAULT.tone, model: null },
};

/** The same shape of turn, ended for the other reason: the decision point
 * refused the whole request. The words are the ones chat/TurnFailureReadout.tsx
 * puts on this card — §5's stated line, and the server's own reason verbatim. */
const TURN_REFUSAL: TranscriptRefusal = {
  tone: "refusal",
  state: "Refused",
  sentence: "Refused by policy.",
  reason: "PolicyDenied: read.finance.ledger is tier 3 for this principal",
};

const REFUSED_TURN: TranscriptTurn = {
  id: "turn-refused",
  question: { text: "show me the finance ledger for Q3", meta: "12:14" },
  steps: [{ ...stepFromTransit(transit(), "t1"), durationSeconds: 0.6 }],
  workElapsedSeconds: 0.6,
  failure: TURN_REFUSAL,
  receipt: { outcome: "Refused", tone: TURN_REFUSAL.tone, model: null },
};

/** The third way a turn ends without an answer: the READER ended it. Neither a
 * fault nor a refusal — the words are chat/TurnFailureReadout.tsx's, and §5's
 * stopped line is honest about what Stop does. */
const TURN_STOPPED: TranscriptStopped = {
  tone: "stopped",
  state: "Stopped",
  sentence:
    "Stopped. The turn may still finish on the server; an approval it requested stays in Approvals.",
};

const STOPPED_TURN: TranscriptTurn = {
  id: "turn-stopped",
  question: { text: "summarise every incident this quarter", meta: "12:19" },
  steps: [{ ...stepFromTransit(transit(), "t1"), durationSeconds: 0.4 }],
  workElapsedSeconds: 0.4,
  failure: TURN_STOPPED,
  receipt: { outcome: "Stopped", tone: TURN_STOPPED.tone, model: null },
};

const STREAMING: TranscriptTurn = {
  id: "turn-streaming",
  question: { text: "summarise open incidents for Beta LLC", meta: "just now" },
  steps: [
    {
      ...stepFromTransit(transit({ name: "read.crm.account_lookup" }), "t1"),
      durationSeconds: 0.8,
    },
    {
      ...stepFromTransit(transit({ name: "read.knowledge.search" }), "t2"),
      verdict: "running",
      verdictLabel: "Running",
      durationSeconds: 4,
    },
  ],
  progress: { elapsedSeconds: 12, liveness: "last word from the server 1s ago" },
  answer: { label: "Answer", body: "Beta LLC has three open incidents.", streaming: true },
};

const LONG_RUN: TranscriptTurn = {
  id: "turn-long",
  question: { text: "prepare a renewal brief for Beta LLC", meta: "12:12" },
  steps: [
    "read.crm.account_lookup",
    "read.contracts.search",
    "read.contracts.clauses",
    "read.tickets.search",
    "read.incidents.history",
    "read.knowledge.search",
    "read.graph.neighborhood",
  ].map((name, i) => ({
    ...stepFromTransit(transit({ name }), `t${i}`),
    durationSeconds: 1.1,
  })),
  workElapsedSeconds: 9.9,
  answer: { label: "Answer", body: "Beta LLC renews on 30 September." },
  receipt: { outcome: "Complete", tone: "ok", model: "qwen3-32b" },
};

function show(turn: TranscriptTurn) {
  return render(
    <ol className="tr-turns">
      <TranscriptTurnView turn={turn} />
    </ol>,
  );
}

function band(container: HTMLElement): HTMLDetailsElement {
  const el = container.querySelector("details.tr-work");
  if (el === null) throw new Error("no work band rendered");
  return el as HTMLDetailsElement;
}

/** What a screen reader is left with: the text minus every aria-hidden subtree. */
function accessibleText(el: HTMLElement): string {
  const clone = el.cloneNode(true) as HTMLElement;
  for (const hidden of clone.querySelectorAll("[aria-hidden='true']")) hidden.remove();
  return (clone.textContent ?? "").replace(/\s+/g, " ").trim();
}

function turnOf(container: HTMLElement): HTMLElement {
  const el = container.querySelector("li.tr-turn");
  if (el === null) throw new Error("no turn rendered");
  return el as HTMLElement;
}

function failCard(container: HTMLElement): HTMLElement {
  const el = container.querySelector(".tr-fail");
  if (el === null) throw new Error("no failure card rendered");
  return el as HTMLElement;
}

/** The glyph half of the glyph-plus-word readout, which is aria-hidden and so
 * is exactly what `accessibleText` removes. */
function glyphOf(card: HTMLElement): string {
  return card.querySelector(".tr-fail__state [aria-hidden='true']")?.textContent ?? "";
}

function receiptIn(container: HTMLElement): HTMLElement {
  const el = container.querySelector(".tr-receipt");
  if (el === null) throw new Error("no receipt rendered");
  return el as HTMLElement;
}

/** The receipt's half of the same glyph-plus-word readout. */
function receiptGlyphOf(receipt: HTMLElement): string {
  return receipt.querySelector(".tr-receipt__glyph")?.textContent ?? "";
}

/** The tone an element wears, read off its own modifier class. Throws rather
 * than returning "" for an element wearing none: a card or a footer that
 * declared no tone is the state this unit exists to make impossible. */
function toneOf(el: HTMLElement, block: string): string {
  const m = new RegExp(`\\b${block}--([a-z]+)\\b`).exec(el.className);
  if (m?.[1] === undefined) throw new Error(`${block} wears no tone: ${el.className}`);
  return m[1];
}

/* ============================================================== the band == */

describe("the work band — routine steps, demoted and folded", () => {
  it("is closed on arrival for a settled turn", () => {
    const { container } = show(COMPLETE);
    expect(band(container).open).toBe(false);
  });

  it("summarises count, tool names, verdict tally and elapsed on its closed line", () => {
    const { container } = show(COMPLETE);
    const summary = band(container).querySelector("summary");
    const text = summary?.textContent ?? "";
    expect(text).toContain("2 steps");
    expect(text).toContain("read.knowledge.search");
    expect(text).toContain("read.graph.neighborhood");
    expect(text).toContain("2 allowed");
    expect(text).toContain("2.1s");
  });

  it("leaves the whole précis readable to assistive technology", () => {
    // The render pins an aria-label ("Steps in this turn") on the summary. An
    // aria-label REPLACES the summary's text for assistive technology, which
    // would hide the very précis the design calls the audit summary. Divergence
    // taken deliberately; this test is what keeps it from drifting back.
    const { container } = show(COMPLETE);
    const summary = band(container).querySelector("summary");
    expect(summary?.getAttribute("aria-label")).toBeNull();
    expect(summary?.getAttribute("aria-labelledby")).toBeNull();
    // What is actually announced: the text with every aria-hidden subtree
    // removed. All four parts of the précis survive it; only the decorative
    // chevron and the tally glyph do not.
    const spoken = accessibleText(summary as HTMLElement);
    expect(spoken).toContain("2 steps");
    expect(spoken).toContain("read.knowledge.search · read.graph.neighborhood");
    expect(spoken).toContain("2 allowed");
    expect(spoken).toContain("2.1s");
    expect(spoken).not.toContain("▶");
  });

  it("singularises a one-step band", () => {
    const { container } = show({ ...COMPLETE, steps: [COMPLETE.steps[0]!] });
    expect(band(container).querySelector("summary")?.textContent).toContain("1 step");
  });

  it("holds seven steps on one closed line and reveals seven rows when opened", async () => {
    const { container } = show(LONG_RUN);
    expect(band(container).open).toBe(false);
    const summary = band(container).querySelector("summary");
    expect(summary?.textContent).toContain("7 steps");
    expect(summary?.textContent).toContain("read.incidents.history");
    expect(container.querySelectorAll(".tr-work__steps .tr-step")).toHaveLength(7);
    await userEvent.click(summary as HTMLElement);
    expect(band(container).open).toBe(true);
  });

  it("opens itself while the server says a step is running", () => {
    const { container } = show(STREAMING);
    expect(band(container).open).toBe(true);
  });

  it("draws no time slot at all for the elapsed the wire did not carry", () => {
    // Owner ruling, 2026-08-24: no wire serves a step or band duration yet, so
    // an em dash here would appear on every row of every turn — an absence the
    // reader can do nothing about, on the one surface built for legibility. The
    // slot is omitted entirely: no dash, no note, no element holding space.
    const { container } = show({ ...COMPLETE, workElapsedSeconds: null });
    expect(container.querySelector(".tr-work__time")).toBeNull();
    const summary = container.querySelector(".tr-work__sum");
    expect(summary?.textContent).not.toContain("—");
    expect(summary?.textContent).not.toContain("no duration served");
    // CONTROL — the same band, one served number, and the slot is back. Without
    // this the suppression above would be satisfied by deleting the slot.
    const served = show({ ...COMPLETE, workElapsedSeconds: 2.1 });
    expect(served.container.querySelector(".tr-work__time")?.textContent).toBe("2.1s");
  });

  it("draws a step's time slot only for the steps whose duration was served", () => {
    // One band, two rows, one served duration between them: the suppression is
    // per row, and the served number is untouched by it.
    const { container } = show({
      ...COMPLETE,
      steps: [
        { ...COMPLETE.steps[0]!, durationSeconds: 1.4 },
        { ...COMPLETE.steps[1]!, durationSeconds: null },
      ],
    });
    const rows = container.querySelectorAll(".tr-work__steps .tr-step");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.querySelector(".tr-step__time")?.textContent).toBe("1.4s");
    expect(rows[1]?.querySelector(".tr-step__time")).toBeNull();
    expect(rows[1]?.textContent).not.toContain("no duration served");
  });

  it("draws a step's tier slot ONLY when a tier was resolved", () => {
    // "Tier —" used to draw on every row of every band. The Runs event wire
    // carries a tier on no kind at all, so a fifteen-row band repeated the same
    // dash fifteen times and buried the part of each line that said something.
    const { container } = show({
      ...COMPLETE,
      steps: [
        { ...COMPLETE.steps[0]!, tier: null },
        { ...COMPLETE.steps[1]!, tier: 3 },
      ],
    });
    const rows = container.querySelectorAll(".tr-work__steps .tr-step");
    expect(rows[0]?.textContent).not.toContain("Tier");
    expect(rows[0]?.textContent).not.toContain("—");
    // CONTROL — a resolved tier is a governance fact and still always draws,
    // so the suppression above cannot be satisfied by deleting the slot.
    expect(rows[1]?.textContent).toContain("Tier 3");
  });

  it("marks the FRONTIER row and only it, and never counts it as a verdict", () => {
    const { container } = show({
      ...COMPLETE,
      steps: [
        { ...COMPLETE.steps[0]!, tier: null },
        { ...COMPLETE.steps[1]!, tier: null, latest: true },
      ],
    });
    const rows = container.querySelectorAll(".tr-work__steps .tr-step");
    expect(rows[0]?.querySelector(".tr-step__latest")).toBeNull();
    expect(rows[1]?.querySelector(".tr-step__latest")).not.toBeNull();
    // It is a statement about the RECORD, not a verdict: the précis tally is
    // the same with and without it, so no row can be counted as "running" for
    // being the newest one.
    const marked = container.querySelector(".tr-work__verdict")?.textContent;
    const plain = show({ ...COMPLETE }).container.querySelector(".tr-work__verdict")?.textContent;
    expect(marked).toBe(plain);
  });
});

/* ========================================================= the promotion == */

describe("a step pending a human decision promotes out of the fold", () => {
  it("renders the gate card OUTSIDE the collapsible region", () => {
    const { container } = show(AWAITING);
    const gate = container.querySelector(".tr-gate");
    expect(gate).not.toBeNull();
    expect(band(container).contains(gate)).toBe(false);
    expect(turnOf(container).contains(gate)).toBe(true);
    // CONTROL — the same containment question, opposite answer. Without this a
    // deleted fold would satisfy the assertion above.
    const row = container.querySelector(".tr-work__steps .tr-step");
    expect(band(container).contains(row)).toBe(true);
  });

  it("is visible with the band collapsed, where the step rows are not", () => {
    const { container } = show(AWAITING);
    expect(band(container).open).toBe(false);
    expect(container.querySelector(".tr-gate")).toBeVisible();
    // CONTROL: the demoted rows are genuinely folded away, so the visibility
    // assertion above is measuring the fold and not merely presence in the DOM.
    expect(container.querySelector(".tr-work__steps .tr-step")).not.toBeVisible();
  });

  it("states the server's sentence verbatim and shows the governed reference", () => {
    const { container } = show(AWAITING);
    const gate = container.querySelector(".tr-gate");
    expect(gate?.querySelector(".tr-gate__body")?.textContent).toBe(
      "Tier 2 write, parked. It executes only after a decision is recorded in Approvals.",
    );
    expect(gate?.querySelector(".tr-gate__ref")?.textContent).toBe("approval-7c19");
    expect(gate?.textContent).toContain("write.internal.crm.note");
    expect(gate?.textContent).toContain("Tier 2");
  });

  it("leaves the promoted step out of the band's count, names and tally", () => {
    const { container } = show(AWAITING);
    const summary = band(container).querySelector("summary")?.textContent ?? "";
    expect(summary).toContain("1 step");
    expect(summary).toContain("1 allowed");
    expect(summary).not.toContain("write.internal.crm.note");
    expect(container.querySelectorAll(".tr-work__steps .tr-step")).toHaveLength(1);
  });
});

describe("a refused call and a failed call are drawn differently on purpose", () => {
  it("gives the refused call the gated card, and the answer still says what did not happen", () => {
    const { container } = show(REFUSED);
    const gate = container.querySelector(".tr-gate");
    expect(gate?.className).toContain("tr-gate--refused");
    expect(gate?.className).not.toContain("tr-gate--pending");
    expect(band(container).contains(gate)).toBe(false);
    expect(gate?.querySelector(".tr-gate__body")?.textContent).toContain("Nothing was sent.");
    expect(container.querySelector(".tr-ans__body")?.textContent).toContain("The send was refused");
  });

  it("keeps the failed call inside the band and puts the TURN's fault in the promoted slot", () => {
    const { container } = show(ERRORED);
    // The fault is a step row, tallied in the fault tone — not a gate card.
    expect(container.querySelector(".tr-gate")).toBeNull();
    const row = container.querySelector(".tr-step--failed");
    expect(band(container).contains(row)).toBe(true);
    expect(band(container).querySelector("summary")?.textContent).toContain("1 failed");
    expect(container.querySelector(".tr-work__tally--fault")).not.toBeNull();
    // …and the turn's own failure readout sits outside the fold.
    const fail = container.querySelector(".tr-fail");
    expect(band(container).contains(fail)).toBe(false);
    expect(fail).toBeVisible();
    expect(fail?.querySelector(".tr-fail__body")?.textContent).toContain(
      "did not respond within the time limit",
    );
  });

  it("CONTROL: an allowed step renders inside the band and mints no card", () => {
    const { container } = show(COMPLETE);
    expect(container.querySelector(".tr-gate")).toBeNull();
    expect(container.querySelector(".tr-fail")).toBeNull();
    const rows = band(container).querySelectorAll(".tr-work__steps .tr-step");
    expect(rows).toHaveLength(2);
    for (const row of rows) expect(row.className).toContain("tr-step--allowed");
  });
});

/* ====================================== the two ways a turn ends badly === */
//
// A turn can end without an answer for two categorically different reasons, and
// the console must not draw them as one thing:
//
//   FAULT    something broke. Red, ✕, "Turn failed", and a retry may help.
//   REFUSAL  policy decided against the whole turn. That is the governance
//            system working: slate, ⊘, "Refused", and nothing to click.
//
// Both are asserted here, in both directions. A test that only proved the
// refusal renders slate would pass just as well if the fault had been recoloured
// with it — which is the exact defect, restated.

describe("a turn-level refusal is the governance system working, not an outage", () => {
  it("draws the refusal in the gated vocabulary, and states the server's reason verbatim", () => {
    const { container } = show(REFUSED_TURN);
    const card = failCard(container);
    expect(card.className).toContain("tr-fail--refusal");
    expect(card.className).not.toContain("tr-fail--fault");
    expect(glyphOf(card)).toBe("⊘");
    // The state WORD carries the fact on its own: strip every aria-hidden
    // glyph and the reader is still told which of the two this was.
    expect(accessibleText(card)).toContain("Refused");
    expect(accessibleText(card)).not.toContain("Turn failed");
    expect(card.querySelector(".tr-fail__body")?.textContent).toBe("Refused by policy.");
    expect(card.querySelector(".tr-fail__reason")?.textContent).toBe(
      "PolicyDenied: read.finance.ledger is tier 3 for this principal",
    );
    // A refusal is announced, not alarmed.
    expect(card.getAttribute("role")).toBe("status");
  });

  it("COMPLEMENT: a turn-level fault still draws the fault vocabulary", () => {
    const { container } = show(ERRORED);
    const card = failCard(container);
    expect(card.className).toContain("tr-fail--fault");
    expect(card.className).not.toContain("tr-fail--refusal");
    expect(glyphOf(card)).toBe("✕");
    expect(accessibleText(card)).toContain("Turn failed");
    expect(accessibleText(card)).not.toContain("Refused");
    expect(card.getAttribute("role")).toBe("alert");
    // The refusal's reason slot belongs to the refusal alone.
    expect(card.querySelector(".tr-fail__reason")).toBeNull();
  });

  it("offers a refusal nothing to click — and the fault the control the caller resolved", () => {
    const { container } = show(REFUSED_TURN);
    const card = failCard(container);
    expect(card.querySelectorAll("button, a, input, [role='button']")).toHaveLength(0);
    // Not merely empty: on this branch the foot the control would sit in is
    // never rendered, and `TranscriptRefusal` has no field to put one in.
    expect(card.querySelector(".tr-fail__foot")).toBeNull();

    // CONTROL — the same card, the other tone, with a retry the caller
    // resolved. Without this the assertion above would also pass if the foot
    // had simply been deleted for every failure.
    const retryable = show({
      ...ERRORED,
      id: "turn-error-retry",
      failure: {
        ...TURN_FAULT,
        action: (
          <button type="button" className="ne-btn ne-btn--quiet">
            Try again
          </button>
        ),
      },
    });
    const faultCard = failCard(retryable.container);
    expect(faultCard.querySelector(".tr-fail__foot")).not.toBeNull();
    expect(faultCard.querySelector("button")?.textContent).toBe("Try again");
    retryable.unmount();
  });

  it("says the server sent no reason rather than drawing a blank line", () => {
    const { container } = show({
      ...REFUSED_TURN,
      failure: { ...TURN_REFUSAL, reason: null },
    });
    const reason = failCard(container).querySelector(".tr-fail__reason")?.textContent ?? "";
    expect(reason).toContain("—");
    expect(reason).toContain("the server sent no reason");
    expect(reason).not.toBe("");
  });

  // The claim the class names alone cannot make. Both modifiers are resolved
  // through this module's stylesheet and then through tokens.css, in both
  // themes: two class names that landed on the same colour would be the defect
  // wearing a new spelling.
  it("resolves the two treatments to different colours in both themes", () => {
    const faultWord = tokenIn(".tr-fail--fault .tr-fail__state", "color");
    const refusalWord = tokenIn(".tr-fail--refusal .tr-fail__state", "color");
    const faultEdge = tokenIn(".tr-fail--fault::before", "background");
    const refusalEdge = tokenIn(".tr-fail--refusal::before", "background");
    const faultFill = tokenIn(".tr-fail--fault", "background");
    const refusalFill = tokenIn(".tr-fail--refusal", "background");
    expect([faultWord, faultEdge, faultFill]).toEqual(["--red", "--red", "--red-tint"]);
    expect([refusalWord, refusalEdge, refusalFill]).toEqual([
      "--state-gated",
      "--state-gated",
      "--gated-tint",
    ]);

    // …and the shell they share carries NO hue of its own, so neither tone can
    // be reached by a card that failed to declare one.
    expect(tokenIn(".tr-fail", "border")).toBe("--rule");
    expect(tokenIn(".tr-fail", "background")).toBe("--panel");

    for (const [theme, selector] of [
      ["dark", ":root {"],
      ["light", ':root[data-theme="light"] {'],
    ] as const) {
      const b = blockFor(selector);
      const fault = hexIn(b, faultWord);
      const refusal = hexIn(b, refusalWord);
      expect(`${theme}:${refusal}`).not.toBe(`${theme}:${fault}`);
      // Separated enough to read as two colours rather than two reds…
      expect(apart(fault, refusal)).toBeGreaterThan(32);
      // …and separated in the way that matters: the fault word is red-dominant,
      // the refusal word is not. A refusal drawn anywhere on the fault ramp
      // fails here even if its hex differs.
      expect(redDominant(fault)).toBe(true);
      expect(redDominant(refusal)).toBe(false);
    }
  });

  // The two residuals of the same defect, resolved the same way: through this
  // module's stylesheet and then through tokens.css, in both themes.
  it("resolves the stop to no hue at all, and every footer to its own card's colour", () => {
    // THE CARD. Fault and refusal each spend a state hue on the fill AND the
    // edge; the stop declares NEITHER and inherits the shell's neutrals, which
    // is the whole of its treatment. The absence is asserted as a value, with
    // the two presences beside it — a stylesheet that lost all three would fail
    // here rather than pass the stop's half.
    expect(hueIn(".tr-fail--fault", "background")).toBe("--red-tint");
    expect(hueIn(".tr-fail--refusal", "background")).toBe("--gated-tint");
    expect(hueIn(".tr-fail--stopped", "background")).toBeNull();
    expect(hueIn(".tr-fail--fault", "border-color")).toBe("--red-line");
    expect(hueIn(".tr-fail--refusal", "border-color")).toBe("--rule-strong");
    expect(hueIn(".tr-fail--stopped", "border-color")).toBeNull();
    // Its edge is the hairline that separates two surfaces, not a state
    // colour — the anatomy every promoted card has, drawn in no vocabulary…
    // (the fault's and the refusal's own edges are pinned by the test above).
    expect(tokenIn(".tr-fail--stopped::before", "background")).toBe("--rule-strong");
    // …and its state word is the body text's own dim, so the card adds no
    // colour to the turn whatsoever.
    expect(tokenIn(".tr-fail--stopped .tr-fail__state", "color")).toBe("--text-dim");

    // THE FOOTER, and the assertion that fails if it is ever recoloured on its
    // own again: each receipt tone resolves to EXACTLY the token its own card's
    // state word resolves to. One vocabulary, so there is nothing to drift.
    for (const tone of ["fault", "refusal", "stopped"] as const) {
      expect(`${tone}:${tokenIn(`.tr-receipt--${tone} .tr-receipt__glyph`, "color")}`).toBe(
        `${tone}:${tokenIn(`.tr-fail--${tone} .tr-fail__state`, "color")}`,
      );
    }
    // Red is earned by an actual fault and by nothing else. The "✕ Failed" in
    // --red that used to close a refused turn now has nowhere to resolve from.
    expect(tokenIn(".tr-receipt--fault .tr-receipt__glyph", "color")).toBe("--red");
    expect(tokenIn(".tr-receipt--refusal .tr-receipt__glyph", "color")).not.toBe("--red");
    expect(tokenIn(".tr-receipt--stopped .tr-receipt__glyph", "color")).not.toBe("--red");

    // Through the TOKEN LAYER, in both themes. The two quiet tones are close to
    // each other on purpose — neither is an alarm — so what is asserted of them
    // is what is true: no two of the three are the same colour, only the fault
    // is on the red ramp, and the stop is literally the line's own text colour.
    // They are told apart by glyph and word, which the DOM tests above pin.
    for (const [theme, selector] of [
      ["dark", ":root {"],
      ["light", ':root[data-theme="light"] {'],
    ] as const) {
      const b = blockFor(selector);
      const hex = {
        fault: hexIn(b, tokenIn(".tr-fail--fault .tr-fail__state", "color")),
        refusal: hexIn(b, tokenIn(".tr-fail--refusal .tr-fail__state", "color")),
        stopped: hexIn(b, tokenIn(".tr-fail--stopped .tr-fail__state", "color")),
      };
      expect(`${theme}:${hex.refusal}`).not.toBe(`${theme}:${hex.fault}`);
      expect(`${theme}:${hex.stopped}`).not.toBe(`${theme}:${hex.fault}`);
      expect(`${theme}:${hex.stopped}`).not.toBe(`${theme}:${hex.refusal}`);
      expect(apart(hex.fault, hex.stopped)).toBeGreaterThan(32);
      expect(`${theme}:${redDominant(hex.fault)}`).toBe(`${theme}:true`);
      expect(`${theme}:${redDominant(hex.refusal)}`).toBe(`${theme}:false`);
      expect(`${theme}:${redDominant(hex.stopped)}`).toBe(`${theme}:false`);
      expect(`${theme}:${hex.stopped}`).toBe(`${theme}:${hexIn(b, "--text-dim")}`);
    }
  });
});

/* ================================ the third way a turn ends without one === */
//
// A GOVERNED OUTCOME WEARING FAULT CLOTHING is the defect the card's two
// treatments fixed, and it had two more homes. The receipt closed EVERY turn
// that produced no answer with "✕ Failed" in red, one line under whatever the
// card said — so a refused turn's own footer called the governance system an
// outage and half-undid the fix above it. And a turn the READER stopped minted
// a fault card outright: red, ✕, for something nobody enforced and nothing
// broke.
//
// Both are asserted with their complements in the same tests. A test that only
// proved the refusal closes gated would pass just as well if the FAULT had been
// recoloured with it, which is the defect restated; a test that only proved the
// stop is quiet would pass if every card had been quietened.

describe("a turn the reader stopped is neither a fault nor a refusal", () => {
  it("draws the stop in its own vocabulary — no hue, its own glyph, its own word", () => {
    const { container } = show(STOPPED_TURN);
    const card = failCard(container);
    expect(card.className).toContain("tr-fail--stopped");
    expect(card.className).not.toContain("tr-fail--fault");
    expect(card.className).not.toContain("tr-fail--refusal");
    expect(glyphOf(card)).toBe("■");
    // The state WORD carries it alone: strip every aria-hidden glyph and the
    // reader is still told which of the three this was, and which two it wasn't.
    expect(accessibleText(card)).toContain("Stopped");
    expect(accessibleText(card)).not.toContain("Turn failed");
    expect(accessibleText(card)).not.toContain("Refused");
    // Announced, not alarmed. Nothing happened TO this reader.
    expect(card.getAttribute("role")).toBe("status");
    // The refusal's reason slot belongs to the refusal alone: a stop quotes no
    // server decision, and this module writes none on anyone's behalf.
    expect(card.querySelector(".tr-fail__reason")).toBeNull();
  });

  it("COMPLEMENT: the same slot still alarms for a fault and announces a refusal", () => {
    const fault = show(ERRORED);
    expect(failCard(fault.container).getAttribute("role")).toBe("alert");
    expect(glyphOf(failCard(fault.container))).toBe("✕");
    fault.unmount();
    const refusal = show(REFUSED_TURN);
    expect(failCard(refusal.container).getAttribute("role")).toBe("status");
    expect(glyphOf(failCard(refusal.container))).toBe("⊘");
  });

  it("carries the control the caller resolved — and draws no foot when it did not", () => {
    const asked = show({
      ...STOPPED_TURN,
      failure: {
        ...TURN_STOPPED,
        action: (
          <button type="button" className="ne-btn ne-btn--quiet">
            Ask again
          </button>
        ),
      },
    });
    const withFoot = failCard(asked.container);
    expect(withFoot.querySelector(".tr-fail__foot")).not.toBeNull();
    // Not "Try again": the last attempt did exactly what it was told.
    expect(withFoot.querySelector("button")?.textContent).toBe("Ask again");
    asked.unmount();

    // CONTROL — the same tone with nothing resolved draws no foot at all, so
    // the assertion above is about the action and not about the branch.
    const { container } = show(STOPPED_TURN);
    expect(failCard(container).querySelector(".tr-fail__foot")).toBeNull();
    expect(failCard(container).querySelectorAll("button, a, input")).toHaveLength(0);
  });
});

describe("the receipt closes a turn in the card's own vocabulary", () => {
  it("closes a refused turn ⊘ Refused, gated — never the fault's ✕ Failed", () => {
    const { container } = show(REFUSED_TURN);
    const footer = receiptIn(container);
    expect(footer.className).toContain("tr-receipt--refusal");
    expect(footer.className).not.toContain("tr-receipt--fault");
    expect(receiptGlyphOf(footer)).toBe("⊘");
    expect(accessibleText(footer)).toContain("Refused");
    expect(accessibleText(footer)).not.toContain("Failed");
  });

  it("COMPLEMENT: closes a genuine fault ✕ Failed, in the fault tone", () => {
    const { container } = show(ERRORED);
    const footer = receiptIn(container);
    expect(footer.className).toContain("tr-receipt--fault");
    expect(footer.className).not.toContain("tr-receipt--refusal");
    expect(receiptGlyphOf(footer)).toBe("✕");
    expect(accessibleText(footer)).toContain("Failed");
    expect(accessibleText(footer)).not.toContain("Refused");
  });

  it("closes a stopped turn ■ Stopped, in neither of the other two", () => {
    const { container } = show(STOPPED_TURN);
    const footer = receiptIn(container);
    expect(footer.className).toContain("tr-receipt--stopped");
    expect(footer.className).not.toContain("tr-receipt--fault");
    expect(footer.className).not.toContain("tr-receipt--refusal");
    expect(receiptGlyphOf(footer)).toBe("■");
    expect(accessibleText(footer)).toContain("Stopped");
    expect(accessibleText(footer)).not.toContain("Failed");
    expect(accessibleText(footer)).not.toContain("Refused");
  });

  it("CONTROL: a turn that answered still closes ✓ Complete, and a paused one ⏸", () => {
    // Without this, everything above would also pass if the receipt had simply
    // been reduced to echoing whatever word the fixture handed it.
    const complete = show(COMPLETE);
    expect(receiptIn(complete.container).className).toContain("tr-receipt--ok");
    expect(receiptGlyphOf(receiptIn(complete.container))).toBe("✓");
    complete.unmount();
    const paused = show(AWAITING);
    expect(receiptIn(paused.container).className).toContain("tr-receipt--paused");
    expect(receiptGlyphOf(receiptIn(paused.container))).toBe("⏸");
  });

  it("wears the CARD's tone and the CARD's glyph, on all three outcomes", () => {
    // The claim under the three tests above: the footer is not choosing from a
    // second vocabulary that could drift. One tone, one glyph table.
    for (const turn of [ERRORED, REFUSED_TURN, STOPPED_TURN]) {
      const { container, unmount } = show(turn);
      const card = failCard(container);
      const footer = receiptIn(container);
      expect(`${turn.id}:${toneOf(footer, "tr-receipt")}`).toBe(
        `${turn.id}:${toneOf(card, "tr-fail")}`,
      );
      expect(`${turn.id}:${receiptGlyphOf(footer)}`).toBe(`${turn.id}:${glyphOf(card)}`);
      unmount();
    }
  });
});

/* ============================================================ the answer == */

describe("the answer is the turn's only unboxed prose", () => {
  it("draws the caret only while the answer is still arriving", () => {
    const { container } = show(STREAMING);
    expect(container.querySelector(".tr-cursor")).not.toBeNull();
    const settled = show(COMPLETE);
    expect(settled.container.querySelector(".tr-cursor")).toBeNull();
  });

  it("splits blank-line breaks into paragraphs and adds no word of its own", () => {
    const { container } = show({
      ...COMPLETE,
      answer: { label: "Answer", body: "First para.\n\nSecond para." },
    });
    const bodies = [...container.querySelectorAll(".tr-ans__body")].map((p) => p.textContent);
    expect(bodies).toEqual(["First para.", "Second para."]);
  });

  it("renders the paused Run's output slot as the em dash and the served sentence", () => {
    // agent-run.html: "— No safe output preview is available while the Run is
    // paused." The wire's `output_preview` is null on that Run.
    const wire = runWire({ status: "waiting_approval", output_preview: null });
    const { container } = show({
      ...AWAITING,
      answer: {
        label: "Output",
        body: wire.output_preview ?? "",
        absentNote: "No safe output preview is available while the Run is paused.",
      },
    });
    const body = container.querySelector(".tr-ans__body")?.textContent ?? "";
    expect(body).toContain("—");
    expect(body).toContain("No safe output preview is available while the Run is paused.");
    expect(container.querySelector(".tr-ans__attr")?.textContent).toContain("Output");
  });
});

/* =========================================================== the receipt == */

describe("one receipt per turn, and it never fills a blank", () => {
  it("closes each settled turn with exactly one receipt", () => {
    const { container } = render(
      <Transcript turns={[COMPLETE, AWAITING, REFUSED, ERRORED]} label="Answers" />,
    );
    for (const turn of container.querySelectorAll("li.tr-turn")) {
      expect(turn.querySelectorAll(".tr-receipt")).toHaveLength(1);
    }
  });

  it("draws no receipt on a turn that has not settled", () => {
    const { container } = show(STREAMING);
    expect(container.querySelectorAll(".tr-receipt")).toHaveLength(0);
  });

  it("renders an absent model as the em dash and the served reason — never No, never 0", () => {
    const { container } = show(ERRORED);
    const receipt = container.querySelector(".tr-receipt");
    const text = receipt?.textContent ?? "";
    expect(text).toContain("Model · —");
    expect(text).toContain("no model reply was served");
    expect(text).not.toMatch(/Model · (No\b|0\b)/);
    expect(receipt?.querySelector(".tr-receipt__model")).toBeNull();
  });

  it("renders a served model in the data face", () => {
    const { container } = show(COMPLETE);
    expect(container.querySelector(".tr-receipt__model")?.textContent).toBe("qwen3-32b");
  });

  it("renders an absent Run verdict as the em dash and its note", () => {
    // agent-run.html, paused Run: "Verdict · — pending".
    const wire = runWire({ status: "waiting_approval", output_verdict: null });
    const { container } = show({
      ...AWAITING,
      receipt: {
        outcome: "Paused",
        tone: "paused",
        facts: [
          { key: "decision", value: "Awaiting a decision" },
          { key: "verdict", label: "Verdict", value: wire.output_verdict, note: "pending" },
        ],
        model: wire.served_model,
      },
    });
    const text = container.querySelector(".tr-receipt")?.textContent ?? "";
    expect(text).toContain("Verdict · — pending");
    expect(text).toContain("Awaiting a decision");
  });
});

/* ======================================================== the progress ==== */

describe("the in-flight line is measured, never simulated", () => {
  afterEach(() => vi.useRealTimers());

  it("prints the elapsed and the liveness sentence the caller measured", () => {
    const { container } = show(STREAMING);
    const line = container.querySelector(".tr-progress")?.textContent ?? "";
    expect(line).toContain("Working");
    expect(line).toContain("12.0s");
    expect(line).toContain("last word from the server 1s ago");
  });

  it("renders the em dash rather than a number when nothing was measured", () => {
    const { container } = show({
      ...STREAMING,
      progress: { elapsedSeconds: null, liveness: null },
    });
    const line = container.querySelector(".tr-progress")?.textContent ?? "";
    expect(line).toContain("no elapsed time measured");
    expect(line).toContain("no word from the server yet");
    expect(line).not.toMatch(/\d/);
  });

  it("does not advance on its own clock", () => {
    vi.useFakeTimers();
    const { container } = show(STREAMING);
    const before = container.querySelector(".tr-progress")?.textContent;
    vi.advanceTimersByTime(60_000);
    expect(container.querySelector(".tr-progress")?.textContent).toBe(before);
  });

  it("declares no timer and no clock read anywhere in the module", () => {
    // The behavioural check above proves this render does not tick. This proves
    // no OTHER code path in the module can: a component added later that reads
    // Date.now() would pass the test above and fail this one.
    const dir = resolve(fileURLToPath(import.meta.url), "..");
    const CLOCK = /setInterval|setTimeout|requestAnimationFrame|Date\.now|performance\.now/;
    const offenders = readdirSync(dir)
      .filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
      .filter((f) => CLOCK.test(readFileSync(join(dir, f), "utf-8")));
    expect(offenders).toEqual([]);
    // Self-test: the scan bites the shape it forbids.
    expect(CLOCK.test("const now = Date.now();")).toBe(true);
    expect(CLOCK.test("const now = props.now;")).toBe(false);
  });
});

/* ============================================================ the order === */

describe("the turn's anatomy", () => {
  it("draws question, work, promoted, answer, sources, receipt in the render's order", () => {
    const { container } = show(AWAITING);
    const marks = [...turnOf(container).children].map((el) => el.className.split(" ")[0]);
    expect(marks).toEqual(["tr-q", "tr-work", "tr-gate", "tr-ans", "tr-receipt"]);
  });

  it("puts the failure readout in the promoted slot when there is no answer", () => {
    const { container } = show(ERRORED);
    const marks = [...turnOf(container).children].map((el) => el.className.split(" ")[0]);
    expect(marks).toEqual(["tr-q", "tr-work", "tr-fail", "tr-receipt"]);
  });

  it("renders sources with an absent field as the em dash, never a blank", () => {
    const { container } = show({
      ...COMPLETE,
      sources: [{ key: "c1", ordinal: 1, system: null, document: "doc-alpha" }],
    });
    const line = container.querySelector(".tr-src__line")?.textContent ?? "";
    expect(line).toContain("[1]");
    expect(line).toContain("—");
    expect(line).toContain("no source system served");
    expect(line).toContain("doc-alpha");
  });
});

/* ========================================================= the contrast === */
//
// The shared contrast guardian measures the STATE-pill tokens only. The
// transcript also draws --green, --cherenkov and --text on tinted promoted
// cards, and none of those pairs is covered there. Measured here, in both
// themes, against the surfaces this module's own stylesheet puts them on.

const TOKENS_CSS = readFileSync(
  resolve(fileURLToPath(import.meta.url), "..", "..", "styles", "tokens.css"),
  "utf-8",
);

const TRANSCRIPT_CSS = readFileSync(
  resolve(fileURLToPath(import.meta.url), "..", "transcript.css"),
  "utf-8",
);

/** The token one rule of THIS module's stylesheet resolves a property through,
 * or `null` when the rule does not exist or declares nothing for it.
 *
 * ABSENCE IS A CLAIM HERE, which is why it is a value rather than a throw. The
 * stopped card's whole treatment is that it spends no hue: it declares neither
 * a fill nor a border and inherits the shell's neutrals, so "declares nothing"
 * is the thing to assert and a rule that started declaring something must fail.
 * A declaration that is present but is NOT a token still throws — that is a raw
 * colour, and it is the token-hardcode guardian's business as well as this
 * file's. */
function hueIn(selector: string, property: string): string | null {
  const idx = TRANSCRIPT_CSS.indexOf(`${selector} {`);
  if (idx < 0) return null;
  const block = TRANSCRIPT_CSS.slice(idx, TRANSCRIPT_CSS.indexOf("}", idx));
  const decl = new RegExp(`(?:^|[;{])\\s*${property}\\s*:\\s*([^;}]+)`, "m").exec(block);
  if (decl?.[1] === undefined) return null;
  const token = /var\((--[a-z-]+)\)/.exec(decl[1]);
  if (token?.[1] === undefined) throw new Error(`${selector} ${property} is not a token`);
  return token[1];
}

/** The same, for a property the rule is REQUIRED to declare. Throws rather than
 * returning a default: a rule that stopped declaring the property is a change
 * to what the screen draws, not a test that shrugs. */
function tokenIn(selector: string, property: string): string {
  const token = hueIn(selector, property);
  if (token === null) throw new Error(`${selector} declares no ${property}`);
  return token;
}

function channels(hex: string): [number, number, number] {
  const c = hex.replace("#", "");
  return [0, 2, 4].map((i) => Number.parseInt(c.slice(i, i + 2), 16)) as [number, number, number];
}

/** The widest single-channel gap between two colours, 0–255. */
function apart(a: string, b: string): number {
  const [x, y] = [channels(a), channels(b)];
  return Math.max(...x.map((v, i) => Math.abs(v - y[i]!)));
}

/** True for a colour on the fault ramp: red is its strongest channel. */
function redDominant(hex: string): boolean {
  const [r, g, b] = channels(hex);
  return r > g && r > b;
}

function blockFor(selector: string): string {
  const idx = TOKENS_CSS.indexOf(selector);
  const open = TOKENS_CSS.indexOf("{", idx);
  return TOKENS_CSS.slice(open, TOKENS_CSS.indexOf("}", open));
}

function hexIn(block: string, name: string): string {
  const m = new RegExp(`${name}:\\s*(#[0-9a-f]{6})\\s*;`, "i").exec(block);
  if (m?.[1] === undefined) throw new Error(`token ${name} not found`);
  return m[1];
}

function rgbaIn(block: string, name: string): [number, number, number, number] {
  const m = new RegExp(`${name}:\\s*rgba\\(([^)]*)\\)`, "i").exec(block);
  if (m?.[1] === undefined) throw new Error(`token ${name} not found`);
  const parts = m[1].split(",").map((p) => Number.parseFloat(p.trim()));
  return [parts[0]!, parts[1]!, parts[2]!, parts[3]!];
}

/** The tint wash the promoted cards lay over --panel, composited. */
function tinted(block: string, tint: string, base: string): string {
  const [r, g, b, a] = rgbaIn(block, tint);
  const under = hexIn(block, base).slice(1);
  const mix = [r, g, b].map((c, i) => {
    const u = Number.parseInt(under.slice(i * 2, i * 2 + 2), 16);
    return Math.round(c * a + u * (1 - a));
  });
  return `#${mix.map((c) => c.toString(16).padStart(2, "0")).join("")}`;
}

function luminance(hex: string): number {
  const c = hex.replace("#", "");
  const ch = [0, 2, 4].map((i) => {
    const v = Number.parseInt(c.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * ch[0]! + 0.7152 * ch[1]! + 0.0722 * ch[2]!;
}

function ratio(a: string, b: string): number {
  const [hi, lo] = luminance(a) > luminance(b) ? [a, b] : [b, a];
  return (luminance(hi) + 0.05) / (luminance(lo) + 0.05);
}

const AA = 4.5;

describe("every colour the transcript draws text in clears AA", () => {
  it("self-test: the ratio agrees with two reference pairs", () => {
    expect(ratio("#000000", "#ffffff")).toBeCloseTo(21, 1);
    expect(ratio("#5cb4d4", "#5cb4d4")).toBeCloseTo(1, 5);
  });

  for (const [theme, selector] of [
    ["dark", ":root {"],
    ["light", ':root[data-theme="light"] {'],
  ] as const) {
    it(`${theme}: no pair this stylesheet draws falls below ${AA}:1`, () => {
      const b = blockFor(selector);
      const on = {
        ground: hexIn(b, "--ground"),
        panel: hexIn(b, "--panel"),
        raised: hexIn(b, "--panel-raised"),
        amberCard: tinted(b, "--amber-tint", "--panel"),
        redCard: tinted(b, "--red-tint", "--panel"),
        gatedCard: tinted(b, "--gated-tint", "--panel"),
      };
      // Exactly the pairs transcript.css declares — text colour by surface.
      const pairs: Array<[string, string, string, string]> = [
        ["--text", "tr-q__bubble", "--panel-raised", on.raised],
        ["--text-dim", "tr-work__sum", "--panel", on.panel],
        ["--text", "tr-step--running name", "--panel", on.panel],
        ["--green", "tr-work tally ✓", "--panel", on.panel],
        ["--red", "tr-work tally ✕", "--panel", on.panel],
        ["--cherenkov", "tr-step--running glyph", "--panel", on.panel],
        ["--amber", "tr-gate--pending state", "amber card", on.amberCard],
        ["--text", "tr-gate body", "amber card", on.amberCard],
        ["--red", "tr-gate--refused state", "red card", on.redCard],
        ["--red", "tr-fail--fault state", "red card", on.redCard],
        ["--text", "tr-fail--fault body", "red card", on.redCard],
        ["--state-gated", "tr-fail--refusal state", "gated card", on.gatedCard],
        ["--text", "tr-fail--refusal body", "gated card", on.gatedCard],
        ["--text-dim", "tr-fail__reason", "gated card", on.gatedCard],
        // The stopped card lays no tint, so its surface is the bare panel.
        ["--text-dim", "tr-fail--stopped state", "--panel", on.panel],
        ["--text", "tr-fail--stopped body", "--panel", on.panel],
        ["--text-dim", "tr-receipt", "--ground", on.ground],
        ["--green", "tr-receipt ✓", "--ground", on.ground],
        ["--amber", "tr-receipt ⏸", "--ground", on.ground],
        ["--red", "tr-receipt ✕", "--ground", on.ground],
        ["--state-gated", "tr-receipt ⊘", "--ground", on.ground],
        ["--text-dim", "tr-receipt ■", "--ground", on.ground],
        ["--text", "tr-ans__body", "--ground", on.ground],
        ["--text-dim", "tr-src__line", "--ground", on.ground],
      ];
      const failures = pairs
        .map(([token, where, surface, hex]) => ({
          where: `${token} (${where}) on ${surface}`,
          r: ratio(hexIn(b, token), hex),
        }))
        .filter((row) => row.r < AA)
        .map((row) => `${row.where}: ${row.r.toFixed(2)}:1`);
      expect(failures).toEqual([]);
    });
  }

  it("self-test: it FAILS the token this stylesheet is forbidden to draw text in", () => {
    // --text-faint is the colour the render uses for the chevron and the "·"
    // separators. If this ever passes, the divergence in transcript.css has
    // stopped being necessary and the comment there has become false.
    for (const selector of [":root {", ':root[data-theme="light"] {']) {
      const b = blockFor(selector);
      expect(ratio(hexIn(b, "--text-faint"), hexIn(b, "--panel"))).toBeLessThan(AA);
    }
  });

  it("the stylesheet sets no text colour to the banned faint token", () => {
    const css = readFileSync(
      resolve(fileURLToPath(import.meta.url), "..", "transcript.css"),
      "utf-8",
    );
    expect(css).not.toMatch(/(?:^|[;{\s])color\s*:\s*[^;}]*--text-faint/m);
  });
});

/* --------------------------------------------------------------------------
 * THE VERTICAL-TEXT DEFECT — a governed identifier drawn one character per line.
 *
 * WHAT HAPPENED. `.tr-step` is a flex row of four items. Three of them
 * (`__glyph`, `__aux`, `__time`) are `flex: none` or `white-space: nowrap` and
 * therefore refuse to shrink. The fourth, `.tr-step__name`, declared
 * `overflow-wrap: anywhere` — a property that does not merely PERMIT a break
 * inside a word, it lowers the element's min-content contribution to about one
 * character. So the automatic minimum size that normally stops a flex item from
 * collapsing was gone, and every pixel of a width deficit came out of that one
 * item. Drawn into the narrower column the docked receipt pane leaves,
 * `read.knowledge.search` became a 1ch track twenty-one lines tall, with the
 * rest of its own row laid out beside the column.
 *
 * WHY THIS IS ASSERTED ON THE RULE AND NOT ON A SCREENSHOT. jsdom performs no
 * layout — every width it reports is 0 — so a rendered assertion could not
 * distinguish the broken stylesheet from the fixed one. The defect IS the
 * declaration, so the declaration is what is pinned.
 *
 * THE PAIR, both halves over the same identifier: the layout may never break a
 * token, AND the data may never shorten one to make it fit. A fix that
 * truncated `read.knowledge.search` to `read.knowledge…` in the model would
 * satisfy the first half and is exactly what the second half forbids.
 * ------------------------------------------------------------------------ */

/** Every declaration of ONE rule of this module's stylesheet, matched at the
 * start of a line so `.tr-step--act .tr-step__name` cannot answer for
 * `.tr-step__name`. */
function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const hit = new RegExp(`^${escaped}\\s*\\{([^}]*)\\}`, "m").exec(TRANSCRIPT_CSS);
  if (hit?.[1] === undefined) throw new Error(`transcript.css declares no ${selector} rule`);
  return hit[1];
}

describe("a long unbroken identifier lays out on one line", () => {
  const LONG = "read.knowledge.search";

  for (const selector of [".tr-step__name", ".tr-gate__name"]) {
    it(`MUTATION \`no-per-character-wrap\`: ${selector} cannot break inside a token`, () => {
      const body = ruleBody(selector);
      // The one declaration that makes a per-character wrap unrepresentable at
      // ANY width. Without it the element's break behaviour is a function of
      // how much room its nowrap siblings left over.
      expect(body).toMatch(/white-space:\s*nowrap/);
      // The two properties that lower an element's min-content contribution to
      // one character. `overflow-wrap: anywhere` is the exact declaration that
      // shipped the defect; `word-break: break-all` is the other spelling of
      // it and is banned here for the same reason.
      expect(body).not.toMatch(/overflow-wrap\s*:/);
      expect(body).not.toMatch(/word-break\s*:/);
      // How it degrades instead: it clips to an ellipsis inside its own track
      // rather than pushing the band wider than the column it is drawn into.
      // A FLOOR, and one that is not `auto` — `min-width: auto` is what the
      // spec resolves to min-content, which `overflow-wrap: anywhere` had just
      // reduced to one character.
      expect(body).toMatch(/min-width:\s*(?!auto)[^;]+;/);
      expect(body).toMatch(/overflow:\s*hidden/);
      expect(body).toMatch(/text-overflow:\s*ellipsis/);
    });
  }

  it("the sentence yields before the tool name does", () => {
    // `__aux` is the greedy item, so a width deficit is taken out of the
    // served intent — which the expanded row and the title still carry — and
    // not out of the governed identifier the row is about.
    const aux = ruleBody(".tr-step__aux");
    expect(aux).toMatch(/flex:\s*1 4 auto/);
    expect(aux).toMatch(/min-width:\s*0/);
    const name = ruleBody(".tr-step__name");
    expect(name).toMatch(/flex:\s*0 1 auto/);
    // The identifier keeps a readable floor: it does not ellipsize to nine
    // characters at a width that had room for twenty-one.
    expect(name).toMatch(/min-width:\s*min\(100%,\s*22ch\)/);
  });

  it("MUTATION `identifier-never-truncated`: the full name is in the DOM, verbatim", () => {
    const step = stepFromTransit(transit({ name: LONG }), "s1");
    const { container } = render(
      <TranscriptTurnView
        turn={{ id: "t", question: { text: "q" }, steps: [step], workOpen: true }}
      />,
    );
    const name = container.querySelector(".tr-step__name");
    if (name === null) throw new Error("no step name rendered");
    // Not an ellipsis in the data, not an abbreviation, not a middle-truncated
    // stand-in. Ellipsizing is the STYLESHEET'S job and is reversible; a
    // shortened string is not.
    expect(name.textContent).toBe(LONG);
    // And the whole of it is one hover away even where the track clips it.
    expect(name.getAttribute("title")).toBe(LONG);
  });

  it("MUTATION `identifier-never-truncated`: and the same on a promoted gate", () => {
    const step = stepFromTransit(transit({ name: LONG, decision: "deny" }), "s1");
    const { container } = render(
      <TranscriptTurnView turn={{ id: "t", question: { text: "q" }, steps: [step] }} />,
    );
    const name = container.querySelector(".tr-gate__name");
    if (name === null) throw new Error("no gate name rendered");
    expect(name.textContent).toBe(LONG);
    expect(name.getAttribute("title")).toBe(LONG);
    // THE PAIRED HALF, restated on this fixture: the refused step is OUTSIDE
    // the fold. A layout fix that moved it inside to make the row fit would
    // pass every assertion above and break the rule the module exists for.
    expect(container.querySelector("details .tr-gate")).toBeNull();
  });
});

/* --------------------------------------------------------------------------
 * THE BAND'S NOTE — why the ledger is thinner than the Run.
 * ------------------------------------------------------------------------ */

describe("the work band's note", () => {
  const step: TranscriptStep = {
    key: "s1",
    name: "model call",
    verdict: "allowed",
    verdictLabel: "Served",
    tier: null,
    durationSeconds: null,
  };

  it("states the absence inside the ledger, and is not counted as work", () => {
    const { container } = render(
      <TranscriptTurnView
        turn={{
          id: "t",
          question: { text: "q" },
          steps: [step],
          workOpen: true,
          workNote: "No tool plan was served for this Run, so no tool call is listed here.",
        }}
      />,
    );
    expect(container.querySelector(".tr-work__note")?.textContent).toContain("No tool plan");
    // The note is NOT a step: it draws no row, and the précis still counts one.
    expect(container.querySelectorAll("li.tr-step")).toHaveLength(1);
    expect(container.querySelector(".tr-work__count")?.textContent).toBe("1 step");
  });

  it("draws nothing at all when the plan was served", () => {
    const { container } = render(
      <TranscriptTurnView
        turn={{ id: "t", question: { text: "q" }, steps: [step], workOpen: true }}
      />,
    );
    expect(container.querySelector(".tr-work__note")).toBeNull();
  });
});

/* --------------------------------------------------------------------------
 * THE FOLD, seen from the précis. A folded row stands for N recorded events,
 * and the summary line's headline number is the one thing that may not shrink
 * when the ledger folds — it is the audit précis.
 * ------------------------------------------------------------------------ */

describe("a folded row does not shrink the précis", () => {
  it("counts the events a folded row stands for, not the rows drawn", () => {
    const folded: TranscriptStep = {
      key: "f",
      name: "12 governed steps",
      verdict: "allowed",
      verdictLabel: "Not disclosable",
      tier: null,
      durationSeconds: null,
      foldedCount: 12,
    };
    const plain: TranscriptStep = {
      key: "p",
      name: "read.knowledge.search",
      verdict: "allowed",
      verdictLabel: "Allowed",
      tier: null,
      durationSeconds: null,
      rank: "act",
    };
    const { container } = render(
      <TranscriptTurnView
        turn={{ id: "t", question: { text: "q" }, steps: [plain, folded], workOpen: true }}
      />,
    );
    // Two <li>s, thirteen recorded steps — and the tally says thirteen too.
    expect(container.querySelectorAll("li.tr-step")).toHaveLength(2);
    expect(container.querySelector(".tr-work__count")?.textContent).toBe("13 steps");
    expect(accessibleText(container.querySelector(".tr-work__verdict") as HTMLElement)).toBe(
      "13 allowed",
    );
  });
});

/* --------------------------------------------------------------------------
 * THE RECEIPT ON A TURN THAT HAS NOT CLOSED.
 * ------------------------------------------------------------------------ */

describe("a live turn's receipt", () => {
  it("MUTATION `live-wears-no-failure-glyph`: running draws ● and never ✕", () => {
    const { container } = render(
      <TranscriptTurnView
        turn={{
          id: "t",
          question: { text: "q" },
          steps: [],
          receipt: { outcome: "Running", tone: "live", model: null, modelNote: "not served yet" },
        }}
      />,
    );
    const receipt = receiptIn(container);
    expect(receipt.className).toContain("tr-receipt--live");
    expect(receipt.querySelector(".tr-receipt__glyph")?.textContent).toBe("●");
    // The exact mark the reader must NOT see beside "Running": the console
    // asserting an outcome the server has not stated.
    expect(receipt.textContent).not.toContain("✕");
  });

  it("MUTATION `failed-still-reads-as-failed`: the fault treatment is untouched", () => {
    // THE PAIRED HALF. Making a running turn quieter must not make a broken one
    // quiet too — the failure glyph and its hue are still exactly what a turn
    // the server actually failed closes with.
    const { container } = render(
      <TranscriptTurnView
        turn={{
          id: "t",
          question: { text: "q" },
          steps: [],
          receipt: { outcome: "Failed", tone: "fault", model: null },
        }}
      />,
    );
    const receipt = receiptIn(container);
    expect(receipt.className).toContain("tr-receipt--fault");
    expect(receipt.querySelector(".tr-receipt__glyph")?.textContent).toBe("✕");
  });

  it("the live tone resolves a hue of its own, and not one from the fault ramp", () => {
    // `--cherenkov` is the running STEP's own hue, so live means one thing on
    // this screen; `--red` beside "Running" is the defect this tone was minted
    // to make unrepresentable.
    expect(tokenIn(".tr-receipt--live .tr-receipt__glyph", "color")).toBe("--cherenkov");
    expect(tokenIn(".tr-receipt--fault .tr-receipt__glyph", "color")).toBe("--red");
  });
});

/* --------------------------------------------------------------------------
 * THE BAND SUMMARY IS A PRÉCIS, NOT THE FIRST THREE ITEMS.
 * ------------------------------------------------------------------------ */

describe("the band summary characterises the work", () => {
  it("MUTATION `repetition-is-stated`: thirty calls to one tool read as thirty", () => {
    const steps: TranscriptStep[] = Array.from({ length: 30 }, (_, i) => ({
      key: `s${i}`,
      name: "read.knowledge.search",
      verdict: "allowed",
      verdictLabel: "Allowed",
      tier: null,
      durationSeconds: null,
    }));
    const { container } = render(
      <TranscriptTurnView turn={{ id: "t", question: { text: "q" }, steps }} />,
    );
    const names = container.querySelector(".tr-work__names");
    expect(names?.textContent).toBe("30 × read.knowledge.search");
    // The headline count is unchanged, and the whole line is one hover away
    // where the track clips it.
    expect(container.querySelector(".tr-work__count")?.textContent).toBe("30 steps");
    expect(names?.getAttribute("title")).toBe("30 × read.knowledge.search");
    // THE PAIRED HALF: the précis is still readable with nothing expanded — it
    // is the summary's own text, not an aria-label.
    expect(accessibleText(container.querySelector("summary") as HTMLElement)).toContain(
      "30 × read.knowledge.search",
    );
  });
});

/* ================================================== the rolled-up row == */

describe("a rolled-up row demotes its per-event ledger, never deletes it", () => {
  const ROLLED: TranscriptTurn = {
    id: "turn-rolled",
    question: { text: "which customers qualify?", meta: "12:02" },
    steps: [
      {
        key: "run-events:e1+3",
        name: "Retrieved 2 records",
        verdict: "allowed",
        verdictLabel: "Recorded",
        tier: null,
        durationSeconds: 0.7,
        intent: "from the verified graph · 1 row verified",
        foldedCount: 3,
        detail: [
          {
            key: "run-event:e1:detail",
            name: "Evidence",
            verdict: "allowed",
            verdictLabel: "Produced",
            tier: null,
            durationSeconds: 0.2,
            intent: "verified_t3 · 1 contributor · run-tool",
          },
          {
            key: "run-event:e2:detail",
            name: "Evidence",
            verdict: "allowed",
            verdictLabel: "Produced",
            tier: null,
            durationSeconds: 0.3,
            intent: "verified_t3 · 1 contributor · run-tool",
          },
          {
            key: "run-event:e3:detail",
            name: "Evidence row",
            verdict: "allowed",
            verdictLabel: "Verified",
            tier: null,
            durationSeconds: 0.2,
            intent: "verified_t3 · run-tool",
          },
        ],
      },
    ],
    workElapsedSeconds: 0.7,
    receipt: { outcome: "Complete", tone: "ok", model: "qwen3-32b" },
  };

  it("draws the plain line at rest and holds the verbatim rows one disclosure away", () => {
    const { container } = show(ROLLED);
    const row = container.querySelector(".tr-step--has-detail");
    expect(row).not.toBeNull();
    const fold = row!.querySelector<HTMLDetailsElement>("details.tr-step__fold");
    expect(fold).not.toBeNull();
    // Closed on arrival: the plain sentence IS the resting account.
    expect(fold!.open).toBe(false);
    const summary = fold!.querySelector(".tr-step__foldsum");
    expect(summary?.textContent).toContain("Retrieved 2 records");
    expect(summary?.textContent).toContain("from the verified graph · 1 row verified");
    // The verbatim per-event rows are IN the document — demoted, not deleted —
    // with the raw schema vocabulary a reader can still reach.
    const rows = fold!.querySelectorAll(".tr-step__detail .tr-step");
    expect(rows).toHaveLength(3);
    expect(rows[0]?.textContent).toContain("verified_t3 · 1 contributor · run-tool");
  });

  it("counts the recorded events in the précis, not the rendered rows", () => {
    const { container } = show(ROLLED);
    // One rendered row standing for three recorded events: the band's headline
    // says three, because folding may compress a screen but never a count.
    expect(band(container).querySelector("summary")?.textContent).toContain("3 steps");
  });

  it("renders a plain row with no disclosure when there is nothing to demote", () => {
    const { container } = show(COMPLETE);
    expect(container.querySelector(".tr-step__fold")).toBeNull();
  });
});

/* ------------------------------------------- the served row anchors -- */

describe("the source block draws the served row anchors as the row's identity", () => {
  const anchor = "4d".repeat(32);
  const VERIFIED_ROWS: TranscriptTurn = {
    id: "turn-verified-rows",
    question: { text: "which disputes are overdue?", meta: "12:06" },
    steps: [],
    answer: { label: "Answer", body: "One dispute is overdue." },
    sourceAnchors: [{ key: "ev-1", anchor }],
    sourcesNote: "This answer rests on 1 server-verified evidence record.",
    receipt: { outcome: "Complete", tone: "ok", model: "qwen3-32b" },
  };

  it("draws the anchor short-form with the FULL served value in the title, plus a copy control", () => {
    const { container } = render(<TranscriptTurnView turn={VERIFIED_ROWS} />);
    const short = Array.from(container.querySelectorAll(".ne-mono")).find(
      (el) => el.textContent === anchor.slice(0, 8),
    );
    expect(short).toBeTruthy();
    expect(short!.getAttribute("title")).toBe(anchor);
    expect(container.querySelector(".tr-src__anchorlabel")!.textContent).toBe("Row identity");
    expect(
      Array.from(container.querySelectorAll("button")).some(
        (el) => el.getAttribute("aria-label") === "Copy row anchor",
      ),
    ).toBe(true);
  });

  it("the anchors draw the source block even with zero source lines, and never join the Sources count", () => {
    const { container } = render(<TranscriptTurnView turn={VERIFIED_ROWS} />);
    const label = container.querySelector(".tr-src__label")!;
    // No "Sources · N" over a list with no lines: the anchors are identities,
    // not source lines, and the count never sees them.
    expect(label.textContent).toBe("Sources");
    expect(container.querySelectorAll(".tr-src__line")).toHaveLength(0);
  });

  it("a turn without anchors draws no anchor row at all", () => {
    const { container } = render(<TranscriptTurnView turn={COMPLETE} />);
    expect(container.querySelector(".tr-src__anchors")).toBeNull();
  });
});
