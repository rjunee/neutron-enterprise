// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The work surface's LOCAL memory: when this principal was last on the home
// screen, and what they last asked. Pure module, no React, no network.
//
// WHY THIS IS LOCAL AND WHY THAT IS HONEST (§0 law 5 — never fabricate)
// ---------------------------------------------------------------------
// Two of the questions a daily user opens the console with — "what happened
// since I was last here" and "what did I ask recently" — have no server answer
// available to every key:
//   - The audit chain answers both precisely, but GET /v1/audit/events is
//     ops-gated. A reader or an approver is refused it (403), by design.
//   - There is no chat/search history endpoint at all.
// The alternative to a local record is either inventing history (forbidden) or
// a home screen that cannot answer the two most ordinary questions in it.
//
// So this module records, in THIS BROWSER only:
//   1. a visit mark — the highest chain seq this principal had already seen (or
//      null when the chain was never readable to them), plus when that was;
//   2. a short journal of the questions they typed, so they can re-run one.
// Everything built from it is labelled with that provenance on screen — "recorded
// in this browser", never "from the chain". A local note presented as a local
// note is a record; a local note presented as a server fact is a fabrication.
// The distinction is the entire point, and the UI keeps it.
//
// Keyed by principal id so two people sharing a machine never see each other's
// journal. Defensive throughout, exactly like demoStorage: malformed or foreign
// data reads as absent, and a write that throws is swallowed.

// REACT SEAM. The snapshot functions at the bottom of this file exist because a
// component may not read localStorage during render (it is an external store,
// and a fresh array every render would spin `useSyncExternalStore` forever).
// They cache per principal and notify subscribers on write, so the work surface
// can subscribe to this store the way React expects instead of copying it into
// component state from an effect.

const KEY = "ne-work:v1";

/** How many asks are retained per principal. Small on purpose: this is "what did
 * I just ask", not a search history product. */
export const ASK_LIMIT = 8;

/** Which surface an ask was typed into — decides how "run again" re-opens it. */
export type AskSurface = "chat" | "search";

/**
 * What the ask actually produced. An entry with no outcome is one whose turn
 * never settled while this browser was watching — it is rendered as "outcome not
 * recorded", never silently as a success. The distinction matters: without it a
 * failed turn and a cited answer look identical in the history, which is exactly
 * the defect review round 1 found ("the history cannot tell me which of my
 * questions ever produced anything").
 */
export type AskOutcome = "answered" | "refused" | "failed";

export interface RecentAsk {
  /** Verbatim text the user typed. Rendered as inert React text (C12). */
  text: string;
  surface: AskSurface;
  /** ISO-8601 UTC, stamped locally at the moment of asking. */
  at: string;
  /** What the turn produced, stamped when it settled. Absent = never settled. */
  outcome?: AskOutcome;
  /** How many citations the answer carried. Absent when there was no answer. */
  citations?: number;
  /** How many results a search returned. Absent when the query never settled. */
  results?: number;
}

/** What the home screen knew last time this principal opened it. */
export interface VisitMark {
  /** Highest audit seq this principal had seen, or null if the chain was never
   * readable to them (a non-ops key). Never invented. */
  lastSeenSeq: number | null;
  /** ISO-8601 UTC of that visit. */
  at: string;
}

interface PrincipalRecord {
  visit?: VisitMark;
  asks?: RecentAsk[];
}

interface Stored {
  v: 1;
  byPrincipal: Record<string, PrincipalRecord>;
}

/** A FRESH empty store on every call. This must never be a shared module-level
 * constant: `markVisit`/`recordAsk` mutate what `load()` hands back, so a shared
 * object would be written into on the first empty read and would then shadow
 * storage forever — surviving a `localStorage.clear()` and answering later reads
 * from memory with records that are no longer stored. The test suite caught
 * exactly that: a cleared store still read back the previous visit mark. */
function emptyStore(): Stored {
  return { v: 1, byPrincipal: {} };
}

function load(): Stored {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw === null) return emptyStore();
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return emptyStore();
    const rec = parsed as { v?: unknown; byPrincipal?: unknown };
    if (rec.v !== 1) return emptyStore();
    if (typeof rec.byPrincipal !== "object" || rec.byPrincipal === null) return emptyStore();
    // Copy the principal map too: the parsed object is fresh per call, but being
    // explicit here keeps the "callers may mutate what load() returns" contract
    // true no matter how this function is later rewritten.
    return { v: 1, byPrincipal: { ...(rec.byPrincipal as Record<string, PrincipalRecord>) } };
  } catch {
    return emptyStore();
  }
}

function save(state: Stored): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    // Storage unavailable (private mode / quota) — the surface degrades to
    // "no local record", which it already renders honestly.
  }
}

function recordFor(state: Stored, principalId: string): PrincipalRecord {
  const rec = state.byPrincipal[principalId];
  return typeof rec === "object" && rec !== null ? rec : {};
}

