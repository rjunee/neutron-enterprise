// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AgentRunEventWire } from "../api/types";
import { ModelContinuityReceipt } from "./ModelContinuityReceipt";

const event: AgentRunEventWire = {
  event_id: "change-7",
  run_id: "run-b",
  attempt_id: "attempt-b",
  sequence: 7,
  event_kind: "model.changed",
  event_status: "changed",
  parent_event_id: null,
  step_id: null,
  metadata: [
    { key: "safe_boundary", value: "between-runs" },
    { key: "actor", value: "owner@example.com" },
    { key: "before_requested_model", value: "frontier-a" },
    { key: "before_served_model", value: "frontier-a-served" },
    { key: "after_requested_model", value: "private-b" },
    { key: "after_authorized_model", value: "private-b-authorized" },
    { key: "context_snapshot_hash", value: "c".repeat(64) },
    { key: "context_fit", value: "fits" },
    { key: "compaction", value: "not_required" },
    { key: "cache_impact", value: "reset_expected" },
  ],
  detail_hash: "d".repeat(64),
  created_at: "2026-08-23T08:00:00Z",
};

describe("ModelContinuityReceipt", () => {
  it("renders server-authored compatibility, compaction, cache and before/after truth separately", () => {
    render(<ModelContinuityReceipt event={event} />);
    const receipt = screen.getByRole("region", { name: "Model continuity receipt change-7" });
    expect(receipt).toHaveTextContent("frontier-a-served");
    expect(receipt).toHaveTextContent("private-b-authorized");
    // ⚠️ THIS ASSERTION USED TO READ `toHaveTextContent("c".repeat(64))` — it
    // PINNED THE DEFECT. The row already knew this fact was a digest (it selects
    // the mono face on `key.includes("hash")`) and then painted all 64 characters,
    // which §2.7 (primitives/HashChip) exists to forbid: it records that the
    // shipped console once painted 64 chars into a 280px rail "where it overflowed
    // and painted across page content on four routes".
    expect(receipt).toHaveTextContent("cccccccc");
    expect(receipt).not.toHaveTextContent("c".repeat(64));
    // The full digest is not LOST — it stays reachable in `title` and on the
    // clipboard, which is the whole point of the chip.
    expect(within(receipt).getByText("cccccccc")).toHaveAttribute("title", "c".repeat(64));
    // The copy button says WHICH hash, so a receipt carrying several does not
    // render a column of identical buttons.
    expect(
      within(receipt).getByRole("button", { name: "Copy context snapshot hash" }),
    ).toBeInTheDocument();
    expect(receipt).toHaveTextContent("Context-window fitfits");
    expect(receipt).toHaveTextContent("Compactionnot_required");
    expect(receipt).toHaveTextContent("Prompt-cache impactreset_expected");
  });

  it("shows unavailable instead of hiding server receipt fields", () => {
    render(<ModelContinuityReceipt event={{ ...event, metadata: [] }} />);
    expect(screen.getAllByText("Unavailable")).toHaveLength(17);
  });
});
