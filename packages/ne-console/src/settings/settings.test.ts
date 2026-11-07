// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §16's deployment rules, without a DOM. Two things can go wrong here and both
// are silent: a unit conversion done in two places drifts by a factor of a
// thousand, and an absent configured value turns into a zero — which would put
// "0.0 s" under "Search time limit" and state that this deployment gives up
// instantly.

import { describe, expect, it } from "vitest";
import { isEmptyPatch, msToSeconds, secondsField, secondsToMs, settingsPatch } from "./settings";

describe("the seconds/milliseconds conversion", () => {
  it("reads the configured milliseconds as seconds", () => {
    expect(msToSeconds(75000)).toBe(75);
    expect(msToSeconds(1500)).toBe(1.5);
  });

  it("keeps an ABSENT limit absent — it never becomes a zero", () => {
    // A "0 s" limit is a claim about this deployment: that a search gives up
    // instantly. An unset limit is the absence of one.
    expect(msToSeconds(null)).toBeNull();
    expect(msToSeconds(undefined)).toBeNull();
    expect(msToSeconds(0)).toBeNull();
    expect(msToSeconds(Number.NaN)).toBeNull();
    expect(secondsField(null)).toBe("");
  });

  it("writes the typed seconds back as milliseconds, and refuses anything else", () => {
    expect(secondsToMs("75")).toBe(75000);
    expect(secondsToMs(" 1.5 ")).toBe(1500);
    for (const bad of ["", "   ", "abc", "0", "-4"]) {
      expect(secondsToMs(bad)).toBeNull();
    }
  });

  it("round-trips, so the field a person reads is the field they can save", () => {
    expect(secondsToMs(secondsField(75000))).toBe(75000);
  });
});

describe("the settings patch", () => {
  const before = { deployment_name: "Fenwick Plant 3", search_deadline_ms: 75000 };

  it("carries ONLY the field that changed", () => {
    // A whole-object write would revert whatever else moved between this read
    // and this submit.
    expect(
      settingsPatch(before, { deployment_name: "Fenwick Plant 4", search_deadline_ms: 75000 }),
    ).toEqual({ deployment_name: "Fenwick Plant 4" });
    expect(
      settingsPatch(before, { deployment_name: "Fenwick Plant 3", search_deadline_ms: 90000 }),
    ).toEqual({ search_deadline_ms: 90000 });
  });

  it("is EMPTY when nothing moved, so the submit control stays disabled", () => {
    const patch = settingsPatch(before, {
      deployment_name: "Fenwick Plant 3",
      search_deadline_ms: 75000,
    });
    expect(patch).toEqual({});
    expect(isEmptyPatch(patch)).toBe(true);
  });

  it("never submits a blank name or an unusable limit", () => {
    expect(settingsPatch(before, { deployment_name: "   ", search_deadline_ms: null })).toEqual({});
  });

  it("CONTROL: setting a value on a deployment that had none is a change", () => {
    expect(
      settingsPatch(
        { deployment_name: null, search_deadline_ms: null },
        { deployment_name: "Fenwick Plant 3", search_deadline_ms: 75000 },
      ),
    ).toEqual({ deployment_name: "Fenwick Plant 3", search_deadline_ms: 75000 });
  });
});
