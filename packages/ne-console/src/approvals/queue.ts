// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8 — THE QUEUE'S RULES, as pure functions.
//
// They live apart from the screen for the same reason the table's sort rules do:
// the rules are the part worth testing, and they are testable here without a
// DOM, a provider or a fixture. The screen renders them; it does not restate
// them.
//
// THIN CLIENT (C7). Nothing here decides an approval's fate. Every predicate is
// a comparison between two SERVER-STATED instants (`sla_deadline`, `expires_at`)
// and the wall clock — which is what a deadline IS — or a read of a server flag
// (`escalated`). The gate still governs the decision; a row this file calls
// EXPIRED can still be refused, or accepted, by the server.

import type { PendingApprovalView } from "../api/types";
import { formatCount } from "../primitives/format";

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;

/** §8's Status column vocabulary, and nothing else. Drawn from §2.3's closed set. */
export type ApprovalStatusWord =
  "AWAITING SECOND" | "EXPIRED" | "OVERDUE" | "ESCALATED" | "PENDING";

export function deadlineMs(approval: PendingApprovalView): number {
  return Date.parse(approval.sla_deadline);
}

/** Past its SLA. The same comparison the rail badge's overdue count makes, so
 * the badge and this screen cannot disagree about what "overdue" means. */
export function isOverdue(approval: PendingApprovalView, now: number): boolean {
  const t = deadlineMs(approval);
  return Number.isFinite(t) && t <= now;
}

export function isExpired(approval: PendingApprovalView, now: number): boolean {
  const t = Date.parse(approval.expires_at);
  return Number.isFinite(t) && t <= now;
}

/**
 * The one pill a row shows.
 *
 * Priority, highest first: AWAITING SECOND (the server's own answer to a
 * decision this console submitted) · EXPIRED (no decision is possible) ·
 * OVERDUE · ESCALATED · PENDING.
 *
 * OVERDUE OUTRANKS ESCALATED deliberately. The toolbar carries an "Overdue only"
 * filter; a row that survives that filter while showing a pill that does not say
 * OVERDUE makes the filter look broken. Escalation is still visible — an
 * escalated row that is not yet past its deadline reads ESCALATED.
 */
export function statusWord(
  approval: PendingApprovalView,
  now: number,
  awaitingSecond = false,
): ApprovalStatusWord {
  if (awaitingSecond) return "AWAITING SECOND";
  if (isExpired(approval, now)) return "EXPIRED";
  if (isOverdue(approval, now)) return "OVERDUE";
  if (approval.escalated) return "ESCALATED";
  return "PENDING";
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** `mm:ss`, or `h:mm:ss` past an hour. Magnitude only — the sign is the caller's. */
export function clock(ms: number): string {
  const total = Math.floor(Math.abs(ms) / SECOND);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/**
 * A time or a date-and-time, locale-independently. Same day: the time alone,
 * which is what an operator reads. Any other day: the date travels with it, so
 * an expiry 24 hours out can never be misread as earlier than a deadline two
 * hours out. The unambiguous absolute form always rides in `title`.
 */
export function timeOrDate(iso: string, now: number): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const today = new Date(now);
  const sameDay =
    at.getFullYear() === today.getFullYear() &&
    at.getMonth() === today.getMonth() &&
    at.getDate() === today.getDate();
  const time = `${pad(at.getHours())}:${pad(at.getMinutes())}`;
  if (sameDay) return time;
  return `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${time}`;
}

/**
 * §8's Due cell: "countdown `mm:ss` under 1 h, else time/date".
 *
 * Past due inside the hour reads `−mm:ss`. The leading minus is not decoration:
 * the caution tint is a colour, and a cell whose only difference from "5 minutes
 * left" is a colour is a state encoded by colour alone.
 */
export function dueCell(iso: string, now: number): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return timeOrDate(iso, now);
  const delta = t - now;
  if (Math.abs(delta) < HOUR) return delta < 0 ? `−${clock(delta)}` : clock(delta);
  return timeOrDate(iso, now);
}

/** The distinct `principal_id` values in the fetched window, for the requester
 * select. Sorted, so the list does not reshuffle on every poll. */
export function requesters(rows: readonly PendingApprovalView[]): string[] {
  return [...new Set(rows.map((r) => r.principal_id))].sort((a, b) =>
    a.localeCompare(b, "en", { numeric: true, sensitivity: "base" }),
  );
}

export type TierFilter = "all" | "2" | "3";

export interface QueueFilters {
  tier: TierFilter;
  requester: string;
  overdueOnly: boolean;
}

export const NO_FILTERS: QueueFilters = { tier: "all", requester: "all", overdueOnly: false };

/** The filter controls, applied to the fetched window. Search-within is applied
 * separately by the table standard's own `filterRows` over the columns' text. */
export function applyFilters(
  rows: readonly PendingApprovalView[],
  filters: QueueFilters,
  now: number,
): readonly PendingApprovalView[] {
  return rows.filter((row) => {
    if (filters.tier !== "all" && String(row.tier) !== filters.tier) return false;
    if (filters.requester !== "all" && row.principal_id !== filters.requester) return false;
    if (filters.overdueOnly && !isOverdue(row, now)) return false;
    return true;
  });
}

/**
 * §8's live count: "{n} pending · {o} overdue" — the `· {o} overdue` part only
 * when o > 0. It describes the rendered rows, never the fetched ones.
 */
export function countText(pending: number, overdue: number): string {
  const head = `${formatCount(pending)} pending`;
  return overdue > 0 ? `${head} · ${formatCount(overdue)} overdue` : head;
}
