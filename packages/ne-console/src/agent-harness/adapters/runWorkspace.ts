// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE RUN WORKSPACE ADAPTER — CR-B4's pure half.
//
// The Agents Run workspace mounts the SAME turn system Ask mounts
// (src/transcript/) — one per-run transcript, in the run list's own sibling
// order. This file is that adapter's PURE half: no React, no client, so the
// promotion rule, the lineage line and the paused sentence are testable as
// rules and the render cannot disagree with them by construction. The half
// that must build a ReactNode (a gate's "Review in Approvals" link, the
// receipt's trace link) stays in AgentRunPage.tsx, exactly as Ask keeps
// ToolRow.tsx's two link-building branches beside its otherwise-pure step
// mapper.
//
// THE LINEAGE LINE IS DERIVED, NEVER FETCHED TWICE. "retry of Run N" and
// "retried as Run M" both fall out of the SAME served sibling-run list: a
// run's own `predecessor_run_id` names the first, and scanning that same
// list for another run whose `predecessor_run_id` equals this one's
// `run_id` names the second. Neither is a field the server does not serve —
// both are a graph already present in one response.
//
// THE PLAN STEPS ARE NOT GOVERNED TOOL DECISIONS. Unlike Ask, the Runs wire
// carries no per-step verdict/tier/duration (`AgentRunEventWire` has no
// analogue of `ToolTransitEvent`) — only the server-projected SAFE PLAN
// (`agentic_plan` / `execution_profile_receipt.ordered_plan`), which is
// already-executed content from the exact attempt. Every plan step therefore
// renders `allowed`, with no tier and no duration: never a guessed verdict,
// never a fabricated policy tier, never a timed step this browser measured.
//
// THE PAUSED SENTENCE IS EXACT (owner ruling, hard rule). Only ONE string
// renders in the Output slot's absent note while a Run is paused, and it is
// exported here so the component and its test read the same literal.

import type {
  AgentRunDetailWire,
  AgentRunEventWire,
  AgentRunStatusWire,
  AgentRunWire,
  PendingApprovalView,
} from "../../api/types";
import { RUN_PLAN_UNAVAILABLE } from "../../chat/askCopy";
import { absoluteTime } from "../../primitives/format";
import type { TranscriptFact, TranscriptReceiptTone, TranscriptStep } from "../../transcript";

/** Reconcile cursor pages by canonical sequence. Arrival time is never order authority. */
export function reconcileRunEvents(
  current: readonly AgentRunEventWire[],
  incoming: readonly AgentRunEventWire[],
): AgentRunEventWire[] {
  const byId = new Map(current.map((event) => [event.event_id, event]));
  for (const event of incoming) byId.set(event.event_id, event);
  return [...byId.values()].sort(
    (left, right) => left.sequence - right.sequence || left.event_id.localeCompare(right.event_id),
  );
}

export interface RunEventGroup {
  event: AgentRunEventWire;
  children: AgentRunEventWire[];
}

/** Group only explicit server-authored ids. Repeated labels/step ids remain flat. */
export function explicitEventGroups(events: readonly AgentRunEventWire[]): RunEventGroup[] {
  const ids = new Set(events.map((event) => event.event_id));
  const children = new Map<string, AgentRunEventWire[]>();
  const roots: AgentRunEventWire[] = [];
  for (const event of events) {
    if (event.parent_event_id !== null && ids.has(event.parent_event_id)) {
      const bucket = children.get(event.parent_event_id) ?? [];
      bucket.push(event);
      children.set(event.parent_event_id, bucket);
    } else {
      roots.push(event);
    }
  }
  return roots.map((event) => ({ event, children: children.get(event.event_id) ?? [] }));
}

