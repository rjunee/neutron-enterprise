// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE CONVERSATION-TURNS PAGE, DELETED INTO THE THREAD.
//
// `/agents/:id/conversations/:cid` was a whole route whose entire content was
// "the turns of this conversation" — the same thing the thread on the previous
// screen is. The owner's word for finding it was "confusing", and he was right:
// a reader who selects yesterday's conversation wants to READ IT, in the place
// conversations are read, not to be moved to a second screen that renders the
// same turns in a worse shape.
//
// So the selected conversation's canonical turns render IN the thread, in the
// transcript grammar, and the route is gone.
//
// WHAT A READ-BACK TURN CAN AND CANNOT SAY. `listConversationTurns` serves the
// principal's own `user_content` plus each turn's Run receipt. It does NOT
// serve the per-step ledger a live turn streams, so a read-back turn draws no
// work band rather than an invented one, and `user_content_state: unavailable`
// renders as an absence and not as an empty question. The receipt's "Run
// receipt" action opens the docked pane — in place, on this screen.

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  RUN_LIST_PREVIEW_CHARS,
  runPreviewProse,
  runSettlementReceiptOutcome,
  runLastUpdateFact,
  runModelAbsentNote,
  runOutputAbsentNote,
  runSettlementReceiptTone,
} from "../agent-harness/adapters/runWorkspace";
import { explainStatedReason } from "../chat/statedReason";
import { absoluteTime } from "../primitives/format";
import type { TranscriptFact, TranscriptTurn } from "../transcript";
import { OutputVerdictPill } from "./RunReceiptPane";
import type { AgentsInteriorClient, ConversationTurnPage } from "./types";
import type { ReadState } from "./useAgentsRead";

export const TURN_CONTENT_UNAVAILABLE = "This turn’s question is not available to read back.";

/** A read-back Run enriched by its own single-Run read, where the caller made
 * one. The detail read is the SAME Run said more fully — the list page cannot
 * spend a source reauthorization per row, the single-Run read can — so where a
 * detail is present it simply replaces the row. Structural, so the map accepts
 * the detail wire without this module depending on which caller fetched it. */
export type ReadBackRunDetails = ReadonlyMap<
  string,
  ConversationTurnPage["items"][number]["run"] & {
    output_state?: "available" | "withheld" | "unavailable";
    failure_reason?: string | null;
  }
>;

