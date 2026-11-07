// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — the thread, on the transcript system.
//
// What is held here is ASK'S ADAPTER, asserted through the anatomy it feeds:
// where each governed call is DRAWN (a routine allowed call inside the fold, a
// call pending a person or refused outside it), the two links that may only
// appear when they resolve, the five stated lines a turn that produced no answer
// can carry, the one receipt that closes a settled turn and what it may and may
// not claim, and the one-shot signal guard, whose synchronous ref is
// load-bearing — a same-tick second click precedes any re-render, so exactly one
// signal reaches the intake per answer.
//
// The turn's anatomy itself is transcript/transcript.test.tsx's subject. These
// tests query it by its `tr-*` class vocabulary because that is what the render
// names, and because "inside the fold" and "outside the fold" are containment
// questions no accessible name can answer.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { Citation } from "../api/types";
import { RUN_RECEIPT_LABEL } from "../agents/RunReceiptPane";
import { MessageList } from "./MessageList";
import type { UiMessage } from "./useAgentStream";

const CITATION: Citation = {
  chunk_id: "chunk-alpha",
  document_id: "doc-alpha",
  source_system: "sharepoint",
  source_ref: "ref/doc-alpha",
};

function answer(overrides: Partial<UiMessage> = {}): UiMessage {
  return {
    id: "m2",
    role: "assistant",
    text: "Four contracts renew this quarter.",
    citations: [CITATION],
    streaming: false,
    tools: [],
    prompt: "which contracts renew?",
    sessionId: "sess-1",
    ...overrides,
  };
}

function show(messages: UiMessage[], props: Record<string, unknown> = {}) {
  return render(
    <MemoryRouter>
      <MessageList messages={messages} onViewSource={() => {}} {...props} />
    </MemoryRouter>,
  );
}

/** The turn's collapsible work band. */
function band(): HTMLDetailsElement | null {
  return document.querySelector<HTMLDetailsElement>(".tr-work");
}

/** One step row of the band, found by the tool name it carries. */
function stepRow(name: string): HTMLElement | null {
  return (
    Array.from(document.querySelectorAll<HTMLElement>(".tr-step")).find(
      (row) => row.querySelector(".tr-step__name")?.textContent === name,
    ) ?? null
  );
}

/** The promoted gate card, which by construction is never inside the fold. */
function gate(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".tr-gate");
}

/** The turn's one governance footer. */
function receipt(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".tr-receipt");
}

function deferred(): { promise: Promise<void>; resolve: () => void; reject: () => void } {
  let resolve!: () => void;
  let reject!: () => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = () => rej(new Error("no"));
  });
  return { promise, resolve, reject };
}

describe("the thread's shape", () => {
  it("renders neither role label — structure carries the distinction", () => {
    show([
      {
        id: "m1",
        role: "user",
        text: "which contracts renew?",
        citations: [],
        streaming: false,
        tools: [],
      },
      answer(),
    ]);
    expect(screen.queryByText("user")).not.toBeInTheDocument();
    expect(screen.queryByText("assistant")).not.toBeInTheDocument();
    expect(screen.getByText("which contracts renew?")).toBeInTheDocument();
    expect(screen.getByText("Four contracts renew this quarter.")).toBeInTheDocument();
  });

  it("renders one compact line per citation: source system, then document", () => {
    show([answer()]);
    const line = screen.getByText(/doc-alpha/).closest("li");
    expect(line).not.toBeNull();
    expect(line?.textContent).toContain("[1]");
    expect(line?.textContent).toContain("sharepoint");
    // Only what the trimmed API returned: the source pane's fields do not leak
    // into the thread, and nothing here invents a title for the document.
    expect(line?.textContent).not.toContain("ref/doc-alpha");
    expect(line?.textContent).not.toContain("chunk-alpha");
  });

  it("names the view action for the citation it opens", async () => {
    const onViewSource = vi.fn();
    show([answer()], { onViewSource });
    await userEvent.click(screen.getByLabelText("View source for citation 1"));
    expect(onViewSource).toHaveBeenCalledWith(CITATION);
  });

  it("suppresses reasoning body and renders an undispatched frame as a failed turn", () => {
    const onThumbs = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
    show(
      [
        answer({
          text: [
            "<think>private chain of thought</think>",
            "The supported answer remains visible.",
            '{"tool_call": {"name": "read.knowledge.search", "arguments": {"query": "q"}}}',
          ].join("\n"),
          citations: [],
        }),
      ],
      { onThumbs },
    );

    expect(screen.getByText("The supported answer remains visible.")).toBeInTheDocument();
    expect(screen.queryByText("private chain of thought")).not.toBeInTheDocument();
    expect(
      screen.getByText("Internal reasoning was received and is not displayed."),
    ).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("<think>");

    const raw = screen.getByText(/\{"tool_call"/);
    expect(raw.tagName).toBe("PRE");
    expect(screen.getByText("RAW — UNCLASSIFIED FRAME 1")).toBeInTheDocument();
    expect(
      screen.getByText("Turn failed — the runtime emitted an undispatched tool-call frame."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Helpful" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Not helpful" })).not.toBeInTheDocument();
  });

  it("production-binds the evidence export without reasoning or invented config truth", async () => {
    const user = userEvent.setup();
    show([
      answer({
        text: "<think>do not export this</think>\nFour contracts renew this quarter.",
        requestedModel: "private-qwen",
        provenance: {
          kind: "grounded",
          trace: { traceId: "a2bf6cea2f1d4a779c315f0e2b8d4411" },
        },
      }),
    ]);

    await user.click(screen.getByRole("button", { name: "COPY ANSWER WITH CITATIONS" }));
    expect(await screen.findByText(/copied — answer text/)).toBeInTheDocument();
    const copied = await navigator.clipboard.readText();
    expect(copied).toContain("Four contracts renew this quarter.");
    expect(copied).toContain("source ref: ref/doc-alpha");
    expect(copied).toContain("governed POST /v1/brain/retrieve");
    expect(copied).not.toContain("governed GET /v1/brain/retrieve");
    expect(copied).toContain("structural outcome: grounded");
    expect(copied).toContain("requested model: private-qwen");
    expect(copied).not.toContain("served model:");
    expect(copied).toContain("run trace: a2bf6cea2f1d4a779c315f0e2b8d4411");
    expect(copied).not.toContain("do not export this");
    expect(copied).not.toContain("configuration in force");
  });

  it("copies an ordinary done trace without inventing structural provenance", async () => {
    const user = userEvent.setup();
    show([
      answer({
        citations: [],
        requestedModel: "frontier-sonnet",
        traceId: "trace-ordinary-7",
      }),
    ]);

    expect(screen.getByText(/Requested model/)).toHaveTextContent(
      "Requested model · frontier-sonnet",
    );
    expect(document.body).not.toHaveTextContent(/served model/i);
    await user.click(screen.getByRole("button", { name: "COPY ANSWER — NO CITATIONS" }));
    const copied = await navigator.clipboard.readText();
    expect(copied).toContain("requested model: frontier-sonnet");
    expect(copied).toContain("structural outcome: none served for this answer");
    expect(copied).toContain("run trace: trace-ordinary-7");
    expect(copied).not.toContain("run trace: not served");
  });

  it("preserves the ordinary trace in the manual-copy fallback block", async () => {
    const write = vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("no"));
    show([answer({ citations: [], traceId: "trace-fallback-9" })]);

    await userEvent.click(screen.getByRole("button", { name: "COPY ANSWER — NO CITATIONS" }));
    const block = await screen.findByText(/run trace: trace-fallback-9/);
    expect(block).toHaveClass("ne-answerexport__block");
    expect(block).not.toHaveTextContent("run trace: not served");
    write.mockRestore();
  });
});

