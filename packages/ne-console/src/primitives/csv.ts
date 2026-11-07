// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.2 — EXPORT. "Exports the currently filtered/sorted window with a header row
// naming the applied filters and the export timestamp."
//
// That preamble is the honest part and the reason this is shared code: a CSV of
// a filtered list, handed on with no record of the filter, is a claim about a
// whole table made from a slice of one. Every export this console writes says
// which slice it is.
//
// The rows serialised here are the rows the table rendered — pass the ALREADY
// filtered and sorted window (see primitives/tableSort.ts), never the raw fetch.
//
// Every cell this module writes is formula-neutralised (CWE-1236, csvSafeCell).
// These client-built exports are governance artifacts opened in spreadsheets,
// and their cells carry attacker-reachable text. The escaping lives here, not
// at each caller, so Approvals and every other client export cannot drift.

import { absoluteTime } from "./format";

/** RFC 4180 quoting. This makes a field parse correctly; it does not make a
 * formula-leading value safe for a spreadsheet. `cell` composes both rules. */
export function csvField(value: string): string {
  if (/[",\n\r]/.test(value)) return `"${value.replace(/"/g, '""')}"`;
  return value;
}

/** Keep this exact set in lockstep with ne.api.routers.audit._FORMULA_PREFIXES.
 * Tab and CR are included because spreadsheets trim leading whitespace before
 * classifying the formula opener that follows. */
export const FORMULA_PREFIXES = ["=", "+", "-", "@", "\t", "\r"] as const;

/** Force a formula-leading cell to render as literal text (CWE-1236).
 * This changes CSV bytes only; the underlying served value is untouched. */
export function csvSafeCell(value: string): string {
  const first = value[0];
  if (first !== undefined && (FORMULA_PREFIXES as readonly string[]).includes(first)) {
    return `'${value}`;
  }
  return value;
}

/** Neutralise before quoting so the apostrophe remains inside an RFC-4180 cell. */
function cell(value: string): string {
  return csvField(csvSafeCell(value));
}

export interface CsvColumn<Row> {
  /** The column header as it appears in the file. */
  header: string;
  /** The cell as text. Absent values export EMPTY, never a zero and never "—":
   * a dash is a rendering convention and a spreadsheet would treat it as data. */
  value: (row: Row) => string;
}

/**
 * The whole file, as a string.
 *
 * Line 1 is a single-field preamble naming the export, the applied filters and
 * the timestamp. Line 2 is the header row. Everything after is data.
 */
export function buildCsv<Row>({
  title,
  filters,
  columns,
  rows,
  now = new Date(),
}: {
  /** What was exported, e.g. "Audit log". */
  title: string;
  /** The applied filters in human words, e.g. ["Outcome: denied", "Search: wire"].
   * Empty means no filter was applied, and the preamble says so. */
  filters: readonly string[];
  columns: ReadonlyArray<CsvColumn<Row>>;
  rows: readonly Row[];
  now?: Date;
}): string {
  const applied = filters.length === 0 ? "no filters applied" : filters.join("; ");
  const preamble = `${title} — ${applied} — exported ${absoluteTime(now.toISOString())}`;
  const lines = [
    cell(preamble),
    columns.map((c) => cell(c.header)).join(","),
    ...rows.map((row) => columns.map((c) => cell(c.value(row))).join(",")),
  ];
  return `${lines.join("\r\n")}\r\n`;
}

/** Hand the built file to the browser. Object URLs are revoked on the next tick
 * — a leaked one pins the whole blob for the life of the document. A server
 * Blob is reused byte-for-byte; client-built strings get the CSV media type. */
export function downloadCsv(filename: string, contents: string | Blob): void {
  const blob =
    typeof contents === "string"
      ? new Blob([contents], { type: "text/csv;charset=utf-8" })
      : contents;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
