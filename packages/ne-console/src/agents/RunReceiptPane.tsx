// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE RUN RECEIPT — DOCKED, NOT NAVIGATED.
//
// The owner: "its so confusing opening a run and it takes me to some other
// page." Opening a Run's governance detail used to leave the thread, load
// another route, and land the reader on a screen with its own rail, its own
// tabs and its own back link. Reading one receipt cost the whole context of
// the conversation it came from.
//
// So the receipt is CONTENT, not a route. `RunReceiptPane` renders the fields;
// the workspace docks them in the standing `DetailPane` beside the thread, and
// the `/agents/:id/runs/:runId` deep link — which audit entries and
// notifications hand out — renders the SAME component standalone. One body of
// facts, two frames. Nothing in the workspace links to the route any more.
//
// WHY A FAILED RUN FAILED, IN THE SERVER'S OWN WORDS.
//
// A terminal non-completed Run used to carry only `failure_class`, an OpaqueId
// this console never referenced, so a stated governance refusal and an
// arbitrary crash reached the reader as the same generic line. `failure_reason`
// is the OPT-IN safe reason the raiser marked publishable
// (ne-api `run_projection._failure_reason`); the exception class is
// deliberately NOT served, because an id is not prose for a person. It renders
// here and it outranks every generic line — and where the Run published none,
// the field says that is what happened rather than letting this console write a
// cause on the server's behalf. Deleting the old `RunReceiptCard` was fine;
// losing this with it would have silently restored the exact defect the wire
// was built to end.
//
// IT IS NO LONGER RENDERED RAW, and that correction matters most on THIS pane.
// `failure_reason` is safe to publish, which is not the same as being written
// for a reader: parts of it are the validators' own closed vocabulary
// ("Attempts: no-span,no-span. [aw=154 sources=2 maxrun=1 floor=5]"), and one
// whole class of it is the runtime's floor sentence, which names a Python
// exception class. This is the pane a presenter opens to show that the system
// governs itself, so it was the worst place in the console to print a stack of
// diagnostics as the answer. The field now states the reason in the same words
// the Ask card uses (chat/statedReason.ts) and hands the EXACT server string to
// the same "what the server said" drawer (chat/TurnFailureReadout `ServerWords`).
// Nothing is dropped and nothing is softened — the raw sentence is one click
// away, which is where it always belonged and never was on this pane.
//
// EVERY FIELD IS SERVED OR SAYS IT IS NOT.
// The wire carries no consumed-step or consumed-tool-call counter, so there is
// no "2 of 12 steps" here: the budget field renders the agent's SERVED caps and
// says that is what they are. Inventing a consumption figure to fill the shape
// of the render would be the one thing this pane exists to make impossible.

import type { ReactNode } from "react";
import { Link } from "react-router";
import { DetailField, DetailFields } from "../primitives/DetailPane";
import { HashChip } from "../primitives/HashChip";
import { ABSENT, absoluteTime } from "../primitives/format";
import { runSettlementPresentation } from "../agent-harness/adapters/runWorkspace";
import { explainStatedReason } from "../chat/statedReason";
import { ServerWords } from "../chat/TurnFailureReadout";
import { ROW_ANCHOR_COPY_LABEL, ROW_ANCHOR_GLOSS, ROW_ANCHOR_LABEL } from "../transcript";
import type { VerifiedRowAnchor } from "./rowAnchors";
import { runCompareHref } from "./runCompareLink";
import { statedFailureReason } from "./runTurn";
import { strategyGloss, strategyWord } from "./StrategyBadge";
import type { AgentDetailView, ConversationSummary, RunDetail } from "./types";

/** The receipt's one hue-carrying badge, drawn only from the server's own
 * output verdict. Ordinary generation stays unbadged. */
export function OutputVerdictPill({ verdict }: { verdict: RunDetail["output_verdict"] }) {
  if (verdict === "verified") return <span className="ne-pill ne-pill--affirm">Verified</span>;
  if (verdict === "grounded") return <span className="ne-pill ne-pill--affirm">Grounded</span>;
  if (verdict === "refused") return <span className="ne-pill ne-pill--refuse">Refused</span>;
  return null;
}