describe("terminal-frame provenance chips", () => {
  it.each([
    ["verified", "VERIFIED", "Verified answer provenance"],
    ["grounded", "GROUNDED", "Grounded answer provenance"],
    ["refused-ungrounded", "REFUSED — ungrounded", "Refused because the answer was ungrounded"],
  ] as const)("renders the %s server outcome with an accessible label", (kind, text, label) => {
    show([answer({ provenance: { kind } })]);
    expect(screen.getByText(text)).toHaveAccessibleName(label);
  });

  it("renders no chip for plain generation or an unrelated refusal", () => {
    // Absent provenance means the server made NO structural claim, and ordinary
    // generation must stay unbadged. Asserted on the element the chip actually
    // is: a query for a label no branch renders any more would pass whatever
    // the receipt drew.
    const { unmount } = show([answer()]);
    expect(document.querySelector(".ne-ask__provenance")).toBeNull();
    // CONTROL — the same query, the opposite answer, so the assertion above is
    // measuring something.
    const badged = show([answer({ provenance: { kind: "verified" } })]);
    expect(badged.container.querySelector(".ne-ask__provenance")).not.toBeNull();
    badged.unmount();
    unmount();

    show([
      answer({
        text: "",
        failure: {
          kind: "refused",
          reason: "refused",
          detail: "PolicyDenied: egress blocked",
          retryable: false,
        },
      }),
    ]);
    expect(screen.queryByText("REFUSED — ungrounded")).not.toBeInTheDocument();
    expect(document.querySelector(".ne-ask__provenance")).toBeNull();
  });

  it.each([
    ["verified", "Verified answer provenance; open related run trace"],
    ["grounded", "Grounded answer provenance; open related run trace"],
    ["refused-ungrounded", "Refused because the answer was ungrounded; open related run trace"],
  ] as const)("links the %s outcome to the related run trace", async (kind, accessibleName) => {
    const user = userEvent.setup();
    show([
      answer({
        provenance: {
          kind,
          trace: { traceId: "trace/123", eventId: "gate:event:9" },
        },
      }),
    ]);
    const link = screen.getByRole("link", { name: accessibleName });
    expect(link).toHaveAttribute("href", "/trace/trace%2F123#trace-event-gate%3Aevent%3A9");
    expect(screen.queryByRole("link", { name: /gate decision/i })).toBeNull();
    await user.tab();
    await user.tab();
    await user.tab();
    expect(link).toHaveFocus();
  });

  it("degrades to non-link text without a server trace id and survives rerender", () => {
    const messages = [answer({ provenance: { kind: "verified" } })];
    const view = show(messages);
    expect(screen.getByText("VERIFIED").tagName).toBe("SPAN");
    expect(screen.queryByRole("link", { name: /Verified answer provenance/ })).toBeNull();

    view.rerender(
      <MemoryRouter>
        <MessageList messages={[...messages]} onViewSource={() => {}} />
      </MemoryRouter>,
    );
    expect(screen.getByText("VERIFIED")).toBeInTheDocument();
  });
});

