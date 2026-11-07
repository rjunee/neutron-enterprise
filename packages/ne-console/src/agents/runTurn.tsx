// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// ONE RUN, AS ONE TRANSCRIPT TURN.
//
// Lifted out of AgentRunPage unchanged, because two surfaces now render it: the
// deterministic workspace's thread (where past runs read in the same transcript
// grammar as questions — the submitted typed input opens the turn where a
// question would) and the `/agents/:id/runs/:runId` deep link. One adapter, so
// the two can never disagree about what a Run says.
//
// WHY THE OUTPUT SLOT IS EMPTY IS FOUR FACTS, NOT ONE, and why a settled Run
// closes the way it does is the SERVED settlement, not the status word alone.
// Both rules live in agent-harness/adapters/runWorkspace.ts — `runOutputAbsentNote`
// and the `runSettlement*` trio — and this calls them rather than restating
// them: "no output was served" is true of exactly one of the four cases, and a
// Run that completed while serving nothing is a fault the receipt must say out
// loud rather than close as "Completed".
//
// This module maps the Run wire onto src/transcript/'s view model and does
// nothing else. It does not touch the step model or the promotion rule: routine
// allowed work demotes into the collapsed band, anything pending a human or
// refused promotes out and can never fold. That rule is transcript/promotion.ts's
// and is load-bearing.

import type { ReactNode } from "react";
import type { PendingApprovalView } from "../api/types";
import {
  approvalReference,
  runIsSettled,
  runLineageSuffix,
  runOutputAbsentNote,
  runPlanAbsentNote,
  runPlanSteps,
  runSettlementReceiptOutcome,
  runSettlementReceiptTone,
} from "../agent-harness/adapters/runWorkspace";
import type { RunTerminalOutcome } from "../agent-harness/adapters/runWorkspace";
import { explainStatedReason } from "../chat/statedReason";
import { absoluteTime } from "../primitives/format";
import type { TranscriptFact, TranscriptStep, TranscriptTurn } from "../transcript";
import { OutputVerdictPill } from "./RunReceiptPane";
import type { RunDetail, RunReceipt } from "./types";

/** The turn's question meta: "Run N · <time>", plus the served lineage suffix
 * when the sibling list names one. */
/** The Run's published safe reason, trimmed, or `null` when it published none.
 * Pure and exported so the turn, the docked receipt and the deep-link page all
 * read one rule rather than three copies of a truthiness check. */
export function statedFailureReason(run: RunDetail): string | null {
  const reason = run.failure_reason;
  if (typeof reason !== "string") return null;
  const trimmed = reason.trim();
  return trimmed === "" ? null : trimmed;
}

export function runQuestionMeta(run: RunDetail, siblings: readonly RunReceipt[]): string {
  const lineage = runLineageSuffix(siblings, run);
  const stamp = `Run ${run.sequence} · ${absoluteTime(run.created_at)}`;
  return lineage ? `${stamp} · ${lineage}` : stamp;
}

export function buildRunTurn({
  run,
  siblings,
  approval,
  terminalOutcome = null,
  gateAction,
  traceLink,
  receiptActions,
}: {
  run: RunDetail;
  siblings: readonly RunReceipt[];
  approval: PendingApprovalView | null;
  /** How a completed Run reached its terminal, read from its own served
   * events (`runTerminalOutcome`). `null` where the caller has no event page
   * — the receipt then says only what the status says. */
  terminalOutcome?: RunTerminalOutcome | null;
  gateAction?: ReactNode;
  traceLink?: ReactNode;
  receiptActions?: ReactNode;
}): TranscriptTurn {
  const paused = run.status === "waiting_approval";
  const steps: TranscriptStep[] = [...runPlanSteps(run)];
  const planNote = runPlanAbsentNote(run);
  if (paused && approval) {
    steps.push({
      key: `gate:${approval.request_id}`,
      name: approval.action,
      verdict: "pending",
      verdictLabel: "Awaiting approval",
      tier: approval.tier,
      durationSeconds: null,
      reference: approvalReference(approval),
      action: gateAction,
    });
  }
  const settled = runIsSettled(run.status);
  const settlement = { ...run, terminal_outcome: terminalOutcome };
  const facts: TranscriptFact[] = [];
  if (paused) {
    facts.push({ key: "decision", value: "Awaiting a decision" });
    facts.push({
      key: "verdict",
      label: "Verdict",
      value: run.output_verdict,
      note: run.output_verdict === null ? "pending" : undefined,
    });
  }
  if (Array.isArray(run.claims) && run.claims.length > 0) {
    facts.push({ key: "claims", label: "Claims", value: String(run.claims.length) });
  }
  // THE SERVER'S STATED CAUSE, ON THE TURN ITSELF — SAID TO A PERSON.
  // `failure_reason` is the opt-in safe reason the raiser marked publishable
  // (ne-api `run_projection._failure_reason`); `failure_class` is an OpaqueId
  // and is deliberately not served, because an id is not prose for a person.
  // Where the server marked no reason this pushes NOTHING — the status word
  // stands alone rather than acquiring a sentence this console wrote on the
  // server's behalf.
  //
  // It used to be pushed VERBATIM, and the receipt footer is a one-line strip
  // of facts with no room for a disclosure, so the reader's line was
  // "Reason · … Attempts: no-span,no-span. [aw=154 sources=2 maxrun=1 floor=5]".
  // chat/statedReason.ts translates the validators' closed vocabulary the same
  // way it does for the Ask card and for the read-back rows
  // (agents/conversationTurns.tsx) — one rule, three surfaces. The verbatim
  // original is NOT lost here: the Run receipt pane this turn's action opens
  // carries it behind "what the server said".
  const stated = statedFailureReason(run);
  if (stated !== null)
    facts.push({ key: "reason", label: "Reason", value: explainStatedReason(stated).sentence });
  const pill = !paused ? <OutputVerdictPill verdict={run.output_verdict} /> : null;
  return {
    id: run.run_id,
    question: { text: run.display_label, meta: runQuestionMeta(run, siblings) },
    steps,
    // The plan projection is all-or-nothing, so its absence is a statable fact
    // and the band says it rather than reconstructing a plan it cannot see.
    ...(planNote ? { workNote: planNote } : {}),
    answer: {
      label: "Output",
      // NO ellipsis heuristic here: every caller hands this the single-Run
      // DETAIL read, whose output_preview is the full text (full_output=True)
      // — marking it truncated would claim text that does not exist.
      body: run.output_preview ?? "",
      // Four facts, one per reason the slot is empty — see runOutputAbsentNote.
      absentNote: run.output_preview === null ? runOutputAbsentNote(run) : undefined,
    },
    ...(settled
      ? {
          receipt: {
            outcome: runSettlementReceiptOutcome(settlement),
            tone: runSettlementReceiptTone(settlement),
            ...(pill ? { pill } : {}),
            facts,
            model: run.served_model,
            modelNote: run.served_model === null ? "no model reply was served" : undefined,
            ...(traceLink ? { trace: traceLink } : {}),
            ...(receiptActions ? { actions: receiptActions } : {}),
          },
        }
      : {}),
  };
}
