// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §13 — the ledger's rules, as pure functions: what the window filters are, how
// the class chips narrow the page, what the count says, and exactly what leaves
// in the export.
//
// They live apart from the screen because the RULES are the part worth testing,
// and they are testable here without a DOM or a fixture server.
//
// THIN CLIENT (C7). Everything here reorders, hides or serialises rows the
// server already sent. Nothing computes a verdict, a hash or a permission.

import type { AuditAnchorView, AuditRecordView } from "../api/types";
import type { CsvColumn } from "../primitives/csv";
import { ABSENT, absoluteTime, formatCount } from "../primitives/format";
import { classifyEvent, countRefusals } from "./eventClass";

/* ---------------------------- the fetch window ---------------------------- */

/** The filters that go to the server on Apply. Every one is a parameter the
 * ledger read actually accepts; the console never links a filter the server
 * would silently ignore. */
export interface AppliedFilters {
  actor: string;
  action: string;
  from: string;
  to: string;
  limit: number;
}

/**
 * The window sizes offered.
 *
 * §13 names 100 / 500 / 1000. The ledger read caps `limit` at 500 and answers
 * 422 above it, so a 1000 entry would be a control with nothing behind it: it
 * could only ever replace the reader's window with an error. Two real sizes are
 * offered until the read's own cap moves.
 */
export const WINDOW_SIZES = [
  { value: "100", label: "100" },
  { value: "500", label: "500" },
] as const;

export type WindowSize = (typeof WINDOW_SIZES)[number]["value"];

export const DEFAULT_WINDOW: WindowSize = "100";

export const NO_FILTERS: AppliedFilters = {
  actor: "",
  action: "",
  from: "",
  to: "",
  limit: Number(DEFAULT_WINDOW),
};

/** The browser's IANA zone names the civil-time rules used by
 * `datetime-local`. It is deliberately read once by the page and rendered next
 * to the controls; a wall-clock value without this context is not an instant. */
export function viewerTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

interface WallClock {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
  millisecond: number;
}

export type InstantBound =
  { kind: "empty" } | { kind: "instant"; value: string } | { kind: "error"; message: string };

function utcEpoch(value: WallClock): number {
  // Date.UTC treats years 0..99 as 1900..1999. setUTCFullYear preserves the
  // literal year and gives us one round-trip validator for every component.
  const date = new Date(0);
  date.setUTCFullYear(value.year, value.month - 1, value.day);
  date.setUTCHours(value.hour, value.minute, value.second, value.millisecond);
  return date.getTime();
}

function parseWallClock(raw: string): WallClock | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(raw);
  if (match === null) return null;
  const value: WallClock = {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
    second: Number(match[6] ?? "0"),
    millisecond: Number((match[7] ?? "").padEnd(3, "0")),
  };
  const date = new Date(utcEpoch(value));
  return date.getUTCFullYear() === value.year &&
    date.getUTCMonth() === value.month - 1 &&
    date.getUTCDate() === value.day &&
    date.getUTCHours() === value.hour &&
    date.getUTCMinutes() === value.minute &&
    date.getUTCSeconds() === value.second &&
    date.getUTCMilliseconds() === value.millisecond
    ? value
    : null;
}

function zonedWallClock(epoch: number, timeZone: string): Omit<WallClock, "millisecond"> {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    calendar: "gregory",
    numberingSystem: "latn",
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(epoch));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((candidate) => candidate.type === type)?.value);
  return {
    year: part("year"),
    month: part("month"),
    day: part("day"),
    hour: part("hour"),
    minute: part("minute"),
    second: part("second"),
  };
}

function sameWallClock(candidate: Omit<WallClock, "millisecond">, expected: WallClock): boolean {
  return (
    candidate.year === expected.year &&
    candidate.month === expected.month &&
    candidate.day === expected.day &&
    candidate.hour === expected.hour &&
    candidate.minute === expected.minute &&
    candidate.second === expected.second
  );
}

