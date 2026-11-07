// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 Usage — the two honesty rules, without a DOM.
//
//   * an unpriced lane is not a $0.00 lane, and it does not float to the top of
//     a reversed spend sort either;
//   * a spend total that omits unpriced lanes says so.

import { describe, expect, it } from "vitest";
import { sortRows } from "../primitives/tableSort";
import {
  monthKey,
  monthLabel,
  monthOptions,
  spendSortValue,
  spendSummary,
  UNPRICED_NOTE,
  usageCount,
} from "./usage";
import type { ModelUsageRow, ModelsUsageView } from "./usage";

function row(over: Partial<ModelUsageRow> = {}): ModelUsageRow {
  return {
    model_id: "claude-fable-5",
    provider: "anthropic",
    lane: "frontier-api",
    calls: 10,
    input_tokens: 100,
    output_tokens: 50,
    cost_usd: 1.5,
    unpriced: false,
    ...over,
  };
}

function view(models: ModelUsageRow[], cost: number | null): ModelsUsageView {
  return {
    window: "2026-08",
    models,
    totals: { calls: 0, input_tokens: null, output_tokens: null, cost_usd: cost },
  };
}

const SPEND_COLUMN = [{ key: "spend", sortValue: spendSortValue }];

describe("the spend sort", () => {
  const rows = [
    row({ model_id: "cheap", cost_usd: 2 }),
    row({ model_id: "self-hosted", cost_usd: 0, unpriced: true }),
    row({ model_id: "dear", cost_usd: 900 }),
  ];

  it("sorts spend descending with the unpriced lane last", () => {
    const sorted = sortRows(rows, SPEND_COLUMN, { key: "spend", dir: "desc" });
    expect(sorted.map((r) => r.model_id)).toEqual(["dear", "cheap", "self-hosted"]);
  });

  it("keeps it last when REVERSED — an unpriced lane is not a cheap lane", () => {
    const sorted = sortRows(rows, SPEND_COLUMN, { key: "spend", dir: "asc" });
    expect(sorted.map((r) => r.model_id)).toEqual(["cheap", "dear", "self-hosted"]);
  });

  it("sorts by the served price, and treats a 0.0 self-hosted price as absent", () => {
    expect(spendSortValue(row({ cost_usd: 12.5 }))).toBe(12.5);
    expect(spendSortValue(row({ cost_usd: 0, unpriced: true }))).toBeNull();
    // CONTROL: a genuine zero on a PRICED lane is a number the server vouched
    // for, and it sorts as one.
    expect(spendSortValue(row({ cost_usd: 0, unpriced: false }))).toBe(0);
  });
});

describe("the spend summary", () => {
  it("names the omission when any lane is unpriced", () => {
    const summary = spendSummary(view([row(), row({ unpriced: true, cost_usd: 0 })], 1296.46));
    expect(summary.text).toBe("$1,296.46 + unpriced");
    expect(summary.note).toBe(UNPRICED_NOTE);
  });

  it("CONTROL: with every lane priced it is the total, and says nothing more", () => {
    const summary = spendSummary(view([row(), row({ cost_usd: 3 })], 4.5));
    expect(summary.text).toBe("$4.50");
    expect(summary.note).toBeNull();
  });

  it("never renders $0.00 for a window the server priced at nothing", () => {
    expect(spendSummary(view([row({ unpriced: true, cost_usd: 0 })], 0)).text).toBe("Unpriced");
    expect(spendSummary(view([row({ unpriced: true, cost_usd: 0 })], null)).text).toBe("Unpriced");
  });
});

describe("the window control", () => {
  it("addresses a window by its year and month", () => {
    expect(monthKey(new Date(2026, 7, 14))).toBe("2026-08");
    expect(monthKey(new Date(2026, 0, 1))).toBe("2026-01");
  });

  it("labels a window in words", () => {
    expect(monthLabel("2026-08")).toBe("August 2026");
  });

  it("offers this month first, then the eleven before it, crossing the year", () => {
    const options = monthOptions(new Date(2026, 1, 9), 3);
    expect(options.map((o) => o.value)).toEqual(["2026-02", "2026-01", "2025-12"]);
    expect(options[0]?.label).toBe("February 2026");
  });
});

describe("the count", () => {
  it("agrees with itself about the noun", () => {
    expect(usageCount(1)).toBe("1 model");
    expect(usageCount(3)).toBe("3 models");
  });
});
