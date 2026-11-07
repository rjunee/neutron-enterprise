// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE READ-BACK THREAD'S ONE OBLIGATION: it says what the turns index served,
// in the SAME words every other Run surface uses for the same facts.
//
// It used to hard-code "no output was served" and take the settlement tone for
// every turn, so a Run the server had not finished came back as
//
//     ✕ Running · Model · — no model reply was served
//     ANSWER — no output was served
//
// — a failure glyph on a live state, over a sentence about a finished turn.
// Both halves are the same defect: the console stating an outcome the server
// had not stated. Every test below pairs the live case with the settled one it
// must not have swallowed.

import { describe, expect, it } from "vitest";
import type { AgentConversationTurnPageWire, AgentRunStatusWire, AgentRunWire } from "../api/types";
import {
  RUN_IN_FLIGHT_MODEL_NOTE,
  RUN_IN_FLIGHT_OUTPUT_SENTENCE,
  RUN_NO_OUTPUT_SENTENCE,
  RUN_OUTPUT_PROTECTED_SENTENCE,
  RUN_SETTLED_MODEL_NOTE,
} from "../agent-harness/adapters/runWorkspace";
import { conversationTurns } from "./conversationTurns";

function run(status: AgentRunStatusWire): AgentRunWire {
  return {
    run_id: "run-1",
    route_position: "unavailable",
    fallback_from: null,
    conversation_id: "conv-1",
    agent_id: "agent-1",
    agent_version: 1,
    sequence: 9,
    version: 1,
    predecessor_run_id: null,
    root_run_id: "run-1",
    retry_ordinal: 0,
    display_label: "Review renewal risk",
    strategy: "agentic",
    status,
    output_verdict: null,
    model_policy_id: null,
    model_request_mode: "auto",
    context_snapshot_hash: null,
    requested_model: null,
    requested_model_lane: null,
    served_model: null,
    output_preview: null,
    config_hash: "a".repeat(64),
    capability_snapshot_hash: "b".repeat(64),
    created_at: "2026-08-22T09:00:00Z",
    updated_at: "2026-08-22T09:01:00Z",
    terminal_at: null,
  };
}

function page(status: AgentRunStatusWire): AgentConversationTurnPageWire {
  return {
    items: [
      {
        turn_id: "turn-9",
        turn_sequence: 9,
        user_content_state: "available",
        user_content: "Review renewal risk",
        run: run(status),
      },
    ],
    next_cursor: null,
  };
}

describe("conversationTurns", () => {
  it("MUTATION `live-turn-reads-as-live`: a running turn closes live, and says when it last moved", () => {
    const [turn] = conversationTurns(page("running"), () => null);
    expect(turn?.receipt?.tone).toBe("live");
    expect(turn?.receipt?.outcome).toBe("Running");
    // The stalled-vs-slow fact, and it is the SERVER'S stamp — this module
    // measures nothing.
    expect(turn?.receipt?.facts?.[0]?.label).toBe("Last update");
    expect(turn?.receipt?.facts?.[0]?.value).toContain("09:01:00");
    // A pending answer, not a missing one — and a pending model reply, not a
    // model that failed to answer.
    expect(turn?.answer?.absentNote).toBe(RUN_IN_FLIGHT_OUTPUT_SENTENCE);
    expect(turn?.receipt?.modelNote).toBe(RUN_IN_FLIGHT_MODEL_NOTE);
  });

  it("MUTATION `settled-turn-still-settles`: a completed empty turn is still an absence", () => {
    // THE PAIRED HALF. The live branch must not have swallowed the one case
    // "no output was served" was always true of, and a settled turn gets no
    // last-update stamp — its outcome word is the whole account.
    const [turn] = conversationTurns(page("completed"), () => null);
    expect(turn?.answer?.absentNote).toBe(RUN_NO_OUTPUT_SENTENCE);
    expect(turn?.receipt?.facts).toBeUndefined();
    // And the existing no-output downgrade is untouched: a Run that reached
    // `completed` while serving nothing is a fault the receipt says out loud,
    // not a quiet "Completed" (runWorkspace.runServedNoOutput).
    expect(turn?.receipt?.tone).toBe("fault");
    expect(turn?.receipt?.outcome).toBe("Completed without output");
    expect(turn?.receipt?.modelNote).toBe(RUN_SETTLED_MODEL_NOTE);
  });

  it("MUTATION `failed-turn-still-fails`: the fault treatment survives", () => {
    const [turn] = conversationTurns(page("failed"), () => null);
    expect(turn?.receipt?.tone).toBe("fault");
    expect(turn?.receipt?.facts).toBeUndefined();
  });

  it("MUTATION `turn-carries-run-identity`: every turn names its Run, so a live copy can supersede it", () => {
    // ChatPage reconciles the read-back copy of a turn against the live copy
    // by this id; without it the same turn rendered twice — a stale
    // "Submitted" card above the finished answer, question drawn twice.
    const [turn] = conversationTurns(page("completed"), () => null);
    expect(turn?.runId).toBe("run-1");
  });

  it("MUTATION `verified-empty-states-existence`: an undisclosed verified answer never reads as absence", () => {
    const wire = page("completed");
    const verified = { ...wire.items[0]!.run, output_verdict: "verified" as const };
    const [turn] = conversationTurns(
      { ...wire, items: [{ ...wire.items[0]!, run: verified }] },
      () => null,
    );
    // The answer EXISTS (the verdict is the server attesting it); the note
    // states that, and the receipt keeps the ordinary Completed close instead
    // of the no-output fault downgrade.
    expect(turn?.answer?.absentNote).toBe(RUN_OUTPUT_PROTECTED_SENTENCE);
    expect(turn?.receipt?.outcome).toBe("Completed");
    expect(turn?.receipt?.tone).toBe("ok");
  });

  it("MUTATION `detail-read-supersedes-list-row`: a fetched single-Run read supplies the answer text", () => {
    const wire = page("completed");
    const listRun = { ...wire.items[0]!.run, output_verdict: "verified" as const };
    const detail = {
      ...listRun,
      output_preview: "The disclosed verified answer.",
      output_state: "available" as const,
    };
    const [turn] = conversationTurns(
      { ...wire, items: [{ ...wire.items[0]!, run: listRun }] },
      () => null,
      new Map([["run-1", detail]]),
    );
    expect(turn?.answer?.body).toBe("The disclosed verified answer.");
    expect(turn?.answer?.absentNote).toBeUndefined();
  });

  it("never marks a detail read's full text as truncated, and always marks a budget-filling list cut", () => {
    // The detail read serves the WHOLE output (full_output=True), so a long
    // one must not grow an ellipsis; the list wire's 500-char slice must.
    const wire = page("completed");
    const long = `${"word ".repeat(120)}end.`;
    const listRun = { ...wire.items[0]!.run, output_preview: long.slice(0, 500) };
    const [cut] = conversationTurns(
      { ...wire, items: [{ ...wire.items[0]!, run: listRun }] },
      () => null,
    );
    expect(cut?.answer?.body.endsWith("…")).toBe(true);
    const detail = { ...listRun, output_preview: long, output_state: "available" as const };
    const [whole] = conversationTurns(
      { ...wire, items: [{ ...wire.items[0]!, run: listRun }] },
      () => null,
      new Map([["run-1", detail]]),
    );
    expect(whole?.answer?.body).toBe(long);
  });
});
