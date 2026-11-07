// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §12 — THE RUN DETAIL PANE: run id · golden set hash · rubric versions · the
// metrics table.
//
// Everything a reader needs to say "this number came from that golden set under
// that configuration" is here, and NOTHING ELSE IS. There is no aggregate GATE
// readout: §12 rules it out because a conjunction over the loaded runs is a
// property of a pipeline, not of this deployment, and the shipped screen drew it
// as a headline anyway.
//
// THIN CLIENT (C7). The verdict pill is `passed`. Each metric's status word is
// the server's `statuses` entry, rendered as sent — including the tint, which is
// decided from that word and never from comparing a value to its threshold.

import type { EvalRunView } from "../api/types";
import { DataTable } from "../primitives/DataTable";
import type { Column } from "../primitives/DataTable";
import { DetailField, DetailFields, DetailPane } from "../primitives/DetailPane";
import { HashChip } from "../primitives/HashChip";
import { StatusPill } from "../primitives/StatePill";
import { Empty } from "../primitives/states";
import { absoluteTime, relativeTime } from "../primitives/format";
import { formatMetric, isFailingStatus, metricNames, rubricEntries } from "./runs";

interface MetricRow {
  name: string;
  value: number | undefined;
  threshold: number | undefined;
  status: string | undefined;
}

const METRIC_COLUMNS: ReadonlyArray<Column<MetricRow>> = [
  {
    key: "metric",
    header: "Metric",
    cell: (row) => row.name,
    sortValue: (row) => row.name,
  },
  {
    key: "value",
    header: "Value",
    numeric: true,
    cell: (row) => (row.value === undefined ? null : formatMetric(row.value)),
    sortValue: (row) => row.value ?? null,
    firstSort: "desc",
  },
  {
    key: "threshold",
    header: "Threshold",
    numeric: true,
    cell: (row) => (row.threshold === undefined ? null : formatMetric(row.threshold)),
    sortValue: (row) => row.threshold ?? null,
    firstSort: "desc",
  },
  {
    key: "status",
    header: "Status",
    cell: (row) =>
      row.status === undefined ? null : (
        <span
          className={`ne-evaluations__status${
            isFailingStatus(row.status) ? " ne-evaluations__status--fail" : ""
          }`}
        >
          {row.status}
        </span>
      ),
    sortValue: (row) => row.status ?? null,
  },
];

export function RunPane({ run, onClose }: { run: EvalRunView; onClose: () => void }) {
  const rows: MetricRow[] = metricNames(run).map((name) => ({
    name,
    value: run.metrics[name],
    threshold: run.thresholds[name],
    status: run.statuses[name],
  }));
  const rubrics = rubricEntries(run);

  return (
    <DetailPane
      title={run.suite}
      subtitle={
        <span className="ne-evaluations__panehead">
          <StatusPill word={run.passed ? "PASSED" : "FAILED"} />
          <time dateTime={run.created_at} title={absoluteTime(run.created_at)}>
            {relativeTime(run.created_at)}
          </time>
        </span>
      }
      onClose={onClose}
    >
      <DetailFields>
        <DetailField label="Run id" mono>
          {run.run_id}
        </DetailField>
        <DetailField label="Golden set hash">
          <HashChip hash={run.golden_set_hash} label="Copy golden set hash" />
        </DetailField>
        <DetailField label="Rubric versions">
          {rubrics.length === 0 ? (
            <span className="ne-absent">—</span>
          ) : (
            <span className="ne-evaluations__rubrics">
              {rubrics.map(([key, value]) => (
                <span className="ne-evaluations__rubric" key={key}>
                  <span className="ne-evaluations__rubrickey">{key}</span>
                  <span className="ne-mono">{value}</span>
                </span>
              ))}
            </span>
          )}
        </DetailField>
      </DetailFields>

      <section className="ne-evaluations__metrics">
        <h3 className="ne-evaluations__blocktitle">Metrics</h3>
        <DataTable<MetricRow>
          columns={METRIC_COLUMNS}
          rows={rows}
          rowKey={(row) => row.name}
          label={`Metrics for run ${run.run_id}`}
          things="metrics"
          defaultSort={{ key: "metric", dir: "asc" }}
          empty={<Empty message="No metrics recorded for this run." />}
        />
      </section>
    </DetailPane>
  );
}
