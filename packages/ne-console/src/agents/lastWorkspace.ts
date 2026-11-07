// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// "/agents LANDS ON THE LAST-USED WORKSPACE."
//
// The catalog is an inventory view, and inventory is not where the work is. The
// reader who opens Agents twenty times a day wants the agent they were last
// talking to, not a roster they have to re-cross every time; the roster is one
// click away in the sidebar, and behind "All agents" for the full paged list.
//
// THIS IS A CONVENIENCE, NOT A RECORD. It is one id in this browser's own
// storage, it authorizes nothing, and every read of it is fail-soft: a storage
// that throws (private mode, blocked site data) or holds a stale id lands on the
// catalog, which is a correct destination rather than a broken one. The
// workspace route re-validates the id against the server on arrival anyway — an
// agent that is no longer visible answers with the same sanitized 404 as one
// that never existed.

const KEY = "ne.agents.lastWorkspace";

export function readLastWorkspace(): string | null {
  try {
    const value = window.localStorage.getItem(KEY);
    return value === null || value.trim() === "" ? null : value;
  } catch {
    return null;
  }
}

export function writeLastWorkspace(agentId: string): void {
  try {
    window.localStorage.setItem(KEY, agentId);
  } catch {
    /* Storage unavailable. The landing behaviour degrades to the catalog, which
     * is the same place a first-time reader lands. Nothing is lost. */
  }
}