/** Resolve one `datetime-local` value to the instant the viewer intended.
 *
 * The input has no offset, so conversion must use the viewer's named zone. We
 * enumerate the offsets in force around that civil date, then round-trip every
 * candidate through the zone. Zero matches is a DST gap; two matches is a DST
 * fold. Both fail visibly instead of letting `Date` silently normalize or pick
 * one occurrence. A valid result is canonical UTC and therefore unambiguous at
 * the server boundary.
 */
export function resolveLocalInstant(
  raw: string,
  label: "From" | "To",
  timeZone: string,
): InstantBound {
  if (raw.trim() === "") return { kind: "empty" };
  const wall = parseWallClock(raw.trim());
  if (wall === null) {
    return { kind: "error", message: `${label} must be a valid local date and time.` };
  }

  const localEpoch = utcEpoch(wall);
  const offsets = new Set<number>();
  try {
    // Three days on either side covers zone transitions wider than an ordinary
    // DST fold. Six-hour samples are only used to discover possible offsets;
    // candidate instants are then checked exactly to the second below.
    for (let hours = -72; hours <= 72; hours += 6) {
      const sample = Math.floor(localEpoch / 1000) * 1000 + hours * 60 * 60 * 1000;
      const local = zonedWallClock(sample, timeZone);
      offsets.add(utcEpoch({ ...local, millisecond: 0 }) - sample);
    }
  } catch {
    return { kind: "error", message: `${label} cannot be resolved in timezone ${timeZone}.` };
  }

  const matches = new Set<number>();
  for (const offset of offsets) {
    const candidate = localEpoch - offset;
    if (
      sameWallClock(zonedWallClock(candidate, timeZone), wall) &&
      ((candidate % 1000) + 1000) % 1000 === wall.millisecond
    ) {
      matches.add(candidate);
    }
  }
  if (matches.size === 0) {
    return {
      kind: "error",
      message: `${label} does not exist in ${timeZone} because the clock changes at that time. Choose another time.`,
    };
  }
  if (matches.size > 1) {
    return {
      kind: "error",
      message: `${label} occurs twice in ${timeZone} because the clock changes at that time. Choose another time.`,
    };
  }
  return { kind: "instant", value: new Date([...matches][0]!).toISOString() };
}

/** Only the values the reader actually set are sent — an empty box is not a
 * filter for the empty string. */
export function requestFilters(applied: AppliedFilters): {
  actor?: string;
  action?: string;
  from?: string;
  to?: string;
  limit: number;
} {
  return {
    ...(applied.actor.trim() === "" ? {} : { actor: applied.actor.trim() }),
    ...(applied.action.trim() === "" ? {} : { action: applied.action.trim() }),
    ...(applied.from.trim() === "" ? {} : { from: applied.from.trim() }),
    ...(applied.to.trim() === "" ? {} : { to: applied.to.trim() }),
    limit: applied.limit,
  };
}

/* ------------------------------ class chips ------------------------------- */

/** §13's three class toggles. Refusals are off by default and the chip carries
 * the hidden count. */
export interface ClassFilters {
  decisions: boolean;
  toolCalls: boolean;
  refusals: boolean;
}

export const DEFAULT_CLASSES: ClassFilters = {
  decisions: true,
  toolCalls: true,
  refusals: false,
};

/**
 * Each chip governs its OWN class. A record of no charted class ("other") has no
 * chip and is always kept: hiding rows that no control on screen accounts for is
 * exactly the silent narrowing the count line exists to prevent.
 */
export function applyClassFilters<T extends { event_type: string; action: string | null }>(
  rows: readonly T[],
  classes: ClassFilters,
): readonly T[] {
  return rows.filter((row) => {
    switch (classifyEvent(row)) {
      case "decision":
        return classes.decisions;
      case "tool-call":
        return classes.toolCalls;
      case "access-refusal":
        return classes.refusals;
      default:
        return true;
    }
  });
}

export function countToolCalls(
  rows: ReadonlyArray<{ event_type: string; action: string | null }>,
): number {
  return rows.filter((row) => classifyEvent(row) === "tool-call").length;
}

export { countRefusals };

