// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §4 — "Recent activity": the last ten non-refusal chain records, newest first.
//
// ACCESS REFUSALS ARE EXCLUDED. They are reads the server declined; they decided
// nothing about an action, they remain on the chain, and the ledger is one click
// away. Ten of them would push every real decision off a screen that shows ten
// rows — which is precisely what the shipped console did, with the console's own
// 403 page loads burying the two governance events in the window.
//
// The order is applied to rows the server sent; nothing here re-verifies,
// re-hashes or re-decides anything (C7).

import type { AuditRecordView } from "../api/types";
import { withoutAccessRefusals } from "../audit/eventClass";

/** §4: the last ten rows of the chain, and no eleventh. */
export const RECENT_ACTIVITY_LIMIT = 10;

export function recentActivity(events: readonly AuditRecordView[]): AuditRecordView[] {
  return withoutAccessRefusals([...events])
    .sort((a, b) => b.seq - a.seq)
    .slice(0, RECENT_ACTIVITY_LIMIT);
}