/** Elapsed from the SERVER's own two stamps. The console owns no clock here:
 * an unsettled Run has no terminal stamp and gets the em dash. */
export function servedElapsed(run: RunDetail): string | null {
  if (run.terminal_at === null) return null;
  const started = Date.parse(run.created_at);
  const ended = Date.parse(run.terminal_at);
  if (Number.isNaN(started) || Number.isNaN(ended)) return null;
  return `${Math.max(0, Math.round((ended - started) / 100) / 10).toFixed(1)} s`;
}

/** The claims figure, which is FOUR different facts and only one of them is a
 * number. `mapping_pending` and `unavailable` are absences and render as one. */
export function claimsReadout(run: RunDetail): { value: string | null; note?: string } {
  if (run.claim_mapping_state === "mapping_pending")
    return { value: null, note: "mapping pending" };
  if (run.claim_mapping_state === "unavailable") return { value: null, note: "unavailable" };
  if (!Array.isArray(run.claims)) return { value: null, note: "unavailable" };
  const suffix = run.claim_mapping_state === "partial" ? " · mapping partial" : "";
  const truncated = run.claims_truncated ? " · truncated by the server" : "";
  return { value: `${run.claims.length}${suffix}${truncated}` };
}

function Absent({ note }: { note: string }) {
  return (
    <>
      <span className="ne-absent">{ABSENT}</span> {note}
    </>
  );
}

/** THE RETRY RULE — REFUSED IS NOT FAILED.
 *
 * A refusal is a COMPLETED outcome: the run finished, and the answer is "no,
 * with proof", server-authored and carrying its own receipt. Clicking again
 * cannot undo it, so a refusal offers no Retry — and because a refused run's
 * served status is `completed`, `completed` is excluded here explicitly rather
 * than by accident. A FAILURE is the absence of an outcome: the run ended
 * before a verdict was produced, so asking again is an ordinary next move and
 * Retry is offered.
 *
 * These are the server's own status words. The console classifies nothing. */
export const RETRYABLE_RUN_STATUSES: ReadonlySet<string> = new Set([
  "failed",
  "cancelled",
  "expired",
  "context_exhausted",
  "budget_exhausted",
]);

export function runOffersRetry(run: RunDetail): boolean {
  if (run.status === "completed") return false;
  return RETRYABLE_RUN_STATUSES.has(run.status);
}

/** The exact optimistic body the canonical retry endpoint accepts. */
export function runControlBody(run: RunDetail) {
  if (run.strategy === "unavailable") return null;
  return {
    expected_version: run.version,
    agent_version: run.agent_version,
    strategy: run.strategy,
    config_hash: run.config_hash,
    capability_snapshot_hash: run.capability_snapshot_hash,
  };
}

/**
 * WHAT THIS IS, AND WHY IT IS NOT CALLED A RECEIPT ON SCREEN.
 *
 * The owner, looking at the live thread: "what is a 'run receipt'?" The label
 * named the artefact by its internal shape and told a reader nothing about what
 * it is for. It carries the model that actually served the turn, the caps the
 * turn ran under, the capability and configuration hashes, and the exit into
 * the audit log — the thing you produce when someone asks why an answer should
 * be believed. So the control says what opening it gets you, and the frame line
 * inside says what it is for. A tooltip glossing "receipt" would have left the
 * label wrong and added a second thing to read.
 *
 * The identifiers underneath are untouched: every field is still the server's
 * own word, and nothing here softens a governance fact to read more kindly.
 */
export const RUN_RECEIPT_LABEL = "How this turn ran";
export const RUN_RECEIPT_FRAME =
  "What served this turn, under what limits, and where its audit trail goes — the record that makes an answer defensible, not merely plausible.";
/** The noun for this pane while it is loading or failed to load. */
export const RUN_RECEIPT_THING = "this turn's record";

