// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The recent-searches store's ACL rule, proven at the store: entries BIND to
// the principal that recorded them, any other reader gets nothing AND evicts,
// and the shape rules (newest first, dedupe, ceiling) hold.

import { beforeEach, describe, expect, it } from "vitest";
import {
  SEARCH_HISTORY_LIMIT,
  accessContextKey,
  clearSearchHistory,
  evictSearchHistoryFor,
  recordSearch,
  searchHistoryFor,
  type SearchHistoryEntry,
} from "./searchHistory";

function entry(over: Partial<SearchHistoryEntry> = {}): SearchHistoryEntry {
  return {
    query: "renewal",
    size: "10",
    accessKey: accessContextKey("alice", ["engineering"]),
    capturedAt: "2026-08-04T10:00:00.000Z",
    rows: [],
    entities: [],
    graphPaths: [],
    provisionalCount: 0,
    elapsedMs: 120,
    retrieval: undefined,
    ...over,
  };
}

beforeEach(() => clearSearchHistory());

describe("the server access-context key", () => {
  it("treats same-principal group membership as a set", () => {
    expect(accessContextKey("alice", ["a", "b", "a"])).toBe(accessContextKey("alice", ["b", "a"]));
  });

  it("distinguishes both a changed API principal and changed groups", () => {
    const captured = accessContextKey("alice", ["engineering"]);
    expect(accessContextKey("alice-v2", ["engineering"])).not.toBe(captured);
    expect(accessContextKey("alice", ["sales"])).not.toBe(captured);
  });
});

describe("the principal binding — the rule that prevents cross-identity replay", () => {
  it("serves a principal exactly what that principal recorded (control)", () => {
    recordSearch("alice", entry());
    expect(searchHistoryFor("alice").map((e) => e.query)).toEqual(["renewal"]);
  });

  it("serves NOTHING to a different principal, and evicts the store", () => {
    recordSearch("alice", entry());
    expect(searchHistoryFor("bob")).toEqual([]);
    // Evicted, not merely hidden: alice's rows are gone from memory too, so a
    // later read under NO identity can reach them.
    expect(searchHistoryFor("alice")).toEqual([]);
  });

  it("serves nothing while the identity is unknown, without evicting", () => {
    recordSearch("alice", entry());
    expect(searchHistoryFor(null)).toEqual([]);
    // Unknown is not "someone else": when /v1/me answers, the owner's own
    // history is still there.
    expect(searchHistoryFor("alice")).toHaveLength(1);
  });

  it("a record under a new principal starts from empty, never merges", () => {
    recordSearch("alice", entry({ query: "alpha" }));
    recordSearch("bob", entry({ query: "beta" }));
    expect(searchHistoryFor("bob").map((e) => e.query)).toEqual(["beta"]);
  });
});

describe("the list shape", () => {
  it("is reverse chronological even when records arrive out of order", () => {
    recordSearch("alice", entry({ query: "newer", capturedAt: "2026-08-04T11:00:00.000Z" }));
    recordSearch("alice", entry({ query: "older", capturedAt: "2026-08-04T09:00:00.000Z" }));
    expect(searchHistoryFor("alice").map((e) => e.query)).toEqual(["newer", "older"]);
  });

  it("a re-run of the same query+size replaces its older capture", () => {
    recordSearch("alice", entry({ capturedAt: "2026-08-04T10:00:00.000Z" }));
    recordSearch("alice", entry({ query: "other" }));
    recordSearch("alice", entry({ capturedAt: "2026-08-04T11:00:00.000Z" }));
    const held = searchHistoryFor("alice");
    expect(held.map((e) => e.query)).toEqual(["renewal", "other"]);
    expect(held[0]?.capturedAt).toBe("2026-08-04T11:00:00.000Z");
  });

  it("the same query at a DIFFERENT size is its own entry", () => {
    recordSearch("alice", entry({ size: "10" }));
    recordSearch("alice", entry({ size: "50" }));
    expect(searchHistoryFor("alice")).toHaveLength(2);
  });

  it("holds at most the ceiling, dropping the oldest", () => {
    for (let i = 0; i < SEARCH_HISTORY_LIMIT + 3; i += 1) {
      recordSearch(
        "alice",
        entry({ query: `q-${i}`, capturedAt: new Date(Date.UTC(2026, 7, 4, 10, i)).toISOString() }),
      );
    }
    const held = searchHistoryFor("alice");
    expect(held).toHaveLength(SEARCH_HISTORY_LIMIT);
    expect(held[0]?.query).toBe(`q-${SEARCH_HISTORY_LIMIT + 2}`);
    expect(held.map((e) => e.query)).not.toContain("q-0");
  });

  it("drops captures from an older access context when a fresh one records", () => {
    recordSearch("alice", entry({ query: "old-context" }));
    recordSearch(
      "alice",
      entry({ query: "current-context", accessKey: accessContextKey("alice", ["sales"]) }),
    );

    expect(searchHistoryFor("alice").map((held) => held.query)).toEqual(["current-context"]);
  });

  it("rejects malformed context stamps and timestamps instead of retaining their rows", () => {
    recordSearch("alice", entry({ accessKey: "" }));
    recordSearch("alice", entry({ capturedAt: "not-an-instant" }));

    expect(searchHistoryFor("alice")).toEqual([]);
  });
});

describe("verified invalidation", () => {
  it("evicts every capture for the bound subject", () => {
    recordSearch("alice", entry({ query: "one" }));
    recordSearch("alice", entry({ query: "two" }));

    evictSearchHistoryFor("alice");

    expect(searchHistoryFor("alice")).toEqual([]);
  });

  it("cannot evict another subject's store", () => {
    recordSearch("alice", entry());

    evictSearchHistoryFor("bob");

    expect(searchHistoryFor("alice")).toHaveLength(1);
  });
});
