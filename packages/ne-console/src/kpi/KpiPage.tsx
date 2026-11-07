// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §17 — INSIGHTS. The business owner, monthly: utilization, proficiency and
// business value for the period, every number a served measurement and every gap
// a stated gap.
//
// WHAT THIS SCREEN KEEPS: its refusal to draw a top-line business figure it
// cannot show the basis for. WHAT IT LOSES: the three paragraphs that argued for
// that refusal, the honesty band that turned four absent tags into a headline,
// and the evidence drawers listing the wire keys. A gap is now one cell with one
// note beside the row it belongs to — which states the same fact and spends none
// of the reader's attention defending it.
//
// THIN CLIENT (C7): every value is the server's, rendered through a unit-keyed
// formatter. This screen computes no metric, no total and no rate. The one
// comparison it makes — a zero beside its witness counter — is between two
// figures the same response carried, and it annotates the row rather than
// replacing the value.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../api/client";
import type { KpiSummaryView } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { useLatestRequest } from "../hooks/useLatestRequest";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { ListScreen } from "../primitives/PageHeader";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { ABSENT, absoluteTime, formatCount } from "../primitives/format";
import { Empty, LoadError, Refused } from "../primitives/states";
import { filterRows } from "../primitives/tableSort";
import { Toolbar, ToolbarButton } from "../primitives/Toolbar";
import { formatKpiValue } from "./format";
import { buildGroups, GROUPS, periodOf } from "./metrics";
import type { MetricGroup, MetricRow } from "./metrics";
import "./kpi.css";

type Status = "loading" | "ok" | "refused" | "error";

/** The group shells the skeleton rows load into: three tables in the table's own
 * geometry, never a spinner paragraph (§2.1). */
const LOADING_GROUPS: MetricGroup[] = GROUPS.map((g) => ({
  heading: g.heading,
  rows: [],
  note: null,
}));

/** Metric · Value · Unit (§17). All three sort; the default is the order §17
 * lists the tags in, which is the order an operator reads them in. */
const COLUMNS: readonly Column<MetricRow>[] = [
  {
    key: "metric",
    header: "Metric",
    width: "46%",
    cell: (row) => (
      <span className="ne-kpi__metric">
        <span>{row.label}</span>
        {row.note ? (
          <span className="ne-kpi__note" title={row.noteTitle ?? undefined}>
            {row.note}
          </span>
        ) : null}
      </span>
    ),
    sortValue: (row) => row.label,
    searchText: (row) => row.label,
  },
  {
    key: "value",
    header: "Value",
    numeric: true,
    firstSort: "desc",
    cell: (row) =>
      row.entry === null || row.entry.value === null || row.suppressed
        ? null
        : formatKpiValue(row.entry.value, row.entry.unit),
    sortValue: (row) =>
      row.entry === null || row.entry.value === null || row.suppressed ? null : row.entry.value,
  },
  {
    key: "unit",
    header: "Unit",
    width: "120px",
    cell: (row) => row.entry?.unit ?? null,
    sortValue: (row) => row.entry?.unit ?? null,
    searchText: (row) => row.entry?.unit ?? "",
  },
];

