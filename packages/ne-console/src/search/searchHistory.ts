// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// RECENT SEARCHES (owner request, 2026-08-04): the empty search screen lists
// this visit's runs, newest first; each replays its captured result set —
// visibly labelled with when it was captured — or re-runs against the server.
//
// WHERE THE HISTORY LIVES, decided: IN MEMORY, THIS TAB, THIS PRINCIPAL — not
// the server, not browser storage.
//
//   * Not the server: a server-kept list of what each employee searched is a
//     governed read over usage logs — the surface SPEC §12 item 1 says must be
//     role-gated and disclosure-audited, and none of that design exists yet.
//     Building it as a free side-effect of a UX feature would ship the
//     disclosure question unanswered. The Ask history pane holds the same line
//     (the Ask surface's own visit journal, since deleted).
//   * Not localStorage: cached result ROWS are ACL-trimmed content. Parked on
//     disk they outlive the session that was authorized to see them — readable
//     after sign-out, after a role change, by the next person on a shared
//     machine. The Ask pane's stated rule applies verbatim: "no browser-storage
//     journal masquerading as one."
//
// THE ACL RULE (C7 — the console renders server verdicts and must never
// resurrect rows the server would now withhold):
//
//   1. Entries BIND to the principal id that ran them. A read under any other
//     principal returns nothing AND evicts the store — fail-closed, so one
//     tab's sign-in change cannot leak the previous identity's rows.
//   2. Only single-identity runs are recorded. A comparison run holds a second
//     principal's served rows under a key that is withdrawn when the
//     comparison ends; caching those would outlive the authority they were
//     read under.
//   3. Nothing persists: sign-out and reload are full navigations, so the
//     store dies with the tab's JS realm.
//   4. A replayed entry renders under a banner naming its capture time, with
//     re-run one click away — a cached verdict is shown AS a dated verdict
//     (the SPEC §12 item 9 lesson: two facts, named correctly), never as
//     current truth.
//   5. Capture binds to the API principal + groups served in the same request
//     window as the search. Replay re-checks that context AND asks the
//     principal-scoped retrieve seam to confirm that every cached row remains
//     visible. A changed or unverifiable answer falls back to a live search.
//
// Residual, stated precisely: replay validation and rendering are not atomic.
// A permission change after validation has the same bounded, already-rendered
// window as a live result table. No cached row is opened without a current
// governed visibility check.

import type { RelatedEntity, ResultRow } from "./results";
import type { AuthorizedRetrievalEnvelopeWire, RetrievalGraphPathWire } from "../api/types";

/** One completed single-identity run, whole — the same shape the screen
 * renders live, so a replay is the captured verdict and nothing re-derived. */
export interface SearchHistoryEntry {
  query: string;
  /** The results-size select at capture time, restored on replay/re-run. */
  size: string;
  /** Canonical API principal + groups stamp. The OIDC subject is the outer
   * store binding; this stamp catches remapping and entitlement changes under
   * the same subject. */
  accessKey: string;
  /** ISO-8601 instant the response landed — the banner's "captured" time. */
  capturedAt: string;
  rows: readonly ResultRow[];
  entities: readonly RelatedEntity[];
  graphPaths: readonly RetrievalGraphPathWire[];
  provisionalCount: number;
  elapsedMs: number;
  /** The server's `complete|partial|exhausted` status and bounds for the
   * captured response. Captured and replayed verbatim: a replay that dropped
   * an authority-limited status would show the same rows under a completeness
   * claim the server never made. `undefined` is a server that stated none. */
  retrieval: AuthorizedRetrievalEnvelopeWire | undefined;
}

/** Newest-first ceiling. Enough for "what did I look at this morning", small
 * enough that the list stays a list. */
export const SEARCH_HISTORY_LIMIT = 20;

interface HistoryState {
  principalId: string;
  entries: readonly SearchHistoryEntry[];
}

/** Module-level so the history survives navigating away and back (the screen
 * unmounts); a tab-lifetime cache, per the header's rules. */
let state: HistoryState | null = null;

/** Stable set identity for the server-side principal context that trimmed a
 * search. JSON framing avoids delimiter collisions; duplicate groups do not
 * change set membership. This is an equality key, not a security hash. */
export function accessContextKey(principalId: string, groups: readonly string[]): string {
  return JSON.stringify([principalId, [...new Set(groups)].sort()]);
}

function isRecordable(entry: SearchHistoryEntry): boolean {
  return (
    typeof entry.accessKey === "string" &&
    entry.accessKey !== "" &&
    typeof entry.capturedAt === "string" &&
    Number.isFinite(Date.parse(entry.capturedAt))
  );
}

/** Record a completed run for `principalId`, newest first. A re-run of the
 * same query+size replaces its older capture — one row per question, carrying
 * the newest verdict. */
export function recordSearch(principalId: string, entry: SearchHistoryEntry): void {
  // A capture without a trustworthy context/timestamp is not useful: it can
  // only be re-run live, and retaining its rows would create stale memory with
  // no safe replay path. Fail closed instead.
  if (!isRecordable(entry)) return;

  const candidates =
    state !== null && state.principalId === principalId
      ? [
          entry,
          ...state.entries.filter(
            (held) => isRecordable(held) && held.accessKey === entry.accessKey,
          ),
        ]
      : [entry];
  const seen = new Set<string>();
  const entries = candidates
    .sort((left, right) => Date.parse(right.capturedAt) - Date.parse(left.capturedAt))
    .filter((held) => {
      const key = JSON.stringify([held.query, held.size]);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, SEARCH_HISTORY_LIMIT);
  state = { principalId, entries };
}

/**
 * The entries `principalId` may see: exactly the ones it recorded.
 *
 * ANY other reader — a different id, or none yet — gets nothing, and a
 * DIFFERENT id additionally evicts the store (rule 1 above): the previous
 * identity's rows must not sit in memory waiting for its owner to come back
 * on a screen someone else is now signed into.
 */
export function searchHistoryFor(principalId: string | null): readonly SearchHistoryEntry[] {
  if (state === null) return [];
  if (principalId === null) return [];
  if (state.principalId !== principalId) {
    state = null;
    return [];
  }
  return state.entries;
}

/** A verified permission-context change invalidates every capture for this
 * subject. Keeping only the clicked entry out would leave other stale ACL-
 * trimmed rows in memory waiting for their own replay attempt. */
export function evictSearchHistoryFor(principalId: string): void {
  if (state?.principalId === principalId) state = null;
}

/** Test seam. Production never clears wholesale — eviction is the
 * principal-mismatch rule above. */
export function clearSearchHistory(): void {
  state = null;
}
