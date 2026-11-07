// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §5 — ONE GOVERNED TOOL CALL, AS ONE ROW OF THE TRANSCRIPT.
//
// This is Ask's half of the transcript contract: it turns one `ToolTransitEvent`
// off the wire into one `TranscriptStep`, and stops there. Where that step is
// DRAWN is not decided here — transcript/promotion.ts decides that from the
// verdict alone, so a routine allowed call demotes into the folded work band and
// anything pending a human or refused promotes out of it into a gate card.
//
// EVERY VALUE IS THE SERVER'S. `tier` and `audit_seq` are null when the server
// could not resolve one and render the em dash — never a zero, never a guess.
// The console decides no verdict here and has no code path that could.
//
// ONE THING THE WIRE DOES NOT CARRY, so one thing this never invents:
//   * a DURATION. `ToolTransitEvent` has no elapsed field, so `durationSeconds`
//     is null on every step and the transcript draws no time slot. Timing the
//     round trip in this browser would measure the console, not the tool.
//
// THE REASON IS THE SERVER'S SENTENCE, PASSED THROUGH UNTOUCHED. `reason` is the
// policy bundle's own words about the verdict, read off the audit record the PDP
// wrote. It goes into `sentence` byte-for-byte: no rewording, no prefix, no
// sentence case, no fallback. The server serves one only where a decision
// genuinely produced one, so where it is absent the gate card keeps saying the
// server sent none — writing a plausible sentence here would put console prose in
// the one slot the design reserves for the server's own words.
//
// THE DECISION LINK IS SHOWN ONLY WHEN IT RESOLVES. "View decision" needs an
// audit sequence AND a key the server has said holds operations; without both it
// is absent rather than present-and-broken. That is legibility, not
// authorization: the audit route runs its own check and refuses on its own.
//
// EXPIRY, ESCALATION AND INTEGRITY FAILURE ARE NOT A HUMAN REJECTION (api/
// types.ts states this as a wire rule). Each keeps its own word here, and the
// two that still await a person keep the verdict that leaves them outside the
// fold.

import { Link } from "react-router";
import type { ToolTransitEvent } from "../api/types";
import type { TranscriptStep, TranscriptStepVerdict } from "../transcript";
import type { ToolTransitEntry } from "./useAgentStream";
import {
  TOOL_ALLOWED,
  TOOL_APPROVAL_ESCALATED,
  TOOL_APPROVAL_EXPIRED,
  TOOL_APPROVAL_FAILED,
  TOOL_APPROVAL_INTEGRITY_FAILED,
  TOOL_APPROVAL_REJECTED,
  TOOL_APPROVAL_EXECUTED,
  TOOL_AUDIT_REFERENCE,
  TOOL_APPROVED_ACTION_FAILED,
  TOOL_AWAITING_APPROVAL,
  TOOL_REFUSED,
  TOOL_REVIEW_IN_APPROVALS,
  TOOL_VIEW_DECISION,
} from "./askCopy";

/**
 * The verdict and the word for each server decision.
 *
 *   allow            routine work. Demotes into the band.
 *   pending          a person owes a decision. Promotes.
 *   escalated        a DIFFERENT person now owes it. Still pending, still
 *                    promoted — an escalation that folded away would read as
 *                    resolved.
 *   deny / rejected  refused, by the policy decision point or by a person.
 *   expired          the decision window closed with nobody deciding, so the
 *                    call did not run and nobody is coming. Refused, promoted.
 *   integrity-failed / approval-failed
 *                    the gate would not honour the approval, so the governed
 *                    action never executed. Refused, promoted: the outcome is
 *                    the same as a refusal and the reader must see it.
 *   failed           the action WAS approved and then fell over while running.
 *                    That is a fault, not a verdict, and faults stay in the band
 *                    (transcript/promotion.ts explains why at length).
 */
const DECISION: Record<
  ToolTransitEvent["decision"],
  { verdict: TranscriptStepVerdict; word: string }
> = {
  allow: { verdict: "allowed", word: TOOL_ALLOWED },
  pending: { verdict: "pending", word: TOOL_AWAITING_APPROVAL },
  escalated: { verdict: "pending", word: TOOL_APPROVAL_ESCALATED },
  deny: { verdict: "refused", word: TOOL_REFUSED },
  rejected: { verdict: "refused", word: TOOL_APPROVAL_REJECTED },
  expired: { verdict: "refused", word: TOOL_APPROVAL_EXPIRED },
  "integrity-failed": { verdict: "refused", word: TOOL_APPROVAL_INTEGRITY_FAILED },
  "approval-failed": { verdict: "refused", word: TOOL_APPROVAL_FAILED },
  failed: { verdict: "failed", word: TOOL_APPROVED_ACTION_FAILED },
};

/** One governed transit as one transcript step. Pure apart from the two links,
 * which are elements, not navigations. */
export function toTranscriptStep(entry: ToolTransitEntry, opsReadable: boolean): TranscriptStep {
  const { event } = entry;
  const { verdict, word } = DECISION[event.decision];
  // An allowed call that consumed an approval is not an ordinary allow: it is
  // the resumption of a parked action, and the server's own word for that is
  // what the reader needs to see.
  const label = verdict === "allowed" && event.approval_id ? TOOL_APPROVAL_EXECUTED : word;
  return {
    key: entry.key,
    name: event.name,
    verdict,
    verdictLabel: label,
    stateLabel: label,
    tier: event.tier,
    // The approval this transit belongs to, in the row's aux slot. A promoted
    // gate draws the same id in its own reference slot and ignores this one;
    // a band row has nowhere else to say WHICH parked request it resumed.
    intent: event.approval_id ?? null,
    // The wire measures no step duration. See the header.
    durationSeconds: null,
    // The server's own sentence about the decision, verbatim. `?? null` folds an
    // omitted key and an explicit null into the ONE absent value the gate card
    // draws its note for — it is not a substitution, and there is no branch here
    // that could produce a sentence the server did not send.
    sentence: event.reason ?? null,
    // The governed reference: the approval request where there is one, and
    // otherwise the audit sequence for this decision — which is the identifier
    // the "View decision" link resolves against. A policy-decision-point deny
    // carries no approval id by design (api/types.ts), so without this every
    // refused gate would report a reference the wire never had one for.
    reference: reference(event),
    action: gateAction(event, opsReadable),
  };
}

/** The identifier a promoted gate names, labelled for what it is. Neither part
 * is constructed: both are values the server put on the transit. */
function reference(event: ToolTransitEvent): string | null {
  if (event.approval_id) return event.approval_id;
  if (event.audit_seq === null) return null;
  return `${TOOL_AUDIT_REFERENCE} ${event.audit_seq}`;
}

/** The one control a promoted gate may carry — and only when it resolves. */
function gateAction(event: ToolTransitEvent, opsReadable: boolean) {
  if (event.decision === "pending" || event.decision === "escalated") {
    if (!event.approval_id) return undefined;
    return (
      <Link className="ne-ask__gatelink" to={`/approvals?request=${event.approval_id}`}>
        {TOOL_REVIEW_IN_APPROVALS}
      </Link>
    );
  }
  if (DECISION[event.decision].verdict !== "refused") return undefined;
  if (event.audit_seq === null || !opsReadable) return undefined;
  return (
    <Link className="ne-ask__gatelink" to={`/audit?seq=${event.audit_seq}`}>
      {TOOL_VIEW_DECISION}
    </Link>
  );
}
