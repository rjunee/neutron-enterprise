// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §12 — the rules behind the Evaluations screen, tested without a DOM.

import { describe, expect, it } from "vitest";
import type { EvalRunView } from "../api/types";
import {
  countText,
  csvColumns,
  formatMetric,
  isFailingStatus,
  judgeErrorText,
  metricColumnNames,
  metricNames,
  middleTruncate,
  rubricEntries,
  suitesOf,
} from "./runs";

function run(overrides: Partial<EvalRunView> = {}): EvalRunView {
  return {
    run_id: "run-1",
    suite: "retrieval-baseline",
    config_hash: "e".repeat(64),
    golden_set_hash: "f".repeat(64),
    judge_model_id: "claude-opus-4",
    judge_family: "claude",
    rubric_versions: { faithfulness: "v3" },
    metrics: { recall_at_10: 0.98 },
    thresholds: { recall_at_10: 0.85 },
    statuses: { recall_at_10: "pass" },
    judge_error_rate: 0.0,
    passed: true,
    created_at: "2026-07-25T09:00:00Z",
    ...overrides,
  };
}

describe("middleTruncate", () => {
  it("keeps BOTH ends, because the ends are what tell two runs apart", () => {
    expect(middleTruncate("run-2026-07-25-retrieval-baseline-9c31f0a2")).toBe("run-2026…31f0a2");
  });

  it("two runs of the same suite stay distinguishable after truncation", () => {
    const a = middleTruncate("run-2026-07-25-retrieval-baseline-aaaaaa");
    const b = middleTruncate("run-2026-07-25-retrieval-baseline-bbbbbb");
    expect(a).not.toBe(b);
  });

  it("leaves a short id exactly as it is", () => {
    expect(middleTruncate("run-pass")).toBe("run-pass");
    expect(middleTruncate("123456789012345")).toBe("123456789012345");
  });

  it("truncates only past head+tail, and never grows a value", () => {
    const long = "a".repeat(40);
    const out = middleTruncate(long);
    expect(out.length).toBeLessThan(long.length);
    expect(out.startsWith("aaaaaaaa…")).toBe(true);
  });
});

describe("judgeErrorText", () => {
  it("is a percent to one decimal", () => {
    expect(judgeErrorText(0.12)).toBe("12.0%");
    expect(judgeErrorText(0.1234)).toBe("12.3%");
  });

  it("renders a served zero as a zero — it is a measurement, not an absence", () => {
    expect(judgeErrorText(0)).toBe("0.0%");
  });

  it("renders an ABSENT rate as the em dash, never as zero", () => {
    expect(judgeErrorText(null)).toBe("—");
    expect(judgeErrorText(undefined)).toBe("—");
    expect(judgeErrorText(Number.NaN)).toBe("—");
  });
});

describe("formatMetric", () => {
  it("gives ratios three decimals and integers their own shape", () => {
    expect(formatMetric(0.98)).toBe("0.980");
    expect(formatMetric(1)).toBe("1");
  });

  it("renders an absent number as the em dash", () => {
    expect(formatMetric(undefined)).toBe("—");
    expect(formatMetric(null)).toBe("—");
  });
});

describe("isFailingStatus", () => {
  it("bites the server's failing words", () => {
    expect(isFailingStatus("fail")).toBe(true);
    expect(isFailingStatus("FAILED")).toBe(true);
    expect(isFailingStatus(" failed ")).toBe(true);
  });

  it("CONTROL: a passing word is not a failure", () => {
    expect(isFailingStatus("pass")).toBe(false);
    expect(isFailingStatus("PASSED")).toBe(false);
  });

  it("a word this console does not know is NOT presented as a failure", () => {
    // A fabricated failure is as false as a fabricated pass.
    expect(isFailingStatus("skipped")).toBe(false);
    expect(isFailingStatus("")).toBe(false);
    expect(isFailingStatus(undefined)).toBe(false);
  });
});

describe("metricNames", () => {
  it("is the UNION of metrics, thresholds and statuses, sorted", () => {
    const r = run({
      metrics: { recall_at_10: 0.9 },
      thresholds: { recall_at_10: 0.8, faithfulness: 0.8 },
      statuses: { permission: "pass" },
    });
    expect(metricNames(r)).toEqual(["faithfulness", "permission", "recall_at_10"]);
  });

  it("keeps a threshold that has no measurement behind it", () => {
    const r = run({ metrics: {}, thresholds: { latency_ms: 900 }, statuses: {} });
    expect(metricNames(r)).toEqual(["latency_ms"]);
  });
});

describe("suitesOf", () => {
  it("is the sorted, de-duplicated set of SERVED suite names", () => {
    expect(suitesOf([run(), run({ suite: "safety-redteam" }), run()])).toEqual([
      "retrieval-baseline",
      "safety-redteam",
    ]);
  });

  it("is empty when nothing was served — no suite is ever invented", () => {
    expect(suitesOf([])).toEqual([]);
  });
});

describe("countText", () => {
  it("counts the rendered window", () => {
    expect(countText(0)).toBe("0 runs");
    expect(countText(1)).toBe("1 run");
    expect(countText(12)).toBe("12 runs");
  });
});

describe("rubricEntries", () => {
  it("renders scalar versions as themselves, sorted by rubric", () => {
    const r = run({ rubric_versions: { faithfulness: "v3", accuracy: 2 } });
    expect(rubricEntries(r)).toEqual([
      ["accuracy", "2"],
      ["faithfulness", "v3"],
    ]);
  });

  it("keeps a structured version rather than dropping it", () => {
    const r = run({ rubric_versions: { faithfulness: { major: 3 } } });
    expect(rubricEntries(r)).toEqual([["faithfulness", '{"major":3}']]);
  });
});

describe("csvColumns", () => {
  const window = [
    run({ run_id: "a", metrics: { recall_at_10: 0.98, permission: 1 } }),
    run({
      run_id: "b",
      suite: "safety-redteam",
      passed: false,
      judge_error_rate: 0.12,
      judge_model_id: null,
      metrics: { faithfulness: 0.95 },
    }),
  ];

  it("is §12's column list, in §12's order, plus one column per metric in the window", () => {
    expect(csvColumns(window).map((c) => c.header)).toEqual([
      "suite",
      "run_id",
      "created_at",
      "passed",
      "judge_error_rate",
      "judge_model_id",
      "config_hash",
      "faithfulness",
      "permission",
      "recall_at_10",
    ]);
  });

  it("names only the metrics PRESENT in the exported window", () => {
    expect(metricColumnNames([window[1]!])).toEqual(["faithfulness"]);
    expect(csvColumns([window[1]!]).map((c) => c.header)).not.toContain("recall_at_10");
  });

  it("exports the served verdict and rate, and an absent judge as EMPTY", () => {
    const columns = csvColumns(window);
    const read = (header: string, row: EvalRunView) =>
      columns.find((c) => c.header === header)?.value(row);
    expect(read("passed", window[1]!)).toBe("false");
    expect(read("judge_error_rate", window[1]!)).toBe("0.12");
    // Absent exports empty — never a zero, never a dash a sheet would read as data.
    expect(read("judge_model_id", window[1]!)).toBe("");
    expect(read("recall_at_10", window[1]!)).toBe("");
    expect(read("recall_at_10", window[0]!)).toBe("0.98");
  });
});