/* ---------------------------- the ?seq= anchor ----------------------------- */

/** The parsed `?seq=` deep-link parameter. `malformed` is its own case because
 * a link that never named an entry must still fail LOUDLY — a parse failure
 * that silently degrades to the unanchored list reads as a successful jump. */
export type AnchorParam =
  { kind: "none" } | { kind: "malformed"; raw: string } | { kind: "seq"; seq: number };

export function parseAnchorParam(raw: string | null): AnchorParam {
  if (raw === null) return { kind: "none" };
  const trimmed = raw.trim();
  const value = Number(trimmed);
  if (/^[0-9]+$/.test(trimmed) && Number.isSafeInteger(value) && value >= 1) {
    return { kind: "seq", seq: value };
  }
  return { kind: "malformed", raw };
}

/** What a rendered anchor notice says, and the one recovery it can offer. */
export interface AnchorNotice {
  text: string;
  action: "clear-filters" | "show-entry" | null;
}

/**
 * The deep link's loud-failure rule: a `?seq=` navigation that does not land
 * highlighted on its entry MUST say which entry failed and why — a silent miss
 * is visually identical to a successful navigation, which is the harm.
 *
 * Reachability comes from the SERVER's anchor verdict, rendered verbatim (C7);
 * the two client-side cases (`malformed`, hidden-by-view-filters) are facts the
 * console itself owns — a parameter it could not parse, and a narrowing IT
 * applied to rows it was served — never inferences about the chain.
 */
export function anchorNotice(
  param: AnchorParam,
  verdict: AuditAnchorView | null,
  anchorFetched: boolean,
  anchorVisible: boolean,
): AnchorNotice | null {
  if (param.kind === "none") return null;
  if (param.kind === "malformed") {
    return {
      text: `“${param.raw}” isn't an audit entry number, so there is no entry to jump to.`,
      action: null,
    };
  }
  const entry = formatCount(param.seq);
  if (verdict === null) {
    // The read answered without a reachability verdict (version skew). The one
    // thing this screen may not do is let that absence read as a successful
    // jump — and when the row IS loaded, the view-hidden check below is the
    // console's own fact, stated the same with or without a server verdict.
    if (!anchorFetched) {
      return { text: `Entry ${entry} couldn't be located in the loaded records.`, action: null };
    }
  } else if (verdict.status === "not_found") {
    return {
      text:
        param.seq > verdict.head_seq
          ? `Entry ${entry} isn't in the audit log — the log ends at entry ${formatCount(verdict.head_seq)}.`
          : `Entry ${entry} is missing from the audit log, which currently ends at entry ${formatCount(verdict.head_seq)}.`,
      action: null,
    };
  } else if (verdict.status === "filtered_out") {
    return {
      text: `Entry ${entry} exists but the applied filters exclude it.`,
      action: "clear-filters",
    };
  }
  if (!anchorVisible) {
    return {
      text: `Entry ${entry} is in the loaded window but hidden by the current view filters.`,
      action: "show-entry",
    };
  }
  return null;
}

/** §13's pagination line for an ANCHORED window: "most recent" would be a
 * false statement about a window positioned around an older entry. */
export function anchorPageNote(fetched: number, seq: number, found: boolean): string {
  const where = found ? "around" : "near";
  return `Showing ${formatCount(fetched)} records ${where} entry ${formatCount(seq)}. Narrow with filters or raise the window.`;
}

/* --------------------------------- copy ----------------------------------- */

/** §13's toolbar count. */
export function showingText(visible: number, fetched: number): string {
  return `Showing ${formatCount(visible)} of ${formatCount(fetched)}`;
}

/** §13's pagination line. The read is limit-only; this states the window the
 * reader is looking at rather than drawing paging controls with no cursor
 * behind them. */
export function pageNote(fetched: number): string {
  return `Showing the most recent ${formatCount(fetched)} records. Narrow with filters or raise the window.`;
}

/** §13's break band. */
export function breakBandText(seq: number): string {
  return `Chain break at entry ${formatCount(seq)}. Records after this point failed verification.`;
}