export function KpiPage() {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const [summary, setSummary] = useState<KpiSummaryView | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const [search, setSearch] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    void (async () => {
      const settled = await runLatest(() => client.kpiSummary());
      if (settled.kind === "superseded") return;
      if (settled.kind === "success") {
        setSummary(settled.value);
        setStatus("ok");
        return;
      }
      // A ROLE BOUNDARY AND AN OUTAGE ARE DIFFERENT FACTS. Only the server's 403
      // renders as a refusal; anything else is an unreachable read, and saying
      // "not visible to your role" about a network failure is a false statement
      // about authorization.
      setSummary(null);
      setStatus(
        settled.error instanceof ApiError && settled.error.status === 403 ? "refused" : "error",
      );
    })();
  }, [client, runLatest, attempt]);

  const kpis = useMemo(() => summary?.kpis ?? [], [summary]);
  // A response with no datapoints is the EMPTY state, not twelve
  // not-instrumented rows: "no metrics recorded" is one fact, and stating it
  // twelve times says something the response does not.
  const groups = useMemo(() => (kpis.length === 0 ? [] : buildGroups(kpis)), [kpis]);

  // The search-within narrows the ROWS, and the count describes the rows on
  // screen — a count that describes the fetched window while the screen shows a
  // filtered one is a number that is wrong on screen (§2.2).
  const shown: MetricGroup[] = useMemo(
    () =>
      groups
        .map((group) => ({ ...group, rows: [...filterRows(group.rows, COLUMNS, search)] }))
        .filter((group) => group.rows.length > 0),
    [groups, search],
  );
  const rendered = useMemo(() => shown.flatMap((g) => g.rows), [shown]);

  const period = periodOf(kpis);
  const retry = useCallback(() => {
    setStatus("loading");
    setAttempt((n) => n + 1);
  }, []);

  const exportCsv = useCallback(() => {
    const filters: string[] = [];
    if (period !== null) filters.push(`Period: ${period}`);
    if (search.trim() !== "") filters.push(`Filter: ${search.trim()}`);
    const contents = buildCsv({
      title: "Insights",
      filters,
      columns: [
        { header: "tag", value: (row: MetricRow) => row.tag },
        {
          header: "value",
          value: (row: MetricRow) =>
            row.entry?.value === null || row.entry === null ? "" : String(row.entry.value),
        },
        {
          header: "measurement_status",
          value: (row: MetricRow) =>
            row.entry === null
              ? "not_instrumented"
              : (row.entry.measurement_status ??
                (row.entry.value === null ? "undefined" : "measured")),
        },
        { header: "unit", value: (row: MetricRow) => row.entry?.unit ?? "" },
        // The served call count. On a self-hosted lane every price is 0, so this
        // column is the ONLY evidence of attribution the exported file carries —
        // without it the buyer's spreadsheet reads "0" spend against every
        // outcome and nothing contradicts it.
        { header: "calls", value: (row: MetricRow) => row.entry?.dimensions.calls ?? "" },
        { header: "window_start", value: (row: MetricRow) => row.entry?.window_start ?? "" },
        { header: "window_end", value: (row: MetricRow) => row.entry?.window_end ?? "" },
      ],
      rows: rendered,
    });
    downloadCsv(period === null ? "insights.csv" : `insights-${period}.csv`, contents);
  }, [period, rendered, search]);

  const tableStatus: TableStatus = status === "loading" ? "loading" : "ok";
  const bodyGroups = status === "loading" ? LOADING_GROUPS : shown;
  const showTables = status === "loading" || (status === "ok" && rendered.length > 0);

  return (
    <ListScreen title="Insights">
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter metrics…" }}
        count={status === "ok" ? `${formatCount(rendered.length)} metrics` : ABSENT}
        actions={
          <ToolbarButton label="Export CSV" onClick={exportCsv} disabled={rendered.length === 0} />
        }
      />

      {status === "refused" ? <Refused thing="metrics" page="Insights" /> : null}
      {status === "error" ? <LoadError things="metrics" onRetry={retry} /> : null}
      {status === "ok" && kpis.length === 0 ? (
        <Empty message="No metrics recorded for this period." />
      ) : null}
      {status === "ok" && kpis.length > 0 && rendered.length === 0 ? (
        <Empty
          message="No metrics match this filter."
          action={
            <button type="button" className="ne-btn ne-btn--quiet" onClick={() => setSearch("")}>
              Clear filter
            </button>
          }
        />
      ) : null}

      {showTables
        ? bodyGroups.map((group) => (
            <section className="ne-kpi__group" key={group.heading}>
              <div className="ne-kpi__grouphead">
                <h2 className="ne-kpi__heading">{group.heading}</h2>
                {group.note ? <span className="ne-kpi__groupnote">{group.note}</span> : null}
              </div>
              <DataTable<MetricRow>
                columns={COLUMNS}
                rows={status === "loading" ? [] : group.rows}
                rowKey={(row) => row.tag}
                label={group.heading}
                things="metrics"
                status={tableStatus}
              />
            </section>
          ))
        : null}

      {summary !== null && status === "ok" ? (
        <p className="ne-kpi__generated">
          Generated{" "}
          <time dateTime={summary.generated_at}>{absoluteTime(summary.generated_at)}</time>
        </p>
      ) : null}
    </ListScreen>
  );
}
