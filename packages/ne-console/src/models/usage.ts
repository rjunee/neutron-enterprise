// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 USAGE TAB — the cost rollup, as pure functions over served fields.
//
// TWO HONESTY TRAPS LIVE IN THIS FILE.
//
//   1. A SELF-HOSTED LANE PRICES AT 0.0 AND IS NOT FREE. The rollup marks such
//      a lane `unpriced`, and its Spend cell reads UNPRICED — never "$0.00",
//      which is a claim that the run cost nothing. The same rule governs the
//      Spend summary: it sums PRICED calls only and says so out loud when any
//      lane is unpriced, rather than presenting a partial total as a total.
//   2. TOKENS ARE NEVER FILLED IN. A total the server cannot vouch for arrives
//      null and renders as the em dash. There is no code path here that turns
//      an absent count into a zero.
//
// PER-PRINCIPAL USAGE IS EXCLUDED, and its absence is the point: the cost
// records carry the gateway's service actor, not the calling principal, so a
// per-user table built from them would attribute every call to one identity.
// On the screen built to prove measurement, that is the one thing that must not
// be guessed. The tab states the gap in one line where the table would be.

import { formatMoney } from "../primitives/format";

// ===========================================================================
// The usage-rollup shapes (§18 B4), typed from the fields §18 names:
// "CostEvent rollup by model+lane: calls, input/output tokens, cost_usd,
// `unpriced: bool` per lane; totals".
// ===========================================================================

/** One model+lane bucket of the window. */
export interface ModelUsageRow {
  model_id: string;
  provider: string;
  lane: string;
  calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  /** The server's own verdict that this lane reports no market price. */
  unpriced: boolean;
}

/** The window's served totals. Nothing here is summed by this console. */
export interface ModelsUsageTotals {
  calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
}

/** `GET` of the usage rollup for one `YYYY-MM` window. */
export interface ModelsUsageView {
  window: string;
  models: ModelUsageRow[];
  totals: ModelsUsageTotals;
}

// ===========================================================================
// The window control
// ===========================================================================

/** `YYYY-MM` for an instant — the window key the rollup is addressed by. */
export function monthKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

/** "August 2026". */
export function monthLabel(key: string): string {
  const [year, month] = key.split("-");
  const y = Number(year);
  const m = Number(month);
  if (!Number.isFinite(y) || !Number.isFinite(m)) return key;
  return new Date(y, m - 1, 1).toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

/**
 * The month select's options: this month first, then the eleven before it.
 *
 * This is the control's INPUT RANGE, not a claim that a window holds data — a
 * window the deployment never ran renders the tab's own empty state. The
 * deployment's served period list is a separate backlog item; until it exists
 * the console offers a fixed year rather than inventing which months have data.
 */
export function monthOptions(now: Date, count = 12): Array<{ value: string; label: string }> {
  const out: Array<{ value: string; label: string }> = [];
  for (let i = 0; i < count; i += 1) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const key = monthKey(d);
    out.push({ value: key, label: monthLabel(key) });
  }
  return out;
}

// ===========================================================================
// The summary
// ===========================================================================

/** The `title` §10 puts on a partial spend total. */
export const UNPRICED_NOTE = "Self-hosted lanes report no market price";

/**
 * §10: "Spend sums priced calls only and, when any lane is unpriced, reads
 * `${total} + unpriced`".
 *
 * The total is the SERVER's (`totals.cost_usd`); this adds the disclosure, not
 * the arithmetic. When the server vouches for no total at all, the value is
 * UNPRICED on its own — "Unpriced + unpriced" would be an accounting of
 * nothing.
 */
export function spendSummary(view: ModelsUsageView): { text: string; note: string | null } {
  const anyUnpriced = view.models.some((row) => row.unpriced);
  const priced = formatMoney(view.totals.cost_usd);
  if (!anyUnpriced) return { text: priced, note: null };
  if (priced === "Unpriced") return { text: priced, note: UNPRICED_NOTE };
  return { text: `${priced} + unpriced`, note: UNPRICED_NOTE };
}

/**
 * The Spend column's sort key: an unpriced lane sorts as ABSENT.
 *
 * The table standard keeps absent values last in BOTH directions, which is
 * exactly §10's "Spend desc, unpriced rows last" — and it stays true when the
 * reader clicks the header to reverse it. An unpriced lane is not a cheap lane.
 */
export function spendSortValue(row: ModelUsageRow): number | null {
  return row.unpriced ? null : row.cost_usd;
}

/** "1 model" / "12 models" — the toolbar count for the usage window. */
export function usageCount(n: number): string {
  return `${n} ${n === 1 ? "model" : "models"}`;
}
