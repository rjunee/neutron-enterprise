// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.1 — the ordering and filtering rules behind <DataTable>, as pure functions.
//
// They live apart from the component for one reason: THE RULES ARE THE PART
// WORTH TESTING, and they are testable here without a DOM, a provider or a
// fixture. A screen that needs the sorted/filtered window for an export (§2.2
// exports "the currently filtered/sorted window") calls exactly what the table
// rendered, rather than re-deriving it and drifting.
//
// THIN CLIENT (C7): these reorder and hide rows the server already sent. They
// compute no verdict, no total, no permission.

export type SortDirection = "asc" | "desc";

export interface SortState {
  key: string;
  dir: SortDirection;
}

/** The value a column sorts by. `null` means the field was absent. */
export type SortValue = string | number | boolean | null;

/** The slice of a column definition the sort rules need. <DataTable>'s own
 * `Column<Row>` is assignable to this. */
export interface SortableColumn<Row> {
  key: string;
  sortValue?: (row: Row) => SortValue;
  searchText?: (row: Row) => string;
}

/** Ascending comparison of two PRESENT values. Numbers compare numerically,
 * strings compare with numeric collation ("entry 9" before "entry 10"). */
export function compareValues(a: SortValue, b: SortValue): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return String(a).localeCompare(String(b), "en", { numeric: true, sensitivity: "base" });
}

/**
 * A STABLE sort of `rows` by `sort`.
 *
 * NULLS STAY LAST IN BOTH DIRECTIONS. Reversing direction reverses the
 * comparison of two present values; it does not reverse the placement of an
 * absent one. An absent field is not "the smallest value" — it is the absence
 * of a value, and a descending click that floats a screenful of em dashes to
 * the top hides the rows the user asked to see.
 *
 * Ties keep their served order (the index tiebreak), so a second click on a
 * different column does not shuffle equal rows.
 */
export function sortRows<Row>(
  rows: readonly Row[],
  columns: readonly SortableColumn<Row>[],
  sort: SortState | null,
): readonly Row[] {
  if (sort === null) return rows;
  const column = columns.find((c) => c.key === sort.key);
  if (column === undefined || column.sortValue === undefined) return rows;
  const read = column.sortValue;
  const keyed = rows.map((row, index) => ({ row, index, value: read(row) }));
  keyed.sort((x, y) => {
    if (x.value === null && y.value === null) return x.index - y.index;
    if (x.value === null) return 1;
    if (y.value === null) return -1;
    const cmp = compareValues(x.value, y.value);
    if (cmp !== 0) return sort.dir === "asc" ? cmp : -cmp;
    return x.index - y.index;
  });
  return keyed.map((k) => k.row);
}

/** Everything a row contributes to the toolbar's search-within, lowercased. */
export function rowSearchText<Row>(row: Row, columns: readonly SortableColumn<Row>[]): string {
  return columns
    .map((c) => (c.searchText ? c.searchText(row) : ""))
    .filter((s) => s !== "")
    .join(" ")
    .toLowerCase();
}

/** §2.2's client-side substring filter over the fetched window. An empty or
 * whitespace-only query filters nothing. */
export function filterRows<Row>(
  rows: readonly Row[],
  columns: readonly SortableColumn<Row>[],
  query: string,
): readonly Row[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return rows;
  return rows.filter((row) => rowSearchText(row, columns).includes(needle));
}
