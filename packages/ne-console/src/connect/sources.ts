// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §9 — SOURCES, the rules behind the screen, as pure functions.
//
// They live apart from the component because THE RULES ARE THE PART WORTH
// TESTING: which kinds the filter offers, which rows a kind selects, what a
// served age means as an instant, and the exact heading the quarantine section
// carries when its store could not be read.
//
// TWO FRESHNESS FACTS, NEVER ONE (SPEC §12 item 9). The server serves when the
// sync cycle last RAN (`last_checked_age_s` — advances every cycle, found
// something or not) and how old the newest RECORD seen is (`cursor_age_s` —
// advances only when a record lands). The screen this replaces rendered the
// record age under "Last sync", so a connector polling a quiet source every
// fifteen minutes read "Last sync: yesterday" — the screen contradicting a true
// sentence. Here each fact keeps its own name: "Last checked" and "Newest
// record".
//
// THIN CLIENT (C7). Nothing here computes a verdict, a permission or a total.
// `instantOfAge` converts a served AGE into the INSTANT that age describes — the
// only arithmetic on this screen.

import type { ConnectorStatusView } from "../api/types";
import { formatCount } from "../primitives/format";

/** The toolbar's kind filter. "all" is the default and renders no chip. */
export interface SourceFilters {
  kind: string;
}

export const NO_FILTERS: SourceFilters = { kind: "all" };

export const ALL_KINDS = "all";

/**
 * The DISTINCT `kind` values the server actually returned, A–Z.
 *
 * The filter offers what is in the response and nothing else. The screen this
 * replaces carried a hardcoded list of eight connector kinds and drew a row for
 * every one of them, so a deployment with three connectors rendered five rows
 * describing systems it was never connected to.
 */
export function kindsOf(rows: readonly ConnectorStatusView[]): readonly string[] {
  return [...new Set(rows.map((row) => row.kind))].sort((a, b) =>
    a.localeCompare(b, "en", { sensitivity: "base" }),
  );
}

/** The kind filter, applied. "all" narrows nothing. */
export function applyFilters(
  rows: readonly ConnectorStatusView[],
  filters: SourceFilters,
): readonly ConnectorStatusView[] {
  if (filters.kind === ALL_KINDS) return rows;
  return rows.filter((row) => row.kind === filters.kind);
}

/**
 * The instant a served age describes, as an ISO string, or null when the age is
 * absent.
 *
 * `at` is the moment the response was received — the age is an age AS OF THE
 * READ, so the instant is measured from the read and not from a clock that has
 * since ticked on. That is why the relative labels on this screen are stable
 * between refetches instead of creeping upward against a fetch that never
 * happened.
 */
export function instantOfAge(age: number | null, at: number): string | null {
  if (age === null || !Number.isFinite(age)) return null;
  return new Date(at - age * 1000).toISOString();
}

/** When the connector last RAN a sync cycle — null means it never has. */
export function lastCheckedAt(row: ConnectorStatusView, at: number): string | null {
  return instantOfAge(row.last_checked_age_s, at);
}

/** When the newest ACCEPTED record from this source arrived — null means no
 * record is known to have been accepted (a pull lane with no cursor row or an
 * unreadable state store; a CDC lane whose batches never accepted a record). */
export function newestRecordAt(row: ConnectorStatusView, at: number): string | null {
  return instantOfAge(row.cursor_age_s, at);
}

/**
 * The sort keys for the two time columns: a monotonic RECENCY, not the age.
 *
 * A column headed with a time must sort as a time — descending is newest first
 * — and the served fields count the other way, so the negated age is what makes
 * ▼ mean what the header says. It is clock-independent: two rows keep their
 * order however long the page sits open. `null` (never checked / no record
 * cursor) stays last in both directions, which is the sort primitive's rule for
 * an absent field. Each column sorts on ITS OWN served field — sorting "Last
 * checked" by the record age would re-collapse the two facts one level down.
 */
export function checkedRecencyOf(row: ConnectorStatusView): number | null {
  const age = row.last_checked_age_s;
  if (age === null || !Number.isFinite(age)) return null;
  return -age;
}

export function recordRecencyOf(row: ConnectorStatusView): number | null {
  const age = row.cursor_age_s;
  if (age === null || !Number.isFinite(age)) return null;
  return -age;
}

/** §2.2's live count: "{n} sources". */
export function countText(n: number): string {
  return `${formatCount(n)} sources`;
}

/** The applied filters, in the words the CSV preamble names them by (§2.2). */
export function filterLabels(filters: SourceFilters, search: string): readonly string[] {
  const out: string[] = [];
  if (filters.kind !== ALL_KINDS) out.push(`Kind: ${filters.kind}`);
  if (search.trim() !== "") out.push(`Search: ${search.trim()}`);
  return out;
}

/**
 * §9's quarantine heading. A null total is UNREADABLE, never zero — the store
 * could not be read, which is a different fact from an empty store and the one
 * an operator has to act on.
 */
export function quarantineHeading(total: number | null): string {
  if (total === null) return "Quarantine — total unavailable";
  return `Quarantine — ${formatCount(total)} items`;
}

/**
 * The quarantine table's empty line.
 *
 * "Nothing in quarantine." is only true when nothing is in quarantine. A
 * readable total above zero with no reason ledger means the categories were not
 * recorded — saying the quarantine is empty there would state, on screen, the
 * opposite of the count printed directly above it.
 */
export function quarantineEmptyLine(total: number | null): string {
  if (total !== null && total > 0) return "No reasons recorded.";
  return "Nothing in quarantine.";
}