/**
 * THE REQUESTED MODEL AND THE SERVED MODEL, WHEN THEY ARE NOT THE SAME.
 *
 * The pane listed both as bare adjacent fields, so a reader who picked one
 * model in the composer and read another here had nothing on the screen
 * reconciling them — the silent degrade. It is a real event when they differ
 * (a provider rejecting every call and the gateway falling down its candidate
 * chain is one way it happens) and the reader is entitled to see that one did.
 *
 * The cause IS on this wire now, and `RUN_MODEL_FELL_BACK` below reads it.
 * `RunView` (ne-api/runs_wire.py) carries `route_position` / `fallback_from`
 * alongside `requested_model`, `requested_model_lane`, `served_model` and
 * `model_request_mode`. An earlier version of this comment said that routing
 * provenance was served "on the TRACE wire only" and sent the reader there;
 * that stopped being true when the fields landed on `RunDetail`, and this pane
 * went on not reading them.
 */
export const RUN_MODEL_DIVERGED = "The request named a different model than the one that served.";

/**
 * AN INVOLUNTARY PROVIDER FALLBACK — the lane moved under an unchanged policy.
 *
 * SEPARATE FROM `RUN_MODEL_DIVERGED`, and it has to be, because `modelDiverged`
 * CANNOT SEE THIS CASE AT ALL. That check needs a `requested_model`, and
 * `run_store` enforces `(request_mode == "auto") == (requested_model is null)`
 * — so for every task-routed request (`reason`, `triage`, `grounding-assist`,
 * the pinned Agent policies) the requested half is null by construction and the
 * divergence field never rendered, fallback or not.
 *
 * That blindness matters more since NE-DEMO-NO-CPU-FALLBACK: a private-primary
 * task now falls back to `frontier-default`, so an involuntary fallback is the
 * difference between a run served on the sovereign lane and one that used the
 * frontier API. A reader who opens a run from an audit entry must not be able
 * to conclude it stayed private when it did not. The chat footer and the trace
 * view already said so; this pane did not.
 */
export const RUN_MODEL_FELL_BACK =
  "The model that should have served did not, and a fallback answered — the provider chain moved under an unchanged policy.";

/** Names the passed-over candidates when the record carries them. */
export function runModelFellBackFrom(passedOver: string): string {
  return `${passedOver} did not serve, and a fallback answered — the provider chain moved under an unchanged policy.`;
}

/** Both models are known AND they are not the same one. An absent half is not
 * a divergence: it is an absence, and the field beside it already says so. */
export function modelDiverged(run: RunDetail): boolean {
  const requested = run.requested_model ?? run.requested_model_lane;
  return requested !== null && run.served_model !== null && requested !== run.served_model;
}

/** The pane's title, so the docked frame and the standalone route name the Run
 * identically. `display_label` ALREADY opens with "Run {sequence}" (ne-api
 * run_projection.run_view), so prefixing it again produced titles reading
 * "Run 7 · Run 7 · agentic · 14:22". The server's label is the whole name. */
export function runReceiptTitle(run: RunDetail): string {
  return run.display_label;
}