describe("where a governed call is drawn", () => {
  const tool = (over: Record<string, unknown>) => ({
    key: "k1",
    event: {
      type: "tool" as const,
      name: "read.finance.ledger",
      tier: 2,
      decision: "allow" as const,
      audit_seq: 4812,
      ...over,
    },
  });

  it("demotes an allowed call into the fold, with its name, tier and decision", () => {
    show([answer({ tools: [tool({})] })]);
    const row = stepRow("read.finance.ledger");
    expect(row?.textContent).toContain("Tier 2");
    expect(row?.textContent).toContain("Allowed");
    // WHERE it is drawn is the point: routine allowed work demotes, and the
    // band's own summary line carries the précis for a reader who never opens
    // it. A gate card here would mean the promotion rule fired on an allow.
    expect(band()?.contains(row)).toBe(true);
    expect(gate()).toBeNull();
    expect(band()?.querySelector("summary")?.textContent).toContain("1 allowed");
  });

  it("renders an unresolved tier as the em dash, never a zero", () => {
    show([answer({ tools: [tool({ tier: null })] })]);
    const row = stepRow("read.finance.ledger");
    // An unresolved tier DRAWS NOTHING (transcript/Transcript.tsx). The wire
    // carries a tier on no Run event kind at all, so "Tier —" appeared on every
    // row of every band and buried the words that said something. What it must
    // never become is a zero, which would be a policy claim.
    expect(row?.textContent).not.toContain("Tier");
    expect(row?.textContent).not.toContain("Tier 0");
  });

  it("draws no time slot on any step, because the wire measures none", () => {
    // The console owns no clock that could fill this, and an em dash on every
    // row of every turn would advertise an absence nobody can act on. Nothing
    // is drawn (owner ruling, 2026-08-24) — and nothing is invented.
    show([answer({ tools: [tool({}), tool({ ...tool({}).event, name: "read.crm.lookup" })] })]);
    expect(document.querySelector(".tr-step__time")).toBeNull();
    expect(document.querySelector(".tr-work__time")).toBeNull();
    expect(band()?.textContent).not.toMatch(/\d+(\.\d+)?s\b/);
  });

  /** A routine allowed call, so every promotion test has a fold to be outside
   * of. The render draws exactly this: the routine step stays demoted beside
   * the promoted card. */
  const routine = {
    key: "k0",
    event: {
      type: "tool" as const,
      name: "read.knowledge.search",
      tier: 1,
      decision: "allow" as const,
      audit_seq: 4800,
    },
  };

  it("promotes a refusal out of the fold and links its decision only for a key the server says holds operations", () => {
    const { unmount } = show([answer({ tools: [routine, tool({ decision: "deny" })] })], {
      opsReadable: true,
    });
    const card = gate();
    expect(card).not.toBeNull();
    // Visible with nothing expanded: the containment, both ways.
    expect(band()?.contains(card)).toBe(false);
    expect(document.querySelector(".tr-turn")?.contains(card)).toBe(true);
    expect(within(card as HTMLElement).getByText("Refused")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View decision" })).toHaveAttribute(
      "href",
      "/audit?seq=4812",
    );
    unmount();

    show([answer({ tools: [routine, tool({ decision: "deny" })] })], { opsReadable: false });
    expect(screen.queryByRole("link", { name: "View decision" })).not.toBeInTheDocument();
    expect(within(gate() as HTMLElement).getByText("Refused")).toBeInTheDocument();
  });

  it("omits the decision link when the server resolved no audit sequence", () => {
    show([answer({ tools: [tool({ decision: "deny", audit_seq: null })] })], {
      opsReadable: true,
    });
    expect(screen.queryByRole("link", { name: "View decision" })).not.toBeInTheDocument();
  });

  it("names the served reference a gate has, and says so when the wire served none", () => {
    // A policy-decision-point deny carries no approval id by design, so the
    // reference is the audit sequence the decision was recorded at — the same
    // number "View decision" resolves against. Both are the server's; neither
    // is constructed.
    const { unmount } = show([answer({ tools: [routine, tool({ decision: "deny" })] })]);
    expect(gate()?.querySelector(".tr-gate__ref")?.textContent).toBe("audit 4812");
    unmount();

    // An approval request is the narrower identity and wins.
    const withApproval = show([
      answer({
        tools: [routine, tool({ decision: "rejected", approval_id: "req-7", audit_seq: 99 })],
      }),
    ]);
    expect(withApproval.container.querySelector(".tr-gate__ref")?.textContent).toBe("req-7");
    withApproval.unmount();

    // CONTROL — neither served. The slot says so rather than showing a number
    // from somewhere else, and there is no link to a decision it cannot reach.
    show([answer({ tools: [routine, tool({ decision: "deny", audit_seq: null })] })], {
      opsReadable: true,
    });
    const ref = gate()?.querySelector(".tr-gate__ref")?.textContent ?? "";
    expect(ref).toContain("—");
    expect(ref).toContain("no reference served");
    expect(ref).not.toMatch(/\d/);
  });

  it("says nothing about a refusal the server gave no reason for", () => {
    // The gate's body is the slot the design reserves for the SERVER's own words.
    // A transit the server served no `reason` on says so rather than this console
    // writing a plausible one.
    show([answer({ tools: [tool({ decision: "deny" })] })]);
    const body = gate()?.querySelector(".tr-gate__body");
    expect(body?.textContent).toContain("—");
    expect(body?.textContent).toContain("the server sent no reason");

    // …and an explicit null on the wire is the SAME absence as an omitted key.
    // (The server omits the key; a relayed frame may carry null. One render.)
    const explicitNull = show([answer({ tools: [tool({ decision: "deny", reason: null })] })]);
    const nullBody = explicitNull.container.querySelector(".tr-gate__body");
    expect(nullBody?.textContent).toContain("the server sent no reason");
  });

  it("prints the served policy reason verbatim, in place of the absent note", () => {
    // The reason is the POLICY BUNDLE's own sentence, read off the audit record
    // the PDP wrote (ne-govern decision.rego) and served on the transit. The card
    // renders it BYTE-FOR-BYTE — the em dash, the colon, the lower-case "tier" all
    // survive — and the absent note is gone, because there is no absence.
    const served = "tier 2: external/PII action — per-item human approval required";
    show([answer({ tools: [tool({ decision: "deny", reason: served })] })]);
    const body = gate()?.querySelector(".tr-gate__body");
    expect(body?.textContent).toBe(served);
    expect(body?.textContent).not.toContain("the server sent no reason");

    // The console adds nothing of its own around it: no prefix, no "Policy:"
    // label, no trailing period, no sentence-casing. The whole body IS the
    // server's string, which `toBe` above already pins — this control pins the
    // OTHER half: the card did not silently swap in a DIFFERENT plausible
    // sentence that happens to mention the same tier.
    const other = show([
      answer({ tools: [tool({ decision: "deny", reason: "unclassified action" })] }),
    ]);
    expect(other.container.querySelector(".tr-gate__body")?.textContent).toBe(
      "unclassified action",
    );
  });

  it("keeps the reason out of the band row a served allow demotes into", () => {
    // CONTROL for the adapter's one-slot rule: `sentence` is read ONLY by a
    // PROMOTED step (transcript/promotion.ts). An allowed call that somehow
    // carried a reason still draws no sentence anywhere — the band row has no
    // body slot, and nothing here promotes it to get one.
    show([answer({ tools: [tool({ decision: "allow", reason: "tier 0: read/internal draft" })] })]);
    expect(gate()).toBeNull();
    expect(document.body.textContent).not.toContain("tier 0: read/internal draft");
  });

  it("keeps a pending call pending, outside the fold, linked to its request", () => {
    show([answer({ tools: [routine, tool({ decision: "pending", approval_id: "req-9" })] })]);
    const card = gate();
    expect(card).not.toBeNull();
    expect(band()?.contains(card)).toBe(false);
    expect(within(card as HTMLElement).getByText("Awaiting approval")).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText("req-9")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review in Approvals" })).toHaveAttribute(
      "href",
      "/approvals?request=req-9",
    );
    // It never converts here: no console-side decision control exists.
    expect(screen.queryByRole("button", { name: /approve|deny|reject/i })).toBeNull();
    // …and the receipt says the turn is parked on a person, not finished.
    expect(receipt()?.textContent).toContain("Paused");
    expect(receipt()?.textContent).toContain("Awaiting a decision");
  });

  it("names the approval an executed call consumed, inside the fold", () => {
    show([
      answer({
        tools: [tool({ decision: "allow", approval_id: "req-resumed", audit_seq: 4813 })],
      }),
    ]);
    const row = stepRow("read.finance.ledger");
    expect(row?.textContent).toContain("Approved · action executed");
    expect(row?.textContent).toContain("req-resumed");
    // An approval that was granted and consumed is finished work: it demotes,
    // and nothing is left awaiting anyone.
    expect(band()?.contains(row)).toBe(true);
    expect(gate()).toBeNull();
    expect(receipt()?.textContent).not.toContain("Awaiting a decision");
    expect(screen.queryByRole("button", { name: /approve|deny|reject/i })).toBeNull();
  });

  it.each([
    ["rejected", "Approval rejected", "gate"],
    ["expired", "Approval expired", "gate"],
    ["escalated", "Approval escalated", "gate"],
    ["integrity-failed", "Approval integrity check failed", "gate"],
    ["approval-failed", "Approval did not proceed", "gate"],
    ["failed", "Approved action failed during execution", "band"],
  ] as const)("renders %s as its own server-owned terminal state", (decision, copy, where) => {
    // The console must never collapse expiry, escalation or an integrity
    // failure into a human rejection (api/types.ts states this as a wire rule),
    // so each keeps its own word — and the one that is a FAULT rather than a
    // verdict is the one that stays in the band: the action was approved and
    // then fell over, and nobody owes it a decision.
    show([
      answer({
        tools: [routine, tool({ decision, approval_id: `req-${decision}`, audit_seq: 4814 })],
      }),
    ]);
    expect(screen.getByText(new RegExp(copy))).toBeInTheDocument();
    expect(document.body.textContent).toContain(`req-${decision}`);
    if (where === "gate") {
      expect(band()?.contains(gate())).toBe(false);
      expect(within(gate() as HTMLElement).getByText(copy)).toBeInTheDocument();
    } else {
      expect(gate()).toBeNull();
      expect(band()?.contains(stepRow("read.finance.ledger"))).toBe(true);
    }
    if (decision !== "rejected") expect(screen.queryByText("Approval rejected")).toBeNull();
    expect(screen.queryByRole("button", { name: /approve|deny|reject/i })).toBeNull();
  });
});

