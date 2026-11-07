// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AgentTranscript } from "./AgentTranscript";
import type { AgentFrame, AgentTranscriptModel } from "./types";

function transcript(frames: readonly AgentFrame[], busy = false): AgentTranscriptModel {
  return { label: "Governed agent activity", frames, busy };
}

function transcriptItems(): HTMLLIElement[] {
  const list = screen.getByRole("list", { name: "Governed agent activity timeline" });
  return Array.from(list.querySelectorAll<HTMLLIElement>(":scope > li"));
}

const prompt: AgentFrame = {
  key: "ask:prompt:1",
  kind: "prompt",
  source: "ask",
  orderBasis: "wire-arrival",
  correlation: { sessionId: "session-1" },
  ordinal: 1,
  occurredAt: "2026-08-19T18:00:20Z",
  title: "Question asked",
  body: "Which accounts need review?",
  status: "neutral",
};

const pending: AgentFrame = {
  key: "conversation-ledger:approval:req-1",
  kind: "governance-checkpoint",
  checkpoint: "approval",
  source: "conversation-ledger",
  orderBasis: "source-order",
  correlation: { sessionId: "session-1", approvalId: "req-1" },
  ordinal: 2,
  occurredAt: "2026-08-19T18:00:10Z",
  title: "Approval required",
  body: "Paused — awaiting an authorized approver.",
  status: "pending",
  announcement: "Approval required. The run is paused.",
  facts: [{ label: "Request ID", value: "req-1", mono: true }],
  links: [{ label: "Open approval queue", href: "/approvals?request=req-1" }],
};

const allowed: AgentFrame = {
  key: "trace:tool:seq-44",
  kind: "tool-decision",
  source: "trace",
  orderBasis: "audit-seq",
  correlation: { sessionId: "session-1" },
  ordinal: 3,
  sequence: 44,
  occurredAt: "2026-08-19T18:00:05Z",
  title: "Tool allowed",
  body: "The governed executor reported an allowed call.",
  status: "allowed",
  tool: "write.internal.crm.note",
  decision: "allow",
  announcement: "Tool allowed: write.internal.crm.note.",
};

