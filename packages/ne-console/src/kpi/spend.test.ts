// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// C8 spend attribution, without a DOM.
//
// This is a SPEND CLAIM SHOWN TO A BUYER, so the assertions here are about what
// the screen must refuse to do: refuse to fold untagged spend into a named
// outcome, refuse to compute the attribution share itself, refuse to call a
// self-hosted lane's absent price "$0.00", and refuse to hide the row that says
// the total is short.

import { describe, expect, it } from "vitest";
import type { KpiEntryView } from "../api/types";
import { formatKpiValue } from "./format";
import { buildGroups, SPEND_HEADING } from "./metrics";

const WINDOW = { window_start: "2026-07-01T00:00:00Z", window_end: "2026-08-01T00:00:00Z" };

function spend(
  kpi_tag: string,
  value: number,
  unit = "usd",
  extra: Record<string, string> = {},
): KpiEntryView {
  return { kpi_tag, value, unit, dimensions: { period: "2026-07", ...extra }, ...WINDOW };
}

/** The spend group, or undefined when the response carries no spend tags. */
function spendGroup(kpis: KpiEntryView[]) {
  return buildGroups(kpis).find((g) => g.heading === SPEND_HEADING);
}

function spendRow(kpis: KpiEntryView[], tag: string) {
  return spendGroup(kpis)?.rows.find((r) => r.tag === tag);
}

describe("spend attribution rows", () => {
  it("gives named outcomes their own group, labelled by the served tag", () => {
    const group = spendGroup([
      spend("spend.outcome.renewal-retention", 1.5, "usd", { calls: "2" }),
    ]);
    expect(group?.heading).toBe(SPEND_HEADING);
    expect(group?.rows.some((r) => r.label === "renewal-retention")).toBe(true);
  });

  it("renders the unattributed bucket as its own row, never merged into an outcome", () => {
    const kpis = [
      spend("spend.outcome.renewal-retention", 1, "usd", { calls: "1" }),
      spend("spend.unattributed", 3, "usd", { calls: "1" }),
    ];
    const outcome = spendRow(kpis, "spend.outcome.renewal-retention");
    const untagged = spendRow(kpis, "spend.unattributed");

    // The outcome shows ONLY its own served figure — the screen never adds the
    // untagged spend to it, and never drops the untagged row to tidy the table.
    expect(outcome?.entry?.value).toBe(1);
    expect(untagged?.entry?.value).toBe(3);
    // The note must NAME the gap and carry the served count — not merely be a
    // non-empty string, which any label at all would satisfy.
    expect(untagged?.note).toContain("No outcome was declared");
    expect(untagged?.note).toContain("1 calls");
  });

  it("keeps the unattributed row visible when nothing at all was attributed", () => {
    const untagged = spendRow(
      [spend("spend.unattributed", 2, "usd", { calls: "4" })],
      "spend.unattributed",
    );
    expect(untagged?.entry?.value).toBe(2);
  });

  it("renders the SERVER's attributed share, and never recomputes it from the parts", () => {
    // The parts here would divide to 0.9. The server says 0.5. A screen that
    // computes attribution locally (C7 violation) shows 0.900 and fails here —
    // which is exactly the point: only the server may state a verdict.
    const kpis = [
      spend("spend.attributed_usd", 9),
      spend("spend.unattributed", 1, "usd", { calls: "1" }),
      spend("spend.total_usd", 10),
      spend("spend.attributed_share", 0.5, "ratio"),
    ];
    const share = spendRow(kpis, "spend.attributed_share");
    expect(share?.entry?.value).toBe(0.5);
    expect(formatKpiValue(share!.entry!.value!, share!.entry!.unit)).toBe("0.500");
  });

  it("states that a share the server omitted has no denominator, not that it is uninstrumented", () => {
    const kpis = [
      spend("spend.outcome.renewal-retention", 0, "usd", { calls: "12" }),
      spend("spend.attributed_usd", 0),
      spend("spend.total_usd", 0),
    ];
    const share = spendRow(kpis, "spend.attributed_share");
    expect(share?.entry).toBeNull();
    expect(share?.note).toBe("No spend recorded — no share to compute");
    // NOT the generic absent-tag note: the instrumentation is working fine.
    expect(share?.note).not.toBe("Not instrumented");
  });

  it("shows the unpriced-calls row with a note when the server reports one", () => {
    const kpis = [
      spend("spend.outcome.renewal-retention", 1, "usd", { calls: "1" }),
      spend("spend.calls_missing_cost", 2, "count"),
    ];
    const gap = spendRow(kpis, "spend.calls_missing_cost");
    expect(gap?.entry?.value).toBe(2);
    expect(gap?.note).toBe("Excluded from the totals above — no price was recorded");
  });

  it("omits the unpriced-calls row entirely when the server reports no gap", () => {
    // The control: the gap row is present BECAUSE there is a gap.
    const kpis = [spend("spend.outcome.renewal-retention", 1, "usd", { calls: "1" })];
    expect(spendRow(kpis, "spend.calls_missing_cost")).toBeUndefined();
  });

  it("renders the served call count beside the money without counting anything", () => {
    const row = spendRow(
      [spend("spend.outcome.renewal-retention", 1.5, "usd", { calls: "7" })],
      "spend.outcome.renewal-retention",
    );
    expect(row?.note).toBe("7 calls");
  });

  it("omits the spend group entirely when the response carries no spend tags", () => {
    // An older server, or a window with no model calls at all: five
    // "Not instrumented" spend rows would say something the response does not.
    const groups = buildGroups([spend("utilization.agent_turns", 12, "count")]);
    expect(groups.map((g) => g.heading)).not.toContain(SPEND_HEADING);
  });

  it("does not divert a spend tag into the Other group", () => {
    const groups = buildGroups([
      spend("spend.outcome.renewal-retention", 1, "usd", { calls: "1" }),
    ]);
    expect(groups.find((g) => g.heading === "Other")).toBeUndefined();
  });

  it("renders the call-coverage share the server sends, beside the dollar share", () => {
    // The dollar share flatters: one priced tagged call beside three untagged
    // $0.00 calls is 100% by money and 25% by calls. Both must be on screen.
    const kpis = [
      spend("spend.attributed_share", 1.0, "ratio"),
      spend("spend.attributed_call_share", 0.25, "ratio"),
    ];
    expect(spendRow(kpis, "spend.attributed_call_share")?.entry?.value).toBe(0.25);
    expect(spendRow(kpis, "spend.attributed_share")?.entry?.value).toBe(1.0);
  });

  it("states that a coverage share the server omitted has no denominator", () => {
    const share = spendRow([spend("spend.total_usd", 0)], "spend.attributed_call_share");
    expect(share?.entry).toBeNull();
    expect(share?.note).toBe("No calls recorded — no share to compute");
  });

  it("surfaces a spend tag it does not recognise instead of dropping it", () => {
    // `buildGroups` excludes every spend tag from `Other`, so an unrecognised
    // one would otherwise render NOWHERE — a hole in "surfaced, never dropped"
    // punched in the one group that reports money. C8 also names departments
    // and tasks, so this row shape is coming.
    const kpis = [spend("spend.by_department.finance", 12.5, "usd", { calls: "40" })];
    const row = spendRow(kpis, "spend.by_department.finance");
    expect(row?.entry?.value).toBe(12.5);
    expect(row?.note).toBe("Not in the standard spend set");
  });

  it("puts the unattributed call count in the visible note, not only a tooltip", () => {
    // A `title` tooltip is invisible in a screenshot, in print, and on touch —
    // and this is the figure a buyer most needs off this screen.
    const row = spendRow(
      [spend("spend.unattributed", 5, "usd", { calls: "31" })],
      "spend.unattributed",
    );
    expect(row?.note).toContain("31 calls");
  });
});