/** The visit mark written on this principal's PREVIOUS visit, or null if this is
 * their first on this browser. */
export function readVisit(principalId: string): VisitMark | null {
  const rec = recordFor(load(), principalId);
  const visit = rec.visit;
  if (!visit || typeof visit.at !== "string") return null;
  const seq = visit.lastSeenSeq;
  return { lastSeenSeq: typeof seq === "number" ? seq : null, at: visit.at };
}

/** Stamp this visit. `seq` is the highest chain seq actually observed on this
 * visit, or null when the chain was not readable to this key — a null is
 * recorded as a null, never as a zero. */
export function markVisit(principalId: string, seq: number | null, now = new Date()): void {
  const state = load();
  const rec = recordFor(state, principalId);
  state.byPrincipal[principalId] = { ...rec, visit: { lastSeenSeq: seq, at: now.toISOString() } };
  save(state);
}

/** This principal's recent asks, newest first. */
export function readAsks(principalId: string): RecentAsk[] {
  const rec = recordFor(load(), principalId);
  const asks = rec.asks;
  if (!Array.isArray(asks)) return [];
  return asks
    .filter(
      (a): a is RecentAsk =>
        typeof a === "object" &&
        a !== null &&
        typeof (a as RecentAsk).text === "string" &&
        typeof (a as RecentAsk).at === "string" &&
        ((a as RecentAsk).surface === "chat" || (a as RecentAsk).surface === "search"),
    )
    .slice(0, ASK_LIMIT);
}

/**
 * Stamp the OUTCOME onto the most recent entry for (surface, text). Never
 * creates an entry: an outcome with no ask behind it would be a record of
 * something this browser did not see. Silently does nothing when the ask has
 * already aged out of the journal.
 */
export function recordAskOutcome(
  principalId: string,
  surface: AskSurface,
  text: string,
  fields: { outcome: AskOutcome; citations?: number; results?: number },
): void {
  const trimmed = text.trim();
  if (trimmed === "") return;
  const state = load();
  const rec = recordFor(state, principalId);
  const prior = readAsks(principalId);
  const idx = prior.findIndex((a) => a.surface === surface && a.text === trimmed);
  if (idx === -1) return;
  const asks = [...prior];
  asks[idx] = { ...asks[idx]!, ...fields };
  state.byPrincipal[principalId] = { ...rec, asks };
  save(state);
  askCache.delete(principalId);
  emit();
}

/** Journal one ask. Blank text is ignored; an identical consecutive re-ask on the
 * same surface refreshes the existing entry rather than stacking duplicates. */
export function recordAsk(
  principalId: string,
  surface: AskSurface,
  text: string,
  now = new Date(),
): void {
  const trimmed = text.trim();
  if (trimmed === "") return;
  const state = load();
  const rec = recordFor(state, principalId);
  const prior = Array.isArray(rec.asks) ? readAsks(principalId) : [];
  const deduped = prior.filter((a) => !(a.surface === surface && a.text === trimmed));
  const asks = [{ text: trimmed, surface, at: now.toISOString() }, ...deduped].slice(0, ASK_LIMIT);
  state.byPrincipal[principalId] = { ...rec, asks };
  save(state);
  askCache.delete(principalId);
  emit();
}

// ===========================================================================
// The React seam — cached snapshots + a subscription (see the note at the top).
// ===========================================================================

type Listener = () => void;
const listeners = new Set<Listener>();

function emit(): void {
  for (const listener of listeners) listener();
}

/** Subscribe to local work-record writes. For `useSyncExternalStore`. */
export function subscribeToWorkRecord(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** No asks, as a stable reference — a new `[]` per call would make
 * `useSyncExternalStore` loop. */
export const NO_ASKS: readonly RecentAsk[] = [];

const askCache = new Map<string, readonly RecentAsk[]>();

/** This principal's asks, referentially stable until the next `recordAsk`. */
export function asksSnapshot(principalId: string): readonly RecentAsk[] {
  const cached = askCache.get(principalId);
  if (cached !== undefined) return cached;
  const fresh = readAsks(principalId);
  const value: readonly RecentAsk[] = fresh.length === 0 ? NO_ASKS : fresh;
  askCache.set(principalId, value);
  return value;
}

const visitCache = new Map<string, VisitMark | null>();

/** The visit mark as it stood the FIRST time this page load asked for it.
 * Deliberately NOT invalidated by `markVisit`: "since your last visit" must keep
 * meaning the same thing while the user is still looking at it, so stamping the
 * current visit cannot move the line under their feet. */
export function priorVisitSnapshot(principalId: string): VisitMark | null {
  if (!visitCache.has(principalId)) visitCache.set(principalId, readVisit(principalId));
  return visitCache.get(principalId) ?? null;
}

/** Drop every cached snapshot. Test seam — a suite that writes storage directly
 * (or switches principal fixtures) must be able to start clean. */
export function resetWorkRecordCache(): void {
  askCache.clear();
  visitCache.clear();
  emit();
}