export function RunReceiptFields({
  run,
  agent,
  conversation,
  rowAnchors,
}: {
  run: RunDetail;
  /** The Agent as served. `null` while its read has not landed — the budget
   * field then says so rather than showing a plausible cap. */
  agent: AgentDetailView | null;
  conversation: ConversationSummary | null;
  /**
   * THE SERVED ROW ANCHORS — each verified row the answer's claims are
   * associated with, read off the Run's evidence envelope by the caller
   * (agents/rowAnchors owns the rule). Absent or empty draws NO field: a Run
   * whose evidence carries no verified rows has no row identity to state, and
   * an em-dash line on every agentic turn would be noise, not an absence
   * worth stating — the evidence drawer still accounts for every record.
   */
  rowAnchors?: readonly VerifiedRowAnchor[] | null;
}) {
  // The SETTLEMENT, not the status word alone: a Run that completed while
  // serving nothing closes as "Completed without output", in the fault tone,
  // because closing it as "Completed" tells the reader it worked.
  const presentation = runSettlementPresentation(run);
  const elapsed = servedElapsed(run);
  const claims = claimsReadout(run);
  const stated = statedFailureReason(run);
  // Translated once, here, so the stated line and its verbatim original are the
  // same object and cannot fall out of step. `technical` is `null` exactly when
  // the server's sentence needed no rewriting — there is then nothing to
  // disclose twice, and no drawer is drawn over a line already verbatim.
  const explained = stated === null ? null : explainStatedReason(stated);
  const settled = run.terminal_at !== null || run.status !== "running";
  return (
    <>
      <p className="ne-detail__lead">{RUN_RECEIPT_FRAME}</p>
      <DetailFields>
        <DetailField label="Status">
          <span className={presentation.tone} aria-hidden="true">
            {presentation.glyph}
          </span>{" "}
          {presentation.word}
          {elapsed === null ? "" : ` · ${elapsed}`}
        </DetailField>
        <DetailField label="Strategy">
          {run.strategy === "unavailable" ? (
            <Absent note="the server served no strategy for this Run" />
          ) : (
            `${strategyWord(run.strategy)} — ${strategyGloss(run.strategy)}`
          )}
        </DetailField>
        {run.status === "completed" && explained === null ? null : (
          <DetailField label="Reason">
            {explained === null ? (
              <Absent
                note={settled ? "the Run published no marked reason" : "the Run has not settled"}
              />
            ) : (
              <>
                {explained.sentence}
                {explained.technical === null ? null : (
                  <ServerWords details={[explained.technical]} />
                )}
              </>
            )}
          </DetailField>
        )}
        <DetailField label="Output verdict">
          {run.output_verdict === null || run.output_verdict === "unavailable" ? (
            <Absent note="no output verdict was served" />
          ) : (
            <OutputVerdictPill verdict={run.output_verdict} />
          )}
        </DetailField>
        <DetailField label="Evidence assurance">
          {claims.value === null ? (
            <Absent note={claims.note ?? "unavailable"} />
          ) : (
            <>{claims.value} claims · each carries the server’s own association and receipt</>
          )}
        </DetailField>
        <DetailField label="Model" mono>
          {run.served_model ?? <Absent note="no model reply was served" />}
        </DetailField>
        <DetailField label="Requested model" mono>
          {run.requested_model ?? run.requested_model_lane ?? (
            <Absent note={`request mode ${run.model_request_mode}`} />
          )}
        </DetailField>
        {run.route_position === "fallback" ? (
          <DetailField label="Model routing">
            {run.fallback_from === null
              ? RUN_MODEL_FELL_BACK
              : runModelFellBackFrom(run.fallback_from)}
          </DetailField>
        ) : modelDiverged(run) ? (
          <DetailField label="Model routing">{RUN_MODEL_DIVERGED}</DetailField>
        ) : null}
        {/* "Caps", not "Budget": the value here is what the Run was ALLOWED,
         * and the wire carries no consumption counter to spend it against, so
         * a reader who takes "Budget" to mean "budget used" is reading a
         * number that does not exist. The composer's own footer already says
         * "Caps · n steps · n tool calls" — same fact, same word. */}
        <DetailField label="Caps">
          {agent === null ? (
            <Absent note="the Agent’s served caps have not loaded" />
          ) : (
            `${agent.budgets.max_steps} steps · ${agent.budgets.max_tool_calls} tool calls · ${agent.budgets.max_input_tokens.toLocaleString("en-US")} in · ${agent.budgets.max_output_tokens.toLocaleString("en-US")} out — the served caps; the wire carries no consumption counter`
          )}
        </DetailField>
        <DetailField label="Started" mono>
          {absoluteTime(run.created_at)}
        </DetailField>
        <DetailField label="Conversation" mono>
          {conversation === null ? run.conversation_id : conversation.title}
        </DetailField>
        <DetailField label="Sequence">
          Run {run.sequence} · retry {run.retry_ordinal} · run version {run.version}
        </DetailField>
        {/* THE ROW'S IDENTITY, ON THE RECEIPT ITSELF. The served canonical
         * hash of each verified row this answer rests on — what a presenter
         * points at and an auditor compares across runs. Served, never
         * recomputed; drawn only when the envelope actually carried anchors. */}
        {rowAnchors && rowAnchors.length > 0 ? (
          <DetailField label={ROW_ANCHOR_LABEL}>
            <span className="ag-rowanchor" title={ROW_ANCHOR_GLOSS}>
              {rowAnchors.map((row) => (
                <HashChip key={row.evidenceId} hash={row.anchor} label={ROW_ANCHOR_COPY_LABEL} />
              ))}
              {rowAnchors.length > 1 ? (
                <span className="ag-rowanchor__count">{rowAnchors.length} rows</span>
              ) : null}
            </span>
          </DetailField>
        ) : null}
        <DetailField label="Capability set">
          <HashChip hash={run.capability_snapshot_hash} label="Copy capability snapshot hash" />
        </DetailField>
        <DetailField label="Configuration">
          <HashChip hash={run.config_hash} label="Copy configuration hash" />
        </DetailField>
      </DetailFields>
    </>
  );
}

