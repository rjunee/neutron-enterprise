// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// WHAT THE RECENT CHAIN SAYS ABOUT A DECISION — exact terminal identity only.
//
// Receipt linkage now uses a bounded server lookup instead of this newest-page
// helper. This matcher remains for recent-window presentation/dedup and obeys
// the same identity law: a terminal action AND exact hash-covered
// provenance.approval_id. A later payload read/tool execution is not a decision.
//
// A key that cannot read the chain gets NO seq line at all — not a line saying
// it cannot have one. The receipt itself is the record that key holds.

import type { AuditRecordView } from "../api/types";

/** The gate's terminal human-verdict actions — the real emitter vocabulary. */
const VERDICT_ACTIONS = new Set(["approval.granted", "approval.rejected"]);

function approvalId(event: AuditRecordView): string | null {
  const provenance = event.payload.provenance;
  if (typeof provenance !== "object" || provenance === null || Array.isArray(provenance)) {
    return null;
  }
  const value = (provenance as Record<string, unknown>).approval_id;
  if (typeof value === "string") return value;
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const wrapped = (value as Record<string, unknown>).value;
  return typeof wrapped === "string" ? wrapped : null;
}

/** Does this record state a terminal verdict for this exact request id? */
export function namesRequest(event: AuditRecordView, requestId: string): boolean {
  if (requestId === "") return false;
  return VERDICT_ACTIONS.has((event.action ?? "").toLowerCase()) && approvalId(event) === requestId;
}

/** The chain sequence for a just-submitted decision, or null while unresolved. */
export function seqForRequest(
  events: readonly AuditRecordView[],
  requestId: string,
): number | null {
  for (const e of events) {
    if (namesRequest(e, requestId)) return e.seq;
  }
  return null;
}

/**
 * Approval decisions in the recent chain window (§8's Decided section, ops keys).
 *
 * Anchored to the verdict records themselves. The previous classifier-based
 * filter admitted the `api.approval.submitted` API echo (whose outcome
 * `success` states no verdict — it rendered a blank Decision cell, and a
 * second row per denial) and DROPPED `approval.granted`, which its regex did
 * not name — an approve decided elsewhere rendered as a verdict-less row.
 * `approval.expired` is excluded deliberately: it is the system's verdict, not
 * a person's, and this table has no vocabulary for it yet.
 */
export function chainDecisions(events: readonly AuditRecordView[]): AuditRecordView[] {
  return events.filter((e) => VERDICT_ACTIONS.has((e.action ?? "").toLowerCase()));
}

/** The §2.3 word for a chain verdict record — read from the served action name,
 * never composed from outcome sniffing. Null for anything that states none, in
 * which case the cell renders the absent mark rather than a guessed verdict. */
export function decisionWord(action: string | null | undefined): "APPROVED" | "DENIED" | null {
  const value = (action ?? "").toLowerCase();
  if (value === "approval.granted") return "APPROVED";
  if (value === "approval.rejected") return "DENIED";
  return null;
}
