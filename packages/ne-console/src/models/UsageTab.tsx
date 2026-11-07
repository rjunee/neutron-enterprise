// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §10 — THE USAGE TAB, once the cost rollup is served.
//
// A window control, four summary tiles, and the per-model table sorted by spend
// with the unpriced lanes last. Everything on screen is a served field: this
// component sums nothing, prices nothing and attributes nothing.
//
// THE LINE WHERE THE PER-USER TABLE WOULD BE is not a placeholder. The cost
// records carry the gateway's service actor rather than the calling principal,
// so grouping them by "user" would put every call on one identity — a
// fabricated attribution on the screen whose entire job is measurement. The tab
// states the gap and renders nothing else in its place.

import { useCallback, useMemo, useState } from "react";
import { useGovernedRead } from "../landing/useGovernedRead";
import { DataTable } from "../primitives/DataTable";
import type { Column, TableStatus } from "../primitives/DataTable";
import { StatePill } from "../primitives/StatePill";
import { Empty, LoadError, Refused } from "../primitives/states";
import { FilterSelect, Toolbar, ToolbarButton } from "../primitives/Toolbar";
import { buildCsv, downloadCsv } from "../primitives/csv";
import { filterRows, sortRows } from "../primitives/tableSort";
import type { SortState } from "../primitives/tableSort";
import { formatCount, formatMoney } from "../primitives/format";
import type { UsageReader } from "./reads";
import {
  monthKey,
  monthLabel,
  monthOptions,
  spendSortValue,
  spendSummary,
  usageCount,
} from "./usage";
import type { ModelUsageRow, ModelsUsageView } from "./usage";

/** §10: "Default sort: Spend desc, unpriced rows last." The second half is the
 * table standard's own rule — an unpriced lane sorts as absent, and absent
 * stays last in both directions. */
const DEFAULT_SORT: SortState = { key: "spend", dir: "desc" };

function Tile({ label, value, note }: { label: string; value: string; note?: string | null }) {
  return (
    <div className="ne-models__tile">
      <span className="ne-models__tilelabel">{label}</span>
      <span className="ne-models__tilevalue" title={note ?? undefined}>
        {value}
      </span>
    </div>
  );
}

function SummaryTiles({ view }: { view: ModelsUsageView }) {
  const spend = spendSummary(view);
  return (
    <div className="ne-models__tiles">
      <Tile label="Calls" value={formatCount(view.totals.calls)} />
      <Tile label="Input tokens" value={formatCount(view.totals.input_tokens)} />
      <Tile label="Output tokens" value={formatCount(view.totals.output_tokens)} />
      <Tile label="Spend" value={spend.text} note={spend.note} />
    </div>
  );
}

function useUsageColumns(): Column<ModelUsageRow>[] {
  return useMemo(
    () => [
      {
        key: "model",
        header: "Model · Lane",
        cell: (row) => (
          <>
            <span className="ne-mono">{row.model_id}</span>{" "}
            <span className="ne-models__sub">{row.lane}</span>
          </>
        ),
        sortValue: (row) => row.model_id,
        searchText: (row) => `${row.model_id} ${row.provider} ${row.lane}`,
      },
      {
        key: "calls",
        header: "Calls",
        width: "104px",
        numeric: true,
        firstSort: "desc",
        cell: (row) => formatCount(row.calls),
        sortValue: (row) => row.calls,
      },
      {
        key: "input",
        header: "Input tokens",
        width: "132px",
        numeric: true,
        firstSort: "desc",
        // Null is the server declining to vouch for a total. The cell returns
        // null and the table renders the em dash — never a zero.
        cell: (row) => (row.input_tokens === null ? null : formatCount(row.input_tokens)),
        sortValue: (row) => row.input_tokens,
      },
      {
        key: "output",
        header: "Output tokens",
        width: "132px",
        numeric: true,
        firstSort: "desc",
        cell: (row) => (row.output_tokens === null ? null : formatCount(row.output_tokens)),
        sortValue: (row) => row.output_tokens,
      },
      {
        key: "spend",
        header: "Spend",
        width: "132px",
        numeric: true,
        firstSort: "desc",
        cell: (row) =>
          row.unpriced ? <StatePill word="UNPRICED" /> : formatMoney(row.cost_usd, {}),
        sortValue: spendSortValue,
      },
    ],
    [],
  );
}

export function UsageTab({ read }: { read: UsageReader }) {
  const [period, setPeriod] = useState(() => monthKey(new Date()));
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const run = useCallback(() => read(period), [read, period]);
  const usage = useGovernedRead(run);
  /** The currently rendered answer must name the selected window. Until the
   * new server response arrives, the prior window is stale evidence and is not
   * rendered under the new Month label. */
  const servedView = usage.data?.window === period ? usage.data : null;

  const columns = useUsageColumns();
  const served = useMemo(() => servedView?.models ?? [], [servedView]);
  const filtered = useMemo(() => filterRows(served, columns, search), [served, columns, search]);
  /** Exactly the rows the table renders, in the order it renders them — the
   * export is the window on screen, not a re-derivation of it. */
  const windowRows = useMemo(() => sortRows(filtered, columns, sort), [filtered, columns, sort]);

  const exportCsv = useCallback(() => {
    if (windowRows.length === 0) return;
    const filters = [`Month: ${monthLabel(period)}`];
    if (search.trim() !== "") filters.push(`Search: ${search.trim()}`);
    downloadCsv(
      "model-usage.csv",
      buildCsv<ModelUsageRow>({
        title: "Model usage",
        filters,
        columns: [
          { header: "Model", value: (row) => row.model_id },
          { header: "Provider", value: (row) => row.provider },
          { header: "Lane", value: (row) => row.lane },
          { header: "Calls", value: (row) => String(row.calls) },
          {
            header: "Input tokens",
            value: (row) => (row.input_tokens === null ? "" : String(row.input_tokens)),
          },
          {
            header: "Output tokens",
            value: (row) => (row.output_tokens === null ? "" : String(row.output_tokens)),
          },
          {
            header: "Spend",
            value: (row) =>
              row.unpriced || row.cost_usd === null ? "Unpriced" : String(row.cost_usd),
          },
        ],
        rows: windowRows,
      }),
    );
  }, [windowRows, period, search]);

  if (usage.status === "refused") return <Refused thing="model usage" page="Models" />;

  const status: TableStatus =
    usage.status === "loading" || (usage.status === "ok" && servedView === null)
      ? "loading"
      : usage.status === "error"
        ? "error"
        : "ok";

  return (
    <>
      {servedView === null ? null : <SummaryTiles view={servedView} />}
      <Toolbar
        search={{ value: search, onChange: setSearch, placeholder: "Filter models…" }}
        filters={
          <FilterSelect
            label="Month"
            value={period}
            options={monthOptions(new Date())}
            onChange={setPeriod}
          />
        }
        // Never a zero over a window still in flight (see the policy tab).
        count={status === "loading" ? "" : usageCount(windowRows.length)}
        actions={
          <ToolbarButton
            label="Export CSV"
            onClick={exportCsv}
            disabled={windowRows.length === 0}
          />
        }
      />
      <DataTable<ModelUsageRow>
        columns={columns}
        rows={filtered}
        rowKey={(row) => `${row.lane} ${row.provider} ${row.model_id}`}
        label="Usage by model"
        things="usage"
        sort={sort}
        onSortChange={setSort}
        status={status}
        empty={<Empty message="No usage recorded in this window." />}
        error={<LoadError things="usage" onRetry={usage.reload} />}
      />
      <p className="ne-models__gap">Per-user usage isn&rsquo;t recorded yet.</p>
    </>
  );
}
