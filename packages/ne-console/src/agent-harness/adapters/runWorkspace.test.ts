// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { AgentRunEventWire } from "../../api/types";
import { explicitEventGroups, reconcileRunEvents } from "./runWorkspace";

function event(id: string, sequence: number, parent: string | null = null): AgentRunEventWire {
  return {
    event_id: id,
    run_id: "run-1",
    attempt_id: "attempt-1",
    sequence,
    event_kind: "event.unavailable",
    event_status: "running",
    parent_event_id: parent,
    step_id: "repeated-name",
    metadata: [],
    detail_hash: "a".repeat(64),
    created_at: "2026-08-23T00:00:00Z",
  };
}

describe("Run workspace adapter", () => {
  it("deduplicates reconnect pages and preserves canonical sequence order", () => {
    expect(
      reconcileRunEvents(
        [event("two", 2)],
        [event("three", 3), event("one", 1), event("two", 2)],
      ).map((item) => item.event_id),
    ).toEqual(["one", "two", "three"]);
  });

  it("groups only explicit parent ids and never repeated step names", () => {
    const groups = explicitEventGroups([
      event("flat-a", 1),
      event("parent", 2),
      event("child", 3, "parent"),
      event("flat-b", 4),
    ]);
    expect(groups.map((group) => group.event.event_id)).toEqual(["flat-a", "parent", "flat-b"]);
    expect(groups[1]?.children.map((child) => child.event_id)).toEqual(["child"]);
    expect(groups[0]?.children).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// CR-B4 additions -- the Run workspace's pure adapter half. Each new
// assertion below is paired: a positive case AND the control that would pass
// if the rule were silently inverted or dropped.

import type { AgentRunDetailWire, AgentRunWire, PendingApprovalView } from "../../api/types";
import {
  RUN_IN_FLIGHT_OUTPUT_SENTENCE,
  RUN_LIST_PREVIEW_CHARS,
  RUN_NO_OUTPUT_SENTENCE,
  RUN_OUTPUT_PROTECTED_SENTENCE,
  RUN_OUTPUT_REFUSED_SENTENCE,
  RUN_OUTPUT_WITHHELD_SENTENCE,
  RUN_PAUSED_OUTPUT_SENTENCE,
  approvalReference,
  runOutputAbsentNote,
  runPreviewProse,
  runIsSettled,
  runLastUpdateFact,
  runLineageSuffix,
  runPlanSteps,
  runReceiptOutcome,
  runReceiptTone,
  runServedNoOutput,
  runSettlementPresentation,
  runSettlementReceiptOutcome,
  runSettlementReceiptTone,
  runStatusPresentation,
  runTerminalOutcome,
} from "./runWorkspace";

function run(overrides: Partial<AgentRunWire> = {}): AgentRunWire {
  return {
    run_id: "run-2",
    conversation_id: "conversation-1",
    agent_id: "meridian",
    agent_version: 1,
    sequence: 2,
    version: 1,
    predecessor_run_id: null,
    root_run_id: "run-2",
    retry_ordinal: 0,
    display_label: "Run 2",
    strategy: "agentic",
    status: "completed",
    output_verdict: null,
    model_policy_id: null,
    model_request_mode: "auto",
    context_snapshot_hash: null,
    requested_model: null,
    requested_model_lane: null,
    served_model: null,
    route_position: "unavailable",
    fallback_from: null,
    output_preview: null,
    config_hash: "a".repeat(64),
    capability_snapshot_hash: "b".repeat(64),
    created_at: "2026-08-23T12:00:00Z",
    updated_at: "2026-08-23T12:00:04Z",
    terminal_at: "2026-08-23T12:00:04Z",
    ...overrides,
  };
}

function detail(overrides: Partial<AgentRunDetailWire> = {}): AgentRunDetailWire {
  return {
    ...run(),
    output_state: "unavailable",
    approval_id: null,
    failure_reason: null,
    agentic_plan: null,
    execution_profile_receipt: null,
    skill_activation_receipt: null,
    claims: [],
    claim_mapping_state: "complete",
    claims_truncated: false,
    ...overrides,
  };
}

function approval(overrides: Partial<PendingApprovalView> = {}): PendingApprovalView {
  return {
    request_id: "approval-9d44",
    action: "write.internal.crm.note",
    tier: 2,
    principal_id: "user:owner",
    requested_at: "2026-08-23T12:06:00Z",
    sla_deadline: "2026-08-23T16:00:00Z",
    expires_at: "2026-08-23T20:00:00Z",
    escalated: false,
    ...overrides,
  };
}

describe("runLineageSuffix", () => {
  it("names the predecessor when the sibling list carries it", () => {
    const run1 = run({ run_id: "run-1", sequence: 1 });
    const run2 = run({ run_id: "run-2", sequence: 2, predecessor_run_id: "run-1" });
    expect(runLineageSuffix([run1, run2], run2)).toBe("retry of Run 1");
  });

  it("names the successor when another Run named THIS one as its predecessor", () => {
    const run2 = run({ run_id: "run-2", sequence: 2 });
    const run3 = run({ run_id: "run-3", sequence: 3, predecessor_run_id: "run-2" });
    expect(runLineageSuffix([run2, run3], run2)).toBe("retried as Run 3");
  });

  it("combines both when a Run is both a retry and later retried", () => {
    const run1 = run({ run_id: "run-1", sequence: 1 });
    const run2 = run({ run_id: "run-2", sequence: 2, predecessor_run_id: "run-1" });
    const run3 = run({ run_id: "run-3", sequence: 3, predecessor_run_id: "run-2" });
    expect(runLineageSuffix([run1, run2, run3], run2)).toBe("retry of Run 1 · retried as Run 3");
  });

  it("names neither for a Run with no lineage in the sibling list (control)", () => {
    const solo = run({ run_id: "run-1", sequence: 1 });
    expect(runLineageSuffix([solo], solo)).toBeNull();
  });

  it("never invents a predecessor label the sibling list does not itself carry", () => {
    // predecessor_run_id names a Run that this sibling list never served.
    const orphan = run({ run_id: "run-2", sequence: 2, predecessor_run_id: "run-missing" });
    expect(runLineageSuffix([orphan], orphan)).toBeNull();
  });
});

describe("RUN_PAUSED_OUTPUT_SENTENCE", () => {
  it("is the exact sentence the paused-Run Output slot draws (hard rule)", () => {
    expect(RUN_PAUSED_OUTPUT_SENTENCE).toBe(
      "No safe output preview is available while the Run is paused.",
    );
  });
});

describe("approvalReference", () => {
  it("composes the reference from exactly the three served fields", () => {
    const reference = approvalReference(approval());
    expect(reference).toContain("approval-9d44");
    expect(reference).toContain("requested");
    expect(reference).toContain("decision due");
    // Both timestamps distinctly present, not one substituting for the other.
    expect(reference).toMatch(/2026-08-23 12:06:00/);
    expect(reference).toMatch(/2026-08-23 16:00:00/);
  });
});

describe("runPlanSteps", () => {
  it("renders an agentic Run's safe plan as allowed steps with no tier or duration", () => {
    const steps = runPlanSteps(
      detail({
        strategy: "agentic",
        agentic_plan: [
          {
            ordinal: 0,
            tool: "read.contracts.search",
            step_intent: "renewal terms",
            tool_call_id: null,
          },
          {
            ordinal: 1,
            tool: "read.tickets.search",
            step_intent: "open tickets",
            tool_call_id: null,
          },
        ],
      }),
    );
    expect(steps.map((step) => step.name)).toEqual([
      "read.contracts.search",
      "read.tickets.search",
    ]);
    expect(steps.every((step) => step.verdict === "allowed")).toBe(true);
    expect(steps.every((step) => step.tier === null)).toBe(true);
    expect(steps.every((step) => step.durationSeconds === null)).toBe(true);
    expect(steps[0]?.intent).toBe("renewal terms");
  });

  it("renders a deterministic Run's frozen ordered plan the same way", () => {
    const steps = runPlanSteps(
      detail({
        strategy: "deterministic",
        execution_profile_receipt: {
          profile_receipt_id: "receipt-1",
          run_id: "run-2",
          attempt_id: "attempt-1",
          workflow_id: "workflow-1",
          profile_version: 1,
          profile_hash: "c".repeat(64),
          input_schema: {},
          input_schema_hash: "d".repeat(64),
          typed_input_state: "unavailable",
          typed_input: null,
          ordered_plan: [
            {
              ordinal: 1,
              step_id: "step-1",
              kind: "governed-tool",
              action: "Frozen lookup",
              model_role: null,
            },
          ],
          ordered_plan_hash: "e".repeat(64),
          receipt_hash: "f".repeat(64),
          created_at: "2026-08-23T12:00:00Z",
        },
      }),
    );
    expect(steps).toHaveLength(1);
    expect(steps[0]?.name).toBe("Frozen lookup");
    expect(steps[0]?.tier).toBeNull();
  });

  it("never invents a plan when the strategy's own receipt is absent (control)", () => {
    expect(runPlanSteps(detail({ strategy: "agentic", agentic_plan: null }))).toEqual([]);
    expect(
      runPlanSteps(detail({ strategy: "deterministic", execution_profile_receipt: null })),
    ).toEqual([]);
  });
});

describe("run status vocabulary", () => {
  it("draws a paused Run's receipt tone/outcome distinct from its list-row word", () => {
    expect(runStatusPresentation("waiting_approval").word).toBe("Awaiting approval");
    expect(runReceiptOutcome("waiting_approval")).toBe("Paused");
    expect(runReceiptTone("waiting_approval")).toBe("paused");
  });

  it("keeps a reader-cancelled Run out of the fault tone (control against over-reddening)", () => {
    expect(runReceiptTone("cancelled")).toBe("stopped");
    expect(runReceiptTone("failed")).toBe("fault");
  });

  it("treats only submitted/queued/running as not yet settled", () => {
    expect(runIsSettled("running")).toBe(false);
    expect(runIsSettled("waiting_approval")).toBe(true);
    expect(runIsSettled("completed")).toBe(true);
  });
});

describe("how a completed Run reached its terminal", () => {
  function terminal(outcome: string | null): AgentRunEventWire {
    const base = event("terminal", 9);
    return {
      ...base,
      event_kind: "run.terminal",
      event_status: "completed",
      metadata: outcome === null ? [] : [{ key: "outcome", value: outcome }],
    };
  }

  it("reads the server's own outcome word off the terminal event", () => {
    expect(runTerminalOutcome([event("a", 1), terminal("approved_effect_complete")])).toBe(
      "approved_effect_complete",
    );
    expect(runTerminalOutcome([terminal("execution_complete")])).toBe("execution_complete");
  });

  it("returns null rather than guessing when no terminal outcome was served", () => {
    // A page that has not reached the terminal event, and a terminal event the
    // server sent without the key, are both "not known" — never "the ordinary
    // way", which is the assumption that made the two histories one word.
    expect(runTerminalOutcome([event("a", 1)])).toBeNull();
    expect(runTerminalOutcome([terminal(null)])).toBeNull();
    expect(runTerminalOutcome([])).toBeNull();
  });

  it("names an approved-effect terminal instead of claiming a plain Completed", () => {
    // The Run parked on an approval, a human released ONE effect, the resume
    // executed exactly that and ended. "Completed" is true of the status and
    // false of what a reader takes from it.
    const settlement = {
      ...run({ status: "completed", output_preview: "The approved action was executed." }),
      terminal_outcome: "approved_effect_complete" as const,
    };
    expect(runSettlementReceiptOutcome(settlement)).toBe("Completed the approved action");
    // Still an honest success: the released action really ran.
    expect(runSettlementReceiptTone(settlement)).toBe("ok");
  });

  it("leaves an ordinary completion — and an unread one — saying Completed (control)", () => {
    const served = run({ status: "completed", output_preview: "Four contracts renew." });
    expect(runSettlementReceiptOutcome({ ...served, terminal_outcome: "execution_complete" })).toBe(
      "Completed",
    );
    // No events read on this surface: the receipt says only what the status
    // says, and never borrows the approved-effect word on a hunch.
    expect(runSettlementReceiptOutcome(served)).toBe("Completed");
  });

  it("keeps the no-output downgrade ahead of the outcome word", () => {
    // A resumed Run that served nothing is still a Run that served nothing;
    // the emptier fact wins, exactly as it does over a plain Completed.
    const empty = {
      ...run({ status: "completed", output_verdict: "unavailable", output_preview: null }),
      terminal_outcome: "approved_effect_complete" as const,
    };
    expect(runSettlementReceiptOutcome(empty)).toBe("Completed without output");
    expect(runSettlementReceiptTone(empty)).toBe("fault");
  });
});

describe("a completed Run that served nothing", () => {
  const served = run({ status: "completed", output_preview: "The portfolio holds 4 subjects." });
  const empty = run({ status: "completed", output_verdict: "unavailable", output_preview: null });

  it("never closes an empty completed turn with the affirming ✓/Completed", () => {
    expect(runServedNoOutput(empty)).toBe(true);
    expect(runSettlementPresentation(empty)).toEqual({
      glyph: "✕",
      tone: "r",
      word: "No output",
    });
    expect(runSettlementReceiptOutcome(empty)).toBe("Completed without output");
    expect(runSettlementReceiptTone(empty)).toBe("fault");
  });

  it("leaves a Run that DID serve its answer green (control against over-reddening)", () => {
    expect(runServedNoOutput(served)).toBe(false);
    expect(runSettlementPresentation(served)).toEqual(runStatusPresentation("completed"));
    expect(runSettlementReceiptOutcome(served)).toBe("Completed");
    expect(runSettlementReceiptTone(served)).toBe("ok");
  });

  it("leaves a refusal alone — refusing is a served outcome, not an absent one", () => {
    const refused = run({ status: "completed", output_verdict: "refused", output_preview: null });
    expect(runServedNoOutput(refused)).toBe(false);
    expect(runSettlementReceiptTone(refused)).toBe("ok");
  });

  it("never calls a grounded Run 'No output' — its answer is served by the drawer", () => {
    // Source-derived output lives in `protected_source_output`, which the
    // Output pane does not read. An empty pane there means "shown through the
    // Evidence drawer's own reauthorization", not "never produced".
    for (const verdict of ["verified", "grounded"] as const) {
      const attested = run({
        status: "completed",
        output_verdict: verdict,
        output_preview: null,
      });
      expect(runServedNoOutput(attested)).toBe(false);
      expect(runSettlementReceiptTone(attested)).toBe("ok");
    }
  });

  it("never reddens an unsettled or paused Run, which owes no output yet", () => {
    for (const status of ["running", "queued", "waiting_approval"] as const) {
      const pending = run({ status, output_verdict: null, output_preview: null });
      expect(runServedNoOutput(pending)).toBe(false);
      expect(runSettlementPresentation(pending)).toEqual(runStatusPresentation(status));
    }
  });
});

describe("runOutputAbsentNote", () => {
  const base = {
    status: "completed" as const,
    output_verdict: null,
    output_preview: null,
  };

  it("states the four reasons an Output slot is empty as four different facts", () => {
    // The whole point: "no output was served" was drawn for all four, and was
    // wrong for three of them.
    expect(runOutputAbsentNote({ ...base, status: "waiting_approval" })).toBe(
      RUN_PAUSED_OUTPUT_SENTENCE,
    );
    expect(runOutputAbsentNote({ ...base, output_state: "withheld" })).toBe(
      RUN_OUTPUT_WITHHELD_SENTENCE,
    );
    expect(runOutputAbsentNote({ ...base, output_verdict: "refused" })).toBe(
      RUN_OUTPUT_REFUSED_SENTENCE,
    );
    expect(runOutputAbsentNote(base)).toBe(RUN_NO_OUTPUT_SENTENCE);
    // Distinct sentences, so none of them can quietly become another.
    expect(
      new Set([
        RUN_PAUSED_OUTPUT_SENTENCE,
        RUN_OUTPUT_WITHHELD_SENTENCE,
        RUN_OUTPUT_REFUSED_SENTENCE,
        RUN_OUTPUT_PROTECTED_SENTENCE,
        RUN_NO_OUTPUT_SENTENCE,
      ]).size,
    ).toBe(5);
  });

  it("MUTATION `verified-is-not-empty`: an undisclosed verified answer states existence, never absence", () => {
    // "no output was served" beside a green Verified pill was the console
    // contradicting itself in one line: the verdict IS the server attesting an
    // answer exists (010F stores it source-protected; only the single-Run read
    // discloses it). The note now states that fact.
    for (const verdict of ["verified", "grounded"] as const) {
      expect(runOutputAbsentNote({ ...base, output_verdict: verdict })).toBe(
        RUN_OUTPUT_PROTECTED_SENTENCE,
      );
    }
  });
});

describe("runPreviewProse", () => {
  it("ends a budget-filling preview at a word boundary with an ellipsis", () => {
    // ne-api slices list previews at RUN_LIST_PREVIEW_CHARS, mid-word — the
    // live corpus really rendered "…the served evidence ide". The render ends
    // at the last whole word and marks the cut.
    const cut = `${"word ".repeat(99)}ident`; // exactly the 500-char budget
    expect(cut).toHaveLength(RUN_LIST_PREVIEW_CHARS);
    expect(runPreviewProse(cut)).toBe(`${"word ".repeat(98)}word…`);
  });

  it("leaves a preview under the budget untouched — that is the whole text", () => {
    expect(runPreviewProse("The whole answer.")).toBe("The whole answer.");
  });

  it("keeps a single unbroken token whole rather than deleting the preview", () => {
    const hashish = "a".repeat(RUN_LIST_PREVIEW_CHARS);
    expect(runPreviewProse(hashish)).toBe(`${hashish}…`);
  });
});

describe("runServedNoOutput", () => {
  it("calls a completed Run that served nothing exactly that", () => {
    expect(
      runServedNoOutput({
        status: "completed",
        output_verdict: "unavailable",
        output_preview: null,
      }),
    ).toBe(true);
  });

  it("never calls a WITHHELD answer 'no output' — it was produced", () => {
    // THE PAIRED HALF. Downgrading a withheld answer to "No output ✕" tells the
    // reader the system failed at the exact moment it enforced a boundary.
    expect(
      runServedNoOutput({
        status: "completed",
        output_verdict: "unavailable",
        output_preview: null,
        output_state: "withheld",
      }),
    ).toBe(false);
  });
});

/* --------------------------------------------------------------------------
 * A TURN IN FLIGHT IS NOT A FAILED TURN.
 *
 * WHAT SHIPPED. `runReceiptTone` had no case for the in-flight statuses, so
 * `submitted`, `queued` and `running` fell through its `default` and closed
 * `fault`. A turn the server had not finished drew a red ✕ beside the word
 * "Running", over an Output slot that said "no output was served" — a sentence
 * about a FINISHED turn. The footer asserted an outcome the server had not
 * stated, and it asserted it in the failure ramp.
 *
 * Both halves are pinned here: the live turn reads as live, AND a genuinely
 * failed one still reads as failed. A "fix" that softened the fault treatment
 * would satisfy the first and is what the second forbids.
 * ------------------------------------------------------------------------ */

describe("the in-flight turn", () => {
  /** A settled Run that produced nothing — the one genuine absence, and the
   * control every in-flight assertion below is measured against. */
  const settledEmpty = {
    status: "completed" as const,
    output_verdict: null,
    output_preview: null,
  };

  it("MUTATION `running-is-not-a-failure`: the in-flight statuses close live", () => {
    for (const status of ["submitted", "queued", "running"] as const) {
      expect(runReceiptTone(status)).toBe("live");
      expect(runIsSettled(status)).toBe(false);
    }
    // THE PAIRED HALF: a turn that actually broke still wears the fault, and a
    // refusal, a pause and a reader's own stop still keep their own tones.
    expect(runReceiptTone("failed")).toBe("fault");
    expect(runReceiptTone("context_exhausted")).toBe("fault");
    expect(runReceiptTone("waiting_approval")).toBe("paused");
    expect(runReceiptTone("cancelled")).toBe("stopped");
    expect(runReceiptTone("completed")).toBe("ok");
  });

  it("MUTATION `pending-is-not-absent`: a live Output slot says it is pending", () => {
    // "no output was served" is a claim about a finished turn. A Run still
    // working has not got there yet — the header of runOutputAbsentNote has
    // always said so, and the branch was never written.
    for (const status of ["submitted", "queued", "running"] as const) {
      expect(runOutputAbsentNote({ ...settledEmpty, status })).toBe(RUN_IN_FLIGHT_OUTPUT_SENTENCE);
    }
    // THE PAIRED HALF: the genuine absence keeps its own sentence, so the new
    // branch cannot swallow the case it was carved out of.
    expect(runOutputAbsentNote(settledEmpty)).toBe(RUN_NO_OUTPUT_SENTENCE);
  });

  it("MUTATION `stalled-is-legible`: a live turn carries the served last-update stamp", () => {
    // The one fact that separates a slow turn from a dead one — and it is the
    // SERVER'S `updated_at`, not an age this console computed. Nothing here
    // owns a clock, so it states the stamp and lets the reader subtract.
    const live = runLastUpdateFact(run({ status: "running", updated_at: "2026-08-22T09:01:00Z" }));
    expect(live?.label).toBe("Last update");
    expect(live?.value).toContain("09:01:00");
    // THE PAIRED HALF: a settled turn gets nothing — its outcome word is
    // already the whole account, and a stamp there would read as an open state.
    expect(runLastUpdateFact(run({ status: "completed" }))).toBeNull();
    expect(runLastUpdateFact(run({ status: "failed" }))).toBeNull();
  });
});
