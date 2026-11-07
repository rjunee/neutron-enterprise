// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.7. The load-bearing case in this file is the UNPRICED trap: a cost the
// server reports as null, or as 0.0 for a self-hosted lane, must render
// "Unpriced" — never "$0.00" and never blank. "$0.00" is a claim that the run
// was free. It is a different statement from "no price was recorded", and it is
// the one the shipped console made.

import { describe, expect, it } from "vitest";
import {
  ABSENT,
  absoluteTime,
  formatCount,
  formatDeadline,
  formatDuration,
  formatMoney,
  formatUsdCents,
  relativeTime,
  truncateHash,
} from "./format";

describe("formatMoney — the unpriced trap", () => {
  it("renders Unpriced for null, undefined, and a declared-unpriced lane", () => {
    expect(formatMoney(null)).toBe("Unpriced");
    expect(formatMoney(undefined)).toBe("Unpriced");
    expect(formatMoney(12.5, { unpriced: true })).toBe("Unpriced");
  });

  it("renders Unpriced for 0 — never $0.00", () => {
    expect(formatMoney(0)).toBe("Unpriced");
    expect(formatMoney(0)).not.toBe("$0.00");
  });

  it("renders a real price with grouping and two decimals", () => {
    // The control: the rules above are not achieved by never printing money.
    expect(formatMoney(1284.061)).toBe("$1,284.06");
    expect(formatMoney(0.4)).toBe("$0.40");
  });
});

describe("formatUsdCents — recorded totals are not infrastructure prices", () => {
  it("renders absent/non-finite values as absent and a recorded zero as $0.00", () => {
    expect(formatUsdCents(null)).toBe(ABSENT);
    expect(formatUsdCents(undefined)).toBe(ABSENT);
    expect(formatUsdCents(Number.NaN)).toBe(ABSENT);
    expect(formatUsdCents(0)).toBe("$0.00");
  });

  it("converts exact server cents to grouped USD with two decimals", () => {
    // Mutation target: treating cents as dollars changes both values by 100x.
    expect(formatUsdCents(7750)).toBe("$77.50");
    expect(formatUsdCents(128406)).toBe("$1,284.06");
  });
});

describe("formatCount — grouping from 10,000 up (§2.7)", () => {
  it("groups at and above ten thousand, and not below", () => {
    expect(formatCount(9999)).toBe("9999");
    expect(formatCount(10000)).toBe("10,000");
    expect(formatCount(4812)).toBe("4812");
  });

  it("renders the em dash for an absent count — never a zero", () => {
    expect(formatCount(null)).toBe(ABSENT);
    expect(formatCount(undefined)).toBe(ABSENT);
    expect(formatCount(0)).toBe("0");
  });
});

describe("formatDuration", () => {
  it("uses seconds above a second and milliseconds below it", () => {
    expect(formatDuration(1200)).toBe("1.2 s");
    expect(formatDuration(840)).toBe("840 ms");
    expect(formatDuration(null)).toBe(ABSENT);
  });
});

describe("truncateHash", () => {
  it("keeps the first eight characters", () => {
    expect(truncateHash("a41f9c2b7d3e4f50a41f9c2b7d3e4f50")).toBe("a41f9c2b");
    expect(truncateHash(null)).toBe(ABSENT);
    // Shorter than the window: returned whole, never padded.
    expect(truncateHash("abc")).toBe("abc");
  });
});

describe("relativeTime and formatDeadline", () => {
  const now = Date.parse("2026-07-01T12:00:00Z");
  const ago = (ms: number) => new Date(now - ms).toISOString();

  it("reads in the table grammar", () => {
    expect(relativeTime(ago(30_000), now)).toBe("just now");
    expect(relativeTime(ago(14 * 60_000), now)).toBe("14 min ago");
    expect(relativeTime(ago(2 * 3_600_000), now)).toBe("2 h ago");
    expect(relativeTime(ago(3 * 86_400_000), now)).toBe("3 d ago");
  });

  it("reads forward for a future instant", () => {
    expect(relativeTime(new Date(now + 42 * 60_000).toISOString(), now)).toBe("in 42 min");
  });

  it("names a passed deadline rather than counting backwards from it", () => {
    expect(formatDeadline(new Date(now + 42 * 60_000).toISOString(), now)).toBe("in 42 min");
    expect(formatDeadline(new Date(now - 60_000).toISOString(), now)).toBe("Overdue");
    expect(formatDeadline(null, now)).toBe(ABSENT);
  });

  it("returns the em dash for an unparseable instant, never NaN on screen", () => {
    expect(relativeTime("not a date", now)).toBe(ABSENT);
    expect(absoluteTime("not a date")).toBe(ABSENT);
  });
});