/**
 * HOW A COMPLETED RUN REACHED ITS TERMINAL — the server's own closed word.
 *
 * `completed` is one status covering two different histories, and the wire is
 * deliberately built that way: `ne_agent_runs.status` stays `completed` for
 * both, and the distinguishing fact rides the `run.terminal` event's `outcome`
 * metadata (ne-api `run_projection`, a server-authored vocabulary).
 *
 *   * `execution_complete`        — the Run finished the plan it was running.
 *   * `approved_effect_complete`  — the Run had parked on an approval, and the
 *     resume executed EXACTLY the one effect a human released and then ended.
 *     It never re-entered the agent loop, so whatever the plan intended after
 *     that effect did not happen.
 *
 * Reading it costs nothing extra: this page already holds the event pages. It
 * returns `null` when no terminal event was served (a page that has not
 * reached it yet, or a truncated read) — never a guess, and never a COUNT of
 * unexecuted steps, which the platform cannot know.
 */
export type RunTerminalOutcome = "execution_complete" | "approved_effect_complete";

export function runTerminalOutcome(
  events: readonly AgentRunEventWire[],
): RunTerminalOutcome | null {
  for (const event of events) {
    if (event.event_kind !== "run.terminal") continue;
    for (const field of event.metadata) {
      if (field.key !== "outcome") continue;
      if (field.value === "approved_effect_complete") return "approved_effect_complete";
      if (field.value === "execution_complete") return "execution_complete";
    }
  }
  return null;
}

/** The EXACT sentence the paused-Run Output slot draws as its absent note
 * (SPEC §13.2 CR-B4, hard rule). Exported so the component and its test read
 * the identical literal rather than two copies that could drift apart. */
export const RUN_PAUSED_OUTPUT_SENTENCE =
  "No safe output preview is available while the Run is paused.";

type OutputState = "available" | "withheld" | "unavailable";

/**
 * WHY THE OUTPUT SLOT IS EMPTY, as four different facts rather than one.
 *
 * "no output was served" was the single note this slot drew for every empty
 * Output pane, and it was wrong three times out of four: a Run whose answer is
 * withheld produced one, a Run that declined produced a decision, and a Run
 * still running has not got there yet. Only the fourth case is an absence.
 *
 * Pure and exported for its own test — the Ask thread states the same four
 * facts in its own vocabulary, and neither may quietly acquire a fifth.
 */
export function runOutputAbsentNote(run: RunOutputSettlement): string {
  // THE TURN HAS NOT FINISHED, so the slot is PENDING, not empty. The header
  // above has always claimed this branch; it was never written, so every Run
  // still in flight drew "no output was served" — a sentence about a finished
  // turn — beside a footer that had already called it a failure. `runIsSettled`
  // is the one place in-flight is defined, and this reads it rather than
  // restating the status set.
  if (!runIsSettled(run.status)) return RUN_IN_FLIGHT_OUTPUT_SENTENCE;
  if (run.status === "waiting_approval") return RUN_PAUSED_OUTPUT_SENTENCE;
  if (run.output_state === "withheld") return RUN_OUTPUT_WITHHELD_SENTENCE;
  if (run.status === "completed" && run.output_verdict === "refused") {
    return RUN_OUTPUT_REFUSED_SENTENCE;
  }
  // A VERIFIED/GROUNDED ANSWER EXISTS — the verdict is the server attesting
  // exactly that — so an undisclosed slot here is "not read in this view",
  // never "nothing was produced". Saying "no output was served" beside the
  // green verdict pill was the console contradicting itself in one line.
  if (
    run.status === "completed" &&
    (run.output_verdict === "verified" || run.output_verdict === "grounded")
  ) {
    return RUN_OUTPUT_PROTECTED_SENTENCE;
  }
  return RUN_NO_OUTPUT_SENTENCE;
}

/** The answer exists; current authority over a source behind it changed. */
export const RUN_OUTPUT_WITHHELD_SENTENCE =
  "The answer was produced, and access to a source behind it has changed.";

/** The Run carries a refused verdict but protected no refusal text — the
 * fallback finalizer's path. The verdict is the whole account there is. */
