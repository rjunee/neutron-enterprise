// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4 — "WAITING ON YOU", as pure functions.
//
// Home merges two queues that the server grants separately: pending approvals
// (the approver surface) and open proposals (the steward surface). The merge,
// the urgency order, the eight-row cap and the status word are the part worth
// testing, so they live here — no DOM, no provider, no fixture.
//
// THE ORDER IS THE SPEC'S: approvals by `sla_deadline` ascending, then proposals
// by `created_at` ascending. It is a COMPOSITE order, not a column sort: a
// proposal has no deadline, so sorting the merged list by one column could never
// express it (nulls last in both directions would park every proposal at the
// bottom in served order, which is not "oldest first"). So the merge is what the
// table receives, and <DataTable> is handed no default sort — the served order
// IS the default, and every column stays sortable on top of it.
//
// THIN CLIENT (C7). Nothing here decides anything: it reorders rows the server
// sent and reads two timestamps against the wall clock, which is what a deadline
// and an age are.

import { sourceDisclosure } from "../api/client";
import type {
  PendingApprovalView,
  ServedStewardProposalView,
  StewardProposalView,
} from "../api/types";
import type { StateWord } from "../primitives/StatePill";

/** Which queue a row came from — the §4 "Type" column. */
export type QueueKind = "approval" | "review";

/** One row of the merged attention list. */
export interface QueueRow {
  /** Stable identity: the request id or the proposal id. */
  key: string;
  kind: QueueKind;
  /** "Approval" / "Review" — the Type cell. */
  typeLabel: "Approval" | "Review";
  /** The Item cell: an approval's `action`, a proposal's `artifact.title`, or
   * the literal withheld label for a proposal the server did not disclose. */
  item: string;
  /** The From cell: an approval's requester, a proposal's lane. Empty for an
   * undisclosed proposal, which HAS no lane — the cell renders the em dash and
   * there is no code path here that could supply one. */
  from: string;
  /** The Age cell's instant: `requested_at` / `created_at`. */
  at: string;
  /** The Due cell's instant: `sla_deadline`; absent on proposals. */
  due: string | null;
  /** The gate's own escalation flag; always false for a proposal. */
  escalated: boolean;
  /** Where a row click goes: the OWNING screen, with this row's detail open. */
  to: string;
}

/** §4: "Max 8 rows". */
export const HOME_QUEUE_LIMIT = 8;

function approvalRow(a: PendingApprovalView): QueueRow {
  return {
    key: a.request_id,
    kind: "approval",
    typeLabel: "Approval",
    item: a.action,
    from: a.principal_id,
    at: a.requested_at,
    due: a.sla_deadline,
    escalated: a.escalated,
    to: `/approvals?request=${encodeURIComponent(a.request_id)}`,
  };
}

/**
 * 015A0-ENFORCEMENT: Home reads the same disclosure status the steward queue
 * does, through the same canonical reader.
 *
 * A proposal the server did not state as available carries no `artifact` and no
 * `lane`, so this row is built WITHOUT dereferencing either: the Item cell is
 * the fixed withheld label and the From cell is empty. Home's queue offers no
 * action and no copy on any row — a row click opens the owning screen, which
 * applies its own shell — so the withheld row is a link to a withheld pane and
 * nothing more.
 *
 * The three fields it does keep — id, safe status time, and the deep link built
 * from the opaque id — are exactly the withheld branch's own fields.
 */
function proposalRow(p: ServedStewardProposalView): QueueRow {
  const disclosure = sourceDisclosure(p);
  const disclosed = disclosure.kind === "available" ? (p as StewardProposalView) : null;
  return {
    key: p.proposal_id,
    kind: "review",
    typeLabel: "Review",
    item: disclosed === null ? PROPOSAL_WITHHELD_LABEL : disclosed.artifact.title,
    from: disclosed === null ? "" : disclosed.lane,
    at: p.created_at,
    // A proposal carries no deadline. The absence renders as the em dash, and
    // there is no code path here that could supply one.
    due: null,
    escalated: false,
    to: `/improvement?proposal=${encodeURIComponent(p.proposal_id)}`,
  };
}

/** The literal Home shows where an undisclosed proposal's title would be. It is
 * the same words the steward queue uses, so one row cannot read as two things
 * on two screens. */
export const PROPOSAL_WITHHELD_LABEL = "Content withheld";

function byInstantAsc(field: (row: QueueRow) => string | null) {
  return (x: QueueRow, y: QueueRow): number => {
    const a = field(x);
    const b = field(y);
    if (a === null || b === null) return 0;
    return Date.parse(a) - Date.parse(b);
  };
}

/**
 * The merged, urgency-ordered attention list (§4): approvals by `sla_deadline`
 * ascending first, then proposals by `created_at` ascending.
 *
 * Both sorts are stable, so rows with the same instant keep the order the server
 * sent them in. Either list may be empty — a queue this key was not granted
 * simply contributes nothing, which is why a refused surface can never put a
 * zero, a placeholder or a row on this screen.
 */
export function mergeQueue(
  approvals: readonly PendingApprovalView[],
  proposals: readonly ServedStewardProposalView[],
): QueueRow[] {
  const decisions = approvals.map(approvalRow).sort(byInstantAsc((r) => r.due));
  const reviews = proposals.map(proposalRow).sort(byInstantAsc((r) => r.at));
  return [...decisions, ...reviews];
}

/** What the truncated tail holds, per owning screen — the §4 footer links. */
export interface QueueOverflow {
  approvals: number;
  reviews: number;
}

/** The rows beyond the cap, counted by the screen that owns them. */
export function overflowCounts(
  rows: readonly QueueRow[],
  limit: number = HOME_QUEUE_LIMIT,
): QueueOverflow {
  const hidden = rows.slice(limit);
  return {
    approvals: hidden.filter((r) => r.kind === "approval").length,
    reviews: hidden.filter((r) => r.kind === "review").length,
  };
}

/** The §2.3 words this table may render. */
export type QueueStatusWord = Extract<StateWord, "PENDING" | "OVERDUE" | "ESCALATED">;

/**
 * The Status cell.
 *
 * OVERDUE OUTRANKS ESCALATED when a row is both. Escalation says how the item
 * arrived; a breached deadline says what is true about it now, and the column is
 * one word wide. The escalation is not lost — it is why the row sorts where it
 * does and it is on the item's own card in Approvals.
 *
 * A proposal has no deadline and no escalation, so it is PENDING until the
 * steward decides it. The console never infers a review's urgency from a clock
 * the server did not set.
 */
export function queueStatusWord(row: QueueRow, now: number): QueueStatusWord {
  if (row.due !== null && Date.parse(row.due) <= now) return "OVERDUE";
  if (row.escalated) return "ESCALATED";
  return "PENDING";
}

/** Sort rank for the Status column: the most urgent word sorts first ascending. */
export function statusRank(row: QueueRow, now: number): number {
  const word = queueStatusWord(row, now);
  return word === "OVERDUE" ? 0 : word === "ESCALATED" ? 1 : 2;
}
