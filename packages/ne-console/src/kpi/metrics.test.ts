// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §17's rules, without a DOM. These are the assertions that decide whether the
// screen states a gap as a gap: an absent tag is not a zero, a rate with no
// denominator is not a measured absence, and a zero the response's own witness
// counter contradicts is annotated — never overwritten.

import { describe, expect, it } from "vitest";
import type { KpiEntryView } from "../api/types";
import { buildGroups, labelFor, periodOf } from "./metrics";

const WINDOW = { window_start: "2026-07-01T00:00:00Z", window_end: "2026-07-27T00:00:00Z" };

function entry(
  kpi_tag: string,
  value: number | null,
  unit: string | null = "count",
  measurement_status?: "measured" | "undefined",
): KpiEntryView {
  return {
    kpi_tag,
    value,
    measurement_status,
    unit,
    dimensions: { period: "2026-07" },
    ...WINDOW,
  };
}

/** The row for a tag, from whichever group holds it. */
function row(kpis: KpiEntryView[], tag: string) {
  return buildGroups(kpis)
    .flatMap((g) => g.rows)
    .find((r) => r.tag === tag);
}

describe("§17 metric rows", () => {
  it("groups the standard tags in the order §17 lists them", () => {
    const groups = buildGroups([entry("utilization.agent_turns", 128)]);
    expect(groups.map((g) => g.heading)).toEqual(["Utilization", "Proficiency", "Business value"]);
    expect(groups[0]?.rows.map((r) => r.tag)).toEqual([
      "utilization.agent_turns",
      "utilization.tool_calls",
      "utilization.active_users",
      "utilization.sessions",
      "utilization.events_total",
    ]);
  });

  it("renders an absent tag as a row with no entry and the note 'Not instrumented'", () => {
    const missing = row([entry("utilization.agent_turns", 128)], "utilization.events_total");
    expect(missing?.entry).toBeNull();
    expect(missing?.note).toBe("Not instrumented");
    // AND NOT A ZERO. The whole screen turns on this distinction.
    expect(missing?.suppressed).toBe(false);
  });

  it("renders a server-authored undefined rate distinctly from missing instrumentation", () => {
    const rate = row(
      [
        entry("utilization.agent_turns", 0),
        entry("proficiency.negative_signal_rate", null, "ratio", "undefined"),
      ],
      "proficiency.negative_signal_rate",
    );
    expect(rate?.suppressed).toBe(true);
    expect(rate?.entry?.value).toBeNull();
    expect(rate?.note).toBe("No agent turns recorded — no rate to compute");
  });

  it("never suppresses a served 0.0 rate based on an unrelated signal counter", () => {
    const rate = row(
      [
        entry("proficiency.correction_signals_total", 0),
        entry("proficiency.negative_signal_rate", 0, "ratio", "measured"),
      ],
      "proficiency.negative_signal_rate",
    );
    expect(rate?.entry?.value).toBe(0);
    expect(rate?.suppressed).toBe(false);
    expect(rate?.note).toBeNull();
  });

  it("names eval's zero denominator without calling the collector uninstrumented", () => {
    const rate = row(
      [entry("proficiency.eval_pass_rate", null, "ratio", "undefined")],
      "proficiency.eval_pass_rate",
    );
    expect(rate?.suppressed).toBe(true);
    expect(rate?.note).toBe("No eval runs recorded — no rate to compute");
    expect(rate?.note).not.toBe("Not instrumented");
  });

  it("annotates a zero its witness counter contradicts, and keeps the served value", () => {
    const users = row(
      [entry("utilization.agent_turns", 41), entry("utilization.active_users", 0)],
      "utilization.active_users",
    );
    expect(users?.entry?.value).toBe(0);
    expect(users?.suppressed).toBe(false);
    expect(users?.note).toBe("Attribution gap");
    expect(users?.noteTitle).toBe("0 recorded while 41 agent turns were counted");
  });

  it("leaves a zero with no witness alone — inventing a fault is the same defect", () => {
    const positives = row(
      [entry("utilization.agent_turns", 41), entry("proficiency.positive_signals", 0)],
      "proficiency.positive_signals",
    );
    expect(positives?.note).toBeNull();
  });

  it("notes the two rows §17 gives standing notes", () => {
    const kpis = [
      entry("utilization.events_total", 6058),
      entry("business_value.hours_saved_floor", 41.5, "hours"),
    ];
    expect(row(kpis, "utilization.events_total")?.note).toBe("Includes this console's own reads.");
    expect(row(kpis, "business_value.hours_saved_floor")?.note).toBe(
      "Floor estimate — configured rate assumptions are applied by the server.",
    );
  });

  it("surfaces a tag outside the standard set in its own group, never dropped", () => {
    const groups = buildGroups([entry("retrieval.chunk_hits", 12)]);
    const other = groups[groups.length - 1];
    expect(other?.heading).toBe("Other");
    expect(other?.note).toBe("Not in the standard metric set");
    expect(other?.rows.map((r) => r.tag)).toEqual(["retrieval.chunk_hits"]);
  });

  it("adds no Other group when every tag is standard", () => {
    expect(buildGroups([entry("utilization.sessions", 4)]).map((g) => g.heading)).not.toContain(
      "Other",
    );
  });

  it("labels a tag by its suffix, in sentence case, and reads the served period", () => {
    expect(labelFor("proficiency.negative_signal_rate")).toBe("Negative signal rate");
    expect(periodOf([entry("utilization.sessions", 4)])).toBe("2026-07");
    expect(periodOf([])).toBeNull();
  });
});