export const RUN_OUTPUT_REFUSED_SENTENCE =
  "The Run declined to answer. No refusal text was served.";

/** The one genuine absence: a completed Run that produced nothing. */
export const RUN_NO_OUTPUT_SENTENCE = "no output was served";

/** The verified answer EXISTS and this view did not read it back: 010F stores
 * a source-derived answer in the protected-source domain, and only the
 * single-Run read spends the reauthorization that discloses it. Stated as the
 * existence fact it is — never as an absence beside a green verdict. */
export const RUN_OUTPUT_PROTECTED_SENTENCE =
  "the verified answer exists — it is source-protected and was not read back into this view. Open the Run receipt for the governed record.";

/** ne-api run_projection's `_MAX_OUTPUT_PREVIEW`: the character budget a LIST
 * row's `output_preview` is cut to. The single-Run read serves the full text. */
export const RUN_LIST_PREVIEW_CHARS = 500;

/**
 * A preview that filled the whole list budget was almost certainly cut
 * MID-WORD by the server's slice ("…the served evidence ide"). This ends the
 * rendered copy at the last word boundary and marks the cut with an ellipsis —
 * a statement that the text continues, which at the budget edge is what the
 * slice means. It never touches a preview under the budget: that is the whole
 * text and gets no mark.
 */
export function runPreviewProse(preview: string): string {
  if (preview.length < RUN_LIST_PREVIEW_CHARS) return preview;
  const trimmed = preview.replace(/\S+$/u, "").trimEnd();
  return `${trimmed === "" ? preview : trimmed}…`;
}

/** THE RUN IS STILL WORKING. A pending answer, not a missing one — and stated
 * without a duration, because nothing in this console measures the Run. What a
 * reader can act on is the served `updated_at` the receipt carries beside it
 * (`runLastUpdateFact`), which lets them tell a slow turn from a dead one
 * without this module guessing which it is. */
/** The receipt's model slot on a turn that HAS NOT CLOSED. Same defect as the
 * Output slot's: "no model reply was served" is a finished turn's sentence, and
 * under a ● Running footer it reads as a Run that failed to get one. */
export const RUN_IN_FLIGHT_MODEL_NOTE = "no model reply has been served yet";

/** The same slot on a turn that DID close having served none. */
export const RUN_SETTLED_MODEL_NOTE = "no model reply was served";

/** Which of the two, from the served status. `null` where a model WAS served —
 * the note has no rendering then, because there is no absence to explain. */
export function runModelAbsentNote(run: AgentRunWire): string | null {
  if (run.served_model !== null) return null;
  return runIsSettled(run.status) ? RUN_SETTLED_MODEL_NOTE : RUN_IN_FLIGHT_MODEL_NOTE;
}

export const RUN_IN_FLIGHT_OUTPUT_SENTENCE =
  "the Run has not finished, so no answer has been served yet";

/** "retry of Run N", "retried as Run M", both, or neither — computed from the
 * sibling list a Run's own conversation already served, never fetched again.
 * Order matches the render: a run's own predecessor names first, then any
 * run that named THIS one as ITS predecessor. */
export function runLineageSuffix(
  siblings: readonly AgentRunWire[],
  run: AgentRunWire,
): string | null {
  const parts: string[] = [];
  if (run.predecessor_run_id !== null) {
    const predecessor = siblings.find((sibling) => sibling.run_id === run.predecessor_run_id);
    if (predecessor) parts.push(`retry of Run ${predecessor.sequence}`);
  }
  const successor = siblings.find((sibling) => sibling.predecessor_run_id === run.run_id);
  if (successor) parts.push(`retried as Run ${successor.sequence}`);
  return parts.length > 0 ? parts.join(" · ") : null;
}

/** The run-list row's own glyph/tone/word — `tone` keys the render's `.g`/
 * `.a`/`.r` status-line classes (agents.css), which are the SAME glyph
 * vocabulary transcript.css's band tally and receipt already draw from. */