export function conversationTurns(
  page: ConversationTurnPage,
  renderReceiptAction: (runId: string) => ReactNode,
  details?: ReadBackRunDetails,
): TranscriptTurn[] {
  return page.items.map((turn) => {
    const detail = details?.get(turn.run.run_id);
    const run = detail ?? turn.run;
    const available = turn.user_content_state === "available" && turn.user_content !== null;
    const pill = <OutputVerdictPill verdict={run.output_verdict} />;
    const lastUpdate = runLastUpdateFact(run);
    const facts: TranscriptFact[] = [];
    // A turn that has not closed says when the server last touched it, so a
    // slow Run is distinguishable from a stalled one. Settled turns get
    // nothing here — the outcome word is already the account.
    if (lastUpdate) facts.push(lastUpdate);
    // THE SERVER'S STATED CAUSE, where the Run published one — said in plain
    // language (chat/statedReason.ts); the verbatim sentence stays on the Run
    // receipt this row's action opens. Present only when the detail read
    // supplied it: the list wire does not carry `failure_reason`, and nothing
    // is invented for it.
    const stated = typeof detail?.failure_reason === "string" ? detail.failure_reason.trim() : "";
    if (stated !== "") {
      facts.push({
        key: "reason",
        label: "Reason",
        value: explainStatedReason(stated).sentence,
      });
    }
    return {
      id: turn.turn_id,
      // The Run identity, so a thread that ALSO holds this turn's live copy can
      // reconcile the two into one rendered turn (transcript/types.ts `runId`).
      runId: run.run_id,
      question: {
        text: available ? turn.user_content! : TURN_CONTENT_UNAVAILABLE,
        meta: `Turn ${turn.turn_sequence} · ${absoluteTime(run.created_at)}`,
      },
      // No work band: the turns index serves no per-step ledger, and a band
      // drawn from nothing would claim the turn made no governed calls.
      steps: [],
      answer: {
        label: "Answer",
        // A LIST-wire preview that filled the server's whole budget is cut
        // mid-word; `runPreviewProse` ends it at the last word boundary with
        // an ellipsis. A DETAIL read's preview is the full text
        // (full_output=True) and is never marked truncated.
        body:
          run.output_preview === null
            ? ""
            : detail !== undefined
              ? run.output_preview
              : runPreviewProse(run.output_preview),
        // WHY THE SLOT IS EMPTY IS FOUR FACTS, and this used to hard-code the
        // fourth. A turn still in flight said "no output was served" — a
        // sentence about a finished turn — under a footer that had already
        // called it a failure. `runOutputAbsentNote` is the one rule; a fifth
        // copy of any of its sentences is how the surfaces drift apart.
        absentNote: run.output_preview === null ? runOutputAbsentNote(run) : undefined,
      },
      receipt: {
        outcome: runSettlementReceiptOutcome(run),
        tone: runSettlementReceiptTone(run),
        ...(pill ? { pill } : {}),
        ...(facts.length > 0 ? { facts } : {}),
        model: run.served_model,
        // A live turn has not FAILED to get a model reply; it has not got there
        // yet. Two different facts, and the settled one used to be drawn for
        // both — under a footer that had already called the turn a failure.
        modelNote: runModelAbsentNote(run) ?? undefined,
        actions: renderReceiptAction(run.run_id),
      },
    } satisfies TranscriptTurn;
  });
}

/** A Run whose read-back row cannot say what the reader needs, and whose
 * single-Run read can:
 *   * completed Verified/Grounded with a null list preview — the answer EXISTS
 *     (010F stored it source-protected; the bounded list read does not spend
 *     the reauthorization, the single-Run read does), so without the detail the
 *     thread would draw a green verdict beside an empty body;
 *   * a preview that filled the server's whole list budget — the detail read
 *     serves the full text instead of the cut. */
function runNeedsDetailRead(run: ConversationTurnPage["items"][number]["run"]): boolean {
  // A terminal Run that is NOT completed can carry a published, safe
  // `failure_reason` — detail-only on the wire. Without the read the thread
  // has a bare status word for a failure the server named precisely.
  if (
    run.status === "failed" ||
    run.status === "expired" ||
    run.status === "context_exhausted" ||
    run.status === "budget_exhausted"
  ) {
    return true;
  }
  if (run.status !== "completed") return false;
  const attested = run.output_verdict === "verified" || run.output_verdict === "grounded";
  if (attested && run.output_preview === null) return true;
  return run.output_preview !== null && run.output_preview.length >= RUN_LIST_PREVIEW_CHARS;
}

/** How soon a thread holding an UNSETTLED read-back turn asks again. The same
 * two timescales the Run poller draws (useRunCompletion): a working Run on a
 * machine's cadence, a Run parked on a human decision on a human's. */
export const TURNS_RECHECK_WORKING_MS = 4_000;
export const TURNS_RECHECK_PAUSED_MS = 20_000;

/** The reason a governed 404 is one neutral state — see useAgentsRead. */
function isUnavailable(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    (error as { status?: unknown }).status === 404
  );
}

