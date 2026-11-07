// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.7 — numbers, money, hashes, and the time grammar every table shares.
//
// ONE place, because the honesty traps live here:
//   * a cost the server reports as null OR as 0.0 for a self-hosted lane renders
//     "Unpriced" — never "$0.00", never blank. A zero price is a claim about the
//     world; an unpriced lane is the absence of one.
//   * an absent value renders the em dash and nothing else. There is no format
//     function in this file that can turn missing into a number.
// These are pure and exported so the rules are testable without a DOM.

/** The one em dash the whole console renders for an absent value. */
export const ABSENT = "—";

/**
 * Thousands are grouped from 10,000 up (§2.7). Below that, grouping costs a
 * glyph and buys nothing: "1,284" and "1284" compare identically in a tabular
 * column, and the separator is noise at four digits.
 */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  if (Math.abs(value) >= 10000) return value.toLocaleString("en-US");
  return String(value);
}

/** Always grouped, always two decimals — the money shape, given a real price. */
function money(value: number, currency: string): string {
  return value.toLocaleString("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * §2.7 / SPEC §11 honesty trap. `null` is unpriced. So is `0` on a lane the
 * server declares unpriced — a self-hosted lane emits no price, and rendering
 * its absence as $0.00 states that the run was free, which is a different and
 * false claim.
 */
export function formatMoney(
  value: number | null | undefined,
  options: { currency?: string; unpriced?: boolean } = {},
): string {
  const { currency = "USD", unpriced = false } = options;
  if (unpriced) return "Unpriced";
  if (value === null || value === undefined || !Number.isFinite(value)) return "Unpriced";
  if (value === 0) return "Unpriced";
  return money(value, currency);
}

/** Format an exact server-owned USD amount carried in integer cents.
 *
 * This is deliberately separate from `formatMoney`: zero recorded cents is a
 * real `$0.00` total, while a zero infrastructure price remains `Unpriced`.
 */
export function formatUsdCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return ABSENT;
  return money(cents / 100, "USD");
}

/** `1.2 s` above a second, `840 ms` below it (§2.7). */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return ABSENT;
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms)} ms`;
}

/** First 8 characters. The full value belongs in `title` and the clipboard —
 * see <HashChip>, which is the only thing that should call this. */
export function truncateHash(hash: string | null | undefined, chars = 8): string {
  if (!hash) return ABSENT;
  return hash.length <= chars ? hash : hash.slice(0, chars);
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * The relative form tables use: "just now", "14 min ago", "2 h ago", "3 d ago".
 * Future instants read "in 14 min" — the same units, the other direction, which
 * is what an SLA countdown needs.
 */
export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return ABSENT;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return ABSENT;
  const delta = now - t;
  const ahead = delta < 0;
  const abs = Math.abs(delta);
  let body: string;
  if (abs < MINUTE) body = ahead ? "in under a minute" : "just now";
  else if (abs < HOUR) body = `${Math.floor(abs / MINUTE)} min`;
  else if (abs < DAY) body = `${Math.floor(abs / HOUR)} h`;
  else body = `${Math.floor(abs / DAY)} d`;
  if (abs < MINUTE) return body;
  return ahead ? `in ${body}` : `${body} ago`;
}

/** The absolute, local, unambiguous form — for `title` and detail panes. */
export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return ABSENT;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return ABSENT;
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

/** A deadline, as a table cell: "in 42 min" while it holds, "overdue" past it. */
export function formatDeadline(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return ABSENT;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return ABSENT;
  if (t <= now) return "Overdue";
  return relativeTime(iso, now);
}
