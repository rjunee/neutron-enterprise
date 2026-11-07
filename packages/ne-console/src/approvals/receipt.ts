// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8 — "Copy receipt" copies request id, action, tier, verdict, approver and
// decided-at as plain text lines.
//
// Six lines, no preamble, no essay about what the block is not. Every value is
// either the request the approver just decided or the gate's own answer to that
// decision; a field the server did not return renders as the console's one
// absent mark rather than as a guess or an omission the reader cannot see.

import type { ApprovalActionResult, PendingApprovalView } from "../api/types";
import { ABSENT, absoluteTime } from "../primitives/format";

/** The §2.3 word for a server verdict. The pill and the receipt say the same
 * thing, from one mapping, so a copied receipt cannot disagree with the screen. */
export function verdictWord(
  status: ApprovalActionResult["status"],
): "APPROVED" | "DENIED" | "AWAITING SECOND" | "EXPIRED" {
  switch (status) {
    case "approved":
      return "APPROVED";
    case "rejected":
      return "DENIED";
    case "pending_second":
      return "AWAITING SECOND";
    case "expired":
      return "EXPIRED";
  }
}

/** Build the copyable receipt. Pure + exported for its own test. */
export function buildReceipt(approval: PendingApprovalView, result: ApprovalActionResult): string {
  return [
    `Request id: ${approval.request_id}`,
    `Action: ${approval.action}`,
    `Tier: ${approval.tier}`,
    `Verdict: ${verdictWord(result.status)}`,
    `Approver: ${result.approver ?? ABSENT}`,
    `Decided at: ${result.decided_at ? absoluteTime(result.decided_at) : ABSENT}`,
  ].join("\n");
}