/**
 * The selected conversation's turns, or `null` when nothing is selected.
 *
 * TWO OBLIGATIONS BEYOND THE PLAIN READ, both of them honesty obligations:
 *
 *   * IT DOES NOT FREEZE AN UNSETTLED TURN. This read used to happen once per
 *     selection, so a turn read back mid-Run said "Submitted · the Run has not
 *     finished" FOREVER — above an answer that had long since arrived. While
 *     any read-back Run is unsettled the read repeats on the poller's own two
 *     timescales, replacing the value in place (never blanking the thread),
 *     and stops the moment every turn is settled.
 *
 *   * IT READS THE ANSWER THE LIST CANNOT SAY. See `runNeedsDetailRead`: for
 *     those Runs the single-Run read is fetched and its fuller row replaces
 *     the list's. A detail read that fails leaves the list row standing — the
 *     absent-note rule (`runOutputAbsentNote`) then states the source-protected
 *     fact instead of "no output was served".
 */
export function useConversationTurns(
  client: AgentsInteriorClient & {
    getAgentRun(
      agentId: string,
      runId: string,
    ): Promise<ConversationTurnPage["items"][number]["run"]>;
  },
  agentId: string,
  conversationId: string | null,
  identityKey: string,
  renderReceiptAction: (runId: string) => ReactNode,
): ReadState<TranscriptTurn[] | null> {
  const key = `${identityKey}:${conversationId ?? ""}`;
  const [state, setState] = useState<{
    key: string;
    read: ReadState<TranscriptTurn[] | null>;
  }>({ key, read: { phase: "loading" } });

  useEffect(() => {
    let live = true;
    let timer: number | undefined;
    if (conversationId === null) {
      setState({ key, read: { phase: "ready", value: null } });
      return;
    }
    setState({ key, read: { phase: "loading" } });
    /** Whether the last successful read still held an unsettled Run — the only
     * fact a failed refresh has to schedule its retry from. */
    let lastUnsettled = false;
    let lastParkedOnly = false;
    const schedule = () => {
      if (!live || !lastUnsettled) return;
      timer = window.setTimeout(
        () => void load(),
        lastParkedOnly ? TURNS_RECHECK_PAUSED_MS : TURNS_RECHECK_WORKING_MS,
      );
    };
    const load = async () => {
      try {
        const page = await client.listConversationTurns(agentId, conversationId);
        if (!live) return;
        const details = new Map<string, ConversationTurnPage["items"][number]["run"]>();
        await Promise.all(
          page.items
            .filter((turn) => runNeedsDetailRead(turn.run))
            .map(async (turn) => {
              try {
                details.set(turn.run.run_id, await client.getAgentRun(agentId, turn.run.run_id));
              } catch {
                // The list row stands; the absent-note rule states the fact.
              }
            }),
        );
        if (!live) return;
        const statuses = page.items.map(
          (turn) => (details.get(turn.run.run_id) ?? turn.run).status,
        );
        // Two cadences, one honest card: a WORKING Run is re-read on a
        // machine's timescale, a thread whose only open turn is parked on a
        // human decision on a human's. Terminal-only threads schedule nothing.
        const working = statuses.some(
          (status) =>
            status === "submitted" ||
            status === "queued" ||
            status === "running" ||
            status === "cancelling",
        );
        const parked = statuses.some((status) => status === "waiting_approval");
        lastUnsettled = working || parked;
        lastParkedOnly = !working;
        setState({
          key,
          read: { phase: "ready", value: conversationTurns(page, renderReceiptAction, details) },
        });
        schedule();
      } catch (error: unknown) {
        if (!live) return;
        // A failed REFRESH keeps the thread it already has and tries again; a
        // failed FIRST read is the states screen. One transient 502 must not
        // blank a conversation the reader is looking at.
        setState((previous) => {
          if (previous.key === key && previous.read.phase === "ready") return previous;
          return { key, read: { phase: isUnavailable(error) ? "unavailable" : "error" } };
        });
        schedule();
      }
    };
    void load();
    return () => {
      live = false;
      if (timer !== undefined) clearTimeout(timer);
    };
    // `key` covers identityKey; renderReceiptAction is the workspace's stable callback.
  }, [agentId, client, conversationId, key, renderReceiptAction]);

  // A value read for another selection or identity never crosses over.
  return state.key === key ? state.read : { phase: "loading" };
}
