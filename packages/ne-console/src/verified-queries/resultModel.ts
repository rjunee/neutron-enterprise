// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import type { VerifiedAnswerWire, VerifiedTemplateWire } from "../api/types";
import { displayOrder } from "../knowledge/directory";
import { buildCsv } from "../primitives/csv";
import { filterRows, sortRows } from "../primitives/tableSort";
import type { SortState, SortableColumn } from "../primitives/tableSort";

export interface VerifiedResultRow {
  readonly index: number;
  readonly cells: readonly string[];
  readonly citation: string | null;
}

export interface VerifiedResultColumn extends SortableColumn<VerifiedResultRow> {
  readonly header: string;
  readonly sourceIndex: number | null;
}

export function verifiedResultRows(answer: VerifiedAnswerWire): VerifiedResultRow[] {
  return answer.rows.map((cells, index) => ({
    index,
    cells,
    citation: answer.citations[index] ?? null,
  }));
}

export function verifiedResultColumns(
  answer: VerifiedAnswerWire,
  template: VerifiedTemplateWire,
): VerifiedResultColumn[] {
  const columns: VerifiedResultColumn[] = displayOrder(
    answer.columns,
    template.citation_var,
    template.label_var,
  ).map((sourceIndex) => ({
    key: `column-${sourceIndex}`,
    header: answer.columns[sourceIndex] ?? `Column ${sourceIndex + 1}`,
    sourceIndex,
    sortValue: (row: VerifiedResultRow) => row.cells[sourceIndex] ?? null,
    searchText: (row: VerifiedResultRow) => row.cells[sourceIndex] ?? "",
  }));
  columns.push({
    key: "entity-citation",
    header: "Entity citation",
    sourceIndex: null,
    sortValue: (row) => row.citation,
    searchText: (row) => row.citation ?? "",
  });
  return columns;
}

export function verifiedResultWindow(
  rows: readonly VerifiedResultRow[],
  columns: readonly VerifiedResultColumn[],
  filter: string,
  sort: SortState | null,
): readonly VerifiedResultRow[] {
  return sortRows(filterRows(rows, columns, filter), columns, sort);
}

export function verifiedResultCsv({
  template,
  columns,
  rows,
  filter,
  sort,
  now,
}: {
  template: VerifiedTemplateWire;
  columns: readonly VerifiedResultColumn[];
  rows: readonly VerifiedResultRow[];
  filter: string;
  sort: SortState | null;
  now?: Date;
}): string {
  const window = verifiedResultWindow(rows, columns, filter, sort);
  const filters = [
    ...(filter.trim() === "" ? [] : [`Search: ${filter.trim()}`]),
    ...(sort === null ? [] : [`Sort: ${sort.key} ${sort.dir}`]),
  ];
  return buildCsv({
    title: `Verified query — ${template.title}`,
    filters,
    columns: columns.map((column) => ({
      header: column.header,
      value: (row: VerifiedResultRow) =>
        column.sourceIndex === null ? (row.citation ?? "") : (row.cells[column.sourceIndex] ?? ""),
    })),
    rows: window,
    now,
  });
}
