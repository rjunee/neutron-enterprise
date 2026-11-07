// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §12 — the Evaluations rules, as pure functions.
//
// They live apart from the screen because THE RULES ARE THE PART WORTH TESTING:
// the middle truncation of a run id, the percent shape of the judge error rate,
// the set of metric rows a run has, and the export's column list. None of them
// needs a DOM, a provider or a fixture to be checked.
//
// THIN CLIENT (C7). Nothing here computes a verdict. `passed` is the server's
// and is only ever read; a metric's status word is the server's `statuses` entry
// and is only ever read. There is deliberately no function in this file that
// compares a metric to its threshold — the console must never be able to
// disagree with the server about whether a run passed.

import type { EvalRunView } from "../api/types";
import type { CsvColumn } from "../primitives/csv";
import { ABSENT } from "../primitives/format";

/** The governed list route's maximum window. Asking for the maximum makes the
 * partial-window disclosure (§2.1) a statement about the server's cap rather
 * than about a number this screen chose. */
export const RUNS_WINDOW = 500;

/**
 * §12's Run column: "mono, middle-truncated". A run id is long, and its two
 * ENDS are what distinguishes two runs of the same suite — a head-only
 * truncation renders a column of identical strings. The full value stays in
 * `title` and in the detail pane, so nothing is lost.
 */
export function middleTruncate(value: string, head = 8, tail = 6): string {
  if (value.length <= head + tail + 1) return value;
  return `${value.slice(0, head)}…${value.slice(value.length - tail)}`;
}

/** §12: "percent, 1 d.p.". The served field is a rate in 0…1. An absent or
 * non-finite rate is the em dash — never a zero, which would read as a judge
 * that made no errors. */
export function judgeErrorText(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return ABSENT;
  return `${(rate * 100).toFixed(1)}%`;
}

/** A metric value or threshold as a cell. Integers keep their shape; everything
 * else takes 3 decimals, so a column of ratios lines up under tabular figures. */
export function formatMetric(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}

/**
 * §12: "row tinted on a failing status".
 *
 * The tint is decided from the SERVER's own status word and from nothing else.
 * A word the server sends that this console does not recognise is left untinted
 * rather than presented as a failure — a fabricated failure is as false as a
 * fabricated pass.
 */
export function isFailingStatus(status: string | null | undefined): boolean {
  if (!status) return false;
  return /^fail/i.test(status.trim());
}

/**
 * Every metric row a run has: the union of the keys the server sent under
 * `metrics`, `thresholds` and `statuses`, sorted.
 *
 * The UNION, not the intersection: a threshold with no measurement behind it is
 * exactly the thing a reader needs to see, and taking only `metrics` would drop
 * it silently. The missing side renders as the em dash.
 */
export function metricNames(run: EvalRunView): string[] {
  return [
    ...new Set([
      ...Object.keys(run.metrics),
      ...Object.keys(run.thresholds),
      ...Object.keys(run.statuses),
    ]),
  ].sort();
}

/** The suites present in the loaded window, sorted — the suite filter's options.
 * Read from served rows; this console never invents a suite name. */
export function suitesOf(runs: readonly EvalRunView[]): string[] {
  return [...new Set(runs.map((run) => run.suite))].sort();
}

/**
 * §12's "one column per metric present in the window", sorted.
 *
 * MEASURED metrics only — the keys of `metrics`, not the union `metricNames`
 * takes. A CSV column is a column of values; a threshold with no measurement
 * behind it would contribute an empty column to every row in the file, which
 * says nothing a reader of the file can use. The pane, which shows the missing
 * side as a row, is where that absence belongs.
 */
export function metricColumnNames(runs: readonly EvalRunView[]): string[] {
  return [...new Set(runs.flatMap((run) => Object.keys(run.metrics)))].sort();
}

/** Rubric versions as displayable pairs. Values arrive as `unknown`; a scalar
 * renders as itself and anything structured renders as its JSON, because
 * dropping it would hide a version the run was pinned to. */
export function rubricEntries(run: EvalRunView): Array<[string, string]> {
  return Object.entries(run.rubric_versions)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => [
      key,
      typeof value === "string" || typeof value === "number" || typeof value === "boolean"
        ? String(value)
        : JSON.stringify(value),
    ]);
}

/** §2.2's live count for this screen. */
export function countText(runs: number): string {
  return `${runs} ${runs === 1 ? "run" : "runs"}`;
}

/**
 * §12's export column list, verbatim and in order, plus one column per metric
 * present in the window.
 *
 * The headers are the served field names because the file is read by a machine
 * or pasted into a sheet next to the API's own output; a renamed column there is
 * a column somebody has to map back by hand.
 */
export function csvColumns(runs: readonly EvalRunView[]): Array<CsvColumn<EvalRunView>> {
  return [
    { header: "suite", value: (run) => run.suite },
    { header: "run_id", value: (run) => run.run_id },
    { header: "created_at", value: (run) => run.created_at },
    { header: "passed", value: (run) => String(run.passed) },
    { header: "judge_error_rate", value: (run) => String(run.judge_error_rate) },
    { header: "judge_model_id", value: (run) => run.judge_model_id ?? "" },
    { header: "config_hash", value: (run) => run.config_hash },
    ...metricColumnNames(runs).map((name) => ({
      header: name,
      value: (run: EvalRunView) => {
        const value = run.metrics[name];
        return value === undefined ? "" : String(value);
      },
    })),
  ];
}