describe("ordered agent activity", () => {
  const pending = {
    key: "approval:approval-1",
    event: {
      type: "tool" as const,
      name: "read.knowledge.search",
      tier: 2,
      decision: "pending" as const,
      approval_id: "approval-1",
      audit_seq: 40,
    },
  };
  const allowed = {
    key: "approval:approval-1",
    event: {
      type: "tool" as const,
      name: "read.knowledge.search",
      tier: 2,
      decision: "allow" as const,
      approval_id: "approval-1",
      audit_seq: 41,
    },
  };

  it("renders the exact approval as one resumed checkpoint without moving focus", () => {
    const before = document.createElement("button");
    document.body.append(before);
    before.focus();
    show([
      answer({
        text: "Four contracts renew.",
        traceId: "trace/plain",
        requestedModel: "private-qwen",
        tools: [pending, allowed],
        streamItems: [
          { key: "tool:approval:approval-1", kind: "tool", entry: allowed },
          { key: "answer:2", kind: "answer", text: "Four contracts renew." },
          { key: "citations:3", kind: "citations", citations: [CITATION] },
          { key: "terminal:5", kind: "terminal", outcome: "complete", traceId: "trace/plain" },
        ],
      }),
    ]);

    // The ORDERED stream is authoritative: one lifecycle, one row. The
    // accumulated `tools` list still holds the superseded pending transit, and
    // rendering that too would show one approval as two.
    expect(document.querySelectorAll(".tr-step")).toHaveLength(1);
    expect(screen.getAllByText(/approval-1/)).toHaveLength(1);
    expect(stepRow("read.knowledge.search")?.textContent).toContain("Approved · action executed");
    expect(gate()).toBeNull();
    // The turn reads in the render's order: question, work, answer, sources,
    // and exactly one receipt closing it.
    expect(screen.getByText("Four contracts renew.")).toBeInTheDocument();
    expect(receipt()?.textContent).toContain("Complete");
    expect(screen.getByRole("link", { name: "Open trace" })).toHaveAttribute(
      "href",
      "/trace/trace%2Fplain",
    );
    expect(screen.queryByRole("link", { name: /gate decision/i })).toBeNull();
    expect(screen.getByText(/Requested model/)).toHaveTextContent("Requested model · private-qwen");
    expect(document.body).not.toHaveTextContent(/served model/i);
    expect(screen.queryByRole("button", { name: /approve|deny|reject/i })).toBeNull();
    expect(document.activeElement).toBe(before);
    before.remove();
  });

  it("renders an approved execution failure as one replacement checkpoint without moving focus", () => {
    const before = document.createElement("button");
    document.body.append(before);
    before.focus();
    const failed = {
      ...allowed,
      event: { ...allowed.event, decision: "failed" as const, audit_seq: 42 },
    };
    show([
      answer({
        tools: [pending, failed],
        streamItems: [{ key: "tool:approval:approval-1", kind: "tool", entry: failed }],
      }),
    ]);

    expect(screen.getAllByText(/approval-1/)).toHaveLength(1);
    const row = stepRow("read.knowledge.search");
    expect(row?.textContent).toContain("Approved action failed during execution");
    expect(document.body.textContent).not.toContain("Awaiting approval");
    // A fault is counted in the précis, in the fault's own colour vocabulary.
    expect(band()?.querySelector("summary")?.textContent).toContain("1 failed");
    expect(document.activeElement).toBe(before);
    before.remove();
  });

  it("announces only the latest material transition", () => {
    const message = answer({
      streaming: true,
      text: "",
      citations: [],
      tools: [pending],
      streamItems: [{ key: "tool:approval:approval-1", kind: "tool", entry: pending }],
      announcement: "Approval required for read.knowledge.search.",
    });
    const view = show([message]);
    const live = document.querySelectorAll('[aria-live="polite"]');
    expect(live).toHaveLength(1);
    expect(live[0]).toHaveTextContent("Approval required for read.knowledge.search.");

    view.rerender(
      <MemoryRouter>
        <MessageList
          messages={[
            {
              ...message,
              text: "Answer text",
              citations: [CITATION],
              streamItems: [
                ...message.streamItems!,
                { key: "answer:2", kind: "answer", text: "Answer text" },
                { key: "citations:3", kind: "citations", citations: [CITATION] },
              ],
            },
          ]}
          onViewSource={() => {}}
        />
      </MemoryRouter>,
    );
    expect(live[0]).toHaveTextContent("Approval required for read.knowledge.search.");
  });
});