/** A long identifier, shortened from the MIDDLE so both ends stay readable —
 * a resource is told apart by its head and its tail, never by its middle. */
export function truncateMiddle(value: string, max = 30): string {
  if (value.length <= max) return value;
  const head = Math.ceil((max - 1) / 2);
  const tail = Math.floor((max - 1) / 2);
  return `${value.slice(0, head)}…${value.slice(value.length - tail)}`;
}

/* --------------------------------- export --------------------------------- */

/** Every filter in force, in words, for the export preamble. A CSV of a
 * narrowed window handed on with no record of the narrowing is a claim about a
 * whole ledger made from a slice of one. */
export function filterLabels(
  applied: AppliedFilters,
  classes: ClassFilters,
  search: string,
): string[] {
  const out: string[] = [];
  if (applied.actor.trim() !== "") out.push(`Actor: ${applied.actor.trim()}`);
  if (applied.action.trim() !== "") out.push(`Action: ${applied.action.trim()}`);
  if (applied.from.trim() !== "") out.push(`From: ${applied.from.trim()}`);
  if (applied.to.trim() !== "") out.push(`To: ${applied.to.trim()}`);
  out.push(`Window: ${formatCount(applied.limit)} records`);
  if (!classes.decisions) out.push("Decisions: hidden");
  if (!classes.toolCalls) out.push("Tool calls: hidden");
  out.push(classes.refusals ? "Access refusals: shown" : "Access refusals: hidden");
  if (search.trim() !== "") out.push(`Filter: ${search.trim()}`);
  return out;
}

/**
 * §13: "all columns including the three hashes". The file carries the whole
 * served record, not the seven columns the table has room for — a compliance
 * reviewer takes the export away and cannot come back for the fields the screen
 * left behind.
 */
export function csvColumns(): ReadonlyArray<CsvColumn<AuditRecordView>> {
  const text = (value: string | null) => value ?? "";
  return [
    { header: "seq", value: (row) => String(row.seq) },
    { header: "occurred_at", value: (row) => row.occurred_at },
    { header: "actor", value: (row) => row.actor },
    { header: "action", value: (row) => text(row.action) },
    { header: "event_type", value: (row) => row.event_type },
    { header: "resource", value: (row) => text(row.resource) },
    { header: "outcome", value: (row) => text(row.outcome) },
    { header: "event_id", value: (row) => row.event_id },
    { header: "kpi_tag", value: (row) => text(row.kpi_tag) },
    { header: "trace_id", value: (row) => text(row.trace_id) },
    { header: "config_hash", value: (row) => row.config_hash },
    { header: "prev_hash", value: (row) => row.prev_hash },
    { header: "record_hash", value: (row) => row.record_hash },
  ];
}

function slug(value: string): string {
  const cleaned = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned === "" ? "deployment" : cleaned;
}

function day(iso: string): string {
  return iso.slice(0, 10);
}

/**
 * `audit-{deployment}-{from}-{to}.csv`.
 *
 * When the reader set no window, `from`/`to` are the first and last days
 * actually present in the exported rows — a measured span of the file's own
 * contents, never an invented one. An empty window has no span and no export.
 */
export function exportFilename(
  deployment: string,
  applied: AppliedFilters,
  rows: readonly AuditRecordView[],
): string {
  const days = rows.map((row) => day(row.occurred_at)).sort();
  const from = applied.from.trim() !== "" ? day(applied.from.trim()) : (days[0] ?? "");
  const to = applied.to.trim() !== "" ? day(applied.to.trim()) : (days[days.length - 1] ?? "");
  return `audit-${slug(deployment)}-${from}-${to}.csv`;
}

/* ------------------------------- pane fields ------------------------------ */

/** The pane renders the record's own timestamp in the unambiguous local form. */
export function paneTime(iso: string): string {
  return absoluteTime(iso);
}

/** Pretty-printed, inert. Never executed, never edited. */
export function payloadText(payload: Record<string, unknown>): string {
  return Object.keys(payload).length === 0 ? ABSENT : JSON.stringify(payload, null, 2);
}
