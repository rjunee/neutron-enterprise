// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-142 Guided Demo Mode — versioned sessionStorage persistence. The overlay is
// RESUMABLE: the current station index and the per-station audit-seq check-offs
// survive a reload (position only — the overlay is NEVER auto-opened, see
// DemoContext). Pure module, no React. Every access is defensively wrapped:
// malformed or foreign data returns null (fall back to a fresh demo), and a
// storage write that throws (private mode / quota) is swallowed — the demo must
// never crash the console.

const KEY = "ne-demo:v1";

/** The persisted demo position. `v` gates the schema; a bump invalidates old blobs. */
export interface DemoStored {
  v: 1;
  station: number;
  checks: Record<string, number>;
}

/** Read + validate the persisted demo state. Returns null on missing/malformed/
 * wrong-version data (never throws). */
export function loadDemoState(): DemoStored | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      (parsed as { v?: unknown }).v !== 1 ||
      typeof (parsed as { station?: unknown }).station !== "number"
    ) {
      return null;
    }
    const rec = parsed as { v: 1; station: number; checks?: unknown };
    const checks =
      typeof rec.checks === "object" && rec.checks !== null
        ? (rec.checks as Record<string, number>)
        : {};
    return { v: 1, station: rec.station, checks };
  } catch {
    return null;
  }
}

/** Persist the demo state. Swallows any storage error (private mode / quota). */
export function saveDemoState(s: DemoStored): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    // Storage unavailable — resumability is best-effort, never fatal.
  }
}