describe("a turn that produced no answer", () => {
  it("renders the refusal with the server's reason, verbatim", () => {
    show([
      answer({
        text: "",
        citations: [],
        failure: {
          kind: "refused",
          reason: "refused",
          detail: "PolicyDenied: read.finance.ledger is tier 3 for this principal",
          retryable: false,
        },
      }),
    ]);
    // The promoted card, with §5's stated line and the SERVER's own reason
    // under it, verbatim — that sentence is the only account of the decision
    // that is not this console guessing.
    const card = document.querySelector(".tr-fail");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByText("Refused")).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText("Refused by policy.")).toBeInTheDocument();
    expect(
      within(card as HTMLElement).getByText(
        "PolicyDenied: read.finance.ledger is tier 3 for this principal",
      ),
    ).toBeInTheDocument();
    // The raw string is never the answer: there is no answer slot at all here.
    expect(document.querySelector(".tr-ans")).toBeNull();
    // A refusal is not a fault: it offers no retry.
    expect(screen.queryByRole("button", { name: /try again|retry/i })).not.toBeInTheDocument();
    // …and it says "Refused", not one of the fault words.
    expect(within(card as HTMLElement).queryByText("Turn failed")).toBeNull();
  });

  it("renders the stream, timeout and no-answer lines in the spec's words", () => {
    const cases: Array<[UiMessage["failure"], string, string]> = [
      [
        { kind: "transport", reason: "stream", detail: "", retryable: true },
        "The answer stream failed.",
        "Try again",
      ],
      [
        { kind: "transport", reason: "timeout", detail: "", retryable: true },
        "No answer arrived in time.",
        "Try again",
      ],
      [
        { kind: "stage", reason: "none", detail: "", retryable: true },
        "No answer was produced.",
        "Retry",
      ],
    ];
    for (const [failure, line, action] of cases) {
      const { unmount } = show([answer({ text: "", citations: [], failure })], {
        onRetry: () => {},
      });
      expect(screen.getByText(line)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: action })).toBeInTheDocument();
      unmount();
    }
  });

  it("names the missing department claim and offers no retry", () => {
    show([
      answer({
        text: "",
        citations: [],
        failure: { kind: "stage", reason: "no-department", detail: "no dept", retryable: false },
      }),
    ]);
    expect(
      screen.getByText(
        "Your account isn't assigned to a department, so questions can't be routed. Contact your administrator.",
      ),
    ).toBeInTheDocument();
  });

  it("names an invalidated grounding mode without calling it a department failure", () => {
    show([
      answer({
        text: "",
        citations: [],
        failure: {
          kind: "stage",
          reason: "grounding-unavailable",
          detail: "unknown agent_name",
          retryable: false,
        },
      }),
    ]);
    expect(
      screen.getByText("Grounding is no longer available for this question."),
    ).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("assigned to a department");
    expect(screen.queryByRole("button", { name: /try again|retry/i })).not.toBeInTheDocument();
  });

  // The classifier's outcome class reaches the screen as the card's TONE. Both
  // directions in one test, and both with a retry callback in hand: the refusal
  // offers nothing to click because it is a refusal, not because this screen
  // had nothing to offer.
  it("draws a policy refusal gated and a transport fault as a fault, retry offered to both", () => {
    const refused = show(
      [
        answer({
          text: "",
          citations: [],
          failure: {
            kind: "refused",
            reason: "refused",
            detail: "PolicyDenied: egress blocked",
            retryable: false,
          },
        }),
      ],
      { onRetry: () => {} },
    );
    const refusal = document.querySelector(".tr-fail") as HTMLElement;
    expect(refusal.className).toContain("tr-fail--refusal");
    expect(refusal.className).not.toContain("tr-fail--fault");
    expect(refusal.querySelector(".tr-fail__state")?.textContent).toBe("⊘Refused");
    expect(refusal.querySelector(".tr-fail__reason")?.textContent).toBe(
      "PolicyDenied: egress blocked",
    );
    expect(within(refusal).queryByRole("button")).toBeNull();
    refused.unmount();

    // COMPLEMENT — the other outcome class, the same screen, the same callback.
    show(
      [
        answer({
          text: "",
          citations: [],
          failure: { kind: "transport", reason: "stream", detail: "", retryable: true },
        }),
      ],
      { onRetry: () => {} },
    );
    const fault = document.querySelector(".tr-fail") as HTMLElement;
    expect(fault.className).toContain("tr-fail--fault");
    expect(fault.className).not.toContain("tr-fail--refusal");
    expect(fault.querySelector(".tr-fail__state")?.textContent).toBe("✕Turn failed");
    expect(within(fault).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  // THE FOOTER SAID THE OPPOSITE OF THE CARD. Whatever the card drew, the
  // receipt closed the turn "✕ Failed" in red — so a refused turn ended with the
  // governance system reported as an outage one line under its own ⊘ REFUSED
  // card. The word and the tone now come out of one table keyed by the
  // failure's own tone, and the tone IS the failure's, so the two cannot part.
  it("closes a refused turn Refused and a faulted one Failed — the card's own word", () => {
    const refused = show([
      answer({
        text: "",
        citations: [],
        failure: {
          kind: "refused",
          reason: "refused",
          detail: "PolicyDenied: egress blocked",
          retryable: false,
        },
      }),
    ]);
    const gatedFooter = receipt() as HTMLElement;
    expect(gatedFooter.className).toContain("tr-receipt--refusal");
    expect(gatedFooter.className).not.toContain("tr-receipt--fault");
    expect(gatedFooter.textContent).toContain("Refused");
    expect(gatedFooter.textContent).not.toContain("Failed");
    refused.unmount();

    // COMPLEMENT — something actually broke, and the footer still says so.
    show([
      answer({
        text: "",
        citations: [],
        failure: { kind: "transport", reason: "stream", detail: "", retryable: true },
      }),
    ]);
    const faultFooter = receipt() as HTMLElement;
    expect(faultFooter.className).toContain("tr-receipt--fault");
    expect(faultFooter.className).not.toContain("tr-receipt--refusal");
    expect(faultFooter.textContent).toContain("Failed");
    expect(faultFooter.textContent).not.toContain("Refused");
  });

  // STOP IS NEITHER. The reader ended the turn: nothing broke and nothing was
  // enforced, so it mints the third tone rather than a red fault card, and its
  // control does not apologise for a failure that did not happen.
  it("draws a turn the reader stopped as neither a fault nor a refusal", () => {
    const stopped = show(
      [
        answer({
          text: "",
          citations: [],
          failure: { kind: "transport", reason: "stopped", detail: "", retryable: true },
        }),
      ],
      { onRetry: () => {} },
    );
    const card = document.querySelector(".tr-fail") as HTMLElement;
    expect(card.className).toContain("tr-fail--stopped");
    expect(card.className).not.toContain("tr-fail--fault");
    expect(card.className).not.toContain("tr-fail--refusal");
    expect(card.querySelector(".tr-fail__state")?.textContent).toBe("■Stopped");
    // §5's line, unchanged: honest about what Stop does to a durable turn.
    expect(card.querySelector(".tr-fail__body")?.textContent).toBe(
      "Stopped. The turn may still finish on the server; an approval it requested stays in Approvals.",
    );
    // The control says what it does. "Try again" would say the last attempt
    // went wrong; it did exactly what the reader told it to.
    expect(within(card).getByRole("button", { name: "Ask again" })).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /try again|retry/i })).toBeNull();
    const stoppedFooter = receipt() as HTMLElement;
    expect(stoppedFooter.className).toContain("tr-receipt--stopped");
    expect(stoppedFooter.textContent).toContain("Stopped");
    expect(stoppedFooter.textContent).not.toContain("Failed");
    stopped.unmount();

    // COMPLEMENT — the same screen, the same retry callback, a turn that
    // actually broke: still the fault card, still "Try again", still "Failed".
    show(
      [
        answer({
          text: "",
          citations: [],
          failure: { kind: "transport", reason: "timeout", detail: "", retryable: true },
        }),
      ],
      { onRetry: () => {} },
    );
    const fault = document.querySelector(".tr-fail") as HTMLElement;
    expect(fault.className).toContain("tr-fail--fault");
    expect(fault.querySelector(".tr-fail__state")?.textContent).toBe("✕Turn failed");
    expect(within(fault).getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect((receipt() as HTMLElement).textContent).toContain("Failed");
  });

  it.each([
    ["model-unavailable", "The selected model is unavailable."],
    ["model-unverified", "Model availability couldn’t be verified."],
  ] as const)("keeps %s server detail in the evidence disclosure", (reason, line) => {
    const detail = `${reason}: server-only diagnostic`;
    show([
      answer({
        text: "",
        citations: [],
        failure: { kind: "stage", reason, detail, retryable: false },
      }),
    ]);

    // The STATED line is the card's sentence; the server's raw diagnostic is
    // behind the disclosure and is not the sentence. Asserted as the two
    // different slots they are, in both directions.
    const stated = document.querySelector(".tr-fail__body");
    expect(stated?.textContent).toBe(line);
    const raw = screen.getByText(detail);
    expect(raw.closest("details")).not.toBeNull();
    expect(raw.closest(".tr-fail__body")).toBeNull();
    expect(stated?.textContent).not.toContain(detail);
    expect(screen.getByText(/Show evidence/)).toHaveTextContent("what the server said");
  });

  it("renders a tool call the runtime did not dispatch as raw failed-turn evidence", () => {
    // The runtime printed a tool call where an answer belongs. It never reached
    // a decision point, so it is not a governed tool call — and it is not an
    // answer either. Nothing of it is rendered in answer typography.
    show([answer({ text: '{"tool_call": {"name": "read.knowledge.search"}}', citations: [] })]);
    expect(screen.queryByText("No answer was produced.")).not.toBeInTheDocument();
    expect(screen.getByText(/\{"tool_call"/).tagName).toBe("PRE");
    expect(
      screen.getByText("Turn failed — the runtime emitted an undispatched tool-call frame."),
    ).toBeInTheDocument();
  });

  it("re-sends the failed turn's own prompt AND the key it was born with", async () => {
    // The key is what makes this a RESEND rather than a second question: the
    // server mints its turn id from it, so the retry resolves to the run
    // already in flight instead of minting another one beside it.
    const onRetry = vi.fn();
    show(
      [
        answer({
          text: "",
          citations: [],
          failure: { kind: "stage", reason: "none", detail: "", retryable: true },
          idempotencyKey: "key-abc",
        }),
      ],
      { onRetry },
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledWith("which contracts renew?", "key-abc");
  });
});

describe("signals reach the intake exactly once", () => {
  it("records feedback once, whatever a same-tick second click does", async () => {
    const gate = deferred();
    const onThumbs = vi.fn<(message: UiMessage, up: boolean) => Promise<void>>(() => gate.promise);
    show([answer()], { onThumbs });

    const helpful = screen.getByRole("button", { name: "Helpful" });
    await userEvent.click(helpful);
    await userEvent.click(screen.getByRole("button", { name: "Not helpful" }));
    gate.resolve();

    expect(await screen.findByText("Feedback recorded")).toBeInTheDocument();
    expect(onThumbs).toHaveBeenCalledTimes(1);
    expect(onThumbs.mock.calls[0]?.[1]).toBe(true);
  });

  it("says the feedback did not save, and re-opens for one retry", async () => {
    const onThumbs = vi
      .fn<(message: UiMessage, up: boolean) => Promise<void>>()
      .mockRejectedValueOnce(new Error("nope"))
      .mockResolvedValueOnce(undefined);
    show([answer()], { onThumbs });

    await userEvent.click(screen.getByRole("button", { name: "Helpful" }));
    expect(await screen.findByText("Couldn't save feedback — try again")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Helpful" }));
    expect(await screen.findByText("Feedback recorded")).toBeInTheDocument();
    expect(onThumbs).toHaveBeenCalledTimes(2);
  });

  it("confirms a flagged citation on the citation it flagged", async () => {
    const onFlagSource = vi.fn<(message: UiMessage, citation: Citation) => Promise<void>>(() =>
      Promise.resolve(),
    );
    show([answer()], { onFlagSource });
    const line = screen.getByText(/doc-alpha/).closest("li")!;
    await userEvent.click(within(line).getByRole("button", { name: "Flag" }));
    expect(await screen.findByText("Citation flagged")).toBeInTheDocument();
    expect(onFlagSource.mock.calls[0]?.[1]).toEqual(CITATION);
  });

  it("offers no feedback on a turn that is still streaming", () => {
    show([answer({ streaming: true, text: "Four contracts" })], { onThumbs: async () => {} });
    expect(screen.queryByRole("button", { name: "Helpful" })).not.toBeInTheDocument();
  });
});

describe("the turn's shape on the transcript", () => {
  /** The document order of the parts of the first turn. */
  function order(): string[] {
    const turn = document.querySelector(".tr-turn");
    const parts = turn?.querySelectorAll(":scope > *") ?? [];
    return Array.from(parts).map((el) => el.className.split(" ")[0] ?? "");
  }

  it("draws the turn in the order the approved render draws it", () => {
    // The design's "loudest to quietest" is an EMPHASIS order carried by
    // typography — the answer is the turn's only unboxed prose — not a document
    // order. ask.html and ask-states.html both draw the work band ABOVE the
    // answer with no CSS reordering, and this pins that.
    show([
      answer({
        tools: [
          {
            key: "k0",
            event: {
              type: "tool",
              name: "read.knowledge.search",
              tier: 1,
              decision: "allow",
              audit_seq: 1,
            },
          },
          {
            key: "k1",
            event: {
              type: "tool",
              name: "write.crm.note",
              tier: 2,
              decision: "pending",
              approval_id: "req-1",
              audit_seq: 2,
            },
          },
        ],
      }),
    ]);
    expect(order()).toEqual(["tr-q", "tr-work", "tr-gate", "tr-ans", "tr-src", "tr-receipt"]);
  });

  it("closes a settled turn with exactly one receipt and gives a live turn none", () => {
    const { unmount } = show([answer()]);
    expect(document.querySelectorAll(".tr-receipt")).toHaveLength(1);
    unmount();

    show([answer({ streaming: true, text: "Four contracts" })], {
      progress: { startedAt: Date.now(), lastSignalAt: Date.now() },
    });
    expect(document.querySelectorAll(".tr-receipt")).toHaveLength(0);
  });

  it("renders the model as the em dash and a factual note — the chat wire names none", () => {
    // This is the absence rule at its most load-bearing: the done frame carries
    // no served model, so the receipt says so. The alias the REQUESTER asked
    // for is a separate fact, separately labelled, and never dressed up as the
    // model that answered.
    show([answer({ requestedModel: "private-qwen" })]);
    const text = receipt()?.textContent ?? "";
    expect(text).toContain("Model · —");
    expect(text).toContain("the server named no model");
    expect(text).toContain("Requested model · private-qwen");
    expect(text).not.toMatch(/served model/i);
    expect(text).not.toContain("Model · private-qwen");
  });

  it("names the involuntary fallback, and the model that did not serve", () => {
    // THE SILENT DEGRADE, ENDED. The composer promised `frontier-best`; the Run
    // served `private-default` because the provider rejected every frontier
    // call and the gateway fell down its candidate chain. Two bare aliases one
    // span apart said nothing about the relationship and read as a fault. The
    // Run's own `route_position` says which it was.
    show([
      answer({
        requestedModel: "frontier-best",
        servedModel: "private-default",
        routePosition: "fallback",
        fallbackFrom: "frontier-sonnet",
      }),
    ]);
    const text = receipt()?.textContent ?? "";
    // Both governed identifiers stay, exactly as served.
    expect(text).toContain("Requested model · frontier-best");
    expect(text).toContain("Model · private-default");
    expect(text).toContain("frontier-sonnet did not serve — a fallback answered");
    // AND THE PAIRED HALF: the reason is a POSITION and NAMES, never the
    // provider's error text — which can echo the prompt back and which the wire
    // does not carry at all.
    expect(text).not.toMatch(/error|rejected|BadRequest|unreachable|timed out/i);
  });

  it("reports a fallback with no named candidate without inventing one", () => {
    show([
      answer({
        requestedModel: "frontier-best",
        servedModel: "private-default",
        routePosition: "fallback",
      }),
    ]);
    const text = receipt()?.textContent ?? "";
    expect(text).toContain("the requested model did not serve — a fallback answered");
    expect(text).not.toContain("did not serve — a fallback answered — ");
  });

  it("says a model changed but the routing is unrecorded when the Run named none", () => {
    // THE HONEST RESIDUE. No `route_position` on the record is NOT "served as
    // requested" and NOT a fallback claim: it is a visible change with no
    // stated cause, and the row says exactly that much.
    show([answer({ requestedModel: "frontier-best", servedModel: "private-default" })]);
    const text = receipt()?.textContent ?? "";
    expect(text).toContain("Requested model · frontier-best");
    expect(text).toContain("a different model served — this turn recorded no routing");
    expect(text).not.toContain("a fallback answered");
    expect(text).not.toContain("served as requested");
  });

  it("states agreement from the RECORD, not from two names matching", () => {
    // `primary` is the Run's own positive claim that nothing was attempted
    // first. The same alias printed twice, once under each label, is the noise
    // that made the divergence above hard to see at all.
    show([
      answer({
        requestedModel: "private-default",
        servedModel: "private-default",
        routePosition: "primary",
      }),
    ]);
    const text = receipt()?.textContent ?? "";
    expect(text).toContain("served as requested");
    expect(text).toContain("Model · private-default");
    expect(text).not.toContain("Requested model · private-default");
    expect(text).not.toContain("a different model served");
  });

  it("never reads matching names as a route the server did not report", () => {
    // THE PAIRED HALF of the test above, and the reason the equality check had
    // to go: an ALIAS and a served model name are different kinds of string, so
    // a Run whose two names happen to match still gets no routing claim unless
    // the Run made one.
    show([answer({ requestedModel: "private-default", servedModel: "private-default" })]);
    expect(receipt()?.textContent ?? "").not.toContain("served as requested");
  });

  it("claims no divergence before a model has served", () => {
    // THE PAIRED HALF of both above: an absent served model is an ABSENCE, and
    // the model slot already states it. Reading it as a mismatch would report a
    // routing event on a turn where no model answered at all.
    show([answer({ requestedModel: "frontier-best" })]);
    const text = receipt()?.textContent ?? "";
    expect(text).toContain("Requested model · frontier-best");
    expect(text).toContain("the server named no model");
    expect(text).not.toContain("a different model served");
    expect(text).not.toContain("a fallback answered");
    expect(text).not.toContain("served as requested");
  });

  it("distinguishes the failed turn's missing model from a finished turn's", () => {
    show([
      answer({
        text: "",
        citations: [],
        failure: { kind: "transport", reason: "stream", detail: "", retryable: true },
      }),
    ]);
    const text = receipt()?.textContent ?? "";
    expect(text).toContain("Failed");
    expect(text).toContain("Model · —");
    // A turn that produced nothing did not get a reply to name a model for; a
    // turn that DID is a different absence, and the note is not the same.
    expect(text).toContain("no model reply was served");
    expect(text).not.toContain("the server named no model");
  });

  it("draws the live turn's measured line and opens the band while work is landing", () => {
    const started = Date.now() - 12_000;
    show([
      answer({
        streaming: true,
        text: "Beta LLC has three open incidents",
        tools: [
          {
            key: "k0",
            event: {
              type: "tool",
              name: "read.knowledge.search",
              tier: 1,
              decision: "allow",
              audit_seq: 1,
            },
          },
        ],
      }),
    ]);
    expect(band()?.open).toBe(true);
    // No progress facts handed in: the console owns no clock of its own here,
    // so no line is drawn at all rather than a zero.
    expect(document.querySelector(".tr-progress")).toBeNull();

    const measured = show(
      [answer({ streaming: true, text: "Beta LLC has three open incidents" })],
      { progress: { startedAt: started, lastSignalAt: Date.now() - 1000 } },
    );
    const line = measured.container.querySelector(".tr-progress")?.textContent ?? "";
    expect(line).toMatch(/Working · 1[12]\.0s/);
    expect(line).toMatch(/last word from the server \d+s ago/);
  });

  it("pairs each question with the reply that followed it, and leaves an unanswered one bare", () => {
    const question = (id: string, text: string): UiMessage => ({
      id,
      role: "user",
      text,
      citations: [],
      streaming: false,
      tools: [],
    });
    show([question("m1", "which contracts renew?"), answer(), question("m3", "and after that?")]);
    const turns = document.querySelectorAll(".tr-turn");
    expect(turns).toHaveLength(2);
    expect(turns[0]?.querySelector(".tr-q__bubble")?.textContent).toBe("which contracts renew?");
    expect(turns[0]?.querySelector(".tr-receipt")).not.toBeNull();
    // The unanswered question is a bubble and nothing else: no band, no answer,
    // no receipt claiming an outcome that has not happened.
    expect(turns[1]?.querySelector(".tr-q__bubble")?.textContent).toBe("and after that?");
    expect(turns[1]?.querySelector(".tr-receipt")).toBeNull();
    expect(turns[1]?.querySelector(".tr-ans")).toBeNull();
    expect(turns[1]?.querySelector(".tr-work")).toBeNull();
  });

  it("draws no timestamp on a question, because no field serves one", () => {
    show([answer()]);
    expect(document.querySelector(".tr-q__meta")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// THE CANONICAL TURN'S FOUR ENDINGS.
//
// Every one of these used to draw as the same blank rectangle with "no output
// was served" under it, so each test below is paired with the one that would
// pass if the adapter collapsed them again.
// ---------------------------------------------------------------------------

describe("a turn that became a canonical Run", () => {
  const RUN = { agentId: "agt_meridian", runId: "run-1" };

  it("draws a paused turn as paused, with both routes out — never as 'no answer'", async () => {
    const user = userEvent.setup();
    const opened: string[] = [];
    show(
      [
        answer({
          text: "",
          citations: [],
          runRef: RUN,
          runStatus: "waiting_approval",
          approvalId: "req-9",
        }),
      ],
      { onOpenReceipt: (runId: string) => opened.push(runId) },
    );
    expect(screen.getByText(/Waiting for an approval decision/)).toBeInTheDocument();
    expect(receipt()?.textContent).toContain("Paused");
    expect(receipt()?.textContent).toContain("Awaiting a decision");
    // THE RECORD OPENS IN PLACE. This was an "Open Run" link to
    // /agents/:id/runs/:runId — the last control on this surface that threw the
    // reader onto another page after the redesign docked every other one.
    await user.click(screen.getByRole("button", { name: RUN_RECEIPT_LABEL }));
    expect(opened).toEqual(["run-1"]);
    expect(screen.getByRole("link", { name: "Review in Approvals" })).toHaveAttribute(
      "href",
      "/approvals?request=req-9",
    );
    // THE PAIRED ASSERTION: the empty-answer card must not fire on a turn whose
    // answer has not been produced YET.
    expect(screen.queryByText("No answer was produced.")).not.toBeInTheDocument();
    expect(receipt()?.className).not.toContain("tr-receipt--fault");
  });

  it("links nowhere near the run route, with or without a dock to open into", () => {
    // The /agents redesign kept `/agents/:id/runs/:runId` as a deep-link target
    // for audit entries and notifications, and removed every route INTO it from
    // the workspace. A turn footer that still linked there was the one survivor.
    show([answer({ text: "", citations: [], runRef: RUN, runStatus: "completed" })]);
    for (const link of screen.queryAllByRole("link")) {
      expect(link.getAttribute("href") ?? "").not.toMatch(/\/runs\//);
    }
    // …and with no dock to fill, the control does not render at all rather than
    // falling back to the page it was taken off.
    expect(screen.queryByRole("button", { name: RUN_RECEIPT_LABEL })).not.toBeInTheDocument();
  });

  it("still says 'No answer was produced.' when the completed Run produced none", () => {
    // THE PAIRED HALF. A genuinely empty turn keeps its honest fault card; the
    // paused exclusion above must not swallow it.
    show([answer({ text: "", citations: [], runRef: RUN, runStatus: "completed" })]);
    expect(screen.getByText("No answer was produced.")).toBeInTheDocument();
    expect(receipt()?.className).toContain("tr-receipt--fault");
  });

  it("draws a DECLINED turn as a decision carrying the agent's own words", () => {
    show([
      answer({
        text: "",
        citations: [],
        runRef: RUN,
        runStatus: "completed",
        runVerdict: "refused",
        failure: {
          kind: "refused",
          reason: "declined",
          detail: "I can't ground an answer to that in the records available.",
          retryable: false,
        },
      }),
    ]);
    expect(screen.getByText("Declined")).toBeInTheDocument();
    expect(screen.getByText("The agent declined to answer this question.")).toBeInTheDocument();
    expect(
      screen.getByText("I can't ground an answer to that in the records available."),
    ).toBeInTheDocument();
    // A decision, in the refusal vocabulary — never the fault red, never green.
    expect(receipt()?.className).toContain("tr-receipt--refusal");
    expect(receipt()?.className).not.toContain("tr-receipt--fault");
    expect(receipt()?.textContent).toContain("Refused");
    expect(receipt()?.textContent).not.toContain("Complete");
    // And it offers no retry: a decision is not undone by asking again.
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("draws a WITHHELD answer as withheld, not as a policy refusal and not as empty", () => {
    show([
      answer({
        text: "",
        citations: [],
        runRef: RUN,
        runStatus: "completed",
        outputState: "withheld",
        failure: { kind: "refused", reason: "withheld", detail: "", retryable: false },
      }),
    ]);
    expect(screen.getByText("Withheld")).toBeInTheDocument();
    expect(
      screen.getByText("Answer withheld — access to a source behind it changed."),
    ).toBeInTheDocument();
    // NOT the decline's words and NOT the policy refusal's — three different
    // decisions that share one tone must not share one sentence.
    expect(screen.queryByText("Refused by policy.")).not.toBeInTheDocument();
    expect(
      screen.queryByText("The agent declined to answer this question."),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("No answer was produced.")).not.toBeInTheDocument();
  });

  it("names the Run's served model in the receipt, and stays silent without one", () => {
    show([answer({ runRef: RUN, runStatus: "completed", servedModel: "qwen3.6-35b-a3b" })]);
    expect(receipt()?.textContent).toContain("qwen3.6-35b-a3b");
    expect(receipt()?.textContent).not.toContain("the server named no model");
  });

  it("offers no feedback affordance on a turn with no answer to judge", () => {
    show([answer({ text: "", citations: [], runRef: RUN, runStatus: "waiting_approval" })], {
      onThumbs: async () => {},
    });
    expect(screen.queryByRole("button", { name: "Helpful" })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// THE RUN'S RECORDED ACTIVITY, DRAWN.
//
// The adapter's job here is only to get the Run's rows into the same band the
// governed transits already use. That the band compresses rather than conceals
// is transcript/transcript.test.tsx's subject; what is held here is that a
// canonical turn's work REACHES it, and that an empty account draws nothing.
// ---------------------------------------------------------------------------

describe("MessageList — a canonical turn's Run steps", () => {
  it("MUTATION `run-steps-drawn`: the Run's recorded work reaches the band", () => {
    show([
      answer({
        text: "",
        citations: [],
        streaming: true,
        runSteps: [
          {
            key: "plan:0:search.contracts",
            name: "search.contracts",
            verdict: "allowed",
            verdictLabel: "Allowed",
            tier: null,
            durationSeconds: null,
            intent: "renewal terms",
          },
          {
            key: "run-event:evt-1",
            name: "qwen3.6-35b-a3b",
            verdict: "allowed",
            verdictLabel: "Served",
            tier: null,
            durationSeconds: null,
            intent: "model:0",
          },
        ],
      }),
    ]);
    expect(band()).not.toBeNull();
    expect(stepRow("search.contracts")).not.toBeNull();
    expect(stepRow("qwen3.6-35b-a3b")).not.toBeNull();
  });

  it("MUTATION `nothing-invented`: a turn with no recorded steps draws no band", () => {
    // THE PAIRED HALF. `runSteps` absent and `runSteps: []` are the same fact —
    // the server recorded nothing — and neither may produce a row.
    show([answer({ text: "", citations: [], streaming: true })]);
    expect(document.querySelectorAll(".tr-step")).toHaveLength(0);

    document.body.innerHTML = "";
    show([answer({ text: "", citations: [], streaming: true, runSteps: [] })]);
    expect(document.querySelectorAll(".tr-step")).toHaveLength(0);
  });

  it("a cancelled turn keeps its steps beside the stated failure", () => {
    show([
      answer({
        text: "",
        citations: [],
        streaming: false,
        runStatus: "cancelled",
        failure: { kind: "stage", reason: "run-incomplete", detail: "cancelled", retryable: true },
        runSteps: [
          {
            key: "run-event:evt-1",
            name: "qwen3.6-35b-a3b",
            verdict: "allowed",
            verdictLabel: "Served",
            tier: null,
            durationSeconds: null,
            intent: "model:0",
          },
        ],
      }),
    ]);
    // The work it reached survives the settlement — "how far did it get" has an
    // answer on screen, and the failure is stated rather than spun forever.
    expect(stepRow("qwen3.6-35b-a3b")).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ONE ACCOUNT PER CALL (owner defect, 2026-08-28).
//
// A canonical turn streams governed transits AND records Run rows for the same
// tool calls, and the adapter used to concatenate both — so every call was
// drawn twice: once timing-less at the head of the list, once in chronology
// with its duration. The owner: "just bold them in their regular chronological
// position don't randomly insert them at the top." What is held here: with Run
// rows present, a SETTLED transit adds no second row — and a transit the
// promotion rule pulls out (pending / refused) still promotes, because a
// parked approval is a gate a human owes, not a duplicate of recorded work.
// ---------------------------------------------------------------------------

describe("MessageList — one account per call on a canonical turn", () => {
  const transit = (over: Record<string, unknown> = {}) => ({
    key: "k1",
    event: {
      type: "tool" as const,
      name: "read.finance.ledger",
      tier: 2,
      decision: "allow" as const,
      audit_seq: 4812,
      ...over,
    },
  });
  const RUN_ROW = {
    key: "run-event:evt-tool",
    name: "read.finance.ledger",
    verdict: "allowed" as const,
    verdictLabel: "Recorded",
    tier: null,
    durationSeconds: 2.1,
    intent: "quarterly totals",
    rank: "act" as const,
  };

  it("MUTATION `no-second-row`: a settled transit the Run recorded is drawn once, in chronology", () => {
    show([answer({ text: "", citations: [], tools: [transit()], runSteps: [RUN_ROW] })]);
    const rows = Array.from(document.querySelectorAll(".tr-step__name")).filter(
      (el) => el.textContent === "read.finance.ledger",
    );
    // One row for one call — the Run's own, carrying the duration the transit
    // never had. The band's headline agrees: one step, not two.
    expect(rows).toHaveLength(1);
    expect(stepRow("read.finance.ledger")?.textContent).toContain("2.1s");
    expect(band()?.querySelector("summary")?.textContent).toContain("1 allowed");
  });

  it("MUTATION `gates-still-promote`: a pending transit is a gate, not a duplicate, beside Run rows", () => {
    show([
      answer({
        text: "",
        citations: [],
        tools: [transit({ decision: "pending", approval_id: "req-9", name: "write.crm.update" })],
        runSteps: [RUN_ROW],
      }),
    ]);
    // The S5 beat survives the dedup: the parked approval promotes out of the
    // band as its own card, and the recorded work still draws inside it.
    expect(gate()?.textContent).toContain("write.crm.update");
    expect(band()?.contains(stepRow("read.finance.ledger"))).toBe(true);
  });

  it("THE PAIRED HALF: with no Run rows the transits are the only account, and all of them draw", () => {
    // A legacy turn — or a canonical one the Run has recorded nothing for yet —
    // loses nothing: the filter applies only where the Run's rows carry the
    // chronology.
    show([answer({ text: "", citations: [], tools: [transit()], runSteps: [] })]);
    expect(stepRow("read.finance.ledger")).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// THE SOURCE BLOCK'S THIRD STATE (owner defect, 2026-08-28).
//
// After the answer lands, the citation read runs for tens of seconds, and the
// window drew nothing — which on this product reads as "cites nothing". The
// window now SAYS what it is ("Resolving citations…", fetched and matched —
// nothing is generated), in its own pending treatment, and only once the turn
// has settled: a streaming turn's absence of citations is a different fact
// again, and a landed read always outranks the pending claim.
// ---------------------------------------------------------------------------

describe("MessageList — the citation read's pending line", () => {
  it("MUTATION `pending-stated`: a settled turn with the read in flight says so", () => {
    show([answer({ citations: [], citationsPending: true })]);
    const line = screen.getByText("Resolving citations…");
    expect(line.classList.contains("tr-src__pending")).toBe(true);
    // NOT the settled note's treatment: "no citations yet" and "the read landed
    // and found none" must never look alike.
    expect(document.querySelector(".tr-src__note")).toBeNull();
  });

  it("MUTATION `not-before-settlement`: a streaming turn draws no pending line", () => {
    show([answer({ citations: [], streaming: true, citationsPending: true })]);
    expect(screen.queryByText("Resolving citations…")).toBeNull();
  });

  it("MUTATION `landed-read-outranks`: the settled note wins over a stale pending flag", () => {
    show([
      answer({
        citations: [],
        citationsPending: true,
        citationsNote: "1 of 1 cited source is no longer readable with your current access.",
      }),
    ]);
    expect(
      screen.getByText("1 of 1 cited source is no longer readable with your current access."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Resolving citations…")).toBeNull();
  });

  it("THE PAIRED HALF: a settled turn with no flag and no note claims nothing", () => {
    show([answer({ text: "answer text", citations: [] })]);
    expect(screen.queryByText("Resolving citations…")).toBeNull();
    expect(document.querySelector(".tr-src")).toBeNull();
  });
});

describe("MessageList — a failed run states its own cause", () => {
  it("MUTATION `stated-cause-rendered`: the server's reason is the line the reader gets", () => {
    show([
      answer({
        text: "",
        citations: [],
        failure: {
          kind: "stage",
          reason: "run-incomplete",
          detail: "failed",
          retryable: true,
          statedReason: "No current serving evidence for the requested lane.",
        },
      }),
    ]);
    // The reason the server marked publishable is ON SCREEN, unopened. A reader
    // who has to expand a disclosure to learn why a run died has been told the
    // generic line and nothing else.
    expect(
      screen.getByText("No current serving evidence for the requested lane."),
    ).toBeInTheDocument();
    // And the console's generic line is NOT what was shown in its place.
    expect(screen.queryByText("The run ended before it produced an answer.")).toBeNull();
  });

  it("MUTATION `no-reason-invented`: without a stated reason the generic line stands", () => {
    // THE PAIRED HALF. Nothing here writes a cause on the server's behalf.
    show([
      answer({
        text: "",
        citations: [],
        failure: { kind: "stage", reason: "run-incomplete", detail: "failed", retryable: true },
      }),
    ]);
    expect(screen.getByText("The run ended before it produced an answer.")).toBeInTheDocument();
  });
});
