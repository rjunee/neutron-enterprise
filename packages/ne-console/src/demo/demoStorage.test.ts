// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-142 sessionStorage persistence. Round-trips a valid blob; returns null on
// malformed JSON / wrong version / missing key; never throws when setItem throws.

import { afterEach, describe, expect, it, vi } from "vitest";
import { loadDemoState, saveDemoState } from "./demoStorage";

const KEY = "ne-demo:v1";

afterEach(() => {
  sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("NE-142 demoStorage", () => {
  it("round-trips a valid state", () => {
    saveDemoState({ v: 1, station: 4, checks: { "beat-1": 7 } });
    expect(loadDemoState()).toEqual({ v: 1, station: 4, checks: { "beat-1": 7 } });
  });

  it("returns null on a missing key", () => {
    expect(loadDemoState()).toBeNull();
  });

  it("returns null on malformed JSON", () => {
    sessionStorage.setItem(KEY, "{not json");
    expect(loadDemoState()).toBeNull();
  });

  it("returns null on the wrong version", () => {
    sessionStorage.setItem(KEY, JSON.stringify({ v: 2, station: 1, checks: {} }));
    expect(loadDemoState()).toBeNull();
  });

  it("returns null when station is not a number", () => {
    sessionStorage.setItem(KEY, JSON.stringify({ v: 1, station: "nope", checks: {} }));
    expect(loadDemoState()).toBeNull();
  });

  it("tolerates a missing checks map (defaults to {})", () => {
    sessionStorage.setItem(KEY, JSON.stringify({ v: 1, station: 2 }));
    expect(loadDemoState()).toEqual({ v: 1, station: 2, checks: {} });
  });

  it("never throws when setItem throws (private mode / quota)", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(() => saveDemoState({ v: 1, station: 0, checks: {} })).not.toThrow();
  });
});