export interface RunStatusPresentation {
  readonly glyph: string;
  readonly tone: "g" | "a" | "r";
  readonly word: string;
}

const RUN_STATUS: Record<AgentRunStatusWire, RunStatusPresentation> = {
  submitted: { glyph: "○", tone: "a", word: "Submitted" },
  queued: { glyph: "○", tone: "a", word: "Queued" },
  running: { glyph: "●", tone: "a", word: "Running" },
  waiting_approval: { glyph: "⏸", tone: "a", word: "Awaiting approval" },
  cancelling: { glyph: "◐", tone: "a", word: "Cancelling" },
  completed: { glyph: "✓", tone: "g", word: "Completed" },
  failed: { glyph: "✕", tone: "r", word: "Failed" },
  cancelled: { glyph: "✕", tone: "r", word: "Cancelled" },
  expired: { glyph: "✕", tone: "r", word: "Expired" },
  context_exhausted: { glyph: "✕", tone: "r", word: "Context exhausted" },
  budget_exhausted: { glyph: "✕", tone: "r", word: "Budget exhausted" },
  unavailable: { glyph: "—", tone: "a", word: "Unavailable" },
};

export function runStatusPresentation(status: AgentRunStatusWire): RunStatusPresentation {
  return RUN_STATUS[status];
}

/** Statuses a reader would still call "in flight" — no settled narrative to
 * close a receipt with yet, so the turn draws no receipt for these. */
const IN_FLIGHT = new Set<AgentRunStatusWire>(["submitted", "queued", "running"]);
export function runIsSettled(status: AgentRunStatusWire): boolean {
  return !IN_FLIGHT.has(status);
}

/** The receipt footer's own outcome word — distinct from the run-list's
 * status word by design (the render: a list row says "Awaiting approval",
 * the same Run's receipt closes "Paused" — Ask's own vocabulary for a turn a
 * human owes a decision on). */
const RECEIPT_OUTCOME: Record<AgentRunStatusWire, string> = {
  submitted: "Submitted",
  queued: "Queued",
  running: "Running",
  waiting_approval: "Paused",
  cancelling: "Paused",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  expired: "Expired",
  context_exhausted: "Context exhausted",
  budget_exhausted: "Budget exhausted",
  unavailable: "Unavailable",
};

export function runReceiptOutcome(status: AgentRunStatusWire): string {
  return RECEIPT_OUTCOME[status];
}

/** The receipt's tone — an identity onto transcript's own closed vocabulary
 * (never a fourth tone invented here): `waiting_approval`/`cancelling` are
 * `paused`, `cancelled` is `stopped` (the reader ended it, nothing broke and
 * nothing refused), and every exhausted/expired/failed close is `fault`. */
export function runReceiptTone(status: AgentRunStatusWire): TranscriptReceiptTone {
  switch (status) {
    case "completed":
      return "ok";
    case "waiting_approval":
    case "cancelling":
      return "paused";
    case "cancelled":
      return "stopped";
    // THE IN-FLIGHT STATUSES, NAMED. They used to reach the `default` and close
    // `fault`, so a turn the server had not finished drew a red ✕ beside the
    // word "Running" — the console asserting an outcome nobody stated. They are
    // written out rather than tested through `runIsSettled` so that this switch
    // is exhaustive over the wire's own status words: a status added to the
    // wire lands in `default` and is a fault, which is the safe direction, and
    // a status that stops being in-flight fails to compile out of this list.
    case "submitted":
    case "queued":
    case "running":
      return "live";
    default:
      return "fault";
  }
}

/**
 * "Last update · 12:04" — the ONE fact that lets a reader tell a slow turn from
 * a dead one, and it is the server's own `updated_at`, not a duration.
 *
 * A stalled Run and a working Run are the same status word; the difference is
 * how long ago the server last touched it. This console owns no clock and will
 * not compute an age (the transcript's whole timing rule), so it states the
 * stamp and lets the reader do the subtraction against their own wall clock.
 * `null` for a settled Run: the receipt already says how the turn ended.
 */