/** The receipt's exits. The trace correlation is a real one the server served;
 * the Audit log link carries no `?seq=` because a Run projection does not serve
 * an audit sequence, and a link that pretended to would land on the wrong row.
 *
 * THE WAY TO THE RUN RECORD, WHICH THE DOCK DOES NOT CARRY.
 *
 * The redesign took the receipt off its own route deliberately — reading one
 * cost the whole context of the thread. But the receipt is not everything the
 * server holds about a Run: the evidence drawer, and with it "Served document
 * versions", is mounted on `/agents/:id/runs/:runId` and NOWHERE else. A reader
 * who opened "How this turn ran" to find which version of a document was served
 * had no click that got there.
 *
 * So the docked frame — and only the docked frame — offers one. The standalone
 * page IS the run record, so the link is absent there rather than pointing at
 * the screen it is already on; `onOpenRun` is the same docked-vs-standalone
 * discriminator the predecessor exit below already turns on. This is the one
 * exit that leaves the thread on purpose, and its label says so. */
export function RunReceiptExits({
  agentId,
  run,
  extra,
  onOpenRun,
}: {
  agentId: string;
  run: RunDetail;
  extra?: ReactNode;
  /** Re-point THIS pane at another Run, where the frame has a dock to re-fill.
   * The standalone page has none — there the predecessor is a page to go to,
   * which is what the deep-link route is for — so this is absent there and the
   * exit stays a link. Docked, a link would have thrown the reader onto the
   * page the redesign took them off. */
  onOpenRun?: (runId: string) => void;
}) {
  return (
    <>
      <Link className="ne-btn" to={`/trace/${encodeURIComponent(run.run_id)}`}>
        Open trace
      </Link>
      <Link className="ne-btn" to="/audit">
        Open in Audit log
      </Link>
      {onOpenRun === undefined ? null : (
        <Link
          className="ne-btn"
          to={`/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(run.run_id)}`}
        >
          Open run record
        </Link>
      )}
      {/* THE WAY INTO THE SIDE-BY-SIDE. Carries THIS Run as the left side;
       * the compare view asks for the other one. Any Run of any Agent — the
       * href is built from the served ids on this receipt and nothing else. */}
      <Link className="ne-btn ne-btn--quiet" to={runCompareHref({ agentId, runId: run.run_id })}>
        Compare with another Run
      </Link>
      {run.predecessor_run_id === null ? null : onOpenRun === undefined ? (
        <Link
          className="ne-btn ne-btn--quiet"
          to={`/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(run.predecessor_run_id)}`}
        >
          Predecessor Run
        </Link>
      ) : (
        <button
          type="button"
          className="ne-btn ne-btn--quiet"
          onClick={() => onOpenRun(run.predecessor_run_id!)}
        >
          Predecessor Run
        </button>
      )}
      {extra}
    </>
  );
}
