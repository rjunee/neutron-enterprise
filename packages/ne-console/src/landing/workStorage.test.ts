// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The work surface's local record. These are the honesty properties the home
// screen leans on, plus the aliasing defect that the LandingPage suite caught the
// hard way:
//
//   THE BUG: `load()` returned a SHARED module-level empty object, and both
//   writers mutate what `load()` returns. The first write against an empty store
//   therefore poisoned that shared object, which then answered every later read
//   from memory — surviving `localStorage.clear()` entirely. The visible symptom
//   was a first-time visitor being told what changed "since their last visit"
//   using a mark that no longer existed. That is a FABRICATED signal on a page
//   whose whole contract is that nothing is fabricated, so it gets its own test.

import { beforeEach, describe, expect, it } from "vitest";
import {
  ASK_LIMIT,
  asksSnapshot,
  markVisit,
  priorVisitSnapshot,
  readAsks,
  readVisit,
  recordAsk,
  resetWorkRecordCache,
} from "./workStorage";

const ALICE = "alice@example.com";
const BOB = "bob@example.com";

beforeEach(() => {
  localStorage.clear();
  resetWorkRecordCache();
});

describe("workStorage — the visit mark", () => {
  it("round-trips a mark and reports no mark before one is written", () => {
    expect(readVisit(ALICE)).toBeNull();
    markVisit(ALICE, 41, new Date("2026-07-30T10:00:00Z"));
    expect(readVisit(ALICE)).toEqual({ lastSeenSeq: 41, at: "2026-07-30T10:00:00.000Z" });
  });

  it("records an unreadable chain as null, never as zero", () => {
    // A non-ops key never sees a seq. Storing 0 would make the next visit claim a
    // delta "since seq 0" over records this key was refused.
    markVisit(ALICE, null);
    expect(readVisit(ALICE)?.lastSeenSeq).toBeNull();
  });

  it("does NOT survive a cleared store (the shared-object aliasing defect)", () => {
    markVisit(ALICE, 7);
    expect(readVisit(ALICE)).not.toBeNull();
    localStorage.clear();
    // Before the fix this returned the mark from memory, because the write had
    // mutated the module's shared empty-store object.
    expect(readVisit(ALICE)).toBeNull();
    // …and a fresh write after the clear must be the ONLY thing in the store.
    markVisit(BOB, 2);
    expect(readVisit(ALICE)).toBeNull();
    expect(readVisit(BOB)?.lastSeenSeq).toBe(2);
  });

  it("keeps principals apart", () => {
    markVisit(ALICE, 5);
    markVisit(BOB, 9);
    expect(readVisit(ALICE)?.lastSeenSeq).toBe(5);
    expect(readVisit(BOB)?.lastSeenSeq).toBe(9);
  });

  it("survives a malformed or foreign blob by reading as absent", () => {
    localStorage.setItem("ne-work:v1", "{not json");
    expect(readVisit(ALICE)).toBeNull();
    localStorage.setItem("ne-work:v1", JSON.stringify({ v: 99, byPrincipal: { [ALICE]: {} } }));
    expect(readVisit(ALICE)).toBeNull();
    // And a write over the foreign blob still works.
    markVisit(ALICE, 3);
    expect(readVisit(ALICE)?.lastSeenSeq).toBe(3);
  });
});

describe("workStorage — the ask journal", () => {
  it("keeps asks newest-first, per principal, with their surface", () => {
    recordAsk(ALICE, "chat", "first", new Date("2026-07-30T10:00:00Z"));
    recordAsk(ALICE, "search", "second", new Date("2026-07-30T10:01:00Z"));
    recordAsk(BOB, "chat", "bob's own", new Date("2026-07-30T10:02:00Z"));

    expect(readAsks(ALICE).map((a) => [a.surface, a.text])).toEqual([
      ["search", "second"],
      ["chat", "first"],
    ]);
    expect(readAsks(BOB).map((a) => a.text)).toEqual(["bob's own"]);
  });

  it("ignores blank text and trims what it keeps", () => {
    recordAsk(ALICE, "chat", "   ");
    expect(readAsks(ALICE)).toEqual([]);
    recordAsk(ALICE, "chat", "  spaced  ");
    expect(readAsks(ALICE)[0]?.text).toBe("spaced");
  });

  it("refreshes a repeat instead of stacking duplicates", () => {
    recordAsk(ALICE, "chat", "same", new Date("2026-07-30T10:00:00Z"));
    recordAsk(ALICE, "search", "other", new Date("2026-07-30T10:01:00Z"));
    recordAsk(ALICE, "chat", "same", new Date("2026-07-30T10:02:00Z"));
    const asks = readAsks(ALICE);
    expect(asks.map((a) => a.text)).toEqual(["same", "other"]);
    expect(asks[0]?.at).toBe("2026-07-30T10:02:00.000Z");
  });

  it("caps the journal — this is 'what did I just ask', not a history product", () => {
    for (let i = 0; i < ASK_LIMIT + 5; i++) recordAsk(ALICE, "chat", `q${i}`);
    expect(readAsks(ALICE)).toHaveLength(ASK_LIMIT);
    // The oldest fell off, the newest is first.
    expect(readAsks(ALICE)[0]?.text).toBe(`q${ASK_LIMIT + 4}`);
  });
});

describe("workStorage — the React snapshots", () => {
  it("returns a STABLE reference until a write, so useSyncExternalStore settles", () => {
    // A fresh array per call would spin React's store subscription forever.
    expect(asksSnapshot(ALICE)).toBe(asksSnapshot(ALICE));
    recordAsk(ALICE, "chat", "hello");
    const after = asksSnapshot(ALICE);
    expect(after).not.toHaveLength(0);
    expect(asksSnapshot(ALICE)).toBe(after);
  });

  it("pins the PRIOR visit for the life of the page, so marking this visit cannot move it", () => {
    markVisit(ALICE, 4, new Date("2026-07-30T10:00:00Z"));
    resetWorkRecordCache();
    const prior = priorVisitSnapshot(ALICE);
    expect(prior?.lastSeenSeq).toBe(4);
    // The work surface stamps the current visit while the page is on screen.
    markVisit(ALICE, 99);
    // "Since your last visit" must still mean seq 4 — otherwise the line the user
    // is reading rewrites itself to "nothing new" underneath them.
    expect(priorVisitSnapshot(ALICE)).toBe(prior);
    // A fresh page load does see the newer mark.
    resetWorkRecordCache();
    expect(priorVisitSnapshot(ALICE)?.lastSeenSeq).toBe(99);
  });
});