describe("usd formatting", () => {
  it("renders a priced outcome as money", () => {
    expect(formatKpiValue(1234.5, "usd")).toBe("$1,234.50");
  });

  it("renders a zero price as Unpriced rather than $0.00", () => {
    // §2.7 / SPEC §11: a self-hosted lane emits no price. Drawing that as $0.00
    // tells a buyer the month was free, which is a different and false claim.
    expect(formatKpiValue(0, "usd")).toBe("Unpriced");
  });

  it("keeps a real sub-cent charge visible instead of rounding it to $0.00", () => {
    // The shared 2dp money shape rounds 0.004 to "$0.00" — the same string the
    // zero case exists to avoid, reached from the other direction and
    // indistinguishable from "unpriced". The server keeps 8dp because frontier
    // per-call costs run to ~1e-7; rounding to free here would discard it at the
    // last step, in front of the buyer.
    expect(formatKpiValue(0.004, "usd")).toBe("$0.004000");
    expect(formatKpiValue(0.0000012, "usd")).toBe("$0.000001");
    expect(formatKpiValue(-0.004, "usd")).toBe("-$0.004000");
  });

  it("still uses the grouped 2dp money shape at and above a cent", () => {
    expect(formatKpiValue(0.01, "usd")).toBe("$0.01");
    expect(formatKpiValue(1234.5, "usd")).toBe("$1,234.50");
  });
});