export function runLastUpdateFact(run: AgentRunWire): TranscriptFact | null {
  if (runIsSettled(run.status)) return null;
  return { key: "last-update", label: "Last update", value: absoluteTime(run.updated_at) };
}

/** The three SERVED fields that decide whether a settled Run actually
 * delivered an answer. Structural, so a list row and a detail view are both
 * accepted without either one widening to the other's wire type. */
export type RunOutputSettlement = Pick<
  AgentRunWire,
  "status" | "output_verdict" | "output_preview"
> & {
  /** Detail-only (see AgentRunDetailWire). A list row supplies nothing, which
   * is why every rule below still holds without it. */
  output_state?: OutputState;
  /** Event-derived (see `runTerminalOutcome`), so only a surface that read the
   * Run's events can supply it. A list row supplies nothing — the LIST wire
   * carries no outcome field at all — and the rules below hold without it:
   * absent means "this surface did not read how the Run ended", never
   * "it ended the ordinary way". */
  terminal_outcome?: RunTerminalOutcome | null;
};

/**
 * A Run that completed and served nothing.
 *
 * `completed` is a statement about the WORKFLOW reaching its terminal state;
 * on its own it says nothing about whether a reader got an answer. When it did
 * not, "Completed ✓" is a false claim about what happened, so the surfaces
 * below withhold the affirming glyph/word rather than dress an empty turn as a
 * success.
 *
 * Two completed Runs are excluded, because for both of them an answer exists
 * and calling them "No output" would be the mirror-image false claim:
 *
 *   * A REFUSAL served a refusal — a disposition, not an absence (SPEC §10F,
 *     "Refused is a successfully served output disposition") — and already
 *     carries its own non-affirming Refused pill.
 *   * A VERIFIED/GROUNDED Run's answer is source-derived, so 010F stored it in
 *     the `protected_source_output` domain. The single-Run read now discloses
 *     that store too, through the same current-source reauthorization the
 *     Evidence drawer uses (ne-api `SourceBoundRunOutputReader`) — but a
 *     BOUNDED LIST read still does not, because 25 reauthorizations per page is
 *     its own decision. So an empty Output slot on a LIST row means "not read
 *     here", never "never produced".
 *
 * And one more, which is why `output_state` exists at all: an answer the
 * reauthorization WITHHELD was produced. Calling it "No output" would tell the
 * reader the system failed at the exact moment it worked.
 *
 * What remains is the honest case: terminal, unattested, and nothing served.
 */
export function runServedNoOutput(run: RunOutputSettlement): boolean {
  return (
    run.status === "completed" &&
    (run.output_verdict === null || run.output_verdict === "unavailable") &&
    run.output_state !== "withheld" &&
    run.output_preview === null
  );
}

/** The run-list row's glyph/tone/word, with the no-output downgrade applied. */
export function runSettlementPresentation(run: RunOutputSettlement): RunStatusPresentation {
  return runServedNoOutput(run)
    ? { glyph: "✕", tone: "r", word: "No output" }
    : runStatusPresentation(run.status);
}

/**
 * The word for a Run that resumed on an approval and stopped at the effect.
 *
 * "Completed" is true of the STATUS and false of the reader's inference: it
 * reads as "the turn did everything it set out to do", and this Run did one
 * released action and ended. The distinction is the whole point of the
 * `outcome` field, so the word states it rather than making a reader open the
 * event timeline to find out.
 */
export const APPROVED_EFFECT_OUTCOME_WORD = "Completed the approved action";

/** The receipt footer's outcome word, with the no-output downgrade applied and
 * the approved-effect terminal named where the surface read it. */