describe("AgentTranscript", () => {
  it("preserves source order instead of sorting by browser-visible time", () => {
    render(<AgentTranscript transcript={transcript([prompt, pending, allowed])} />);

    const items = transcriptItems();
    expect(items.map((item) => within(item).getByRole("heading").textContent)).toEqual([
      "Question asked",
      "Approval required",
      "Tool allowed",
    ]);
    expect(items.map((item) => item.getAttribute("data-order-basis"))).toEqual([
      "wire-arrival",
      "source-order",
      "audit-seq",
    ]);
    expect(items.map((item) => item.getAttribute("data-frame-source"))).toEqual([
      "ask",
      "conversation-ledger",
      "trace",
    ]);
  });

  it("keeps a pending checkpoint and later tool event separate without a shared lifecycle id", () => {
    render(<AgentTranscript transcript={transcript([pending, allowed])} />);

    const items = transcriptItems();
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveAttribute("data-frame-key", "conversation-ledger:approval:req-1");
    expect(items[1]).toHaveAttribute("data-frame-key", "trace:tool:seq-44");
    expect(within(items[0]!).getByText("req-1")).toBeInTheDocument();
    expect(within(items[1]!).queryByText("req-1")).toBeNull();
  });

  it("keeps requester-side approval checkpoints informational", async () => {
    render(<AgentTranscript transcript={transcript([pending])} />);

    await userEvent.click(screen.getByText("Details"));
    expect(screen.getByRole("link", { name: "Open approval queue" })).toHaveAttribute(
      "href",
      "/approvals?request=req-1",
    );
    expect(screen.queryByRole("button", { name: /approve|deny|reject/i })).toBeNull();
  });

  it("renders only recursive schema metadata for an unknown event", async () => {
    const secrets = [
      "<think>PRIVATE_CLOSED</think>",
      "<think>PRIVATE_OPEN",
      "PRIVATE_NESTED_SENTINEL",
      "PRIVATE_ARRAY_SENTINEL",
    ];
    const raw = {
      delta: secrets[0],
      partial: secrets[1],
      nested: { secret: secrets[2], list: [{ payload: secrets[3] }] },
      count: 0,
      enabled: false,
    };
    const frame: AgentFrame = {
      key: "trace:unknown:9",
      kind: "unknown",
      source: "trace",
      orderBasis: "audit-seq",
      correlation: { traceId: "trace-9", eventId: "event-9" },
      sequence: 9,
      title: "Future event",
      status: "neutral",
      raw,
    };
    const { container } = render(<AgentTranscript transcript={transcript([frame])} />);

    expect(screen.queryByRole("link")).toBeNull();
    const disclosure = screen.getByText("Details").closest("details");
    expect(disclosure).not.toBeNull();
    await userEvent.click(within(disclosure!).getByText("Details"));

    const schema = disclosure!.querySelector("pre");
    expect(schema).toBeVisible();
    expect(schema).toHaveTextContent("Payload values withheld.");
    expect(schema).toHaveTextContent("delta: string");
    expect(schema).toHaveTextContent("nested: object");
    expect(schema).toHaveTextContent("payload: string");
    for (const secret of secrets) {
      expect(container.textContent).not.toContain(secret);
      expect(container.innerHTML).not.toContain(secret);
      expect(screen.queryByText(secret, { exact: false })).toBeNull();
    }
    const article = screen.getByRole("article", { name: "Future event" });
    expect(article.closest("li")).toHaveAttribute("data-frame-source", "trace");
    expect(article.closest("li")).toHaveAttribute("data-order-basis", "audit-seq");
    expect(frame.correlation).toEqual({ traceId: "trace-9", eventId: "event-9" });
  });

  it("uses a semantic ordered list and exposes busy state without stealing focus", () => {
    const before = document.createElement("button");
    before.textContent = "Keep focus";
    document.body.append(before);
    before.focus();

    render(<AgentTranscript transcript={transcript([prompt], true)} />);

    const region = screen.getByRole("region", { name: "Governed agent activity" });
    expect(region).toHaveAttribute("aria-busy", "true");
    expect(
      within(region).getByRole("list", { name: "Governed agent activity timeline" }).tagName,
    ).toBe("OL");
    expect(document.activeElement).toBe(before);
    before.remove();
  });

  it("announces only the latest material transition", () => {
    const { container, rerender } = render(<AgentTranscript transcript={transcript([pending])} />);
    const live = screen.getByText("Approval required. The run is paused.");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(container.querySelectorAll('[aria-live="polite"]')).toHaveLength(1);

    rerender(<AgentTranscript transcript={transcript([pending, prompt])} />);
    expect(live).toHaveTextContent("Approval required. The run is paused.");

    rerender(<AgentTranscript transcript={transcript([pending, prompt, allowed])} />);
    expect(live).toHaveTextContent("Tool allowed: write.internal.crm.note.");
  });

  it("disables insertion animation under reduced motion", () => {
    const css = readFileSync(resolve(__dirname, "agentHarness.css"), "utf8");
    const reduced = /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*)\}\s*$/.exec(css);
    expect(reduced?.[1]).toContain(".ne-agent-frame");
    expect(reduced?.[1]).toContain("animation: none");
  });

  it("stacks frame headers and facts on narrow screens", () => {
    const css = readFileSync(resolve(__dirname, "agentHarness.css"), "utf8");
    const mobileStart = css.indexOf("@media (max-width: 48rem)");
    const reducedStart = css.indexOf("@media (prefers-reduced-motion: reduce)");
    const mobile = css.slice(mobileStart, reducedStart);

    expect(mobileStart).toBeGreaterThan(-1);
    expect(mobile).toContain("flex-direction: column");
    expect(mobile).toContain("grid-template-columns: minmax(0, 1fr)");
  });
});
