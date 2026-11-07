// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useMemo } from "react";
import { Link } from "react-router";
import { HashChip } from "../primitives/HashChip";
import { absoluteTime } from "../primitives/format";
import type { VerifiedInvokeWire, VerifiedTemplateWire } from "../api/types";
import { truncationNote } from "../knowledge/directory";
import { DataTable } from "../primitives/DataTable";
import type { Column } from "../primitives/DataTable";
import { downloadCsv } from "../primitives/csv";
import { formatCount } from "../primitives/format";
import type { SortState } from "../primitives/tableSort";
import { Empty } from "../primitives/states";
import { Toolbar, ToolbarButton } from "../primitives/Toolbar";
import {
  verifiedResultColumns,
  verifiedResultCsv,
  verifiedResultRows,
  verifiedResultWindow,
} from "./resultModel";
import type { VerifiedResultRow } from "./resultModel";
import { verifiedCitationLink } from "./verifiedQueryLinks";

export function VerifiedResultTable({
  result,
  template,
  filter,
  sort,
  onFilter,
  onSort,
}: {
  result: VerifiedInvokeWire;
  template: VerifiedTemplateWire;
  filter: string;
  sort: SortState | null;
  onFilter: (value: string) => void;
  onSort: (value: SortState) => void;
}) {
  const answer = result.answer;
  const rows = useMemo(() => verifiedResultRows(answer), [answer]);
  const specs = useMemo(() => verifiedResultColumns(answer, template), [answer, template]);
  const filtered = useMemo(
    () => verifiedResultWindow(rows, specs, filter, null),
    [filter, rows, specs],
  );
  const window = useMemo(
    () => verifiedResultWindow(rows, specs, filter, sort),
    [filter, rows, sort, specs],
  );
  const columns = useMemo<ReadonlyArray<Column<VerifiedResultRow>>>(
    () =>
      specs.map((spec) => ({
        key: spec.key,
        header: spec.header,
        mono:
          spec.sourceIndex !== null && answer.columns[spec.sourceIndex] === template.citation_var,
        cell: (row) => {
          if (spec.sourceIndex !== null) return row.cells[spec.sourceIndex] ?? null;
          if (row.citation === null) return null;
          const citation = verifiedCitationLink(row.citation, row.cells, answer.columns, template);
          return (
            <span className="ne-verified-queries__citation">
              {citation.href === null ? (
                <span>{citation.humanLabel}</span>
              ) : (
                <Link to={citation.href}>{citation.humanLabel}</Link>
              )}
              <code>{citation.immutableId}</code>
            </span>
          );
        },
        sortValue: spec.sortValue,
        searchText: spec.searchText,
      })),
    [answer, specs, template],
  );

  const exportCsv = useCallback(() => {
    downloadCsv(
      `verified-query-${template.template_id}-v${template.version}.csv`,
      verifiedResultCsv({ template, columns: specs, rows, filter, sort }),
    );
  }, [filter, rows, sort, specs, template]);

  return (
    <section className="ne-verified-queries__results" aria-labelledby="ne-verified-results-heading">
      <h2 id="ne-verified-results-heading" tabIndex={-1}>
        Verified result
      </h2>
      <Toolbar
        search={{ value: filter, onChange: onFilter, placeholder: "Filter result rows…" }}
        count={`${formatCount(window.length)} of ${formatCount(answer.returned_rows)} returned rows`}
        actions={<ToolbarButton label="Export CSV" onClick={exportCsv} />}
      />
      <DataTable
        columns={columns}
        rows={filtered}
        rowKey={(row) => String(row.index)}
        label="Verified query result"
        things="result rows"
        sort={sort ?? undefined}
        onSortChange={onSort}
        empty={
          <Empty
            message={
              answer.returned_rows === 0
                ? "This verified query returned zero rows."
                : "No returned rows match the shown-row filter."
            }
          />
        }
        note={truncationNote(answer)}
      />
      <p
        className={`ne-verified-queries__replay${result.replay_match ? "" : " ne-verified-queries__replay--diverged"}`}
        role="status"
      >
        {result.replay_match
          ? "Server replay matched this result."
          : "Server replay diverged from this result."}
      </p>
      <dl className="ne-verified-queries__provenance">
        <div>
          <dt>Ontology version</dt>
          <dd>{answer.ontology_version}</dd>
        </div>
        <div>
          {/* STATION 3 OF THE GUIDED WALKTHROUGH renders this block: `demo/stations.ts`
              scripts "Open Verified Queries, choose the account directory, and run it."
              It painted a full 64-character digest and a raw `2026-07-07T12:00:00Z` on
              the one screen whose pitch is enterprise-grade provenance. §2.7
              (primitives/HashChip) is a written rule, and `absoluteTime` is what the
              other 31 files use -- this was house rule broken on the demo path. */}
          <dt>Configuration SHA-256</dt>
          <dd>
            <HashChip hash={answer.config_hash} label="Copy configuration hash" />
          </dd>
        </div>
        <div>
          <dt>Executed</dt>
          <dd>
            <time dateTime={answer.executed_at}>{absoluteTime(answer.executed_at)}</time>
          </dd>
        </div>
      </dl>
    </section>
  );
}