export function runSettlementReceiptOutcome(run: RunOutputSettlement): string {
  if (runServedNoOutput(run)) return "Completed without output";
  if (run.status === "completed" && run.terminal_outcome === "approved_effect_complete") {
    return APPROVED_EFFECT_OUTCOME_WORD;
  }
  return runReceiptOutcome(run.status);
}

/** The receipt footer's tone, with the no-output downgrade applied. */
export function runSettlementReceiptTone(run: RunOutputSettlement): TranscriptReceiptTone {
  return runServedNoOutput(run) ? "fault" : runReceiptTone(run.status);
}

/** The gate's reference line, composed verbatim from three SERVED fields —
 * never a fourth invented one. Matches the render's
 * "approval-9d44 · requested 12:06 · decision due 16:00" shape using the
 * console's one absolute-time format (§2.7), which every other approval
 * surface already renders `requested_at`/`sla_deadline` through
 * (approvals/DecisionPane.tsx). */
export function approvalReference(approval: PendingApprovalView): string {
  return (
    `${approval.request_id} · requested ${absoluteTime(approval.requested_at)}` +
    ` · decision due ${absoluteTime(approval.sla_deadline)}`
  );
}

/** The server-projected SAFE PLAN as band steps — always `allowed`, never a
 * tier or a duration the wire does not carry (see header). A Run whose
 * strategy's own receipt is absent gets no steps rather than a guessed one.
 *
 * THESE ARE `act` ROWS. A plan row is the tool call the agent MADE, carrying
 * the server's own tool name and its recorded `step_intent` — the two facts a
 * reader watching a working turn is looking for. Everything else the Run
 * records (a model hop, a governed check) is how that act was carried out, and
 * ranks below it. The rank is typography only: it sets no verdict and moves no
 * row into or out of the fold. (Ask's band ranks a NAMED `run.tool.pre_effect`
 * event row `act` for the same reason, and drops the plan row the event log
 * has already recorded — chat/runActivity.ts `runTurnSteps` — so one call is
 * one emphasised row, in chronology. This workspace reads no event log, so
 * every plan row draws here.) */
export function runPlanSteps(run: AgentRunDetailWire): TranscriptStep[] {
  if (run.strategy === "agentic" && run.agentic_plan) {
    return run.agentic_plan.map((step) => ({
      key: `plan:${step.ordinal}:${step.tool}`,
      name: step.tool,
      verdict: "allowed",
      verdictLabel: "Allowed",
      tier: null,
      durationSeconds: null,
      intent: step.step_intent,
      rank: "act",
    }));
  }
  if (run.strategy === "deterministic" && run.execution_profile_receipt) {
    return run.execution_profile_receipt.ordered_plan.map((step) => ({
      key: `plan:${step.ordinal}:${step.step_id}`,
      name: step.action,
      verdict: "allowed",
      verdictLabel: "Allowed",
      tier: null,
      durationSeconds: null,
      rank: "act",
    }));
  }
  return [];
}

/**
 * WHY THE BAND LISTS NO TOOL CALL — or `null` when it lists them fine.
 *
 * Both plan projections are all-or-nothing (askCopy RUN_PLAN_UNAVAILABLE states
 * the agentic case exactly), so their absence is a real, statable condition
 * rather than "nothing happened". This returns the sentence for the band's note
 * slot; it never returns a reconstructed plan, because the only other durable
 * account the Run keeps — its event log — carries no tool name at all.
 *
 * A strategy the server would not name (`"unavailable"`) gets `null`: this
 * console does not know which plan field to expect, and asserting that a tool
 * plan is missing would be a claim about a shape it cannot see.
 */
export function runPlanAbsentNote(run: AgentRunDetailWire): string | null {
  if (run.strategy === "agentic" && !run.agentic_plan) return RUN_PLAN_UNAVAILABLE;
  if (run.strategy === "deterministic" && !run.execution_profile_receipt) {
    return RUN_PLAN_UNAVAILABLE;
  }
  return null;
}
